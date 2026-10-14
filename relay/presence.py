"""Who is watching which printer right now, from Supabase Realtime Presence.

When someone opens a printer's details on the dashboard, their browser joins the Realtime channel
"camera-watch" and tracks {"printer_id": ...}. Supabase removes them by itself when they close it,
close the tab or lose connection. The relay listens on the same channel, so it knows instantly
which printers are being watched, without asking the database every few seconds (each request
is a Supabase log entry, and log volume is limited).

This is a small client for the Realtime websocket protocol (Phoenix channels, JSON v1), so the
reconnect behaviour is ours: if the connection drops, nobody counts as watching (photos fall back
to their normal schedule) and it reconnects forever, backing off up to a minute.
"""

import json
import logging
import threading
import time
from urllib.parse import urlencode

from websockets.sync.client import connect

log = logging.getLogger("relay.presence")

CHANNEL = "camera-watch"         # the dashboard joins the same channel (src/lib/snapshots.ts)
HEARTBEAT_S = 25                 # Realtime drops connections that are silent for about 60 s


class PresenceState:
    """The channel's presence: key -> list of metas (one per browser tab), kept up to date
    from "presence_state" (a full copy) and "presence_diff" (joins and leaves) messages."""

    def __init__(self):
        self.entries: dict[str, list[dict]] = {}

    def replace(self, state: dict) -> None:
        self.entries = {key: list(value.get("metas", [])) for key, value in state.items()}

    def apply_diff(self, diff: dict) -> None:
        for key, value in (diff.get("leaves") or {}).items():
            gone = {m.get("phx_ref") for m in value.get("metas", [])}
            left = [m for m in self.entries.get(key, []) if m.get("phx_ref") not in gone]
            if left:
                self.entries[key] = left
            else:
                self.entries.pop(key, None)
        for key, value in (diff.get("joins") or {}).items():
            self.entries.setdefault(key, []).extend(value.get("metas", []))

    def watched(self) -> set[str]:
        return {str(m["printer_id"]) for metas in self.entries.values() for m in metas if m.get("printer_id")}


class PresenceWatcher:
    def __init__(self, supabase_url: str, key: str, channel: str = CHANNEL, connector=connect):
        base = supabase_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        self.url = f"{base}/realtime/v1/websocket?" + urlencode({"apikey": key, "vsn": "1.0.0"})
        self.topic = f"realtime:{channel}"
        self.connector = connector       # swapped for a fake in tests
        self.state = PresenceState()
        self.connected = False
        self._lock = threading.Lock()
        self._ref = 0
        self._failures = 0

    def watching(self) -> set[str]:
        """Printer ids someone has open right now. Empty while disconnected."""
        with self._lock:
            return self.state.watched() if self.connected else set()

    def start(self, stop: threading.Event) -> None:
        threading.Thread(target=self.run, args=(stop,), name="presence", daemon=True).start()

    def run(self, stop: threading.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                self.session(stop)
                backoff = 1.0  # it was up and closed cleanly: reconnect promptly
            except Exception as e:
                if self.connected:
                    backoff = 1.0  # it had been working: the first retry is quick
                self._failures += 1
                if self._failures in (1, 10) or self._failures % 100 == 0:
                    log.warning("camera watching: Realtime connection problem (%s); retrying", e)
            finally:
                with self._lock:
                    self.connected = False
                    self.state = PresenceState()
            stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def session(self, stop: threading.Event) -> None:
        """One connection: join the channel, follow presence, send heartbeats, until it ends."""
        with self.connector(self.url, open_timeout=15, close_timeout=2) as ws:
            ws.send(self._message(self.topic, "phx_join", {
                "config": {"presence": {"key": ""}, "broadcast": {"self": False}, "postgres_changes": []},
            }))
            last_heartbeat = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - last_heartbeat >= HEARTBEAT_S:
                    ws.send(self._message("phoenix", "heartbeat", {}))
                    last_heartbeat = time.monotonic()
                try:
                    raw = ws.recv(timeout=1.0)
                except TimeoutError:
                    continue
                self.handle(json.loads(raw))

    def handle(self, msg: dict) -> None:
        event, payload = msg.get("event"), msg.get("payload") or {}
        if msg.get("topic") != self.topic:
            return
        if event == "phx_reply" and payload.get("status") == "ok" and not self.connected:
            with self._lock:
                self.connected = True
            if self._failures:
                log.info("camera watching: Realtime connected again")
            else:
                log.info("camera watching: following who has a printer open (Realtime)")
            self._failures = 0
        elif event == "phx_reply" and payload.get("status") == "error":
            raise RuntimeError(f"couldn't join {self.topic}: {payload.get('response')}")
        elif event == "presence_state":
            with self._lock:
                self.state.replace(payload)
        elif event == "presence_diff":
            with self._lock:
                self.state.apply_diff(payload)
        elif event in ("phx_error", "phx_close"):
            raise RuntimeError(f"channel {event}")

    def _message(self, topic: str, event: str, payload: dict) -> str:
        self._ref += 1
        ref = str(self._ref)
        return json.dumps({"topic": topic, "event": event, "payload": payload, "ref": ref, "join_ref": ref})
