"""TEST TOOL, not part of the relay: start a print on each printer, then cancel it.

Checks that every printer accepts a print, and that the relay records the job
(it should show up in print_jobs as "cancelled"). Delete this file once testing is done.
The relay itself stays read-only.

Each printer needs Developer Mode on (Settings > WLAN > LAN Only Mode > Developer Mode),
or it rejects the print. Run it on the Pi, which can reach the printer hotspot:

    uv run python -m tools.test_print_all cube.gcode.3mf                 # dry run: shows the plan
    uv run python -m tools.test_print_all cube.gcode.3mf --only "Peter Griffin" --go
    uv run python -m tools.test_print_all cube.gcode.3mf --go            # every printer

The file must be a sliced A1 plate ("Export plate sliced file" in Bambu Studio).
Ctrl+C at any point cancels every print this script started.
"""

import argparse
import ftplib
import json
import socket
import ssl
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

import paho.mqtt.client as mqtt

from relay.config import load_config
from relay.database import Database
from relay.history import ACTIVE_STATES

REMOTE_NAME = "relay-test.gcode.3mf"
START_TIMEOUT_S = 60  # how long to wait for a printer to accept the print


def tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # printers use a self-signed certificate
    return ctx


class ImplicitFTPS(ftplib.FTP_TLS):
    """The printers use FTPS with TLS from the first byte (port 990), and require the data
    connection to reuse the control connection's TLS session. ftplib does neither by default."""

    def __init__(self):
        super().__init__(context=tls_context())
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def upload(host: str, access_code: str, local: Path) -> None:
    ftp = ImplicitFTPS()
    ftp.connect(host, 990, timeout=30)
    ftp.login("bblp", access_code)
    ftp.prot_p()
    with local.open("rb") as f:
        ftp.storbinary(f"STOR {REMOTE_NAME}", f)
    try:
        ftp.quit()
    except (ftplib.all_errors, socket.error):
        pass


class PrinterSession:
    """An MQTT connection used to start and stop one print, watching the printer's reports."""

    def __init__(self, printer):
        self.printer = printer
        self.state = None
        self.reply = None  # the printer's answer to project_file, if it sends one
        self.connected = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"print-test-{uuid.uuid4().hex[:8]}")
        self.client.username_pw_set("bblp", printer.access_code)
        self.client.tls_set_context(tls_context())
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def open(self) -> None:
        self.client.connect(self.printer.host, 8883, keepalive=60)
        self.client.loop_start()
        if not self.connected.wait(15):
            raise RuntimeError("MQTT connection refused or timed out (check the access code)")

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            client.subscribe(f"device/{self.printer.serial}/report")
            self.connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            p = json.loads(msg.payload).get("print", {})
        except (json.JSONDecodeError, AttributeError):
            return
        if p.get("gcode_state"):
            self.state = p["gcode_state"]
        if p.get("command") == "project_file" and "result" in p:
            self.reply = p

    def _send(self, command: dict) -> None:
        self.client.publish(f"device/{self.printer.serial}/request", json.dumps({"print": command}), qos=1)

    def start(self, plate: int) -> None:
        self._send({
            "sequence_id": "0", "command": "project_file", "param": f"Metadata/plate_{plate}.gcode",
            "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
            "subtask_name": "relay test print", "file": REMOTE_NAME, "url": f"ftp:///{REMOTE_NAME}", "md5": "",
            # Quick start: skip the calibrations, this print gets cancelled anyway
            "timelapse": False, "bed_type": "auto", "bed_levelling": False, "flow_cali": False,
            "vibration_cali": False, "layer_inspect": False, "use_ams": False, "ams_mapping": "",
        })

    def stop(self) -> None:
        self._send({"sequence_id": "0", "command": "stop", "param": ""})

    def wait_until_started(self) -> str | None:
        """None once the printer is preparing/printing, otherwise why it didn't start."""
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.state in ACTIVE_STATES:
                return None
            if self.reply and str(self.reply.get("result", "")).lower() not in ("success", "ok"):
                return f"printer rejected it: {self.reply.get('reason') or self.reply}"
            time.sleep(1)
        return ("didn't start within a minute (is Developer Mode on? last state: "
                f"{self.state}, reply: {self.reply})")


def check_sliced_file(path: Path, plate: int) -> None:
    if not path.is_file():
        sys.exit(f"error: {path} not found")
    try:
        names = zipfile.ZipFile(path).namelist()
    except zipfile.BadZipFile:
        sys.exit(f"error: {path} isn't a .3mf file")
    if f"Metadata/plate_{plate}.gcode" not in names:
        sys.exit(f"error: {path} has no sliced plate {plate}. In Bambu Studio, slice it for the A1 and use "
                 "File > Export > Export plate sliced file")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path, help="sliced plate file (.gcode.3mf)")
    parser.add_argument("--go", action="store_true", help="actually start the prints (default: dry run)")
    parser.add_argument("--only", action="append", metavar="LABEL", help="only this printer (repeatable)")
    parser.add_argument("--minutes", type=float, default=2, help="cancel each print after this long (default 2)")
    parser.add_argument("--stagger", type=float, default=10, help="seconds between starts (default 10)")
    parser.add_argument("--plate", type=int, default=1)
    parser.add_argument("-c", "--config", default="relay.toml")
    args = parser.parse_args()

    check_sliced_file(args.file, args.plate)
    config = load_config(args.config)
    db = Database(config.supabase_url, config.supabase_key)
    printers = [p for p in db.load_printers() if not p.retired and p.access_code]
    if args.only:
        wanted = {name.lower() for name in args.only}
        printers = [p for p in printers if p.label.lower() in wanted]
        missing = wanted - {p.label.lower() for p in printers}
        if missing:
            sys.exit(f"error: no printer named {', '.join(sorted(missing))}")
    status = db.load_last_status([p.id for p in printers]) if printers else {}

    plan, skipped = [], []
    for p in sorted(printers, key=lambda p: p.label):
        s = status.get(p.id, {})
        if not s.get("is_online") or not p.host:
            skipped.append((p, "offline"))
        elif s.get("gcode_state") in ACTIVE_STATES:
            skipped.append((p, f"busy ({s.get('gcode_state')})"))
        else:
            plan.append(p)

    print(f"Will print {args.file.name} on {len(plan)} printer(s), {args.stagger:g} s apart, "
          f"cancelling each after {args.minutes:g} min:")
    for i, p in enumerate(plan):
        print(f"  +{i * args.stagger:>4.0f}s  {p.label:<26} {p.host}")
    for p, why in skipped:
        print(f"  skip    {p.label:<26} {why}")
    if not args.go:
        print("\nDry run. Add --go to start the prints.")
        return
    if not plan:
        return
    answer = input("\nCheck every one of these printers has a CLEAR plate and filament loaded.\n"
                   "Type 'plates clear' to start: ")
    if answer.strip().lower() != "plates clear":
        sys.exit("Not started.")

    started: list[tuple[PrinterSession, float]] = []
    try:
        for i, p in enumerate(plan):
            if i:
                time.sleep(args.stagger)
            session = PrinterSession(p)
            try:
                print(f"[{p.label}] uploading…", flush=True)
                upload(p.host, p.access_code, args.file)
                session.open()
                session.start(args.plate)
                started.append((session, time.monotonic()))  # tracked before waiting, so Ctrl+C cancels it
                problem = session.wait_until_started()
                print(f"[{p.label}] {'started' if not problem else 'FAILED: ' + problem}", flush=True)
            except Exception as e:
                print(f"[{p.label}] FAILED: {e}", flush=True)
                session.close()

        for session, started_at in started:
            remaining = started_at + args.minutes * 60 - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            session.stop()
            print(f"[{session.printer.label}] cancelled", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted: cancelling every print that was started…")
        for session, _ in started:
            session.stop()
    finally:
        time.sleep(2)  # let the stop commands go out
        for session, _ in started:
            session.close()

    print("\nDone. Within a minute, each printer's job should appear in print_jobs as 'cancelled'.")


if __name__ == "__main__":
    main()
