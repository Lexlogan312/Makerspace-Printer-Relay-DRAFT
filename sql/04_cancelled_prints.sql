-- Record cancelled prints separately from failed ones.
-- Run once in the Supabase SQL editor, after 03_cleanup.sql. Safe to re-run.
--
-- A print cancelled on the printer (or in Bambu Studio) ends in FAILED with error 0300-400C.
-- The relay now records those jobs as "cancelled", so they don't count as failures or as
-- printer errors in the reliability stats.

alter table public.print_jobs drop constraint if exists print_jobs_outcome_check;
alter table public.print_jobs add constraint print_jobs_outcome_check
  check (outcome in ('finished', 'failed', 'cancelled', 'unknown'));

-- Jobs recorded before this change
update public.print_jobs set outcome = 'cancelled'
where outcome = 'failed' and print_error = 50348044;  -- 0x0300400C = error 0300-400C

-- Adds a `cancelled` count (new columns have to go last when replacing a view).
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
       count(*) filter (where j.outcome = 'cancelled') as cancelled
from public.print_jobs j
left join public.printers p on p.id = j.printer_id
group by 1, 2, 3;

-- Errors no longer include cancellations.
create or replace view public.printer_reliability_daily with (security_invoker = true) as
with connection_changes as (
  select printer_id, ts, to_value,
         lead(ts) over (partition by printer_id order by ts) as next_ts
  from public.printer_events
  where kind = 'connection'
),
offline_periods as (
  select printer_id, ts as started_at, coalesce(next_ts, now()) as ended_at
  from connection_changes
  where to_value = 'offline'
),
offline as (
  select o.printer_id,
         (h.slot at time zone public.analytics_time_zone())::date as day,
         sum(extract(epoch from least(o.ended_at, h.slot + interval '1 hour')
                              - greatest(o.started_at, h.slot)) / 3600.0) as offline_hours
  from offline_periods o
  cross join lateral generate_series(date_trunc('hour', o.started_at), o.ended_at, interval '1 hour') as h(slot)
  where h.slot < o.ended_at
  group by 1, 2
),
errors as (
  select printer_id, (ts at time zone public.analytics_time_zone())::date as day, count(*) as errors
  from public.printer_events
  where kind = 'error' and to_value <> '0300-400C'  -- a cancelled print isn't a printer problem
  group by 1, 2
)
select coalesce(o.day, e.day) as day,
       p.id as printer_id,
       p.label as printer_label,
       round(coalesce(o.offline_hours, 0)::numeric, 2) as offline_hours,
       coalesce(e.errors, 0) as errors
from offline o
full join errors e on e.printer_id = o.printer_id and e.day = o.day
join public.printers p on p.id = coalesce(o.printer_id, e.printer_id);
