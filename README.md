# Makerspace Printer Relay

Runs on a Raspberry Pi in the makerspace. It reads live status from the Bambu Lab printers
over their local MQTT connection (`mqtts://<printer-ip>:8883`, user `bblp`, password = LAN
access code, topic `device/<serial>/report`) and sends it to Supabase for the dashboard.
It also records history (print jobs, state changes, uptime) for the admin analytics.

```
printers ──MQTT──▶ relay (Pi) ──REST──▶ Supabase ◀── dashboard (Vite/React on Vercel)
    └── IP announcements (UDP 2021) ──▶ relay
```

- **Printers are managed in Supabase**, not in a config file. Admins add, rename, retire and
  update printers from the dashboard. The relay picks up changes within 30 seconds.
- **IPs are found automatically.** Printers announce themselves every ~10 s, and the relay
  connects to (and saves) each printer's current IP.
- **Read-only.** The relay never controls a printer. Its one message to a printer is
  `pushall`, which asks for a full status report, since A1s otherwise only send changed fields.

## First-time setup

1. **Database.** In the Supabase SQL editor, run these in order (each is safe to re-run):
   [sql/01_public_read.sql](sql/01_public_read.sql),
   [sql/02_admin_discovery_history.sql](sql/02_admin_discovery_history.sql),
   [sql/03_cleanup.sql](sql/03_cleanup.sql),
   [sql/04_cancelled_prints.sql](sql/04_cancelled_prints.sql).
2. **Admins.** Create each admin under *Authentication → Users*, then run
   `insert into public.admins (user_id) values ('<their user id>');`
3. **Config.** Copy the template and fill in the Supabase URL and **secret** key:
   ```bash
   cp relay.example.toml relay.toml
   ```
4. **Printers.** Import the existing printers once from the old `printers.toml`, then delete that file:
   ```bash
   uv run python -m relay.import_printers printers.toml
   ```
   After this, add printers from the dashboard. A new printer on the hotspot shows up in
   `discovered_printers` automatically.
5. **Run it**, on a computer on the same network as the printers:
   ```bash
   uv sync
   uv run python -m relay --dry-run     # read-only: prints each printer's status
   uv run python -m relay               # for real
   uv run python -m relay --dump-raw raw/   # also save raw MQTT messages (handy for tests)
   uv run pytest
   ```

To deploy on the Pi, see [docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md). The
service and firewall files are in `deploy/`.

## What's in the database

See [docs/database.md](docs/database.md) for every table and analytics view, and who writes what.

## Code layout

| File | What it does |
|---|---|
| `relay/__main__.py` | Command line: loads `relay.toml`, starts the relay, stops cleanly on Ctrl+C / SIGTERM |
| `relay/app.py` | The main loop. Every 2 s: sync printers, fix connections, push status, record history, heartbeat |
| `relay/config.py` | Reads `relay.toml`. Defines `Printer`, a printer as stored in Supabase |
| `relay/database.py` | Every Supabase read and write (REST API), plus the `--dry-run` version |
| `relay/discovery.py` | Listens for printers' UDP announcements to learn their IP, model and firmware |
| `relay/printer.py` | One MQTT connection per printer. Merges the partial reports into a full state |
| `relay/status.py` | Raw printer data → the status record (status, progress, ETA, filament, …) |
| `relay/history.py` | Status changes → `printer_events` rows and one `print_jobs` row per print |
| `relay/import_printers.py` | One-time import from the old `printers.toml` |
| `sql/` | Database migrations, run in order in the Supabase SQL editor |
