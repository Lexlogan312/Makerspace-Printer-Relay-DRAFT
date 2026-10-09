"""Relay settings (relay.toml) and the Printer record the rest of the code passes around."""

import ipaddress
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Printer:
    """One printer as stored in Supabase: a `printers` row plus its `printer_connections` row."""
    id: str
    serial: str
    label: str
    model: str
    maintenance_status: str
    access_code: str
    host: str | None = None             # last known IP. Discovery keeps it up to date
    firmware_version: str | None = None
    filament_color: str | None = None
    filament_color_name: str | None = None
    filament_type: str | None = None
    last_error: str | None = None

    @property
    def retired(self) -> bool:
        return self.maintenance_status == "offline_permanent"


@dataclass(frozen=True)
class RelayConfig:
    supabase_url: str
    supabase_key: str
    # Only accept printer announcements from this network, e.g. the Pi hotspot "10.42.0.0/24",
    # so printers elsewhere on campus are ignored. None accepts any network, which is handy on a Mac.
    printer_network: ipaddress.IPv4Network | None = None
    push_interval_s: float = 2.0            # how often the main loop runs and pushes changes
    status_heartbeat_s: float = 30.0        # re-send unchanged printer status this often
    printer_sync_interval_s: float = 30.0   # re-read the printer list from Supabase this often
    request_full_status: bool = True        # ask printers for a full report ("pushall")
    full_status_interval_s: float = 300.0
    camera_enabled: bool = False            # camera snapshots of printing printers
    camera_interval_s: float = 60.0         # one snapshot per printing printer this often


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> RelayConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path} (copy relay.example.toml to get started)")
    with path.open("rb") as f:
        data = tomllib.load(f)

    sb = data.get("supabase", {})
    url = os.environ.get("SUPABASE_URL") or sb.get("url")
    key = os.environ.get("SUPABASE_KEY") or sb.get("key")
    if not url or not key:
        raise ConfigError("Supabase url/key not set (config [supabase] or SUPABASE_URL / SUPABASE_KEY)")
    if key.startswith("sb_publishable_"):
        raise ConfigError("That's the publishable (read-only) key. The relay needs the secret key (sb_secret_...)")

    relay = data.get("relay", {})
    camera = data.get("camera", {})
    network = relay.get("printer_network") or None
    try:
        network = ipaddress.IPv4Network(network) if network else None
    except ValueError as e:
        raise ConfigError(f"printer_network: {e}") from None

    return RelayConfig(
        supabase_url=url,
        supabase_key=key,
        printer_network=network,
        push_interval_s=float(relay.get("push_interval_s", 2.0)),
        status_heartbeat_s=float(relay.get("status_heartbeat_s", 30.0)),
        printer_sync_interval_s=float(relay.get("printer_sync_interval_s", 30.0)),
        request_full_status=bool(relay.get("request_full_status", True)),
        full_status_interval_s=float(relay.get("full_status_interval_s", 300.0)),
        camera_enabled=bool(camera.get("enabled", False)),
        camera_interval_s=max(15.0, float(camera.get("interval_s", 60.0))),
    )
