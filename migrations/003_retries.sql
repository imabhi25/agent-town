-- Day 5: retry budget stored on the job. `attempts` (001) counts claims; the job is dead when
-- attempts reaches max_attempts and the last attempt still failed.
ALTER TABLE jobs ADD COLUMN max_attempts INT NOT NULL DEFAULT 5;
