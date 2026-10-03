-- Remove columns nothing uses, and send live updates for the printers table too.
-- Run once in the Supabase SQL editor, after 02_admin_discovery_history.sql. Safe to re-run.

-- Bambu printers always use MQTT port 8883, so this was never a real setting.
alter table public.printer_connections drop column if exists port;

-- Not used by any analytics view or the dashboard.
alter table public.print_jobs drop column if exists total_layers;

-- The printers table already gives the count.
alter table public.relay_heartbeats drop column if exists printers_total;

-- The dashboard shows filament color and maintenance status from `printers`, so it needs
-- live updates for that table as well as printer_status.
do $$
begin
  if not exists (select 1 from pg_publication_tables
                 where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'printers') then
    alter publication supabase_realtime add table public.printers;
  end if;
end $$;
