-- Admin management, printer connections, discovery, history and analytics.
-- Run once in the Supabase SQL editor, after 01_public_read.sql. Safe to re-run.
--
-- Who writes what:
--   relay (secret key, bypasses RLS): printer_status, printer_events, print_jobs, relay_heartbeats,
--       discovered_printers, and some columns it keeps current: printers.filament_color / model /
--       firmware_version, printer_connections.host / last_error
--   admins (signed in, listed in public.admins): printers, printer_connections (access codes)
--   public (publishable key): read printers and printer_status only

-- ───────────────────────────── Admins ─────────────────────────────
-- To make someone an admin: create their user under Authentication → Users, then run
--   insert into public.admins (user_id) values ('<their user id>');

create table if not exists public.admins (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  created_at timestamptz not null default now()
);
alter table public.admins enable row level security;  -- no policies: only is_admin() reads it
revoke all on table public.admins from anon, authenticated;

create or replace function public.is_admin()
returns boolean
language sql stable security definer set search_path = ''
as $$ select exists (select 1 from public.admins where user_id = auth.uid()) $$;

-- ───────────────────────────── printers ─────────────────────────────
alter table public.printers add column if not exists firmware_version text;   -- filled in by the relay
alter table public.printers alter column model set default 'Unknown';         -- the relay fills in known models
-- Connection details live in printer_connections instead (printers is publicly readable).
alter table public.printers drop column if exists connection_info;

drop policy if exists "admins insert" on public.printers;
drop policy if exists "admins update" on public.printers;
drop policy if exists "admins delete" on public.printers;
create policy "admins insert" on public.printers for insert to authenticated with check (public.is_admin());
create policy "admins update" on public.printers for update to authenticated using (public.is_admin()) with check (public.is_admin());
create policy "admins delete" on public.printers for delete to authenticated using (public.is_admin());

-- Keep updated_at current on every edit, whether from the admin dashboard or the relay.
create or replace function public.set_updated_at()
returns trigger language plpgsql set search_path = ''
as $$ begin new.updated_at = now(); return new; end $$;
drop trigger if exists set_updated_at on public.printers;
create trigger set_updated_at before update on public.printers
  for each row execute function public.set_updated_at();

-- To remove a printer but keep its history, set maintenance_status = 'offline_permanent'.
-- Deleting a row also deletes its status and events. Its print jobs are kept with printer_id = null.

-- ───────────────────────────── printer_status ─────────────────────────────
alter table public.printer_status add column if not exists print_error integer not null default 0;

-- ───────────────────────────── printer_connections (secret) ─────────────────────────────
create table if not exists public.printer_connections (
  printer_id  uuid primary key references public.printers (id) on delete cascade,
  host        text,                          -- current IP. The relay updates it from network announcements
  port        integer not null default 8883,
  access_code text not null,
  last_error  text                           -- written by the relay. null = connected
);
alter table public.printer_connections enable row level security;
revoke all on table public.printer_connections from anon;
grant select, insert, update, delete on table public.printer_connections to authenticated;

drop policy if exists "admins manage" on public.printer_connections;
create policy "admins manage" on public.printer_connections for all to authenticated
  using (public.is_admin()) with check (public.is_admin());

-- ───────────────────────────── discovered_printers ─────────────────────────────
-- Printers announcing themselves on the hotspot that aren't in `printers` yet.
create table if not exists public.discovered_printers (
  serial           text primary key,
  ip               text,
  model            text,
  dev_name         text,                     -- the printer's own name, e.g. 3DP-039-916
  firmware_version text,
  last_seen        timestamptz not null default now()
);
alter table public.discovered_printers enable row level security;
revoke all on table public.discovered_printers from anon;
grant select, delete on table public.discovered_printers to authenticated;

drop policy if exists "admins read" on public.discovered_printers;
drop policy if exists "admins delete" on public.discovered_printers;
create policy "admins read" on public.discovered_printers for select to authenticated using (public.is_admin());
create policy "admins delete" on public.discovered_printers for delete to authenticated using (public.is_admin());

-- ───────────────────────────── History (written by the relay) ─────────────────────────────
create table if not exists public.printer_events (
  id         uuid primary key,               -- generated by the relay, so retries can't duplicate rows
  printer_id uuid not null references public.printers (id) on delete cascade,
  ts         timestamptz not null,
  kind       text not null check (kind in ('connection', 'state', 'error')),
  from_value text,
  to_value   text
);
create index if not exists printer_events_printer_ts on public.printer_events (printer_id, ts);

-- One row per print. Job names can identify students, so this is admin-only.
create table if not exists public.print_jobs (
  id                  uuid primary key,      -- generated by the relay
  printer_id          uuid references public.printers (id) on delete set null,
  printer_label       text not null,         -- label when the print started (kept if the printer is deleted)
  job_name            text,
  started_at          timestamptz not null,
  ended_at            timestamptz,           -- null while printing
  outcome             text check (outcome in ('finished', 'failed', 'unknown')),
  total_layers        integer,
  filament_type       text,
  filament_color      text,                  -- "#RRGGBB"
  filament_color_name text,                  -- closest named color, for grouping
  print_error         integer not null default 0
);
create index if not exists print_jobs_printer_started on public.print_jobs (printer_id, started_at);
create index if not exists print_jobs_started on public.print_jobs (started_at);
create index if not exists print_jobs_open on public.print_jobs (printer_id) where ended_at is null;

-- One row per minute the relay is running and can reach Supabase.
create table if not exists public.relay_heartbeats (
  id                  uuid primary key default gen_random_uuid(),
  ts                  timestamptz not null default now(),   -- Supabase's clock, not the Pi's
  printers_total      smallint,
  printers_online     smallint,
  max_push_latency_ms integer      -- slowest printer report -> Supabase write in that minute
);
create index if not exists relay_heartbeats_ts on public.relay_heartbeats (ts);

-- Deleting a printer leaves its unfinished job with no printer, so close it.
create or replace function public.close_jobs_of_deleted_printer()
returns trigger
language plpgsql security definer set search_path = ''
as $$
begin
  update public.print_jobs set ended_at = now(), outcome = 'unknown'
  where printer_id = old.id and ended_at is null;
  return old;
end $$;
drop trigger if exists close_open_jobs on public.printers;
create trigger close_open_jobs before delete on public.printers
  for each row execute function public.close_jobs_of_deleted_printer();

do $$
declare t text;
begin
  foreach t in array array['printer_events', 'print_jobs', 'relay_heartbeats'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on table public.%I from anon', t);
    execute format('grant select on table public.%I to authenticated', t);
    execute format('drop policy if exists "admins read" on public.%I', t);
    execute format('create policy "admins read" on public.%I for select to authenticated using (public.is_admin())', t);
  end loop;
end $$;

-- ───────────────────────────── Analytics views ─────────────────────────────
-- Every stat uses full 24-hour days, in the makerspace's time zone. Days with no activity
-- have no row, so treat a missing day as 0. All views use security_invoker, so they follow
-- the admin-only rules of the tables underneath.

create or replace function public.analytics_time_zone()
returns text language sql immutable as $$ select 'America/New_York' $$;

-- Every print split into the clock hours it covers. A 9:30–11:45 print gives
-- 9:00 → 0.5 h, 10:00 → 1 h, 11:00 → 0.75 h. Running prints count up to now.
create or replace view public.print_job_hours with (security_invoker = true) as
select j.id as job_id,
       j.printer_id,
       coalesce(p.label, j.printer_label) as printer_label,
       (h.slot at time zone public.analytics_time_zone())::date as day,
       extract(isodow from h.slot at time zone public.analytics_time_zone())::int as weekday,  -- 1 = Monday
       extract(hour from h.slot at time zone public.analytics_time_zone())::int as hour,
       extract(epoch from least(coalesce(j.ended_at, now()), h.slot + interval '1 hour')
                        - greatest(j.started_at, h.slot)) / 3600.0 as hours
from public.print_jobs j
left join public.printers p on p.id = j.printer_id
cross join lateral generate_series(date_trunc('hour', j.started_at), coalesce(j.ended_at, now()), interval '1 hour') as h(slot)
where h.slot < coalesce(j.ended_at, now());  -- skips the empty hour when a print ends exactly on the hour

-- Utilization: hours spent printing ÷ 24.
create or replace view public.printer_utilization_daily with (security_invoker = true) as
select day, printer_id, printer_label,
       round(sum(hours)::numeric, 2) as printing_hours,
       round((sum(hours) / 24 * 100)::numeric, 1) as utilization_percent
from public.print_job_hours
group by day, printer_id, printer_label;

-- Utilization of the whole fleet: printing hours ÷ (24 × printers not retired).
-- Uses today's printer count for every day.
create or replace view public.fleet_utilization_daily with (security_invoker = true) as
with fleet as (select count(*) as printers from public.printers where maintenance_status <> 'offline_permanent')
select h.day,
       round(sum(h.hours)::numeric, 2) as printing_hours,
       fleet.printers as printer_count,
       round((sum(h.hours) / nullif(24 * fleet.printers, 0) * 100)::numeric, 1) as utilization_percent
from public.print_job_hours h cross join fleet
group by h.day, fleet.printers;

-- Busy times: busy_printers = average number of printers printing in that hour of that day.
-- Average it over several weeks of the same weekday/hour to build the heatmap.
create or replace view public.busy_hours_daily with (security_invoker = true) as
select day, weekday, hour, round(sum(hours)::numeric, 2) as busy_printers
from public.print_job_hours
group by day, weekday, hour;

-- Jobs per printer per day (by the day the print started).
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
              filter (where j.outcome = 'finished'))::numeric, 1) as avg_finished_minutes
from public.print_jobs j
left join public.printers p on p.id = j.printer_id
group by 1, 2, 3;

-- Filament: jobs and print hours by type and color (by the day the print started).
create or replace view public.filament_usage_daily with (security_invoker = true) as
select (started_at at time zone public.analytics_time_zone())::date as day,
       coalesce(filament_type, 'Unknown') as filament_type,
       coalesce(filament_color_name, 'unknown') as color_name,
       count(*) as jobs,
       round(sum(extract(epoch from coalesce(ended_at, now()) - started_at) / 3600)::numeric, 2) as printing_hours
from public.print_jobs
group by 1, 2, 3;

-- Reliability: hours offline and new errors per printer per day.
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
  where kind = 'error'
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

-- Uptime of the relay pipeline (relay running and able to write to Supabase):
-- minutes with a heartbeat ÷ minutes in the day. The first day counts from the first
-- heartbeat, and today counts up to now. Every day from the first heartbeat has a row,
-- including days with 0% uptime. For a longer period, add up minutes_up and minutes_total.
create or replace view public.relay_uptime_daily with (security_invoker = true) as
with first_beat as (select min(ts) as ts from public.relay_heartbeats),
days as (
  select d::date as day,
         greatest(d::timestamp at time zone public.analytics_time_zone(), first_beat.ts) as day_start,
         least((d + interval '1 day')::timestamp at time zone public.analytics_time_zone(), now()) as day_end
  from first_beat,
       generate_series((first_beat.ts at time zone public.analytics_time_zone())::date::timestamp,
                       (now() at time zone public.analytics_time_zone())::date::timestamp, interval '1 day') as d
),
beats as (
  select (ts at time zone public.analytics_time_zone())::date as day,
         count(distinct date_trunc('minute', ts)) as minutes_up,
         avg(printers_online) as avg_printers_online,
         max(max_push_latency_ms) as max_latency_ms,
         percentile_cont(0.95) within group (order by max_push_latency_ms) as p95_latency_ms
  from public.relay_heartbeats
  group by 1
)
select days.day,
       coalesce(beats.minutes_up, 0) as minutes_up,
       greatest(ceil(extract(epoch from days.day_end - days.day_start) / 60), 1)::int as minutes_total,
       round(least(100.0, 100.0 * coalesce(beats.minutes_up, 0)
                    / greatest(ceil(extract(epoch from days.day_end - days.day_start) / 60), 1))::numeric, 2) as uptime_percent,
       beats.max_latency_ms,
       round(beats.p95_latency_ms::numeric) as p95_latency_ms,
       round(beats.avg_printers_online, 1) as avg_printers_online  -- were the printers reachable, too?
from days left join beats on beats.day = days.day;

do $$
declare v text;
begin
  foreach v in array array['print_job_hours', 'printer_utilization_daily', 'fleet_utilization_daily',
                           'busy_hours_daily', 'print_jobs_daily', 'filament_usage_daily',
                           'printer_reliability_daily', 'relay_uptime_daily'] loop
    execute format('revoke all on table public.%I from anon', v);
    execute format('grant select on table public.%I to authenticated', v);
  end loop;
end $$;
