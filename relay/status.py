"""Turn a printer's raw merged MQTT state into the flat record the dashboard uses."""

from datetime import datetime, timedelta, timezone

from .config import Printer

# Printer gcode_state -> dashboard status. What counts as "available" is up to the
# frontend. For example, "finished" can still mean a print is sitting on the plate.
GCODE_STATE_TO_STATUS = {
    "IDLE": "idle",
    "PREPARE": "printing",
    "SLICING": "printing",
    "RUNNING": "printing",
    "PAUSE": "paused",
    "FINISH": "finished",
    "FAILED": "failed",
}

EXTERNAL_SPOOL = 254

# These change on every push (wifi_signal jitters a few dBm on every report). Leave them out
# when checking whether a record actually changed. The heartbeat still sends them.
VOLATILE_FIELDS = {"last_seen", "estimated_completion", "wifi_signal"}


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round(value) -> int | None:
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _hex_color(tray_color) -> str | None:
    """'FF6A13FF' (RRGGBBAA) -> '#FF6A13'. Empty or fully transparent means nothing's loaded."""
    if not isinstance(tray_color, str) or len(tray_color) < 6:
        return None
    if len(tray_color) == 8 and tray_color[6:].upper() == "00":
        return None
    return "#" + tray_color[:6].upper()


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat(timespec="seconds") if ts else None


def _ams_trays(p: dict) -> list[tuple[int, dict]]:
    """Every AMS tray as (global_slot_index, tray). Global index = unit * 4 + tray."""
    trays = []
    for unit in (p.get("ams") or {}).get("ams") or []:
        unit_id = _int(unit.get("id")) or 0
        for tray in unit.get("tray") or []:
            tray_id = _int(tray.get("id"))
            if tray_id is not None:
                trays.append((unit_id * 4 + tray_id, tray))
    return trays


def active_filament(p: dict) -> dict | None:
    """The tray feeding the nozzle right now: an AMS slot or the external spool."""
    tray_now = _int((p.get("ams") or {}).get("tray_now"))
    trays = dict(_ams_trays(p))
    if tray_now is not None and tray_now < EXTERNAL_SPOOL:
        return trays.get(tray_now)
    if tray_now == EXTERNAL_SPOOL or not trays:
        return p.get("vt_tray")
    return None  # tray_now == 255: AMS present, but nothing loaded


def build_status(printer: Printer, state: dict, connected: bool,
                 last_message_at: float | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    p = state.get("print") or {}

    gcode_state = p.get("gcode_state")
    if not connected:
        status = "offline"
    else:
        status = GCODE_STATE_TO_STATUS.get(gcode_state, "unknown")

    printing = status == "printing"
    percent = _int(p.get("mc_percent"))
    remaining = _int(p.get("mc_remaining_time"))  # minutes
    eta = now + timedelta(minutes=remaining) if printing and remaining is not None else None

    active = active_filament(p) or {}
    last_seen = datetime.fromtimestamp(last_message_at, timezone.utc) if last_message_at else None

    return {
        "printer_id": printer.id,
        "label": printer.label,
        "status": status,
        "gcode_state": gcode_state,
        "online": connected,
        "progress_percent": percent if printing or status == "paused" else None,
        "remaining_minutes": remaining if printing or status == "paused" else None,
        "estimated_completion": _iso(eta),
        "job_name": p.get("subtask_name") or None,
        "layer_current": _int(p.get("layer_num")),
        "layer_total": _int(p.get("total_layer_num")),
        "nozzle_temp": _round(p.get("nozzle_temper")),
        "nozzle_target": _round(p.get("nozzle_target_temper")),
        "bed_temp": _round(p.get("bed_temper")),
        "bed_target": _round(p.get("bed_target_temper")),
        "filament_type": active.get("tray_type") or None,
        "filament_color": _hex_color(active.get("tray_color")),
        "print_error": _int(p.get("print_error")) or 0,
        "wifi_signal": p.get("wifi_signal") or None,
        "last_seen": _iso(last_seen),
    }


def fingerprint(record: dict) -> tuple:
    """Hashable form of a record's meaningful fields, for detecting changes."""
    return tuple(
        (k, repr(v)) for k, v in sorted(record.items()) if k not in VOLATILE_FIELDS
    )


# Reference colors for grouping filament in analytics. Different presets give slightly
# different hex codes for the "same" color, so each code gets the name of the closest one.
NAMED_COLORS = {
    "white": (255, 255, 255),
    "silver": (192, 192, 192),
    "gray": (128, 128, 128),
    "black": (0, 0, 0),
    "red": (200, 30, 35),
    "orange": (255, 120, 20),
    "yellow": (250, 215, 30),
    "green": (40, 160, 70),
    "blue": (30, 80, 200),
    "light blue": (120, 190, 230),
    "purple": (120, 60, 160),
    "pink": (245, 140, 180),
    "brown": (120, 75, 40),
    "beige": (230, 210, 170),
}


def color_name(hex_color: str | None) -> str | None:
    """'#FF6A13' -> 'orange' (closest entry in NAMED_COLORS)."""
    if not hex_color or len(hex_color) != 7:
        return None
    try:
        rgb = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return None
    return min(NAMED_COLORS, key=lambda name: sum((a - b) ** 2 for a, b in zip(rgb, NAMED_COLORS[name], strict=True)))


def format_status_line(r: dict) -> str:
    """One readable terminal line per printer, used by --dry-run."""
    parts = [f"{r['label']:<16}", f"{r['status']:<9}"]
    if r["progress_percent"] is not None:
        parts.append(f"{r['progress_percent']:>3}%")
    if r["remaining_minutes"] is not None:
        h, m = divmod(r["remaining_minutes"], 60)
        parts.append(f"{h}h{m:02d}m left")
    if r["estimated_completion"]:
        eta = datetime.fromisoformat(r["estimated_completion"]).astimezone()
        parts.append(f"(ETA {eta:%H:%M})")
    if r["layer_total"]:
        parts.append(f"layer {r['layer_current']}/{r['layer_total']}")
    if r["filament_type"] or r["filament_color"]:
        parts.append(f"{r['filament_type'] or '?'} {r['filament_color'] or ''}".strip())
    if r["nozzle_temp"] is not None:
        parts.append(f"nozzle {r['nozzle_temp']}/{r['nozzle_target']}°C bed {r['bed_temp']}/{r['bed_target']}°C")
    if r["job_name"]:
        parts.append(f"job={r['job_name']!r}")
    if r["print_error"]:
        parts.append(f"ERROR {r['print_error']}")
    return "  ".join(parts)
