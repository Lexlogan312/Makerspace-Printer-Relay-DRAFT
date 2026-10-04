"""The main loop, run against fake Supabase / discovery / printer connections."""

import dataclasses
from datetime import datetime, timedelta, timezone

from relay.app import Relay, STARTUP_GRACE_S, connection_problem
from relay.config import RelayConfig
from relay.database import DatabaseError
from relay.discovery import Announcement
from relay.printer import Snapshot
from tests.fixtures import PRINTER, PRINTING, state

PRINTING_AMS = PRINTING["print"]["ams"]["ams"]

OTHER_SERIAL = "03900D5C2000916"


class FakeDB:
    def __init__(self, printers):
        self.printers = printers
        self.calls = []
        self.fail: dict[str, DatabaseError] = {}   # method name -> error to raise

    def _call(self, name, *args):
        if name in self.fail:
            raise self.fail[name]
        self.calls.append((name, *args))

    def written(self, name):
        return [c[1:] for c in self.calls if c[0] == name]

    def load_printers(self):
        self._call("load_printers")
        return list(self.printers)

    def load_last_status(self, ids):
        return {}

    def load_open_jobs(self, ids):
        return {}

    def upsert_status(self, records): self._call("upsert_status", records)
    def update_printer(self, pid, fields): self._call("update_printer", pid, fields)
    def update_connection(self, pid, fields): self._call("update_connection", pid, fields)
    def insert_events(self, rows): self._call("insert_events", rows)
    def upsert_jobs(self, rows): self._call("upsert_jobs", rows)
    def insert_heartbeat(self, row): self._call("insert_heartbeat", row)
    def upsert_discovered(self, rows): self._call("upsert_discovered", rows)
    def delete_discovered(self, serials): self._call("delete_discovered", serials)
    def close(self): pass


class FakeDiscovery:
    available = True

    def __init__(self):
        self.announcements: dict[str, Announcement] = {}

    def announce(self, serial, ip, model_code="N2S", firmware="01.08.00.00"):
        self.announcements[serial] = Announcement(serial, ip, model_code, "3DP-039-895", firmware, seen_at=0)

    def get(self, serial): return self.announcements.get(serial)
    def recent(self): return list(self.announcements.values())
    def start(self): pass
    def stop(self): pass


class FakeConnection:
    created = []

    def __init__(self, printer, host, request_full_status, dump_dir):
        self.printer, self.host = printer, host
        self.snap = Snapshot()
        self.started = self.stopped = False
        FakeConnection.created.append(self)

    @property
    def is_connected(self): return self.snap.connected

    def report(self, st, at):
        self.snap = Snapshot(state=st, connected=True, last_message_at=at, first_unread_at=at)

    def snapshot(self):
        snap, self.snap = self.snap, dataclasses.replace(self.snap, first_unread_at=None)
        return snap

    def start(self): self.started = True
    def stop(self): self.stopped = True
    def request_full_status(self): pass


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = datetime(2026, 10, 2, 14, 0, 40, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


def make_relay(printers=(PRINTER,)):
    FakeConnection.created = []
    db, disc, clock = FakeDB(list(printers)), FakeDiscovery(), Clock()
    config = RelayConfig(supabase_url="x", supabase_key="x")
    relay = Relay(config, db, disc, connection_factory=FakeConnection,
                  clock=lambda: clock.mono, wall_clock=lambda: clock.wall)
    return relay, db, disc, clock


def test_connects_to_discovered_ip_and_records_what_discovery_learned():
    relay, db, disc, clock = make_relay([dataclasses.replace(PRINTER, model="Unknown")])
    disc.announce(PRINTER.serial, "10.42.0.23")
    disc.announce(OTHER_SERIAL, "10.42.0.40")  # not registered
    relay.tick()

    (conn,) = FakeConnection.created
    assert conn.host == "10.42.0.23" and conn.started  # discovery beats the stored 172.20.10.7
    assert (PRINTER.id, {"host": "10.42.0.23"}) in db.written("update_connection")
    assert (PRINTER.id, {"model": "A1"}) in db.written("update_printer")
    assert (PRINTER.id, {"firmware_version": "01.08.00.00"}) in db.written("update_printer")
    assert db.written("delete_discovered") == [([PRINTER.serial],)]
    (rows,) = db.written("upsert_discovered")[0]
    assert [r["serial"] for r in rows] == [OTHER_SERIAL]

    # Nothing changed, so the next pass writes none of that again
    before = len(db.calls)
    clock.advance(2)
    relay.tick()
    assert [c[0] for c in db.calls[before:]] == []


def test_pushes_status_only_when_it_changes_and_measures_latency():
    relay, db, disc, clock = make_relay()
    relay.tick()
    conn = FakeConnection.created[0]
    conn.report(state("RUNNING"), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    (records,) = db.written("upsert_status")[-1]
    assert records[0]["status"] == "printing"
    assert relay.latencies == [2.0]  # report arrived 2 s before the write

    pushes = len(db.written("upsert_status"))
    clock.advance(2)
    relay.tick()
    assert len(db.written("upsert_status")) == pushes  # unchanged

    clock.advance(31)  # status heartbeat
    relay.tick()
    assert len(db.written("upsert_status")) == pushes + 1


def test_loaded_filament_is_written_in_one_update_only_when_it_changes():
    relay, db, disc, clock = make_relay()
    relay.tick()
    conn = FakeConnection.created[0]
    conn.report(state("RUNNING"), clock.wall.timestamp())   # PETG, #FF6A13 (fixtures)
    clock.advance(2)
    relay.tick()
    assert db.written("update_printer") == [(PRINTER.id, {
        "filament_color": "#FF6A13", "filament_color_name": "Orange", "filament_type": "PETG"})]

    conn.report(state("RUNNING", mc_percent=50), clock.wall.timestamp())   # same filament
    clock.advance(2)
    relay.tick()
    assert len(db.written("update_printer")) == 1

    swapped = {"tray_now": "0", "ams": PRINTING_AMS}  # switch to the white PLA slot
    conn.report(state("RUNNING", ams=swapped), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert db.written("update_printer")[-1] == (PRINTER.id, {
        "filament_color": "#FFFFFF", "filament_color_name": "White", "filament_type": "PLA"})


def test_no_offline_flash_during_startup_then_reports_problem():
    relay, db, disc, clock = make_relay()
    relay.tick()  # connection created but never connects
    assert db.written("upsert_status") == []
    assert db.written("update_connection") == []

    clock.advance(STARTUP_GRACE_S + 1)
    relay.tick()
    (records,) = db.written("upsert_status")[-1]
    assert records[0]["online"] is False
    # Discovery is running but hasn't heard this printer
    assert (PRINTER.id, {"last_error": "Not seen on the network. Check that it's powered on and joined "
                                       "to the printer hotspot."}) in db.written("update_connection")


def test_ip_change_reconnects():
    relay, db, disc, clock = make_relay()
    disc.announce(PRINTER.serial, "10.42.0.23")
    relay.tick()
    disc.announce(PRINTER.serial, "10.42.0.77")
    clock.advance(2)
    relay.tick()
    first, second = FakeConnection.created
    assert first.stopped and second.host == "10.42.0.77"
    assert (PRINTER.id, {"host": "10.42.0.77"}) in db.written("update_connection")


def test_admin_changes_take_effect_on_next_sync():
    relay, db, disc, clock = make_relay()
    relay.tick()
    db.printers = [dataclasses.replace(PRINTER, label="Fred", access_code="87654321")]
    clock.advance(31)
    relay.tick()
    first, second = FakeConnection.created
    assert first.stopped and second.printer.access_code == "87654321"


def test_retiring_closes_open_job():
    relay, db, disc, clock = make_relay()
    relay.tick()
    FakeConnection.created[0].report(state("RUNNING"), clock.wall.timestamp())
    clock.advance(STARTUP_GRACE_S)
    relay.tick()
    db.printers = [dataclasses.replace(PRINTER, maintenance_status="offline_permanent")]
    clock.advance(31)
    relay.tick()
    assert FakeConnection.created[0].stopped and relay.printers == {}
    (rows,) = db.written("upsert_jobs")[-1]
    assert rows[0]["outcome"] == "unknown" and rows[0]["ended_at"]


def test_failed_writes_are_retried_and_rejected_rows_dropped():
    relay, db, disc, clock = make_relay()
    relay.tick()
    FakeConnection.created[0].report(state("RUNNING"), clock.wall.timestamp())
    db.fail["upsert_status"] = DatabaseError("can't reach Supabase")
    db.fail["insert_events"] = DatabaseError("can't reach Supabase")
    clock.advance(2)
    relay.tick()
    assert relay.pending_events  # kept for later

    del db.fail["upsert_status"], db.fail["insert_events"]
    clock.advance(2)
    relay.tick()
    assert db.written("upsert_status") and relay.pending_events == []

    # A row Supabase rejects (e.g. printer deleted a moment ago) is dropped, not retried forever
    relay.pending_events.append({"printer_id": "gone"})
    db.fail["insert_events"] = DatabaseError("violates foreign key", status=409)
    clock.advance(2)
    relay.tick()
    assert relay.pending_events == []


def test_heartbeat_once_per_minute_in_second_half():
    relay, db, disc, clock = make_relay()   # clock starts at 14:00:40
    relay.tick()
    clock.advance(2)
    relay.tick()
    assert len(db.written("insert_heartbeat")) == 1
    assert db.written("insert_heartbeat")[0][0]["printers_online"] is None  # still starting up
    clock.advance(20)   # 14:01:02, first half of the next minute
    relay.tick()
    assert len(db.written("insert_heartbeat")) == 1
    clock.advance(30)   # 14:01:32
    relay.tick()
    assert len(db.written("insert_heartbeat")) == 2
    assert db.written("insert_heartbeat")[1][0]["printers_online"] == 0  # past start-up: a real count


def test_connection_problem_messages():
    up = Snapshot(connected=True)
    down = Snapshot(connected=False, error="Access code rejected.")
    assert connection_problem(up, True, True) is None
    assert connection_problem(down, True, False).startswith("Not seen on the network")
    assert connection_problem(down, True, True) == "Access code rejected."
    assert connection_problem(Snapshot(), False, None).startswith("No IP address yet")
    assert connection_problem(Snapshot(), True, None) == "Connecting…"
