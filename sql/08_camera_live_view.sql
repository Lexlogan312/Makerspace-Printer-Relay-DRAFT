-- Faster camera photos while someone is looking at a printer.
-- Run once in the Supabase SQL editor, after 07. Safe to re-run.
--
-- When someone opens a printer's details, the dashboard calls watch_printer() about every 45 s.
-- While a printer has an unexpired row here, the relay refreshes its photo every few seconds
-- instead of every minute (at most 6 printers at once). Rows expire on their own after 90 s.

create table if not exists public.camera_watch (
  printer_id uuid primary key references public.printers (id) on delete cascade,
  until timestamptz not null
);

-- Row-level security on, and no policies: nobody can read or write the table directly.
-- The dashboard goes through watch_printer() below; the relay's secret key bypasses RLS to read it.
alter table public.camera_watch enable row level security;

-- SECURITY DEFINER so anyone (no login) can ask for a faster photo, but only this way: the expiry
-- is set by the database clock, so a caller can't keep a printer on fast refresh for hours.
create or replace function public.watch_printer(p_printer_id uuid)
returns void
language sql security definer set search_path = ''
as $$
  insert into public.camera_watch (printer_id, until)
  values (p_printer_id, now() + interval '90 seconds')
  on conflict (printer_id) do update set until = excluded.until;
$$;

revoke all on function public.watch_printer(uuid) from public;
grant execute on function public.watch_printer(uuid) to anon, authenticated;
