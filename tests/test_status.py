from relay.printer import deep_merge
from relay.status import FILAMENT_COLORS, build_status, color_name, fingerprint
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


def test_every_elegoo_color_gets_its_own_name():
    for name, hex_ in FILAMENT_COLORS.items():
        assert color_name(hex_) == name
        assert color_name(hex_.lower()) == name


def test_other_colors_get_the_closest_elegoo_color_in_their_family():
    cases = {
        # Colors reported by the makerspace printers
        "#FF6A13": "Orange", "#FF6910": "Orange",
        "#104831": "Sea Green",   # dark green: plain closest-color matching said black
        # Near-misses of each family
        "#0A2989": "Dark Blue", "#000080": "Dark Blue", "#C12E1F": "Red", "#F4EE2A": "Yellow",
        "#00AE42": "Neon Green", "#22FF22": "Neon Green", "#5E43B7": "Purple", "#F55A74": "Pink",
        "#8BD5EE": "Sky Blue", "#9D432C": "Brown", "#E8D3A9": "Beige", "#B8A07A": "Wood Color",
        "#8E9089": "Grey", "#F0F0F0": "White", "#1A1A1A": "Black",
    }
    assert {hex_: color_name(hex_) for hex_ in cases} == cases


def test_specialty_colors_need_an_exact_match():
    assert color_name("#FDFCF2") == "White"      # not Translucent
    assert color_name("#7E7E7F") == "Grey"       # not Space Grey
    assert color_name("#895838") == "Brown"      # not Copper Filled
    assert color_name(None) is None and color_name("nope") is None
