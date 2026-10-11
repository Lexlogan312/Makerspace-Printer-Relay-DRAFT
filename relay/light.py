"""Test whether a printer's light can be switched over the local network.

A one-off check before building it into the relay: the relay itself still only reads from
printers. This connects to the printer's MQTT broker (as the relay does), reads the light's
current state, switches it, waits for the printer to confirm, then puts it back as it was.

    uv run python -m relay.light --printer "Fred Printstone"           # on for 5 s, then back
    uv run python -m relay.light --printer "Fred Printstone" --mode on  # just turn it on
    uv run python -m relay.light --printer "Fred Printstone" --mode off
    uv run python -m relay.light --host 10.42.0.139 --serial 03900D5C2526895 --code 12345678

The command is Bambu's "ledctrl" with the light the A1 reports as "chamber_light". Printers
ignore control commands while signed in to Bambu Cloud; on the offline hotspot they shouldn't be.
"""

import argparse
import json
import ssl
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt

from .printer import MQTT_PORT, PUSHALL

LIGHT = "chamber_light"


def ledctrl(mode: str) -> dict:
    return {"system": {"sequence_id": "0", "command": "ledctrl", "led_node": LIGHT, "led_mode": mode,
                       "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}}


def light_mode(report: dict) -> str | None:
    """The light's mode from a report, if the report mentions it."""
    for light in (report.get("print") or {}).get("lights_report") or []:
        if light.get("node") == LIGHT:
            return light.get("mode")
    return None


class LightTest:
    def __init__(self, host: str, serial: str, access_code: str):
        self.host = host
        self.report_topic = f"device/{serial}/report"
        self.request_topic = f"device/{serial}/request"
        self.mode: str | None = None
        self.replies: list[dict] = []           # what the printer said about our commands
        self.changed = threading.Condition()
        self.connected = threading.Event()
        self.error: str | None = None

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"makerspace-lighttest-{uuid.uuid4().hex[:8]}", protocol=mqtt.MQTTv311)
        self.client.username_pw_set("bblp", access_code)
        tls = ssl.create_default_context()
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE         # the printer's certificate is self-signed
        self.client.tls_set_context(tls)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.error = f"the printer refused the connection ({reason_code}). Check the access code."
        else:
            client.subscribe(self.report_topic)
            client.publish(self.request_topic, json.dumps(PUSHALL))
        self.connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            report = json.loads(msg.payload)
        except ValueError:
            return
        system = report.get("system") or {}
        if system.get("command") == "ledctrl":
            self.replies.append(system)
        mode = light_mode(report)
        if mode is not None:
            with self.changed:
                self.mode = mode
                self.changed.notify_all()

    def wait_for(self, predicate, timeout: float) -> bool:
        with self.changed:
            return self.changed.wait_for(predicate, timeout)

    def set(self, mode: str, timeout: float = 8.0) -> bool:
        self.client.publish(self.request_topic, json.dumps(ledctrl(mode)))
        # Ask for a full report too, in case the printer only reports the light occasionally.
        time.sleep(0.5)
        self.client.publish(self.request_topic, json.dumps(PUSHALL))
        return self.wait_for(lambda: self.mode == mode, timeout)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test switching a printer's light over the network.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--printer", metavar="NAME", help="look up the printer in Supabase (needs relay.toml)")
    target.add_argument("--host", metavar="IP", help="the printer's IP (with --serial and --code)")
    parser.add_argument("--serial", help="the printer's serial number")
    parser.add_argument("--code", help="the printer's LAN access code")
    parser.add_argument("-c", "--config", default="relay.toml", help="relay config, for --printer (default: relay.toml)")
    parser.add_argument("--mode", choices=["test", "on", "off"], default="test",
                        help="test: on for --hold seconds, then back as it was (default)")
    parser.add_argument("--hold", type=float, default=5.0, help="seconds to leave it on in test mode (default: 5)")
    args = parser.parse_args()

    if args.printer:
        from .config import load_config
        from .database import Database
        config = load_config(args.config)
        db = Database(config.supabase_url, config.supabase_key)
        try:
            matches = [p for p in db.load_printers() if p.label.casefold() == args.printer.casefold()]
        finally:
            db.close()
        if not matches or not matches[0].host or not matches[0].access_code:
            sys.exit(f"error: no printer named {args.printer!r} with an IP and access code")
        p = matches[0]
        label, host, serial, code = p.label, p.host, p.serial, p.access_code
    else:
        if not (args.serial and args.code):
            parser.error("--host needs --serial and --code")
        label, host, serial, code = args.host, args.host, args.serial, args.code

    test = LightTest(host, serial, code)
    print(f"Connecting to {label} at {host}:{MQTT_PORT}…")
    test.client.connect_async(host, MQTT_PORT, keepalive=30)
    test.client.loop_start()
    try:
        if not test.connected.wait(15):
            sys.exit("error: no answer from the printer. Is it on and on this network?")
        if test.error:
            sys.exit(f"error: {test.error}")
        if not test.wait_for(lambda: test.mode is not None, 10):
            sys.exit(f"The printer never reported a light called {LIGHT!r}, so this test can't check it.")
        before = test.mode
        print(f"The light is {before}.")

        steps = [("on", args.hold), (before, 0)] if args.mode == "test" else [(args.mode, 0)]
        for mode, hold in steps:
            print(f"Turning it {mode}…")
            if test.set(mode):
                print(f"  The printer confirmed: light {mode}.")
            else:
                note = f" The printer replied: {test.replies[-1]}" if test.replies else ""
                print(f"  No confirmation after 8 s (it still reports {test.mode}).{note}")
                print("  Look at the printer to see whether the light changed. If it didn't, the printer is "
                      "ignoring control commands (for example while signed in to Bambu Cloud).")
                break
            if hold:
                print(f"  Leaving it on for {hold:g} s. Check the printer (or take a camera photo).")
                time.sleep(hold)
    finally:
        test.client.disconnect()
        test.client.loop_stop()


if __name__ == "__main__":
    main()
