-- The incarnation of the worker that holds a running job (a new token on every worker start).
-- A heartbeat carries the same token, so "is the holder still running THIS job" can be told
-- apart from "a process with that name is alive". See _held_by_other in app/worker.py.
ALTER TABLE jobs ADD COLUMN lock_token text;
