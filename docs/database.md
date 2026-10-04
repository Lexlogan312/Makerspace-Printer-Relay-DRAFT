  # Database

All tables live in Supabase's `public` schema. The migrations in `sql/` create everything below.

**Who can access what:**
- **Public dashboard** (publishable key, no login): can read `printers` and `printer_status`.
- **Admins** (signed in, listed in `admins`): can manage printers, and read everything else.
- **Relay** (secret key on the Pi): can read and write everything. The secret key bypasses row-level security.

## Tables

| Table | Written by | Read by | What it holds |
|---|---|---|---|
| `printers` | admins, plus relay for `filament_color`, `filament_color_name`, `filament_type`, `model`, `firmware_version` | everyone | One row per printer: serial, label, model, `maintenance_status`, and the loaded filament (hex color, Elegoo color name, type) |
| `printer_connections` | admins (`access_code`), relay (`host`, `last_error`) | admins | How to connect. Separate from `printers` because access codes must not be public |
| `printer_status` | relay | everyone | Live state, one row per printer (upserted) |
| `discovered_printers` | relay | admins | Printers announcing themselves on the hotspot that aren't in `printers` yet |
| `print_jobs` | relay | admins | One row per print: start/end, outcome, layers, filament. Admin-only because job names can identify students |
| `printer_events` | relay | admins | Each online/offline change, `gcode_state` change, and new error |
| `relay_heartbeats` | relay | admins | One row per minute the relay is running and can reach Supabase |
| `admins` | you, in the SQL editor | nobody (`is_admin()` checks it) | Which signed-in users are admins |

### Adding a printer

1. On the printer's touchscreen, join it to the `MakerspacePrinters` hotspot and note its
   **access code** (LAN settings).
2. Within ~10 s it appears in `discovered_printers` with its serial, model and IP.
3. Until the admin dashboard exists, run this in the Supabase SQL Editor (one line per printer):

   ```sql
   with new_printers(serial, label, access_code) as (values
     ('03900D5C0000001', 'PrinterNameOne', '12345678'),
     ('03900D5C0000002', 'PrinterNameTwo', '87654321')
   ),
   added as (
     insert into printers (serial, label)
     select serial, label from new_printers
     returning id, serial
   )
   insert into printer_connections (printer_id, access_code)
   select added.id, new_printers.access_code
   from added join new_printers using (serial);
   ```

4. Within ~30 s the relay connects: `printer_connections.host` fills in and `last_error` stays empty.

The relay fills in `host`, `model` and `firmware_version`, and removes the printer from
`discovered_printers`. The admin dashboard should do the same two inserts.

### Removing a printer

Set `maintenance_status = 'offline_permanent'`. The relay disconnects and closes any open
print job, and the history is kept. Deleting the row instead removes its status and events.
Its print jobs are kept (with `printer_id` set to null) so fleet totals stay correct.

### `last_error` messages

| Message | Meaning |
|---|---|
| *(empty)* | Connected |
| Not seen on the network… | No announcement from this printer in the last minute: it's off or not on the hotspot |
| Access code rejected… | Wrong `access_code`, or the printer needs Developer Mode |
| Can't reach the printer at … | It announced itself but MQTT didn't answer, usually right after it boots |
| No IP address yet… | No announcement yet and no saved `host` |

## How print jobs are tracked

Bambu's LAN reports have no job id or start time, so jobs come from `gcode_state`:
- A job **starts** when the printer enters `PREPARE`, `SLICING`, `RUNNING` or `PAUSE`.
- It **ends** when the printer leaves those states: `FINISH` → `finished`, `FAILED` → `failed`,
  `FAILED` with error `0300-400C` → `cancelled` (the print was cancelled, not a printer problem),
  and anything else → `unknown`.
- **Times** are when the relay saw the change, accurate to a few seconds.
- **Gaps:**
  - If a printer drops off Wi-Fi briefly mid-print, the job continues, and ends when the printer
    is next seen in `FINISH`/`FAILED`.
  - If a printer stays offline for over an hour mid-print (e.g. power loss), the job is closed
    as `unknown` at the moment it went offline, so it doesn't count as printing forever.
  - If a print ends while the relay is down, the end time is the printer's last report before
    the relay stopped.
- **Restarts:** after a relay restart, an unfinished job continues in the same row.

`FINISH` doesn't end when someone takes the print off the plate (the A1 can't tell), so
there's no "time until pickup" stat.

## Analytics views (admin-only)

Every stat uses full 24-hour days in the `America/New_York` time zone (set in
`analytics_time_zone()`). A day with no activity has no row, so treat a missing day as 0.

| View | Columns | Use |
|---|---|---|
| `printer_utilization_daily` | day, printer_id, printer_label, printing_hours, utilization_percent | Hours printing ÷ 24, per printer |
| `fleet_utilization_daily` | day, printing_hours, printer_count, utilization_percent | Same for all printers (÷ 24 × printers not retired) |
| `busy_hours_daily` | day, weekday (1 = Mon), hour, busy_printers | Average printers printing in that hour. Average over weeks for the heatmap |
| `print_jobs_daily` | day, printer_id, printer_label, jobs_started, finished, failed, unknown, in_progress, avg_finished_minutes, cancelled | Job counts and success rate |
| `filament_usage_daily` | day, filament_type, color_name, jobs, printing_hours | Which filament gets used |
| `printer_reliability_daily` | day, printer_id, printer_label, offline_hours, errors (cancellations excluded) | Printers that may need maintenance |
| `relay_uptime_daily` | day, minutes_up, minutes_total, uptime_percent, max_latency_ms, p95_latency_ms, avg_printers_online | The >95% uptime and <10 s latency goals |
| `print_job_hours` | job_id, printer_id, printer_label, day, weekday, hour, hours | Building block for the views above |

For a longer period, add up the daily rows, for example
`sum(minutes_up) / sum(minutes_total)` for uptime over a semester, or
`sum(printing_hours) / (24 × days)` for a printer's utilization.

**Latency** is measured from the first printer report after the previous push to Supabase
confirming the write. It's slightly pessimistic, and it doesn't include the dashboard's own
Realtime delay.
