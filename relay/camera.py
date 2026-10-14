"""Still photos from a printer's built-in camera, over the local network.

The A1 and P1 series serve their camera on TCP port 6000, inside TLS with the printer's own
self-signed certificate (the same as MQTT on 8883). After an 80-byte login packet carrying the
LAN access code, the printer streams JPEG frames (about 1-2 a second), each preceded by a 16-byte
header whose first 4 bytes are the frame's length (little-endian). This takes the first complete
frame and hangs up, so a snapshot costs one frame (roughly 50-200 KB), not a video stream. If a
camera's first frame ever looks washed out, `settle_s` keeps reading for that long and returns the
newest frame instead. (A flat tan blur is usually the protective film still on the lens.)

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
from concurrent.futures import ThreadPoolExecutor
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


SETTLE_S = 0.0  # seconds to let the camera adjust before keeping a frame (0: the first frame is fine)


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
    printing: bool = True


class SnapshotWorker:
    """Keeps each printer's photo about as fresh as it's useful to be, on background threads so a
    slow camera never holds up status updates. Each photo replaces the printer's previous one.

    - printing: every `printing_s` (default 60 s)
    - open (idle or finished): every `idle_s` (default 10 min), plus one right after a print ends,
      so people can see whether the last print is still on the plate
    - someone has its details open on the dashboard: every `watch_s` (default 4 s), for at most
      `watch_max` printers at once (default 6), so anyone with the board open can't put the whole
      fleet on fast refresh
    """

    def __init__(self, db, targets: Callable[[], list[CameraTarget]], *, printing_s: float = 60.0,
                 idle_s: float = 600.0, watch_s: float = 4.0, watch_max: int = 6, workers: int | None = None,
                 watched: Callable[[], set[str]] | None = None,
                 grab: Callable[[str, str], bytes] = grab_frame,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 clock: Callable[[], float] = time.monotonic):
        self.db = db
        self.targets = targets          # printers worth photographing right now (from the relay)
        self.printing_s, self.idle_s, self.watch_s, self.watch_max = printing_s, idle_s, watch_s, watch_max
        self.watched = watched or (lambda: set())   # printer ids someone is looking at
        self.grab = grab                # swapped for a fake in tests
        self.wall_clock = wall_clock
        self.clock = clock
        # A photo takes about 2 s, so keeping `watch_max` printers on a `watch_s` cycle needs about
        # watch_max * 2 / watch_s grabs at once, plus room for the regular schedule.
        self.workers = workers or max(4, round(watch_max * 2 / watch_s) + 2)
        self._last_shot: dict[str, float] = {}      # printer id -> when its latest capture started
        self._was_printing: dict[str, bool] = {}
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()
        self._watching: set[str] = set()
        self._watch_checked: float | None = None
        self._errors: dict[str, str] = {}           # printer id (or "setup"/"watch") -> last error logged

    @property
    def watching(self) -> set[str]:
        """Printers someone has open right now (as of the last check, every 2 s)."""
        return set(self._watching)

    def start(self, stop: threading.Event) -> None:
        threading.Thread(target=self.run, args=(stop,), name="camera", daemon=True).start()

    def run(self, stop: threading.Event) -> None:
        log.info("camera photos on: printing every %.0f s, open every %.0f s, every %.0f s while someone watches",
                 self.printing_s, self.idle_s, self.watch_s)
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="camera") as pool:
            while not stop.is_set():
                for t in self.due():
                    pool.submit(self._capture_and_release, t)
                stop.wait(0.5)

    def due(self) -> list[CameraTarget]:
        """Targets whose next photo is due now. Marks them in flight."""
        now = self.clock()
        watching = self._refresh_watching(now)
        out = []
        for t in self.targets():
            was = self._was_printing.get(t.printer_id)
            self._was_printing[t.printer_id] = t.printing
            just_finished = was is True and not t.printing
            interval = self.watch_s if t.printer_id in watching else (self.printing_s if t.printing else self.idle_s)
            last = self._last_shot.get(t.printer_id)
            with self._lock:
                if t.printer_id in self._in_flight:
                    continue
                if last is None or just_finished or now - last >= interval:
                    self._in_flight.add(t.printer_id)
                    self._last_shot[t.printer_id] = now
                    out.append(t)
        return out

    def _refresh_watching(self, now: float) -> set[str]:
        if self._watch_checked is not None and now - self._watch_checked < 0.5:
            return self._watching
        self._watch_checked = now
        try:
            ids = self.watched()
        except Exception as e:
            self._log_once("watch", "%s", str(e))
            return self._watching
        self._errors.pop("watch", None)
        self._watching = set(sorted(ids)[:self.watch_max])
        return self._watching

    def _capture_and_release(self, t: CameraTarget) -> None:
        try:
            self.capture(t)
        finally:
            with self._lock:
                self._in_flight.discard(t.printer_id)

    def capture_all(self) -> int:
        """Photograph every target once, now (used by tests and for a manual pass)."""
        return sum(self.capture(t) for t in self.targets())

    def capture(self, t: CameraTarget) -> bool:
        try:
            jpeg = self.grab(t.host, t.access_code)
            self.db.upload_snapshot(t.printer_id, jpeg, self.wall_clock().isoformat(timespec="seconds"))
        except Exception as e:  # a camera or upload problem must never stop the other printers
            message = str(e)
            if "sql/07" in message:  # a setup problem, the same for every printer: say it once
                self._log_once("setup", "%s", message)
            else:
                self._log_once(t.printer_id, "[%s] camera snapshot failed: %s", t.label, message)
            return False
        if self._errors.pop("setup", None) is not None:
            log.info("camera photos are uploading now")
        if self._errors.pop(t.printer_id, None) is not None:
            log.info("[%s] camera snapshots working again", t.label)
        return True

    def _log_once(self, key: str, fmt: str, *args) -> None:
        message = fmt % args
        if self._errors.get(key) != message:
            log.warning("%s", message)
            self._errors[key] = message


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
