-- Access rules for the printers / printer_status tables. Run once in the Supabase SQL editor.
-- Safe to re-run. The relay uses the secret key, which bypasses RLS. The dashboard uses the
-- publishable (anon) key and can only read.

alter table public.printers enable row level security;
alter table public.printer_status enable row level security;

drop policy if exists "public read" on public.printers;
drop policy if exists "public read" on public.printer_status;
create policy "public read" on public.printers for select using (true);
create policy "public read" on public.printer_status for select using (true);

-- Lets the React app get live updates when the relay writes new status rows.
do $$
begin
  if not exists (select 1 from pg_publication_tables
                 where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'printer_status') then
    alter publication supabase_realtime add table public.printer_status;
  end if;
end $$;
