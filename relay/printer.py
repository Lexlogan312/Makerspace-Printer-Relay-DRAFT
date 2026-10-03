"""One MQTT connection per printer, keeping a merged copy of its latest report."""

import copy
import json
import logging
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import paho.mqtt.client as mqtt

from .config import Printer

log = logging.getLogger(__name__)

MQTT_PORT = 8883  # Bambu printers' local MQTT broker (TLS)

# Asks for a full status report. This doesn't control the printer.
PUSHALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}


def deep_merge(base: dict, update: dict) -> dict:
    """Merge `update` into `base` in place.

    A1/P1 printers only send the fields that changed, so each report gets
    layered on top of what we already have. Lists (like AMS trays) get replaced.
    """
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


@dataclass
class Snapshot:
    """A consistent copy of a connection's state, taken by the main loop."""
    state: dict = field(default_factory=dict)
    connected: bool = False
    last_message_at: float | None = None   # wall clock (time.time()) of the latest report
    first_unread_at: float | None = None   # wall clock of the first report since the previous snapshot
    error: str | None = None               # why the connection isn't up, in plain language


class PrinterConnection:
    def __init__(self, printer: Printer, host: str, request_full_status: bool = True,
                 dump_dir: Path | None = None):
        self.printer = printer      # the main loop swaps this when the label etc. changes
        self.host = host
        self.request_full_status_on_connect = request_full_status
        self.report_topic = f"device/{printer.serial}/report"
        self.request_topic = f"device/{printer.serial}/request"

        self._lock = threading.Lock()
        self._state: dict = {}
        self._connected = False
        self._stopping = False
        self._last_message_at: float | None = None
        self._first_unread_at: float | None = None
        self._error: str | None = None

        self._dump_file = None
        if dump_dir is not None:
            dump_dir.mkdir(parents=True, exist_ok=True)
            self._dump_file = (dump_dir / f"{printer.serial}.jsonl").open("a", encoding="utf-8")

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"makerspace-relay-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv311,
        )
        self._client.username_pw_set("bblp", printer.access_code)
        # Printers use a self-signed cert, which is the same as turning off "SSL Secure" in MQTTX.
        tls = ssl.create_default_context()
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
        self._client.tls_set_context(tls)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def label(self) -> str:
        return self.printer.label

    @property
    def is_connected(self) -> bool:
        return self._connected

    # lifecycle

    def start(self) -> None:
        log.info("[%s] connecting to %s", self.label, self.host)
        self._client.connect_async(self.host, MQTT_PORT, keepalive=60)
        self._client.loop_start()  # background thread, reconnects automatically

    def stop(self) -> None:
        self._stopping = True
        self._client.disconnect()
        self._client.loop_stop()
        if self._dump_file:
            self._dump_file.close()

    def request_full_status(self) -> None:
        if self._connected:
            self._client.publish(self.request_topic, json.dumps(PUSHALL))

    def snapshot(self) -> Snapshot:
        with self._lock:
            snap = Snapshot(
                state=copy.deepcopy(self._state),
                connected=self._connected,
                last_message_at=self._last_message_at,
                first_unread_at=self._first_unread_at,
                error=self._error,
            )
            self._first_unread_at = None
        return snap

    # paho callbacks (these run on the MQTT thread)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            if "authori" in str(reason_code).lower():
                error = "Access code rejected. Check the access code shown on the printer's screen."
            else:
                error = f"Printer refused the connection: {reason_code}"
            with self._lock:
                self._error = error
            log.error("[%s] %s", self.label, error)
            return
        log.info("[%s] connected, subscribing to %s", self.label, self.report_topic)
        with self._lock:
            self._connected = True
            self._error = None
        client.subscribe(self.report_topic)
        if self.request_full_status_on_connect:
            self.request_full_status()

    def _on_connect_fail(self, client, userdata):
        with self._lock:
            self._error = f"Can't reach the printer at {self.host}."

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            was_connected = self._connected
            self._connected = False
        if was_connected and not self._stopping:
            log.warning("[%s] disconnected: %s (will retry)", self.label, reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("[%s] ignoring non-JSON message on %s", self.label, msg.topic)
            return
        if not isinstance(payload, dict):
            return

        now = time.time()
        with self._lock:
            deep_merge(self._state, payload)
            self._last_message_at = now
            if self._first_unread_at is None:
                self._first_unread_at = now
        if self._dump_file:
            self._dump_file.write(json.dumps({"t": now, "payload": payload}) + "\n")
            self._dump_file.flush()
        log.debug("[%s] report: %s", self.label, list(payload.get("print", payload).keys()))
