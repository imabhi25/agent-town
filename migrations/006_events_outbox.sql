-- Step 3: the events table is itself the outbox for the Redis "events" stream. Events are written
-- in the same transaction as the state change (see app/events.py); a publisher copies unpublished
-- rows to the stream and sets published_at. History from before this migration is not replayed.
ALTER TABLE events ADD COLUMN published_at TIMESTAMPTZ;
UPDATE events SET published_at = created_at;
CREATE INDEX events_unpublished ON events (id) WHERE published_at IS NULL;
