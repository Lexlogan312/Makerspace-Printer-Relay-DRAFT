-- Camera snapshots: one still photo per printing printer, shown to everyone on the dashboard.
-- Run once in the Supabase SQL editor, after 06. Safe to re-run.
--
-- The relay (secret key) uploads printer-snapshots/<printer id>.jpg, replacing the previous photo,
-- then sets printer_status.snapshot_at. The dashboard shows the photo for each printing printer
-- so students can check on their build.

-- 1. When the latest photo was taken (public, like the rest of printer_status).
alter table public.printer_status add column if not exists snapshot_at timestamptz;

-- 2. A public bucket: anyone can view a photo by its URL (that's the point), but nobody can list,
--    upload, change or delete files. There are no write policies, so only the relay's secret key
--    (which bypasses row-level security) can write. 1 MB per file is far more than an A1 frame
--    needs; only JPEGs are accepted.
--    To make photos private again later: update storage.buckets set public = false where id = 'printer-snapshots';
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('printer-snapshots', 'printer-snapshots', true, 1048576, array['image/jpeg'])
on conflict (id) do update
  set public = true, file_size_limit = excluded.file_size_limit, allowed_mime_types = excluded.allowed_mime_types;
