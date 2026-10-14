-- Runtime follow-up migration: command acceptance creates an accepted job.
ALTER TABLE runtime_jobs ALTER COLUMN status SET DEFAULT 'accepted';
