-- Remove the camera_watch table and watch_printer() from 08.
-- Run once in the Supabase SQL editor, after updating the relay and the dashboard. Safe to re-run.
--
-- "Someone is watching this printer" now goes through Supabase Realtime Presence (channel
-- "camera-watch") instead of a table the relay polled every 2 seconds: each poll was a request,
-- and every request is a log entry against the project's log quota.

drop function if exists public.watch_printer(uuid);
drop table if exists public.camera_watch;

notify pgrst, 'reload schema';
