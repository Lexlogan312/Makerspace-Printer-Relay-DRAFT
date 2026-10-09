-- Keep job names private, and let the dashboard add printers in one step.
-- Run once in the Supabase SQL editor, after 05_printer_filament.sql. Safe to re-run.
--
-- ORDER MATTERS: update the relay on the Pi first (git pull + restart). The old relay still
-- writes printer_status.subtask_name and would fail once this column is gone.

-- 1. printer_status is publicly readable, and a print's file name can identify a student.
--    The current job lives only in print_jobs (staff-only).
alter table public.printer_status drop column if exists subtask_name;

-- 2. Add a printer and its access code together (all or nothing), for the staff dashboard.
--    SECURITY INVOKER: it runs with the caller's permissions, so the admin-only row-level
--    security on printers and printer_connections still applies; the is_admin() check just
--    gives a clear error message.
create or replace function public.add_printer(p_serial text, p_label text, p_access_code text)
returns uuid
language plpgsql security invoker set search_path = ''
as $$
declare
  new_id uuid;
begin
  if not public.is_admin() then
    raise exception 'Only makerspace staff can add printers' using errcode = '42501';
  end if;
  if coalesce(trim(p_serial), '') = '' or coalesce(trim(p_label), '') = '' or coalesce(trim(p_access_code), '') = '' then
    raise exception 'Serial number, name and access code are all required' using errcode = '22023';
  end if;
  insert into public.printers (serial, label) values (upper(trim(p_serial)), trim(p_label)) returning id into new_id;
  insert into public.printer_connections (printer_id, access_code) values (new_id, trim(p_access_code));
  return new_id;
end $$;

revoke all on function public.add_printer(text, text, text) from public, anon;
grant execute on function public.add_printer(text, text, text) to authenticated;
