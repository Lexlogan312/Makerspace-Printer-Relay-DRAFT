"""Camera snapshots: the printer's frame protocol, the worker, and which printers get photographed."""

import struct
from datetime import datetime, timezone

import pytest

from relay.camera import CameraError, CameraTarget, SnapshotWorker, login_packet, read_frame
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
    worker = SnapshotWorker(db, 60, lambda: targets, grab=grab, wall_clock=lambda: when)
    assert worker.capture_all() == 1
    assert db.uploads == [("b", len(JPEG), "2026-10-09T15:00:00+00:00")]

    worker.capture_all()  # the same failure again isn't logged twice
    assert sum("camera snapshot failed" in r.message for r in caplog.records) == 1


def test_only_connected_printing_printers_are_photographed():
    relay, db, disc, clock = make_relay()
    disc.announce(PRINTER.serial, "10.42.0.23")
    relay.tick()
    assert relay.camera_targets() == []          # connected, but no report yet

    conn = FakeConnection.created[0]
    conn.report(state("IDLE"), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert relay.camera_targets() == []          # idle: nothing to look at

    conn.report(state("RUNNING"), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert relay.camera_targets() == [CameraTarget(PRINTER.id, PRINTER.label, "10.42.0.23", PRINTER.access_code)]
