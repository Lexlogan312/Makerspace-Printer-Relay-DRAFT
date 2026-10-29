-- From the technician's requirements questionnaire:
--   * printers can be marked Reserved (for a class, say), with an optional note that students see;
--     the same note works for Out of service ("Nozzle replacement, back Friday")
--   * the board shows the file name of the print in progress, to everyone
-- Run once in the Supabase SQL editor, BEFORE updating the relay (the new relay writes
-- printer_status.job_name; until this has run it leaves the file name out and says so in its log).
-- Safe to re-run.

-- 1. A "reserved" status. maintenance_status may be limited by a check constraint or by an enum
--    type, depending on how the table was first created; handle either.
do $$
declare
  col record;
  con record;
begin
  select data_type, udt_schema, udt_name into col
  from information_schema.columns
  where table_schema = 'public' and table_name = 'printers' and column_name = 'maintenance_status';

  if col.data_type = 'USER-DEFINED' then
    execute format('alter type %I.%I add value if not exists %L', col.udt_schema, col.udt_name, 'reserved');
  else
    for con in
      select conname from pg_constraint
      where conrelid = 'public.printers'::regclass and contype = 'c'
        and pg_get_constraintdef(oid) ilike '%maintenance_status%'
    loop
      execute format('alter table public.printers drop constraint %I', con.conname);
    end loop;
    alter table public.printers add constraint printers_maintenance_status_check
      check (maintenance_status in ('operational', 'maintenance', 'reserved', 'offline_permanent'));
  end if;
end $$;

-- 2. A short note shown with Reserved or Out of service. printers is publicly readable, so keep it
--    to things students may see.
alter table public.printers add column if not exists status_note text;
alter table public.printers drop constraint if exists printers_status_note_length;
alter table public.printers add constraint printers_status_note_length check (char_length(status_note) <= 120);

-- 3. The file name of the print in progress, on the public board. This reverses part of
--    06_private_job_names_and_add_printer.sql: the technician asked for file names to be public.
--    (print_jobs, the history, stays staff-only.)
alter table public.printer_status add column if not exists job_name text;

notify pgrst, 'reload schema';
