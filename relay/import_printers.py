"""One-time import: copy printers from an old-style printers.toml into Supabase.

    uv run python -m relay.import_printers printers.toml

Creates or updates a `printers` row (serial, label, model) and a `printer_connections`
row (host, access_code) for each [[printers]] entry. It's safe to run more than once.
After importing, printers are managed from the admin dashboard and printers.toml can be deleted.
"""

import argparse
import sys
import tomllib
from pathlib import Path

from .config import ConfigError, load_config
from .database import Database, DatabaseError


def read_printers(path: Path) -> list[dict]:
    with path.open("rb") as f:
        entries = tomllib.load(f).get("printers", [])
    if not entries:
        raise ConfigError(f"No [[printers]] entries in {path}")
    seen = set()
    for i, entry in enumerate(entries):
        missing = [k for k in ("label", "serial", "access_code") if not entry.get(k)]
        if missing:
            raise ConfigError(f"printers[{i}] is missing: {', '.join(missing)}")
        if entry["serial"] in seen:
            raise ConfigError(f"Duplicate printer serial: {entry['serial']}")
        seen.add(entry["serial"])
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("printers_file", type=Path, help="old printers.toml with [[printers]] entries")
    parser.add_argument("-c", "--config", default="relay.toml", help="relay config with Supabase credentials")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        entries = read_printers(args.printers_file)
    except (ConfigError, OSError) as e:
        parser.exit(2, f"error: {e}\n")

    db = Database(config.supabase_url, config.supabase_key)
    try:
        rows = db.upsert_printers([
            {"serial": e["serial"], "label": e["label"], "model": e.get("model", "A1")} for e in entries
        ])
        ids = {r["serial"]: r["id"] for r in rows}
        db.upsert_connections([
            {"printer_id": ids[e["serial"]], "host": e.get("host") or None, "access_code": str(e["access_code"])}
            for e in entries
        ])
    except DatabaseError as e:
        sys.exit(f"error: {e}")
    finally:
        db.close()

    for e in entries:
        print(f"imported {e['label']} ({e['serial']})")
    print(f"\n{len(entries)} printer(s) imported. You can delete {args.printers_file} now.")


if __name__ == "__main__":
    main()
