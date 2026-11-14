-- Step 1: planner output. `key` is the planner's own id ("t1"), kept for display and debugging;
-- depends_on holds real task UUIDs. plan_source says who made the plan: gemini | mock | fallback.
ALTER TABLE goals
  ADD COLUMN plan_source TEXT,
  ADD COLUMN planned_at TIMESTAMPTZ;
ALTER TABLE tasks
  ADD COLUMN key TEXT,
  ADD COLUMN description TEXT NOT NULL DEFAULT '';
