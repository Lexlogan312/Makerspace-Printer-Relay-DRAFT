from relay.printer import deep_merge
from relay.status import build_status, color_name, fingerprint
from tests.fixtures import NOW, PRINTER, PRINTING

def test_printing_record():
    r = build_status(PRINTER, PRINTING, connected=True, last_message_at=NOW.timestamp(), now=NOW)
    assert r["printer_id"] == PRINTER.id
    assert r["status"] == "printing"
    assert r["progress_percent"] == 42
    assert r["remaining_minutes"] == 75
    assert r["estimated_completion"] == "2026-09-26T21:15:00+00:00"
    assert (r["filament_type"], r["filament_color"]) == ("PETG", "#FF6A13")
    assert r["nozzle_temp"] == 220 and r["bed_temp"] == 65


def test_external_spool_without_ams():
    state = {"print": {"gcode_state": "IDLE", "vt_tray": {"tray_type": "PLA", "tray_color": "0A2989FF"}}}
    r = build_status(PRINTER, state, connected=True, last_message_at=None, now=NOW)
    assert r["status"] == "idle"
    assert r["filament_color"] == "#0A2989"
    assert r["progress_percent"] is None and r["estimated_completion"] is None


def test_offline_overrides_last_state():
    r = build_status(PRINTER, PRINTING, connected=False, last_message_at=None, now=NOW)
    assert r["status"] == "offline" and r["online"] is False


def test_deep_merge_applies_partial_reports():
    state = {}
    deep_merge(state, PRINTING)
    deep_merge(state, {"print": {"mc_percent": 43, "nozzle_temper": 221.0}})
    assert state["print"]["mc_percent"] == 43
    assert state["print"]["subtask_name"] == "bracket_v2"


def test_fingerprint_ignores_timestamps():
    a = build_status(PRINTER, PRINTING, True, NOW.timestamp(), now=NOW)
    b = build_status(PRINTER, PRINTING, True, NOW.timestamp() + 5, now=NOW.replace(second=5))
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_ignores_wifi_jitter():
    a = build_status(PRINTER, PRINTING, True, NOW.timestamp(), now=NOW)
    b = build_status(PRINTER, {"print": {**PRINTING["print"], "wifi_signal": "-55dBm"}}, True, NOW.timestamp(), now=NOW)
    assert fingerprint(a) == fingerprint(b)


def test_color_names_group_similar_colors():
    # Real Bambu filament colors (including two from the makerspace printers)
    assert color_name("#FFFFFF") == "white"
    assert color_name("#000000") == "black"
    assert color_name("#FF6A13") == "orange"   # Bambu PLA Basic Orange
    assert color_name("#0A2989") == "blue"     # Bambu PLA Basic Blue
    assert color_name("#C12E1F") == "red"
    assert color_name("#8E9089") == "gray"
    assert color_name(None) is None and color_name("nope") is None
