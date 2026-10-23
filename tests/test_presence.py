"""Who's watching which printer, from Realtime Presence messages."""

import json
import threading

from relay.presence import PresenceState, PresenceWatcher


def meta(ref, printer):
    return {"phx_ref": ref, "printer_id": printer}


def test_state_follows_full_copies_joins_and_leaves():
    s = PresenceState()
    s.replace({"tab1": {"metas": [meta("a", "P1")]}, "tab2": {"metas": [meta("b", "P2")]}})
    assert s.watched() == {"P1", "P2"}
    s.apply_diff({"joins": {"tab3": {"metas": [meta("c", "P1")]}}, "leaves": {"tab1": {"metas": [meta("a", "P1")]}}})
    assert s.watched() == {"P1", "P2"}          # P1 is still open in tab3
    s.apply_diff({"joins": {}, "leaves": {"tab3": {"metas": [meta("c", "P1")]}}})
    assert s.watched() == {"P2"}


class FakeSocket:
    """Plays a list of server messages, then raises TimeoutError until told to stop."""

    def __init__(self, messages, stop):
        self.messages, self.stop, self.sent = list(messages), stop, []

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def send(self, raw): self.sent.append(json.loads(raw))

    def recv(self, timeout=None):
        if self.messages:
            return json.dumps(self.messages.pop(0))
        self.stop.set()
        raise TimeoutError


def test_session_joins_the_channel_and_tracks_watchers():
    stop = threading.Event()
    topic = "realtime:camera-watch"
    sock = FakeSocket([
        {"topic": topic, "event": "phx_reply", "payload": {"status": "ok", "response": {}}, "ref": "1"},
        {"topic": topic, "event": "presence_state", "payload": {"t1": {"metas": [meta("a", "P9")]}}},
        {"topic": topic, "event": "presence_diff", "payload": {"joins": {"t2": {"metas": [meta("b", "P4")]}}, "leaves": {}}},
    ], stop)
    w = PresenceWatcher("https://abc.supabase.co", "sb_secret_x", connector=lambda url, **kw: sock)
    w.session(stop)
    assert sock.sent[0]["event"] == "phx_join" and sock.sent[0]["topic"] == topic
    assert w.url.startswith("wss://abc.supabase.co/realtime/v1/websocket?apikey=")
    assert w.watching() == {"P9", "P4"}


def test_nobody_counts_as_watching_while_disconnected():
    w = PresenceWatcher("https://abc.supabase.co", "k")
    w.state.replace({"t1": {"metas": [meta("a", "P1")]}})
    assert w.watching() == set()               # not connected


def test_photos_switch_from_any_tab_and_assumed_on_while_disconnected():
    w = PresenceWatcher("https://abc.supabase.co", "k")
    assert w.photos_wanted() is True                 # can't tell yet: keep the regular schedule
    w.connected = True
    w.state.replace({"t1": {"metas": [meta("a", "P1")]}})
    assert w.photos_wanted() is False                # someone watching one printer isn't Photos on
    w.state.apply_diff({"joins": {"t2": {"metas": [{"phx_ref": "b", "photos": True}]}}, "leaves": {}})
    assert w.photos_wanted() is True
