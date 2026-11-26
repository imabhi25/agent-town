# A TLA+ model of the engine

The chaos campaign (README, "Checked under crashes") shows that 10,000 jobs survive hundreds of
real crashes. That is strong evidence, but it only covers the interleavings that happened to occur.
This folder holds a model of the same protocol that TLC, the TLA+ model checker, explores
**exhaustively**: every order of every worker step, crash, heartbeat lapse and provider error, within
small bounds. Then it checks the properties in every reachable state.

Run it: `python scripts/check_spec.py` (needs Java 11+; it downloads a pinned `tla2tools.jar` and
checks its SHA-256). CI runs it on every pull request. The results and the counterexample traces
are in `results/spec/`, and the README table is generated from them.

The design as built is checked in three configurations, because the state space grows very fast
with the fault budget (2 crashes plus 2 errors is well over 150 million states): up to 2 crashes or
heartbeat lapses, up to 2 provider errors, and 1 of each. Each broken variant runs with the
smallest budget that shows its bug.

## What is in the model
`AgentTown.tla` models one goal whose tasks form a graph (in `MC.tla`: `t3` needs `t1` and `t2`, the
smallest graph where two siblings finish into one dependent), 2 workers, and:

| Model action | Code |
|---|---|
| `DispatchXadd`, `DispatchCommit`, `DispatcherCrash` | `publish_pending` in `app/dispatcher.py`: XADD, then mark the outbox row published |
| `Read`, `Reclaim` | `XREADGROUP >` and `XAUTOCLAIM` in `worker_loop` |
| `Claim` | `_claim` (one transaction with the job row lock: finished / busy / claimed), then the provider call, which may fail (retryable) |
| `Complete` | `_complete` + `complete_task` + `advance_goal`: one transaction that also queues dependents |
| `ScheduleRetry`, `Zadd`, `Ack` | `_schedule_retry`, then ZADD to the delayed set, then XACK |
| `MoverXadd`, `MoverZrem`, `MoverCrash` | `move_due_jobs`: XADD, then remove the delayed entry |
| `Crash`, `HeartbeatLapse`, `Beat` | SIGKILL followed by a restart under the same name (new lock token); a heartbeat key expiring while the process still runs (which, to other workers, is also what a worker that stays down looks like) |

Every Postgres transaction is one atomic step (that is what a transaction gives). Each Redis
command is its own step, so a crash can land between any two of them. Time is not modelled: "the
lease ran out" is just "a reclaim may happen now", which allows more behaviours than real clocks,
not fewer.

## Properties
Safety (checked in every state):
- `NoDuplicateOutput`: a job never has more than one stored result.
- `OutputIffSucceeded`: a job has a result exactly when it is `succeeded`.
- `DependencyOrder`: a task's job exists only after all its dependencies succeeded.
- `NoLostJob`: an unfinished job always has something that will run it again: an unpublished
  outbox row, a delayed-set entry, an unacknowledged stream message, or a live worker on it.
- `NoStuckTask`: a pending task always has a dependency that has not succeeded yet.

Liveness (checked over infinite behaviours, assuming each correct component keeps taking steps
and faults stop after the budget): `GoalFinishes` (every job succeeds) and `StreamDrains` (every
message is eventually acknowledged).

## Broken variants
A model checker that always says "no error" proves nothing, so each design choice is switched off
in turn, and TLC must find a counterexample. The traces are in `results/spec/`.

- `ack_before_commit`: XACK when the job is claimed. A crash before the commit loses the job.
- `no_goal_lock`: finish a task without `SELECT ... FOR UPDATE` on the goal. `t1` and `t2` finish
  at the same time, each transaction reads the other as still queued, and `t3` is never queued.
- `plain_zrem` and `name_only_heartbeat`: these were the real code. The model found both bugs.

## Two bugs the model found
**1. The retry mover could lose a job.** `move_due_jobs` did XADD, then a plain `ZREM`. Between
the two commands a worker can take the new message, fail again, and `ZADD` the job with a later
score. The `ZREM` then deletes that **new** entry, the worker acknowledges its message, and the job
sits in `retrying` with nothing left to run it. TLC found it as a 15-step trace (`results/spec/plain_zrem.trace.txt`). The chaos campaign
never hit it: the window is two Redis round trips wide and needs a second failure inside it.
*Fix:* the mover removes the entry only if its score is still the one it read (a small Lua
script, `_ZREM_IF_SCORE` in `app/worker.py`).

**2. A restarted worker could keep its old job "busy" for ever.** `_claim` treated a job as busy
when a worker with the holder's *name* had a heartbeat. Names are reused (a restarted process
starts again at `worker-1`), so a worker that crashed and came back looked like it was still
running the job. Other workers left the job alone, and only the restarted worker itself could take
it back. With two such workers, each kept handing the other's job back, and TLC found a cycle where
neither job finishes. In the real system the random scheduling would usually break this, but
nothing guaranteed it would.
*Fix:* every worker start gets a fresh **lock token**. The heartbeat carries it and `_claim`
stores it on the job (`jobs.lock_token`, migration 008). A job counts as busy only if the holder's
heartbeat carries the same token that took the lock. This is the same idea as a fencing token.

Both have regression tests in `tests/test_spec_findings.py`. Both tests fail on the old code.

## Limits
This is a model, not the code: a mismatch between the two would not be caught (the action table
above is the mapping, and should be updated together with the code). The bounds are small (2
workers, 3 tasks, a few faults), which is where most protocol bugs show up, but it is still a
bound. Postgres failures, losing Redis data, the DLQ, approvals and the planner job are left out
(see the header of `AgentTown.tla`).
