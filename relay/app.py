"""The relay's main loop.

Every `push_interval_s` (2 s) it:
  1. re-reads the printer list from Supabase (every 30 s) and starts/stops/updates connections
  2. points each connection at the printer's current IP (from discovery, else the stored host)
  3. records what discovery learned: IPs, models, firmware, and printers not yet registered
  4. builds each printer's status, pushes changes to printer_status, and records history
  5. writes plain-language connection problems to printer_connections.last_error
  6. sends queued printer_events / print_jobs rows
  7. once a minute, writes a relay_heartbeats row (uptime + latency)

Each step runs separately (see _step), so one failing step, such as Supabase being
unreachable, doesn't stop the others. Anything that failed is retried on a later pass.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Printer, RelayConfig
from .database import Database, DatabaseError
from .discovery import Discovery
from .history import PrinterHistory
from .printer import PrinterConnection, Snapshot
from .status import build_status, fingerprint

log = logging.getLogger("relay")

STARTUP_GRACE_S = 15          # time for connections and first announcements before reporting printers offline
MAX_PENDING_EVENTS = 10_000   # cap on queued events if Supabase is down for a long time
DISCOVERED_REFRESH_S = 300    # how often to refresh last_seen for an unregistered printer
HISTORY_BATCH = 500


def connection_problem(snap: Snapshot, has_host: bool, seen_on_network: bool | None) -> str | None:
    """Why a printer isn't connected, in words an admin can act on. None when it's connected.

    `seen_on_network` is None when discovery isn't running, so nothing is known either way.
    """
    if snap.connected:
        return None
    if seen_on_network is False:
        return "Not seen on the network. Check that it's powered on and joined to the printer hotspot."
    if not has_host:
        return "No IP address yet. Waiting for the printer to announce itself on the network."
    return snap.error or "Connecting…"


class Relay:
    def __init__(self, config: RelayConfig, db: Database, discovery: Discovery, dump_dir: Path | None = None,
                 connection_factory=PrinterConnection, clock=time.monotonic,
                 wall_clock=lambda: datetime.now(timezone.utc)):
        self.config = config
        self.db = db
        self.discovery = discovery
        self.dump_dir = dump_dir
        self.connection_factory = connection_factory   # swapped for a fake in tests
        self.clock = clock                              # monotonic seconds, for intervals
        self.wall_clock = wall_clock                    # real time, for timestamps

        self.printers: dict[str, Printer] = {}          # printers being relayed, by id
        self.registered_serials: set[str] | None = None # every serial in the printers table (None until first sync)
        self.conns: dict[str, PrinterConnection] = {}
        self.histories: dict[str, PrinterHistory] = {}

        self.last_sent: dict[str, tuple[tuple, float]] = {}  # printer id -> (fingerprint, time sent)
        self.unsent_since: dict[str, float] = {}             # printer id -> when unsent reports started arriving
        self.latencies: list[float] = []                     # seconds from report to Supabase, since the last heartbeat
        self.problems: dict[str, str | None] = {}            # printer id -> current connection problem
        self.pending_events: list[dict] = []
        self.pending_jobs: dict[str, dict] = {}              # job id -> latest row
        self.discovered_sent: dict[str, tuple[tuple, float]] = {}
        self._discovered_cleaned_for: set[str] | None = None
        # Values written since the last printer sync, so a column isn't rewritten every pass
        # while self.printers still holds the value from before the write.
        self.written: dict[tuple[str, str], object] = {}

        self.started = clock()
        self.last_sync: float | None = None
        self.last_full_request = self.started
        self.last_heartbeat_minute: datetime | None = None
        self._failing: dict[str, str] = {}
        self._warned: set[str] = set()

    # main loop

    def run(self, stop: threading.Event) -> None:
        self.discovery.start()
        try:
            while not stop.is_set():
                self.tick()
                stop.wait(self.config.push_interval_s)
        finally:
            log.info("shutting down")
            for conn in self.conns.values():
                conn.stop()
            self.discovery.stop()
            self._step("history", self._flush_history)  # last chance to save queued rows
            self.db.close()

    def tick(self) -> None:
        now = self.clock()
        if self.last_sync is None or now - self.last_sync >= self.config.printer_sync_interval_s:
            self.last_sync = now  # on failure, try again next interval instead of every pass
            self._step("printer sync", self._sync_printers)
        self._step("connections", self._reconcile_connections)
        self._step("discovery", self._record_discovery)
        self._step("status", self._push_status)
        self._step("connection errors", self._write_problems)
        self._step("history", self._flush_history)

        wall = self.wall_clock()
        minute = wall.replace(second=0, microsecond=0)
        # Send in the second half of each minute, so the row's server timestamp (which decides the
        # minute it counts for) still lands in the same minute even if the clocks differ a little.
        if wall.second >= 30 and minute != self.last_heartbeat_minute:
            if self._step("heartbeat", self._heartbeat):
                self.last_heartbeat_minute = minute

        if self.config.request_full_status and now - self.last_full_request >= self.config.full_status_interval_s:
            for conn in self.conns.values():
                conn.request_full_status()
            self.last_full_request = now

    def _step(self, name: str, fn) -> bool:
        """Run one step of the loop. Logs a failure once (not every 2 s) and logs when it recovers."""
        try:
            fn()
        except Exception as e:
            message = str(e)
            if self._failing.get(name) != message:
                if isinstance(e, DatabaseError):
                    log.error("%s failed: %s", name, message)
                else:
                    log.exception("%s failed", name)  # a bug, so include the traceback
            self._failing[name] = message
            return False
        if self._failing.pop(name, None) is not None:
            log.info("%s working again", name)
        return True

    # 1. printer list

    def _sync_printers(self) -> None:
        # Do every read first, so a failure halfway through changes nothing.
        all_printers = self.db.load_printers()
        active = {}
        for p in all_printers:
            if p.retired:
                continue
            if not p.access_code:
                self._warn_once(f"no-code:{p.id}", "[%s] has no access code in printer_connections, skipping", p.label)
                continue
            active[p.id] = p
        new_ids = [pid for pid in active if pid not in self.printers]
        last_status = self.db.load_last_status(new_ids) if new_ids else {}
        open_jobs = self.db.load_open_jobs(new_ids) if new_ids else {}

        present_ids = {p.id for p in all_printers}
        for pid in [pid for pid in self.printers if pid not in active]:
            self._remove_printer(pid, deleted=pid not in present_ids)

        for pid, p in active.items():
            old = self.printers.get(pid)
            if old is None:
                log.info("[%s] tracking printer %s", p.label, p.serial)
                self.histories[pid] = PrinterHistory(pid, last_status.get(pid), open_jobs.get(pid))
            elif pid in self.conns:
                if p.access_code != old.access_code:
                    log.info("[%s] connection settings changed, reconnecting", p.label)
                    self._disconnect(pid)  # reconnected by _reconcile_connections
                else:
                    self.conns[pid].printer = p  # e.g. a new label

        self.printers = active
        self.registered_serials = {p.serial for p in all_printers}
        self.written.clear()
        if self.registered_serials != self._discovered_cleaned_for:
            # Registered printers don't belong in discovered_printers any more
            self.db.delete_discovered(sorted(self.registered_serials))
            self._discovered_cleaned_for = self.registered_serials

    def _remove_printer(self, pid: str, deleted: bool) -> None:
        p = self.printers.pop(pid)
        log.info("[%s] %s, disconnecting", p.label, "deleted" if deleted else "retired")
        self._disconnect(pid)
        history = self.histories.pop(pid, None)
        if deleted:
            # Its rows can't be saved any more (they'd point at a printer that no longer exists).
            self.pending_events = [e for e in self.pending_events if e["printer_id"] != pid]
            self.pending_jobs = {k: v for k, v in self.pending_jobs.items() if v["printer_id"] != pid}
        elif history:
            row = history.close_open_job(self.wall_clock())
            if row:
                self.pending_jobs[row["id"]] = row
        for d in (self.last_sent, self.unsent_since, self.problems):
            d.pop(pid, None)

    # 2. connections

    def _reconcile_connections(self) -> None:
        """Connect each printer to its current IP, reconnecting if the IP changed."""
        for pid, p in self.printers.items():
            ann = self.discovery.get(p.serial)
            host = ann.ip if ann else self.written.get((pid, "host"), p.host)
            conn = self.conns.get(pid)
            if host is None or (conn and conn.host == host):
                continue
            if conn:
                log.warning("[%s] IP changed from %s to %s", p.label, conn.host, host)
                self._disconnect(pid)
            conn = self.connection_factory(p, host, self.config.request_full_status, self.dump_dir)
            conn.start()
            self.conns[pid] = conn

    def _disconnect(self, pid: str) -> None:
        conn = self.conns.pop(pid, None)
        if conn:
            conn.stop()

    # 3. discovery

    def _record_discovery(self) -> None:
        if not self.discovery.available:
            return
        for pid, p in self.printers.items():
            ann = self.discovery.get(p.serial)
            if not ann:
                continue
            self._update_if_changed(pid, "host", p.host, ann.ip, self.db.update_connection)
            if ann.firmware:
                self._update_if_changed(pid, "firmware_version", p.firmware_version, ann.firmware, self.db.update_printer)
            if ann.model:
                self._update_if_changed(pid, "model", p.model, ann.model, self.db.update_printer)
            elif ann.model_code:
                self._warn_once(f"model:{ann.model_code}", "[%s] unknown model code %r: add it to MODEL_NAMES in "
                                "relay/discovery.py", p.label, ann.model_code)

        if self.registered_serials is None:
            return  # until the first sync, every printer would look unregistered
        now = self.clock()
        rows, new = [], []
        for ann in self.discovery.recent():
            if ann.serial in self.registered_serials:
                continue
            info = (ann.ip, ann.model or ann.model_code, ann.dev_name, ann.firmware)
            sent = self.discovered_sent.get(ann.serial)
            if sent and sent[0] == info and now - sent[1] < DISCOVERED_REFRESH_S:
                continue
            rows.append({"serial": ann.serial, "ip": ann.ip, "model": info[1], "dev_name": ann.dev_name,
                         "firmware_version": ann.firmware, "last_seen": self._iso_now()})
            if not sent:
                new.append(ann)
        if rows:
            self.db.upsert_discovered(rows)
            for row in rows:
                self.discovered_sent[row["serial"]] = ((row["ip"], row["model"], row["dev_name"],
                                                        row["firmware_version"]), now)
            for ann in new:
                log.info("unregistered printer on the network: %s (%s) at %s", ann.serial, ann.dev_name, ann.ip)

    # 4. status and history

    def _push_status(self) -> None:
        now, wall = self.clock(), self.wall_clock()
        in_grace = now - self.started < STARTUP_GRACE_S
        to_send = []
        for pid, p in self.printers.items():
            conn = self.conns.get(pid)
            snap = conn.snapshot() if conn else Snapshot()
            if snap.first_unread_at is not None:
                self.unsent_since.setdefault(pid, snap.first_unread_at)
            if not in_grace:
                seen = (self.discovery.get(p.serial) is not None) if self.discovery.available else None
                self.problems[pid] = connection_problem(snap, has_host=conn is not None, seen_on_network=seen)

            if snap.connected and not snap.state:
                continue  # connected, but the first report hasn't arrived yet
            if not snap.connected and pid not in self.last_sent and in_grace:
                continue  # don't flash "offline" while the relay is starting up

            record = build_status(p, snap.state, snap.connected, snap.last_message_at, wall)
            self._record_history(p, record, wall)
            fp = fingerprint(record)
            sent = self.last_sent.get(pid)
            changed = sent is None or sent[0] != fp
            if changed or now - sent[1] >= self.config.status_heartbeat_s:
                to_send.append((record, fp, changed))
            else:
                self.unsent_since.pop(pid, None)  # the reports since last time didn't change anything

        if not to_send:
            return
        self.db.upsert_status([record for record, _, _ in to_send])
        done = self.wall_clock().timestamp()
        for record, fp, changed in to_send:
            pid = record["printer_id"]
            self.last_sent[pid] = (fp, now)
            since = self.unsent_since.pop(pid, None)
            if changed and since is not None:
                # From the first report after the previous push, so it's an upper bound.
                self.latencies.append(max(0.0, done - since))
        for record, _, _ in to_send:
            if record["filament_color"]:
                pid = record["printer_id"]
                self._update_if_changed(pid, "filament_color", self.printers[pid].filament_color,
                                        record["filament_color"], self.db.update_printer)

    def _record_history(self, p: Printer, record: dict, wall: datetime) -> None:
        events, jobs = self.histories[p.id].observe(record, wall)
        for e in events:
            log.info("[%s] %s: %s -> %s", p.label, e["kind"], e["from_value"], e["to_value"])
        for job in jobs:
            if job["ended_at"]:
                log.info("[%s] print %s: %r", p.label, job["outcome"], job["job_name"])
            self.pending_jobs[job["id"]] = job
        self.pending_events.extend(events)
        if len(self.pending_events) > MAX_PENDING_EVENTS:
            dropped = len(self.pending_events) - MAX_PENDING_EVENTS
            del self.pending_events[:dropped]
            self._warn_once("events-dropped", "event queue full, dropping the oldest events")

    # 5. connection problems

    def _write_problems(self) -> None:
        for pid, problem in self.problems.items():
            p = self.printers.get(pid)
            if p:
                self._update_if_changed(pid, "last_error", p.last_error, problem, self.db.update_connection)

    # 6. history

    def _flush_history(self) -> None:
        while self.pending_events:
            batch = self.pending_events[:HISTORY_BATCH]
            self._send_or_drop("printer_events", self.db.insert_events, batch)
            del self.pending_events[:len(batch)]
        if self.pending_jobs:
            self._send_or_drop("print_jobs", self.db.upsert_jobs, list(self.pending_jobs.values()))
            self.pending_jobs.clear()

    def _send_or_drop(self, table: str, write, rows: list[dict]) -> None:
        """Send rows. Re-raise on errors worth retrying. Drop rows Supabase rejected outright,
        so one bad row can't block everything queued behind it."""
        try:
            write(rows)
        except DatabaseError as e:
            if e.retryable:
                raise
            log.error("dropping %d %s row(s) that Supabase rejected: %s", len(rows), table, e)

    # 7. heartbeat

    def _heartbeat(self) -> None:
        self.db.insert_heartbeat({
            "printers_online": sum(1 for c in self.conns.values() if c.is_connected),
            "max_push_latency_ms": round(max(self.latencies) * 1000) if self.latencies else None,
        })
        self.latencies.clear()

    # helpers

    def _update_if_changed(self, pid: str, column: str, db_value, new_value, write) -> None:
        key = (pid, column)
        if new_value == self.written.get(key, db_value):
            return
        write(pid, {column: new_value})
        self.written[key] = new_value

    def _warn_once(self, key: str, message: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    def _iso_now(self) -> str:
        return self.wall_clock().isoformat(timespec="seconds")
