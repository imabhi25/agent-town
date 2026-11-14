-- Day 4: worker bookkeeping. Results stay in job_outputs (unique on job_id), not on jobs.
-- `attempts` already exists from 001 and is incremented when a worker claims a job.
ALTER TABLE jobs
  ADD COLUMN locked_by TEXT,
  ADD COLUMN started_at TIMESTAMPTZ,
  ADD COLUMN completed_at TIMESTAMPTZ;
