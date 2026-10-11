"""Printer lights ([camera] light = "auto"): on while printing or watched, never fighting a person."""

import dataclasses

from relay.app import LIGHT_END_HOLD_S, LIGHT_RETRY_S, Relay
from relay.config import RelayConfig
from tests.fixtures import PRINTER, state
from tests.test_app import Clock, FakeConnection, FakeDB, FakeDiscovery


class LightConnection(FakeConnection):
    def __init__(self, *args):
        super().__init__(*args)
        self.commands = []
        self.light = "off"
        self.obey = True

    def set_light(self, mode):
        self.commands.append(mode)
        if self.obey:
            self.light = mode
        return True


class Watching:
    def __init__(self): self.watching = set()


def make(tmp_path, light="auto", printer=PRINTER):
    FakeConnection.created = []
    db, disc, clock = FakeDB([printer]), FakeDiscovery(), Clock()
    config = RelayConfig(supabase_url="x", supabase_key="x", camera_light=light)
    relay = Relay(config, db, disc, connection_factory=LightConnection, clock=lambda: clock.mono,
                  wall_clock=lambda: clock.wall, light_state_path=tmp_path / "lights.json")
    relay.camera = Watching()
    disc.announce(printer.serial, "10.42.0.23")
    relay.tick()
    conn = FakeConnection.created[0]

    def report(gcode_state, advance=2):
        st = state(gcode_state)
        st["print"]["lights_report"] = [{"node": "chamber_light", "mode": conn.light}]
        conn.report(st, clock.wall.timestamp())
        clock.advance(advance)
        relay.tick()
    return relay, conn, clock, report


def test_on_while_printing_kept_on_for_the_end_photo_then_off(tmp_path):
    relay, conn, clock, report = make(tmp_path)
    report("IDLE")
    assert conn.commands == []
    report("RUNNING")
    assert conn.commands == ["on"]
    report("RUNNING", advance=LIGHT_RETRY_S + 1)
    assert conn.commands == ["on"]                 # no repeats while it's on
    report("FINISH")
    assert conn.commands == ["on"]                 # held for the photo of the finished print
    clock.advance(LIGHT_END_HOLD_S)
    report("FINISH")
    assert conn.commands == ["on", "off"]
    report("IDLE", advance=LIGHT_RETRY_S + 1)      # once the printer reports it off, it's no longer ours
    assert conn.commands == ["on", "off"]
    assert (tmp_path / "lights.json").read_text() == "[]"


def test_on_while_someone_watches(tmp_path):
    relay, conn, clock, report = make(tmp_path)
    report("IDLE")
    relay.camera.watching = {PRINTER.id}
    report("IDLE")
    assert conn.commands == ["on"]
    relay.camera.watching = set()
    report("IDLE", advance=LIGHT_RETRY_S + 1)
    report("IDLE")
    assert conn.commands == ["on", "off"]


def test_a_light_someone_else_turned_on_is_left_on(tmp_path):
    relay, conn, clock, report = make(tmp_path)
    conn.light = "on"                              # switched on at the printer
    report("RUNNING")
    report("FINISH", advance=LIGHT_END_HOLD_S + 1)
    report("IDLE")
    assert conn.commands == []


def test_someone_switching_our_light_off_is_respected_until_the_next_print(tmp_path):
    relay, conn, clock, report = make(tmp_path)
    report("RUNNING")
    assert conn.commands == ["on"]
    conn.light = "off"                             # someone turned it off at the printer
    report("RUNNING", advance=LIGHT_RETRY_S + 1)
    report("RUNNING")
    assert conn.commands == ["on"]                 # not switched back on
    report("FINISH")
    clock.advance(LIGHT_END_HOLD_S)
    report("IDLE")                                 # the print and its hold are over
    report("RUNNING")                              # a new print
    assert conn.commands == ["on", "on"]


def test_a_printer_that_ignores_the_command_is_not_spammed(tmp_path):
    relay, conn, clock, report = make(tmp_path)
    conn.obey = False
    for _ in range(10):
        report("RUNNING", advance=LIGHT_RETRY_S)
    assert conn.commands == ["on"]


def test_never_means_never(tmp_path):
    relay, conn, clock, report = make(tmp_path, light="never")
    report("RUNNING")
    assert conn.commands == []


def test_lights_turned_on_before_a_restart_are_still_turned_off(tmp_path):
    (tmp_path / "lights.json").write_text(f'["{PRINTER.id}"]')
    relay, conn, clock, report = make(tmp_path)
    conn.light = "on"
    report("IDLE")
    assert conn.commands == ["off"]
