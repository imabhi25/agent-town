-- Step 2: running a goal. A task keeps its output (dependents read it into their prompt);
-- a goal keeps the combined result, or the reason it failed.
ALTER TABLE tasks ADD COLUMN output JSONB;
ALTER TABLE goals
  ADD COLUMN result JSONB,
  ADD COLUMN error TEXT;
