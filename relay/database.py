"""Everything that reads from or writes to Supabase, through its REST API (PostgREST).

The relay uses the secret key, which bypasses row-level security, so it can read access
codes from printer_connections and write to every table.
"""

import logging

import requests

from .config import Printer
from .history import JOB_COLUMNS
from .status import format_status_line

log = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        """False when Supabase rejected the data itself (e.g. a row for a printer that was just
        deleted). Sending the same rows again would fail forever."""
        return self.status is None or self.status >= 500 or self.status in (401, 403, 408, 429)


def _in(values) -> str:
    """PostgREST 'in' filter: in.("a","b")."""
    return "in.(" + ",".join('"' + str(v).replace('"', '\\"') + '"' for v in values) + ")"


def _status_row(r: dict) -> dict:
    """A status record (relay/status.py) -> a printer_status row.

    printer_status is publicly readable, so it never carries the job name (it can identify a
    student). Staff see the current job through print_jobs instead."""
    row = {"printer_id": r["printer_id"], "is_online": r["online"]}
    if r["last_seen"]:
        row["last_seen"] = r["last_seen"]
    if not r["online"]:
        return row  # keep the last known job data in the table, just flag it offline
    row.update({
        "gcode_state": r["gcode_state"],
        "mc_percent": r["progress_percent"],
        "mc_remaining_time": r["remaining_minutes"],
        "layer_num": r["layer_current"],
        "total_layer_num": r["layer_total"],
        "nozzle_temper": r["nozzle_temp"],
        "bed_temper": r["bed_temp"],
        "wifi_signal": r["wifi_signal"],
        "print_error": r["print_error"],
    })
    return row


SNAPSHOT_BUCKET = "printer-snapshots"
NEEDS_SNAPSHOT_SETUP = "camera photos need sql/07_camera_snapshots.sql: run it once in the Supabase SQL editor"


class Database:
    def __init__(self, url: str, key: str, timeout: float = 10.0):
        self.base = f"{url.rstrip('/')}/rest/v1"
        self.storage = f"{url.rstrip('/')}/storage/v1"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"apikey": key, "Content-Type": "application/json"})
        # Legacy service_role JWTs go in Authorization. The newer sb_secret_ keys only work in apikey.
        if not key.startswith("sb_"):
            self.session.headers["Authorization"] = f"Bearer {key}"

    def close(self) -> None:
        self.session.close()

    # reads

    def load_printers(self) -> list[Printer]:
        """Every printer, including retired ones and ones without connection settings yet."""
        rows = self._request("GET", "printers", params={
            "select": "id,serial,label,model,maintenance_status,firmware_version,"
                      "filament_color,filament_color_name,filament_type,"
                      "printer_connections(host,access_code,last_error)",
        })
        printers = []
        for row in rows:
            conn = row.pop("printer_connections", None)
            if isinstance(conn, list):  # PostgREST returns one-to-one embeds as an object, older versions as a list
                conn = conn[0] if conn else None
            conn = conn or {}
            printers.append(Printer(
                id=row["id"], serial=row["serial"], label=row["label"], model=row["model"],
                maintenance_status=row["maintenance_status"], firmware_version=row.get("firmware_version"),
                filament_color=row.get("filament_color"),
                filament_color_name=row.get("filament_color_name"), filament_type=row.get("filament_type"),
                access_code=conn.get("access_code") or "", host=conn.get("host"),
                last_error=conn.get("last_error"),
            ))
        return printers

    def load_last_status(self, printer_ids: list[str]) -> dict[str, dict]:
        rows = self._request("GET", "printer_status", params={
            "select": "printer_id,gcode_state,is_online,print_error,last_seen", "printer_id": _in(printer_ids),
        })
        return {r["printer_id"]: r for r in rows}

    def load_open_jobs(self, printer_ids: list[str]) -> dict[str, dict]:
        rows = self._request("GET", "print_jobs", params={
            "select": ",".join(JOB_COLUMNS), "printer_id": _in(printer_ids),
            "ended_at": "is.null", "order": "started_at.desc",
        })
        jobs = {}
        for r in rows:
            jobs.setdefault(r["printer_id"], r)  # newest open job per printer
        return jobs

    # writes

    def upsert_status(self, records: list[dict]) -> None:
        # Group rows by which columns they have, so a short offline row doesn't null out
        # columns it leaves off.
        groups: dict[frozenset, list[dict]] = {}
        for r in records:
            row = _status_row(r)
            groups.setdefault(frozenset(row), []).append(row)
        for rows in groups.values():
            self._upsert("printer_status", rows, on_conflict="printer_id")

    def update_printer(self, printer_id: str, fields: dict) -> None:
        self._request("PATCH", "printers", params={"id": f"eq.{printer_id}"}, json=fields,
                      headers={"Prefer": "return=minimal"})

    def update_connection(self, printer_id: str, fields: dict) -> None:
        self._request("PATCH", "printer_connections", params={"printer_id": f"eq.{printer_id}"}, json=fields,
                      headers={"Prefer": "return=minimal"})

    def insert_events(self, rows: list[dict]) -> None:
        # Events have ids made by the relay, so re-sending after a timeout can't create duplicates.
        self._upsert("printer_events", rows, on_conflict="id", resolution="ignore-duplicates")

    def upsert_jobs(self, rows: list[dict]) -> None:
        self._upsert("print_jobs", rows, on_conflict="id")

    def insert_heartbeat(self, row: dict) -> None:
        self._request("POST", "relay_heartbeats", json=row, headers={"Prefer": "return=minimal"})

    def upsert_discovered(self, rows: list[dict]) -> None:
        self._upsert("discovered_printers", rows, on_conflict="serial")

    def delete_discovered(self, serials: list[str]) -> None:
        if serials:
            self._request("DELETE", "discovered_printers", params={"serial": _in(serials)},
                          headers={"Prefer": "return=minimal"})

    def upload_snapshot(self, printer_id: str, jpeg: bytes, taken_at: str) -> None:
        """Replace the printer's camera snapshot (one file per printer, so storage never grows),
        then record when it was taken so the dashboard knows it's fresh."""
        url = f"{self.storage}/object/{SNAPSHOT_BUCKET}/{printer_id}.jpg"
        try:
            resp = self.session.post(url, data=jpeg, timeout=self.timeout, headers={
                "Content-Type": "image/jpeg", "x-upsert": "true",
                # The dashboard asks for ?v=<snapshot_at>, so each new photo has its own URL.
                "Cache-Control": "max-age=300",
            })
        except requests.RequestException as e:
            raise DatabaseError(f"can't reach Supabase Storage ({e.__class__.__name__})") from e
        if not resp.ok:
            if "bucket not found" in resp.text.lower():
                raise DatabaseError(NEEDS_SNAPSHOT_SETUP, status=resp.status_code)
            raise DatabaseError(f"Supabase Storage upload failed ({resp.status_code}): {resp.text[:300]}",
                                status=resp.status_code)
        try:
            self._request("PATCH", "printer_status", params={"printer_id": f"eq.{printer_id}"},
                          json={"snapshot_at": taken_at}, headers={"Prefer": "return=minimal"})
        except DatabaseError as e:
            if "snapshot_at" in str(e):  # the column comes from migration 07
                raise DatabaseError(NEEDS_SNAPSHOT_SETUP, status=e.status) from e
            raise

    # used by import_printers.py

    def upsert_printers(self, rows: list[dict]) -> list[dict]:
        return self._request("POST", "printers", params={"on_conflict": "serial", "select": "id,serial"}, json=rows,
                             headers={"Prefer": "resolution=merge-duplicates,return=representation"})

    def upsert_connections(self, rows: list[dict]) -> None:
        self._upsert("printer_connections", rows, on_conflict="printer_id")

    # plumbing

    def _upsert(self, table: str, rows: list[dict], on_conflict: str, resolution: str = "merge-duplicates") -> None:
        self._request("POST", table, params={"on_conflict": on_conflict}, json=rows,
                      headers={"Prefer": f"resolution={resolution},return=minimal"})

    def _request(self, method: str, table: str, **kwargs):
        try:
            resp = self.session.request(method, f"{self.base}/{table}", timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise DatabaseError(f"can't reach Supabase ({e.__class__.__name__})") from e
        if not resp.ok:
            raise DatabaseError(f"Supabase {method} {table} failed ({resp.status_code}): {resp.text[:300]}",
                                status=resp.status_code)
        return resp.json() if resp.content else None


class DryRunDatabase(Database):
    """Reads from Supabase like normal, but prints status lines instead of writing anything."""

    def upsert_status(self, records):
        for r in records:
            print(format_status_line(r), flush=True)

    def update_printer(self, printer_id, fields): log.debug("[dry-run] printers %s <- %s", printer_id, fields)
    def update_connection(self, printer_id, fields): log.debug("[dry-run] printer_connections %s <- %s", printer_id, fields)
    def insert_events(self, rows): log.debug("[dry-run] %d printer_events row(s)", len(rows))
    def upsert_jobs(self, rows): log.debug("[dry-run] %d print_jobs row(s)", len(rows))
    def insert_heartbeat(self, row): log.debug("[dry-run] relay_heartbeats <- %s", row)
    def upsert_discovered(self, rows): log.debug("[dry-run] %d discovered_printers row(s)", len(rows))
    def delete_discovered(self, serials): log.debug("[dry-run] delete discovered_printers %s", serials)
    def upload_snapshot(self, printer_id, jpeg, taken_at): log.info("[dry-run] snapshot for %s: %d KB", printer_id, len(jpeg) // 1024)
