-- American spelling in stored data: print outcome "cancelled" -> "canceled", and the gray filament
-- colors "Grey" -> "Gray", "Space Grey" -> "Space Gray".
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- Order matters, because the relay writes these values:
--   1. stop the relay          sudo systemctl stop printer-relay
--   2. run this file
--   3. update and start it     cd ~/printer-relay && git pull && sudo systemctl start printer-relay
-- (An old relay recording a canceled print after step 2 would be turned away by the new check.)

-- Print outcomes
alter table public.print_jobs drop constraint if exists print_jobs_outcome_check;
update public.print_jobs set outcome = 'canceled' where outcome = 'cancelled';
alter table public.print_jobs add constraint print_jobs_outcome_check
  check (outcome in ('finished', 'failed', 'canceled', 'unknown'));

-- The analytics view's count column: rename it in place (keeps its permissions), then point it
-- at the new value.
do $$
begin
  if exists (select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'print_jobs_daily' and column_name = 'cancelled') then
    alter view public.print_jobs_daily rename column cancelled to canceled;
  end if;
end $$;

create or replace view public.print_jobs_daily with (security_invoker = true) as
select (j.started_at at time zone public.analytics_time_zone())::date as day,
       j.printer_id,
       coalesce(p.label, j.printer_label) as printer_label,
       count(*) as jobs_started,
       count(*) filter (where j.outcome = 'finished') as finished,
       count(*) filter (where j.outcome = 'failed') as failed,
       count(*) filter (where j.outcome = 'unknown') as unknown,
       count(*) filter (where j.ended_at is null) as in_progress,
       round((avg(extract(epoch from j.ended_at - j.started_at) / 60)
              filter (where j.outcome = 'finished'))::numeric, 1) as avg_finished_minutes,
       count(*) filter (where j.outcome = 'canceled') as canceled
from public.print_jobs j
left join public.printers p on p.id = j.printer_id
group by 1, 2, 3;

-- Filament colors (analytics groups prints by these names)
update public.printers set filament_color_name = 'Gray' where filament_color_name = 'Grey';
update public.printers set filament_color_name = 'Space Gray' where filament_color_name = 'Space Grey';
update public.print_jobs set filament_color_name = 'Gray' where filament_color_name = 'Grey';
update public.print_jobs set filament_color_name = 'Space Gray' where filament_color_name = 'Space Grey';

notify pgrst, 'reload schema';
