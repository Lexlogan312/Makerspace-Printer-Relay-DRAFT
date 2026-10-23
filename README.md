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
   [sql/04_cancelled_prints.sql](sql/04_cancelled_prints.sql),
   [sql/05_printer_filament.sql](sql/05_printer_filament.sql),
   [sql/06_private_job_names_and_add_printer.sql](sql/06_private_job_names_and_add_printer.sql)
   (update the relay before running 06),
   [sql/07_camera_snapshots.sql](sql/07_camera_snapshots.sql),
   [sql/08_camera_live_view.sql](sql/08_camera_live_view.sql),
   [sql/09_remove_camera_watch.sql](sql/09_remove_camera_watch.sql) (08 is superseded; 09 removes what it added),
   [sql/10_staff_realtime.sql](sql/10_staff_realtime.sql) (live updates for the staff pages).
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

## Camera snapshots

The relay saves still photos from each printer's built-in camera (on by default; `[camera]` in
`relay.toml` turns it off or changes the timing):

- printing: every minute
- open: every 10 minutes, plus one right after each print ends, so people can see whether the plate is clear
- while someone has the printer's details open on the dashboard: every 4 seconds, for up to 6 printers at once
  (`watch_max`, up to 20; the relay grabs more photos in parallel as it goes up)

With `light = "auto"` under `[camera]` (off by default), the relay also turns a printer's light on
while it prints (and for 30 s after, for the photo of the result) or while someone watches it, and
off again afterwards. It only turns off lights it turned on, and leaves alone any light someone
switches themselves. That's the only thing the relay ever changes on a printer; it never starts,
stops or changes a print. `uv run python -m relay.light --all` checks that every printer accepts it.

It needs `sql/07_camera_snapshots.sql`; until that's run, the log says so once and everything else
carries on. Who's watching comes from Supabase Realtime Presence (channel `camera-watch`): the
dashboard joins it while a printer is open, or while the board's Photos switch is on, and the relay
listens, so nothing polls the database. Printing printers are photographed every minute only while
someone has Photos on; otherwise every `idle_interval_s`, like open ones. Each photo replaces the printer's last one
in the public Supabase Storage bucket `printer-snapshots`, and `printer_status.snapshot_at`
says when it was taken. That time goes out with the next status push rather than in a request of
its own (within a minute; right away while someone is watching). The dashboard stops live view
after 3 minutes, with a "Resume live view" button, so a forgotten tab doesn't keep downloading. The dashboard shows it to everyone, so students can check on their build.
Only the relay can upload; nobody can list the bucket.

A photo is one ~65 KB frame, so even the fast refresh is light on the hotspot (about 16 KB/s per
watched printer). A continuous video stream from 20 printers would not be. Test one printer before turning it on:

```bash
uv run python -m relay.camera --printer "Fred Printstone" --out fred.jpg   # uses relay.toml
uv run python -m relay.camera --host 10.42.0.112 --code 12345678         # or by IP + access code
```

This needs to run on the printer network: on the Pi, or on a laptop joined to `MakerspacePrinters`.
If it fails, the message says why (wrong access code, camera video turned off on the printer, …).
A flat tan or grey blur means the protective film is still on the camera lens.

To deploy on the Pi, see [docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md). The
service and firewall files are in `deploy/`.

## Supabase usage

Every request to Supabase is logged, and the Free plan includes 1 GB of logs a month, so the relay
keeps requests down: status is pushed when something a student would see changes (temperature and
layer number alone don't count; they ride along), unchanged printers are refreshed once a minute in
a single request, and camera viewers arrive over Realtime instead of being polled. That's roughly
10,000-15,000 requests a day for 20 printers.

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
| `relay/camera.py` | Camera snapshots: the printer's port-6000 frame protocol, the background worker, and a test command |
| `relay/light.py` | Test command for switching printer lights (the relay's own light control is in `app.py`) |
| `relay/import_printers.py` | One-time import from the old `printers.toml` |
| `sql/` | Database migrations, run in order in the Supabase SQL editor |
