"""Shared test data."""

from datetime import datetime, timezone

from relay.config import Printer
from relay.status import build_status

NOW = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)

PRINTER = Printer(
    id="11111111-1111-1111-1111-111111111111", serial="03900D5C2526895", label="FredPrintstone",
    model="A1", maintenance_status="operational", access_code="12345678", host="172.20.10.7",
)

PRINTING = {
    "print": {
        "gcode_state": "RUNNING",
        "mc_percent": 42,
        "mc_remaining_time": 75,
        "subtask_name": "bracket_v2",
        "layer_num": 30,
        "total_layer_num": 120,
        "nozzle_temper": 219.8,
        "nozzle_target_temper": 220,
        "bed_temper": 64.6,
        "bed_target_temper": 65,
        "print_error": 0,
        "hms": [],
        "wifi_signal": "-49dBm",
        "ams": {
            "tray_now": "1",
            "ams": [{"id": "0", "tray": [
                {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF"},
                {"id": "1", "tray_type": "PETG", "tray_color": "FF6A13FF"},
            ]}],
        },
        "vt_tray": {"tray_type": "", "tray_color": "00000000"},
    }
}


def state(gcode_state: str, **fields) -> dict:
    """PRINTING with a different gcode_state and/or fields."""
    return {"print": {**PRINTING["print"], "gcode_state": gcode_state, **fields}}


def record(gcode_state: str = "RUNNING", online: bool = True, at: datetime = NOW, **fields) -> dict:
    return build_status(PRINTER, state(gcode_state, **fields), online, at.timestamp(), now=at)
