"""Turn a printer's changing status into history rows for analytics.

- printer_events: one row each time a printer goes online/offline, changes gcode_state,
  or reports a new error.
- print_jobs: one row per print, opened when the print starts and closed when it ends.

Bambu LAN reports have no job id (task_id is always "0") and no start time, so prints are
tracked through gcode_state. A print starts when the printer enters an active state and
ends when it leaves one. Times are when the relay saw the change, accurate to a few seconds
while the relay is running.
"""

import uuid
from datetime import datetime, timedelta

from .status import color_name

ACTIVE_STATES = {"PREPARE", "SLICING", "RUNNING", "PAUSE"}
END_OUTCOMES = {"FINISH": "finished", "FAILED": "failed"}  # leaving an active state any other way -> "unknown"
# A cancelled print ends in FAILED with this error code ("0300-400C", the task was cancelled),
# so it's recorded as "cancelled" and doesn't count as a failure.
CANCELLED_ERROR = 0x0300400C

# A printer offline this long with a print in progress probably lost power. Its job is closed
# at the moment it went offline, so it can't count as "printing" forever. If the printer comes
# back still printing, a new job starts.
OFFLINE_JOB_TIMEOUT = timedelta(hours=1)

# print_jobs columns, in the order the relay writes them (also what's read back on startup)
JOB_COLUMNS = ("id", "printer_id", "printer_label", "job_name", "started_at", "ended_at", "outcome",
               "filament_type", "filament_color", "filament_color_name", "print_error")


def format_error(code: int) -> str:
    """83935248 -> '0500-C010', the format Bambu's wiki uses for error codes."""
    digits = f"{code:08X}"
    return f"{digits[:4]}-{digits[4:]}"


def _online_word(online: bool | None) -> str | None:
    return None if online is None else ("online" if online else "offline")


class PrinterHistory:
    """History tracking for one printer.

    `last_status` is the printer's printer_status row from before the relay started, and
    `open_job` is its unfinished print_jobs row, if any. Starting from these means a relay
    restart doesn't create duplicate events or split a print into two jobs.
    """

    def __init__(self, printer_id: str, last_status: dict | None = None, open_job: dict | None = None):
        last_status = last_status or {}
        self.printer_id = printer_id
        self.online: bool | None = last_status.get("is_online")
        self.gcode_state: str | None = last_status.get("gcode_state")
        self.print_error: int = last_status.get("print_error") or 0
        self.job: dict | None = open_job
        self.offline_since: datetime | None = None
        # If the open job turns out to have ended while the relay was down, the best end time
        # available is when the relay last heard from the printer.
        self.end_hint: str | None = last_status.get("last_seen") if open_job else None

    def observe(self, record: dict, at: datetime) -> tuple[list[dict], list[dict]]:
        """Compare a new status record with the previous one.

        Returns (new printer_events rows, print_jobs rows to upsert).
        """
        ts = at.isoformat(timespec="seconds")
        events = []

        if record["online"] != self.online:
            events.append(self._event(ts, "connection", _online_word(self.online), _online_word(record["online"])))
            self.online = record["online"]

        if not record["online"]:
            self.offline_since = self.offline_since or at
            if self.job and at - self.offline_since >= OFFLINE_JOB_TIMEOUT:
                end = self.end_hint or self.offline_since.isoformat(timespec="seconds")
                return events, [self._close(end, "unknown", self.job["print_error"])]
            return events, []  # an open job stays open, the printer may still be printing
        self.offline_since = None

        state = record["gcode_state"]
        if state is None:
            return events, []  # connected, but gcode_state hasn't been reported yet

        if state != self.gcode_state:
            events.append(self._event(ts, "state", self.gcode_state, state))
            self.gcode_state = state

        error = record["print_error"]
        if error != self.print_error:
            if error:
                previous = format_error(self.print_error) if self.print_error else None
                events.append(self._event(ts, "error", previous, format_error(error)))
            self.print_error = error

        return events, self._track_job(record, state, ts)

    def close_open_job(self, at: datetime) -> dict | None:
        """End an open job with outcome "unknown", e.g. when the printer is retired."""
        if not self.job:
            return None
        return self._close(at.isoformat(timespec="seconds"), "unknown", self.job["print_error"])

    def _track_job(self, record: dict, state: str, ts: str) -> list[dict]:
        rows = []
        active = state in ACTIVE_STATES
        name = record["job_name"]

        if self.job:
            new_print = name and self.job["job_name"] and name != self.job["job_name"]
            end = self.end_hint or ts  # end_hint only applies to the first report after a restart
            if not active:
                outcome = END_OUTCOMES.get(state, "unknown")
                if state == "FAILED" and record["print_error"] == CANCELLED_ERROR:
                    outcome = "cancelled"
                rows.append(self._close(end, outcome, record["print_error"]))
            elif new_print:
                # A different print is running, so the previous one ended while the relay wasn't watching
                rows.append(self._close(end, "unknown", self.job["print_error"]))
        self.end_hint = None

        if active:
            if self.job is None:
                self.job = {
                    "id": str(uuid.uuid4()),
                    "printer_id": self.printer_id,
                    "printer_label": record["label"],
                    "job_name": name,
                    "started_at": ts,
                    "ended_at": None,
                    "outcome": None,
                    "filament_type": record["filament_type"],
                    "filament_color": record["filament_color"],
                    "filament_color_name": record["filament_color_name"],
                    "print_error": 0,
                }
                rows.append(dict(self.job))
            elif self._fill_in(record):
                rows.append(dict(self.job))
        return rows

    def _fill_in(self, record: dict) -> bool:
        """Fill in details that weren't reported yet when the job started.
        Returns True if anything changed."""
        updates = {
            "job_name": record["job_name"],
            "filament_type": record["filament_type"],
            "filament_color": record["filament_color"],
        }
        changed = False
        for key, value in updates.items():
            if value and not self.job[key]:
                self.job[key] = value
                changed = True
        if changed:
            self.job["filament_color_name"] = color_name(self.job["filament_color"])
        return changed

    def _close(self, ts: str, outcome: str, print_error: int) -> dict:
        self.job.update(ended_at=ts, outcome=outcome, print_error=print_error)
        row, self.job, self.end_hint = dict(self.job), None, None
        return row

    def _event(self, ts: str, kind: str, from_value: str | None, to_value: str | None) -> dict:
        return {"id": str(uuid.uuid4()), "printer_id": self.printer_id, "ts": ts,
                "kind": kind, "from_value": from_value, "to_value": to_value}
