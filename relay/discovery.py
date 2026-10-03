"""Listen for the announcements Bambu printers broadcast, to learn each printer's current IP.

Every ~10 seconds each printer broadcasts an SSDP-style NOTIFY on UDP port 2021:

    NOTIFY * HTTP/1.1
    Location: 172.20.10.9                 <- its IP
    NT: urn:bambulab-com:device:3dprinter:1
    USN: 03900D5C2000916                  <- its serial
    DevModel.bambu.com: N2S               <- model code (N2S = A1)
    DevName.bambu.com: 3DP-039-916
    DevVersion.bambu.com: 01.08.00.00     <- firmware
"""

import ipaddress
import logging
import socket
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

DISCOVERY_PORT = 2021

# Bambu's internal model codes. Add codes here as new models show up in the logs.
MODEL_NAMES = {
    "N2S": "A1",
    "N1": "A1 mini",
}

# How recent an announcement must be to trust it. Printers announce every ~10 s.
RECENT_S = 60.0


@dataclass(frozen=True)
class Announcement:
    serial: str
    ip: str
    model_code: str | None
    dev_name: str | None
    firmware: str | None
    seen_at: float  # time.monotonic()

    @property
    def model(self) -> str | None:
        """Readable model name, or None if the code isn't in MODEL_NAMES yet."""
        return MODEL_NAMES.get(self.model_code or "")


def parse_announcement(data: bytes, sender_ip: str, seen_at: float) -> Announcement | None:
    """Parse one packet. Returns None for anything that isn't a valid Bambu printer announcement."""
    try:
        lines = data.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return None
    if not lines or not lines[0].startswith("NOTIFY"):
        return None

    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()

    serial = headers.get("usn")
    if "bambulab" not in headers.get("nt", "") or not serial:
        return None
    # The printer puts its own IP in Location. Use it only if it matches the packet's real
    # sender, so a packet can't point the relay at some other address.
    if headers.get("location") != sender_ip:
        log.debug("ignoring announcement for %s: Location %r != sender %s", serial, headers.get("location"), sender_ip)
        return None

    return Announcement(
        serial=serial,
        ip=sender_ip,
        model_code=headers.get("devmodel.bambu.com") or None,
        dev_name=headers.get("devname.bambu.com") or None,
        firmware=headers.get("devversion.bambu.com") or None,
        seen_at=seen_at,
    )


class Discovery:
    """Background listener that keeps the latest announcement from each printer."""

    def __init__(self, network: ipaddress.IPv4Network | None = None, port: int = DISCOVERY_PORT):
        self.network = network
        self.port = port
        self.available = False          # False if the port couldn't be opened
        self._lock = threading.Lock()
        self._latest: dict[str, Announcement] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):  # lets Bambu Studio listen at the same time on a Mac
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self.port))
            sock.settimeout(1.0)
        except OSError as e:
            log.error("printer discovery disabled: can't listen on UDP %d (%s)", self.port, e)
            return
        self._sock = sock
        self.available = True
        self._thread = threading.Thread(target=self._listen, name="discovery", daemon=True)
        self._thread.start()
        log.info("listening for printer announcements on UDP %d%s", self.port,
                 f" from {self.network}" if self.network else "")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()

    def get(self, serial: str) -> Announcement | None:
        """The printer's most recent announcement, if it's recent enough to trust."""
        with self._lock:
            ann = self._latest.get(serial)
        return ann if ann and time.monotonic() - ann.seen_at <= RECENT_S else None

    def recent(self) -> list[Announcement]:
        now = time.monotonic()
        with self._lock:
            return [a for a in self._latest.values() if now - a.seen_at <= RECENT_S]

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                data, (sender_ip, _) = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    log.exception("discovery socket error")
                return
            if self.network and ipaddress.IPv4Address(sender_ip) not in self.network:
                continue  # e.g. a printer elsewhere on the campus network
            ann = parse_announcement(data, sender_ip, time.monotonic())
            if ann:
                with self._lock:
                    self._latest[ann.serial] = ann
