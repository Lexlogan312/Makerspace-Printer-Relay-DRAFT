-- Live updates for the staff pages, the same way the board already gets them.
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- The staff pages used to re-download every 20-30 seconds. With these tables sending changes
-- through Supabase Realtime, they update the moment the relay writes, and only re-download every
-- 5 minutes as a safety net.
--
-- Realtime only delivers a change to someone allowed to read the row (the "admins read" /
-- "admins manage" policies from 02), so students never receive these, including the access codes
-- in printer_connections.

do $$
declare
  t text;
begin
  foreach t in array array['printer_connections', 'discovered_printers', 'relay_heartbeats', 'print_jobs'] loop
    if not exists (select 1 from pg_publication_tables
                   where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end $$;
