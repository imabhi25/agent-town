-- Step 5: /stats reads job counts by status, recent completions and retry events.
CREATE INDEX jobs_status ON jobs (status);
CREATE INDEX jobs_completed_at ON jobs (completed_at) WHERE completed_at IS NOT NULL;
CREATE INDEX events_type ON events (type);
