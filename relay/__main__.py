"""Makerspace printer relay: Bambu printers (local MQTT) -> Supabase.

    uv run python -m relay                      # run for real
    uv run python -m relay --dry-run            # read printers from Supabase, print status, write nothing
    uv run python -m relay --dump-raw raw/      # also save raw MQTT payloads to raw/<serial>.jsonl
"""

import argparse
import logging
import signal
import threading
from pathlib import Path

from .app import Relay
from .config import ConfigError, load_config
from .database import Database, DryRunDatabase
from .discovery import Discovery


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", default="relay.toml", help="path to config (default: relay.toml)")
    parser.add_argument("--dry-run", action="store_true", help="print status to the terminal instead of writing to Supabase")
    parser.add_argument("--dump-raw", type=Path, metavar="DIR", help="append raw MQTT payloads to DIR/<serial>.jsonl")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        parser.exit(2, f"error: {e}\n")

    db = (DryRunDatabase if args.dry_run else Database)(config.supabase_url, config.supabase_key)
    relay = Relay(config, db, Discovery(config.printer_network), dump_dir=args.dump_raw)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # systemd sends SIGTERM on the Pi
    logging.getLogger("relay").info("starting%s", " (dry run: nothing is written to Supabase)" if args.dry_run else "")
    relay.run(stop)


if __name__ == "__main__":
    main()
