-- Show the loaded filament's color name and type on each printer, for the dashboard.
-- Run once in the Supabase SQL editor, after 04_cancelled_prints.sql. Safe to re-run.
--
-- The relay fills both in (alongside filament_color) as soon as it's restarted with the
-- matching code, and keeps them current whenever a spool changes.

alter table public.printers add column if not exists filament_color_name text;  -- e.g. 'Sky Blue' (Elegoo PLA names)
alter table public.printers add column if not exists filament_type text;        -- e.g. 'PLA', 'PETG'
