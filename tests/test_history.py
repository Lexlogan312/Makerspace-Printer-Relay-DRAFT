from datetime import timedelta

from relay.history import OFFLINE_JOB_TIMEOUT, PrinterHistory, format_error
from tests.fixtures import NOW, PRINTER, record

T = [NOW + timedelta(minutes=m) for m in range(10)]


def kinds(events):
    return [(e["kind"], e["from_value"], e["to_value"]) for e in events]


def test_print_lifecycle_creates_one_job():
    h = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "IDLE", "print_error": 0})

    events, jobs = h.observe(record("PREPARE", subtask_name=""), T[0])
    assert kinds(events) == [("state", "IDLE", "PREPARE")]
    assert len(jobs) == 1 and jobs[0]["ended_at"] is None and jobs[0]["job_name"] is None
    job_id = jobs[0]["id"]

    # The job name is reported a moment later, so the job is filled in
    events, jobs = h.observe(record("RUNNING"), T[1])
    assert [j["id"] for j in jobs] == [job_id] and jobs[0]["job_name"] == "bracket_v2"

    # No change, so nothing to write
    assert h.observe(record("RUNNING"), T[2]) == ([], [])

    events, jobs = h.observe(record("FINISH"), T[3])
    assert kinds(events) == [("state", "RUNNING", "FINISH")]
    assert jobs[0]["id"] == job_id and jobs[0]["outcome"] == "finished"
    assert jobs[0]["started_at"] == T[0].isoformat(timespec="seconds")
    assert jobs[0]["ended_at"] == T[3].isoformat(timespec="seconds")
    assert jobs[0]["filament_type"] == "PETG" and jobs[0]["filament_color_name"] == "orange"
    assert h.job is None


def test_failed_print_records_error():
    h = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "RUNNING", "print_error": 0})
    h.observe(record("RUNNING"), T[0])
    events, jobs = h.observe(record("FAILED", print_error=83935248), T[1])
    assert ("error", None, "0500-C010") in kinds(events)
    assert jobs[0]["outcome"] == "failed" and jobs[0]["print_error"] == 83935248


def test_offline_keeps_job_open_and_logs_connection():
    h = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "RUNNING"})
    h.observe(record("RUNNING"), T[0])
    events, jobs = h.observe(record(online=False), T[1])
    assert kinds(events) == [("connection", "online", "offline")] and jobs == []
    assert h.job is not None

    events, jobs = h.observe(record("RUNNING"), T[2])
    assert kinds(events) == [("connection", "offline", "online")] and jobs == []


def test_restart_resumes_open_job_instead_of_duplicating():
    first = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "IDLE"})
    _, jobs = first.observe(record("RUNNING"), T[0])
    open_row = jobs[0]

    # The relay restarts: it reads back printer_status and the open print_jobs row
    after = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "RUNNING"}, open_job=open_row)
    assert after.observe(record("RUNNING"), T[1]) == ([], [])
    _, jobs = after.observe(record("FINISH"), T[2])
    assert jobs[0]["id"] == open_row["id"] and jobs[0]["outcome"] == "finished"


def test_new_print_while_relay_was_down_closes_old_job():
    old = {"id": "old", "printer_id": PRINTER.id, "printer_label": "FredPrintstone", "job_name": "old part",
           "started_at": "x", "ended_at": None, "outcome": None, "filament_type": "PLA",
           "filament_color": "#FFFFFF", "filament_color_name": "white", "print_error": 0}
    h = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "RUNNING"}, open_job=old)
    _, jobs = h.observe(record("RUNNING", subtask_name="new part"), T[0])
    assert [(j["job_name"], j["outcome"]) for j in jobs] == [("old part", "unknown"), ("new part", None)]


def test_first_ever_observation_records_state():
    h = PrinterHistory(PRINTER.id)  # no printer_status row yet
    events, _ = h.observe(record("IDLE"), T[0])
    assert kinds(events) == [("connection", None, "online"), ("state", None, "IDLE")]


def test_missing_gcode_state_does_not_close_job():
    h = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "RUNNING"})
    h.observe(record("RUNNING"), T[0])
    events, jobs = h.observe(record(None), T[1])
    assert events == [] and jobs == [] and h.job is not None


def test_close_open_job_when_retired():
    h = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "IDLE"})
    h.observe(record("RUNNING"), T[0])
    row = h.close_open_job(T[1])
    assert row["outcome"] == "unknown" and row["ended_at"] == T[1].isoformat(timespec="seconds")
    assert h.close_open_job(T[2]) is None


def test_format_error():
    assert format_error(83935248) == "0500-C010"


def test_job_closed_when_printer_stays_offline():
    h = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "IDLE"})
    _, (first,) = h.observe(record("RUNNING"), T[0])
    h.observe(record(online=False), T[1])                      # goes offline (e.g. power cut)
    assert h.observe(record(online=False), T[5]) == ([], [])    # still within the timeout
    _, jobs = h.observe(record(online=False), T[1] + OFFLINE_JOB_TIMEOUT)
    assert jobs[0]["outcome"] == "unknown"
    assert jobs[0]["ended_at"] == T[1].isoformat(timespec="seconds")  # when it went offline, not now
    # It comes back still printing: a new job starts
    _, (second,) = h.observe(record("RUNNING"), T[1] + OFFLINE_JOB_TIMEOUT * 2)
    assert second["id"] != first["id"] and second["ended_at"] is None


def test_print_that_ended_while_relay_was_down_uses_last_seen():
    _, jobs = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "IDLE"}).observe(record("RUNNING"), T[0])
    last_seen = T[2].isoformat(timespec="seconds")
    after = PrinterHistory(PRINTER.id, {"is_online": True, "gcode_state": "RUNNING", "last_seen": last_seen},
                           open_job=jobs[0])
    _, closed = after.observe(record("FINISH"), T[9])  # relay comes back much later
    assert closed[0]["outcome"] == "finished" and closed[0]["ended_at"] == last_seen

    # The hint is only for that first report: a later job ends at the time it's seen
    after.observe(record("RUNNING", subtask_name="next"), T[9])
    _, closed = after.observe(record("FINISH", subtask_name="next"), T[9] + OFFLINE_JOB_TIMEOUT)
    assert closed[0]["ended_at"] == (T[9] + OFFLINE_JOB_TIMEOUT).isoformat(timespec="seconds")
