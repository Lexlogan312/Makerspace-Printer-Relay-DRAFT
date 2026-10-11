"""Camera snapshots: the printer's frame protocol, the worker, and which printers get photographed."""

import struct
from datetime import datetime, timezone

import pytest

from relay.camera import CameraError, CameraTarget, SnapshotWorker, login_packet, read_frame, read_settled_frame
from tests.fixtures import PRINTER, state
from tests.test_app import FakeConnection, make_relay

JPEG = b"\xff\xd8\xff\xe0" + b"pixels" * 100 + b"\xff\xd9"


def framed(payload: bytes) -> bytes:
    """A frame as the printer sends it: a 16-byte header (length first, little-endian), then the JPEG."""
    return struct.pack("<IIII", len(payload), 0, 1, 0) + payload


class FakeSocket:
    """Hands out the given bytes in small pieces, like a real socket can."""

    def __init__(self, data: bytes, chunk: int = 7):
        self.data, self.chunk = data, chunk

    def recv(self, n: int) -> bytes:
        out, self.data = self.data[:min(n, self.chunk)], self.data[min(n, self.chunk):]
        return out


def test_login_packet_layout():
    packet = login_packet("12345678")
    assert len(packet) == 80
    assert struct.unpack("<IIII", packet[:16]) == (0x40, 0x3000, 0, 0)
    assert packet[16:48] == b"bblp".ljust(32, b"\0")
    assert packet[48:80] == b"12345678".ljust(32, b"\0")


def test_reads_one_frame_across_many_small_reads():
    assert read_frame(FakeSocket(framed(JPEG) + framed(b"next"))) == JPEG


def test_skips_a_malformed_frame():
    assert read_frame(FakeSocket(framed(b"not a jpeg") + framed(JPEG))) == JPEG


def test_waits_for_the_camera_to_settle_and_keeps_the_newest_frame():
    first, later = JPEG.replace(b"pixels", b"blurry"), JPEG
    ticks = iter([0.0, 1.0, 2.0, 3.5])  # deadline at 3.0
    sock = FakeSocket(framed(first) + framed(first) + framed(later) + framed(b"unused"))
    assert read_settled_frame(sock, 3.0, clock=lambda: next(ticks)) == later


def test_rejects_a_bad_header_and_a_closed_connection():
    with pytest.raises(CameraError, match="frame size"):
        read_frame(FakeSocket(struct.pack("<IIII", 0, 0, 0, 0)))
    with pytest.raises(CameraError, match="closed"):
        read_frame(FakeSocket(framed(JPEG)[:40]))


class FakeDB:
    def __init__(self):
        self.uploads = []

    def upload_snapshot(self, printer_id, jpeg, taken_at):
        self.uploads.append((printer_id, len(jpeg), taken_at))


def test_worker_uploads_each_target_and_one_failure_does_not_stop_the_rest(caplog):
    db = FakeDB()
    targets = [CameraTarget("a", "Fred", "10.42.0.1", "1"), CameraTarget("b", "Flint", "10.42.0.2", "2")]

    def grab(host, code):
        if host == "10.42.0.1":
            raise CameraError("refused")
        return JPEG

    when = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
    worker = SnapshotWorker(db, lambda: targets, grab=grab, wall_clock=lambda: when)
    assert worker.capture_all() == 1
    assert db.uploads == [("b", len(JPEG), "2026-10-09T15:00:00+00:00")]

    worker.capture_all()  # the same failure again isn't logged twice
    assert sum("camera snapshot failed" in r.message for r in caplog.records) == 1


def test_printing_printers_are_photographed_and_open_ones_flagged_as_not_printing():
    relay, db, disc, clock = make_relay()
    disc.announce(PRINTER.serial, "10.42.0.23")
    relay.tick()
    assert relay.camera_targets() == []          # connected, but no report yet

    conn = FakeConnection.created[0]
    conn.report(state("IDLE"), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert relay.camera_targets() == [CameraTarget(PRINTER.id, PRINTER.label, "10.42.0.23", PRINTER.access_code,
                                                   printing=False)]  # open: photographed now and then

    conn.report(state("RUNNING"), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert relay.camera_targets() == [CameraTarget(PRINTER.id, PRINTER.label, "10.42.0.23", PRINTER.access_code)]


def test_a_missing_migration_is_reported_once_for_all_printers(caplog):
    from relay.database import NEEDS_SNAPSHOT_SETUP, DatabaseError

    class NoBucket:
        def upload_snapshot(self, *args):
            raise DatabaseError(NEEDS_SNAPSHOT_SETUP, status=404)

    targets = [CameraTarget(str(i), f"P{i}", f"10.42.0.{i}", "1") for i in range(5)]
    worker = SnapshotWorker(NoBucket(), lambda: targets, grab=lambda h, c: JPEG)
    worker.capture_all()
    worker.capture_all()
    assert sum("sql/07" in r.message for r in caplog.records) == 1


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def schedule(watched=(), watch_max=6):
    clock, targets = Clock(), {}
    worker = SnapshotWorker(FakeDB(), lambda: list(targets.values()), printing_s=60, idle_s=300, watch_s=4,
                            watch_max=watch_max, watched=lambda: set(watched), clock=clock)

    def due():
        shots = [t.printer_id for t in worker.due()]
        for pid in shots:  # as if each capture finished at once
            worker._in_flight.discard(pid)
        return shots
    return worker, clock, targets, due


def target(pid, printing):
    return CameraTarget(pid, pid, "10.42.0.1", "1", printing=printing)


def test_printing_every_minute_open_every_five_and_once_right_after_a_print_ends():
    worker, clock, targets, due = schedule()
    targets["p"] = target("p", True)
    targets["o"] = target("o", False)
    assert sorted(due()) == ["o", "p"]   # first photo of each straight away
    clock.t = 59
    assert due() == []
    clock.t = 60
    assert due() == ["p"]
    clock.t = 70
    targets["p"] = target("p", False)    # the print just finished: photograph the plate now
    assert due() == ["p"]
    clock.t = 299
    assert due() == []
    clock.t = 300
    assert due() == ["o"]


def test_watched_printers_refresh_every_few_seconds_up_to_a_limit():
    worker, clock, targets, due = schedule(watched={"a", "b", "c"}, watch_max=2)
    for pid in "abc":
        targets[pid] = target(pid, False)
    due()
    clock.t = 4
    assert sorted(due()) == ["a", "b"]   # c is watched too, but over the limit: normal schedule


def test_a_printer_already_being_photographed_is_not_queued_twice():
    worker, clock, targets, due = schedule()
    targets["p"] = target("p", True)
    assert [t.printer_id for t in worker.due()] == ["p"]   # still in flight
    clock.t = 120
    assert worker.due() == []


def test_light_command_and_report_parsing():
    from relay.light import ledctrl, light_mode
    assert ledctrl("on")["system"] == {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light",
                                       "led_mode": "on", "led_on_time": 500, "led_off_time": 500,
                                       "loop_times": 0, "interval_time": 0}
    assert light_mode({"print": {"lights_report": [{"node": "chamber_light", "mode": "off"}]}}) == "off"
    assert light_mode({"print": {"gcode_state": "IDLE"}}) is None
