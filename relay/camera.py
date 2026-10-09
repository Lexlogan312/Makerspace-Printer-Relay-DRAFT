"""Still photos from a printer's built-in camera, over the local network.

The A1 and P1 series serve their camera on TCP port 6000, inside TLS with the printer's own
self-signed certificate (the same as MQTT on 8883). After an 80-byte login packet carrying the
LAN access code, the printer streams JPEG frames (about 1-2 a second), each preceded by a 16-byte
header whose first 4 bytes are the frame's length (little-endian). The camera wakes up when someone
connects, and its first frames come out before exposure and white balance settle (a flat beige
blur), so this keeps reading for `settle_s` seconds and returns the last frame, then hangs up.
A snapshot costs a few frames (roughly 50-200 KB each), not a video stream.

(The X1 and H2 series use RTSP on port 322 instead. They aren't in the makerspace, so they
aren't handled here.)

Test it on one printer, from the Pi or a laptop joined to the printer network:

    uv run python -m relay.camera --host 10.42.0.112 --code 12345678 --out fred.jpg
    uv run python -m relay.camera --printer "Fred Printstone" --out fred.jpg   # looks it up in Supabase
"""

import argparse
import logging
import socket
import ssl
import struct
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("relay.camera")

CAMERA_PORT = 6000
USERNAME = "bblp"                 # the fixed LAN-mode user name
HEADER_SIZE = 16
MAX_FRAME_BYTES = 4 * 1024 * 1024  # a 1080p JPEG is well under this; anything bigger is garbage
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


class CameraError(RuntimeError):
    """Why a snapshot failed, in words an admin can act on."""


def login_packet(access_code: str) -> bytes:
    """The 80-byte login: four little-endian uint32s (0x40, 0x3000, 0, 0), then the user name and
    the access code, each as ASCII padded with zeros to 32 bytes."""
    user, code = USERNAME.encode("ascii"), access_code.encode("ascii")
    if len(code) > 32:
        raise CameraError("Access code is too long (the printer's code is 8 characters).")
    return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + user.ljust(32, b"\0") + code.ljust(32, b"\0")


def _read_exactly(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise CameraError("The printer closed the camera connection. Check the access code, and that "
                              "the printer's camera video setting is on.")
        buf += chunk
    return bytes(buf)


def read_frame(sock, attempts: int = 3) -> bytes:
    """Read frames until one is a complete JPEG (a malformed one is skipped; the next is normally fine)."""
    for _ in range(attempts):
        header = _read_exactly(sock, HEADER_SIZE)
        size = int.from_bytes(header[0:4], "little")
        if not 0 < size <= MAX_FRAME_BYTES:
            raise CameraError(f"The printer sent an unexpected camera header (frame size {size}).")
        frame = _read_exactly(sock, size)
        if frame.startswith(JPEG_START) and frame.endswith(JPEG_END):
            return frame
    raise CameraError("The printer's camera frames weren't valid JPEG images.")


SETTLE_S = 3.0  # how long the camera gets to adjust after waking up


def read_settled_frame(sock, settle_s: float, clock=time.monotonic) -> bytes:
    """Read frames for `settle_s` seconds (at least one), returning the newest."""
    deadline = clock() + settle_s
    frame = read_frame(sock)
    while clock() < deadline:
        try:
            frame = read_frame(sock)
        except CameraError:
            break  # the stream ended early: the last good frame is still worth keeping
    return frame


def grab_frame(host: str, access_code: str, timeout: float = 15.0, settle_s: float = SETTLE_S) -> bytes:
    """Connect, log in, and return one settled JPEG frame. Raises CameraError with a plain explanation."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the printer's certificate is self-signed
    try:
        raw = socket.create_connection((host, CAMERA_PORT), timeout=timeout)
    except socket.timeout:
        raise CameraError(f"No answer from {host}:{CAMERA_PORT}. Is the printer on and on this network?") from None
    except ConnectionRefusedError:
        raise CameraError(f"{host} refused the camera connection. The camera may be off in the printer's "
                          "settings, or this model doesn't serve it on port 6000.") from None
    except OSError as e:
        raise CameraError(f"Can't reach {host}:{CAMERA_PORT}: {e.strerror or e}") from None

    try:
        with ctx.wrap_socket(raw, server_hostname=host) as sock:
            sock.settimeout(timeout)
            sock.sendall(login_packet(access_code))
            return read_settled_frame(sock, settle_s)
    except socket.timeout:
        raise CameraError("The camera connected but sent no picture in time. Check the access code, and that "
                          "the printer's camera video setting is on.") from None
    except ssl.SSLError as e:
        raise CameraError(f"Secure connection to the camera failed: {e.reason or e}") from None
    except OSError as e:
        raise CameraError(f"Camera connection dropped: {e.strerror or e}") from None
    finally:
        raw.close()  # in case the TLS handshake failed before the wrapped socket took it over


@dataclass(frozen=True)
class CameraTarget:
    printer_id: str
    label: str
    host: str
    access_code: str


class SnapshotWorker:
    """Takes one snapshot of each printing printer every `interval_s`, on its own thread so a
    slow camera never holds up status updates. Each photo replaces the printer's previous one."""

    def __init__(self, db, interval_s: float, targets: Callable[[], list[CameraTarget]],
                 grab: Callable[[str, str], bytes] = grab_frame,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.db = db
        self.interval_s = interval_s
        self.targets = targets          # printing printers right now (supplied by the relay)
        self.grab = grab                # swapped for a fake in tests
        self.wall_clock = wall_clock
        self._errors: dict[str, str] = {}   # printer id -> last error logged, so it's logged once
        self._thread: threading.Thread | None = None

    def start(self, stop: threading.Event) -> None:
        self._thread = threading.Thread(target=self.run, args=(stop,), name="camera", daemon=True)
        self._thread.start()

    def run(self, stop: threading.Event) -> None:
        log.info("camera snapshots on: every %.0f s for each printing printer", self.interval_s)
        while not stop.is_set():
            started = time.monotonic()
            self.capture_all(stop)
            stop.wait(max(1.0, self.interval_s - (time.monotonic() - started)))

    def capture_all(self, stop: threading.Event | None = None) -> int:
        """One pass over the printing printers. Returns how many snapshots were saved."""
        saved = 0
        for t in self.targets():
            if stop is not None and stop.is_set():
                break
            if self.capture(t):
                saved += 1
        return saved

    def capture(self, t: CameraTarget) -> bool:
        try:
            jpeg = self.grab(t.host, t.access_code)
            self.db.upload_snapshot(t.printer_id, jpeg, self.wall_clock().isoformat(timespec="seconds"))
        except Exception as e:  # a camera or upload problem must never stop the other printers
            message = str(e)
            if self._errors.get(t.printer_id) != message:
                log.warning("[%s] camera snapshot failed: %s", t.label, message)
                self._errors[t.printer_id] = message
            return False
        if self._errors.pop(t.printer_id, None) is not None:
            log.info("[%s] camera snapshots working again", t.label)
        return True


def _lookup(config_path: str, name: str) -> tuple[str, str, str]:
    """(label, host, access code) for a printer by name, from Supabase via relay.toml."""
    from .config import load_config
    from .database import Database

    config = load_config(config_path)
    db = Database(config.supabase_url, config.supabase_key)
    try:
        printers = db.load_printers()
    finally:
        db.close()
    matches = [p for p in printers if p.label.casefold() == name.casefold()]
    if not matches:
        known = ", ".join(sorted(p.label for p in printers))
        raise CameraError(f"No printer named {name!r}. Printers: {known}")
    p = matches[0]
    if not p.host or not p.access_code:
        raise CameraError(f"{p.label} has no IP address or access code stored yet.")
    return p.label, p.host, p.access_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Save one camera snapshot from a printer.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--printer", metavar="NAME", help="look up the IP and access code in Supabase (needs relay.toml)")
    target.add_argument("--host", metavar="IP", help="the printer's IP address (use with --code)")
    parser.add_argument("--code", help="the printer's LAN access code (Settings > WLAN on the printer)")
    parser.add_argument("-c", "--config", default="relay.toml", help="relay config, for --printer (default: relay.toml)")
    parser.add_argument("--out", type=Path, default=Path("snapshot.jpg"), help="where to save it (default: snapshot.jpg)")
    parser.add_argument("--settle", type=float, default=SETTLE_S, metavar="SECONDS",
                        help=f"let the camera adjust this long before keeping a frame (default: {SETTLE_S:g})")
    args = parser.parse_args()

    try:
        if args.printer:
            label, host, code = _lookup(args.config, args.printer)
        else:
            if not args.code:
                parser.error("--host needs --code")
            label, host, code = args.host, args.host, args.code
        print(f"Connecting to {label} at {host}:{CAMERA_PORT}…")
        started = time.monotonic()
        frame = grab_frame(host, code, settle_s=args.settle)
    except CameraError as e:
        sys.exit(f"error: {e}")
    args.out.write_bytes(frame)
    print(f"Saved {args.out} ({len(frame) / 1024:.0f} KB) in {time.monotonic() - started:.1f} s")


if __name__ == "__main__":
    main()
