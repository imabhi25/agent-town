----------------------------- MODULE AgentTown -----------------------------
(***************************************************************************)
(* A model of Agent Town's job engine, small enough for TLC to try EVERY   *)
(* interleaving of workers, crashes and retries.                            *)
(*                                                                          *)
(* What is modelled (each action names the Python function it stands for): *)
(*   - one goal whose tasks form a DAG; a task's job is created (with its  *)
(*     outbox row) only inside the transaction that finishes its last      *)
(*     dependency                       (app/goals.py advance_goal)        *)
(*   - the outbox dispatcher: XADD, then commit "published"                *)
(*                                      (app/dispatcher.py publish_pending)*)
(*   - Redis Streams with a consumer group: new messages (XREADGROUP >),   *)
(*     a pending list per message, XAUTOCLAIM after the lease, XACK        *)
(*   - workers: claim (one transaction), provider call, complete (one      *)
(*     transaction, guarded), then XACK; or on a provider error: mark      *)
(*     retrying, ZADD to the delayed set, then XACK  (app/worker.py)       *)
(*   - the delayed-set mover: XADD, then ZREM    (move_due_jobs)           *)
(*   - heartbeats: a worker is "alive" while its key exists                *)
(*   - faults: SIGKILL of any worker, the dispatcher or the mover at any   *)
(*     step; a heartbeat key expiring while the worker still runs (a long  *)
(*     GC pause or a Redis restart); provider errors.                      *)
(*                                                                          *)
(* What is NOT modelled: Postgres failures (each transaction is one atomic *)
(* step), losing Redis data (AOF fsync always; app/reconcile.py covers the *)
(* rest), the DLQ (provider errors are bounded below max_attempts, so no   *)
(* job dies), approvals and the planner job (the plan is given). Time is   *)
(* not modelled: "the lease expired" is simply "a reclaim may happen now", *)
(* which allows strictly more behaviours than real clocks do.              *)
(*                                                                          *)
(* Three switches turn on real alternatives; each must produce a           *)
(* counterexample, which shows the property depends on the design choice:  *)
(*   AckBeforeCommit  XACK when the job is claimed (the at-most-once bug)  *)
(*   GoalLock=FALSE   finishing a task without the goal row lock (FOR      *)
(*                    UPDATE): two siblings finishing together can each    *)
(*                    miss the other, and their dependent is never queued  *)
(*   ConditionalZrem=FALSE  the mover removes the delayed entry with a     *)
(*                    plain ZREM (the code before this spec existed)       *)
(*   LockToken=FALSE  "busy" means only "a worker with that NAME is alive" *)
(*                    (the code before this spec existed); a worker that  *)
(*                    restarts under the same name is then mistaken for   *)
(*                    the one that holds the job                          *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    Workers,            \* worker ids; a restarted worker keeps its id
    Tasks,              \* task ids of the goal
    Deps,               \* Deps[t] = the tasks t depends on
    MaxFaults,          \* crashes + heartbeat lapses, in total
    MaxProviderErrors,  \* provider errors, in total (each leads to a retry)
    AckBeforeCommit, GoalLock, ConditionalZrem, LockToken   \* see the header

None == "none"

VARIABLES
    taskStatus,   \* tasks.status:    pending | queued | succeeded
    jobStatus,    \* jobs.status:     none (no job yet) | queued | running | retrying | succeeded
    lockedBy,     \* jobs.locked_by (worker name)
    lockTok,      \* jobs.lock_token: the incarnation of that worker that took the lock
    token,        \* per worker: its current incarnation (new on every start)
    outputs,      \* number of job_outputs rows per job (job id = task id here)
    outbox,       \* jobs whose outbox row is not yet marked published
    msgs,         \* the jobs stream: <<[job, delivered, acked]>> (which consumer owns a pending
                  \* message does not matter: XAUTOCLAIM can move it to any worker)
    delayed,      \* the delayed sorted set: delayed[j] = 0 (absent) or the version of its score
    zver,         \* a fresh score per ZADD (stands for time.time() + delay)
    pc,           \* per worker: where it is in handle_message
    hb,           \* its heartbeat key exists
    disp,         \* the dispatcher has XADDed this job but not committed "published" yet
    mover,        \* the mover has XADDed [job, ver] but not done the ZREM yet
    faults, perrs

vars == <<taskStatus, jobStatus, lockedBy, lockTok, outputs, outbox, msgs, delayed, zver,
          pc, hb, token, disp, mover, faults, perrs>>

\* token only changes on Crash, so it rides along with the Postgres variables in UNCHANGED.
dbVars == <<taskStatus, jobStatus, lockedBy, lockTok, outputs, outbox, token>>

\* Enough room for every XADD the fault and error budgets allow; checked by BoundedStream.
MaxMsgs == Cardinality(Tasks) + 2 * MaxFaults + 2 * MaxProviderErrors

Idle == [step |-> "idle", msg |-> 0, seen |-> {}]
NoMove == [job |-> None, ver |-> 0]

Init ==
    /\ taskStatus = [t \in Tasks |-> IF Deps[t] = {} THEN "queued" ELSE "pending"]
    /\ jobStatus  = [t \in Tasks |-> IF Deps[t] = {} THEN "queued" ELSE "none"]
    /\ lockedBy   = [t \in Tasks |-> None]
    /\ lockTok    = [t \in Tasks |-> 0]
    /\ token      = [w \in Workers |-> 1]
    /\ outputs    = [t \in Tasks |-> 0]
    /\ outbox     = {t \in Tasks : Deps[t] = {}}
    /\ msgs       = << >>
    /\ delayed    = [t \in Tasks |-> 0]
    /\ zver       = 0
    /\ pc         = [w \in Workers |-> Idle]
    /\ hb         = [w \in Workers |-> TRUE]
    /\ disp       = None
    /\ mover      = NoMove
    /\ faults     = 0
    /\ perrs      = 0

NewMsg(j) == [job |-> j, delivered |-> FALSE, acked |-> FALSE]
JobOf(w)  == msgs[pc[w].msg].job
Go(w, step) == pc' = [pc EXCEPT ![w] = [@ EXCEPT !.step = step]]

(*************************** outbox dispatcher ****************************)
DispatchXadd ==
    /\ disp = None
    /\ \E j \in outbox :
         /\ msgs' = Append(msgs, NewMsg(j))
         /\ disp' = j
    /\ UNCHANGED <<dbVars, delayed, zver, pc, hb, mover, faults, perrs>>

DispatchCommit ==       \* UPDATE outbox SET published_at = now(); COMMIT
    /\ disp # None
    /\ outbox' = outbox \ {disp}
    /\ disp' = None
    /\ UNCHANGED <<taskStatus, jobStatus, lockedBy, lockTok, outputs, token, msgs, delayed,
                   zver, pc, hb, mover, faults, perrs>>

DispatcherCrash ==      \* the open transaction rolls back; the row stays unpublished
    /\ disp # None /\ faults < MaxFaults
    /\ disp' = None /\ faults' = faults + 1
    /\ UNCHANGED <<dbVars, msgs, delayed, zver, pc, hb, mover, perrs>>

(*************************** delayed-set mover ****************************)
MoverXadd ==            \* ZRANGEBYSCORE, read the job row, XADD unless it is finished
    /\ mover = NoMove
    /\ \E j \in Tasks :
         /\ delayed[j] > 0
         /\ mover' = [job |-> j, ver |-> delayed[j]]
         /\ msgs' = IF jobStatus[j] = "succeeded" THEN msgs ELSE Append(msgs, NewMsg(j))
    /\ UNCHANGED <<dbVars, delayed, zver, pc, hb, disp, faults, perrs>>

MoverZrem ==
    /\ mover # NoMove
    /\ delayed' = IF ~ConditionalZrem \/ delayed[mover.job] = mover.ver
                  THEN [delayed EXCEPT ![mover.job] = 0]
                  ELSE delayed     \* rescheduled since we read it: leave the new entry
    /\ mover' = NoMove
    /\ UNCHANGED <<dbVars, msgs, zver, pc, hb, disp, faults, perrs>>

MoverCrash ==
    /\ mover # NoMove /\ faults < MaxFaults
    /\ mover' = NoMove /\ faults' = faults + 1
    /\ UNCHANGED <<dbVars, msgs, delayed, zver, pc, hb, disp, perrs>>

(******************************** workers *********************************)
Deliver(w, i) ==
    /\ msgs' = [msgs EXCEPT ![i].delivered = TRUE]
    /\ pc' = [pc EXCEPT ![w] = [step |-> "claim", msg |-> i, seen |-> {}]]
    /\ UNCHANGED <<dbVars, delayed, zver, hb, disp, mover, faults, perrs>>

Read(w, i) ==           \* XREADGROUP GROUP workers w STREAMS jobs >
    /\ pc[w].step = "idle"
    /\ i \in DOMAIN msgs /\ ~msgs[i].delivered
    /\ Deliver(w, i)

Reclaim(w, i) ==        \* XAUTOCLAIM: any pending message, once its lease has run out
    /\ pc[w].step = "idle"
    /\ i \in DOMAIN msgs /\ msgs[i].delivered /\ ~msgs[i].acked
    /\ Deliver(w, i)

\* "Someone else is running job j right now", as _claim decides it.
HeldByOther(w, j) ==
    LET o == lockedBy[j] IN
    /\ jobStatus[j] = "running"
    /\ IF LockToken
       THEN \* the holder's heartbeat carries the SAME incarnation that took the lock, and it is not us
            /\ hb[o] /\ token[o] = lockTok[j]
            /\ ~(o = w /\ lockTok[j] = token[w])
       ELSE o # w /\ hb[o]

\* _claim (one transaction holding the job row lock), followed by the provider call. The call is
\* folded into this step: it changes nothing the model can see, so a crash just before it and a
\* crash just after it lead to the same state. Its outcome decides the next step.
Claim(w) ==
    LET j == JobOf(w) IN
    /\ pc[w].step = "claim"
    /\ IF jobStatus[j] = "succeeded" THEN
            /\ Go(w, "ack")                                    \* "finished"
            /\ UNCHANGED <<dbVars, msgs, perrs>>
       ELSE IF HeldByOther(w, j) THEN
            /\ pc' = [pc EXCEPT ![w] = Idle]                   \* "busy": leave it pending
            /\ UNCHANGED <<dbVars, msgs, perrs>>
       ELSE
            /\ jobStatus' = [jobStatus EXCEPT ![j] = "running"]
            /\ lockedBy'  = [lockedBy EXCEPT ![j] = w]
            /\ lockTok'   = [lockTok EXCEPT ![j] = token[w]]
            /\ msgs' = IF AckBeforeCommit
                       THEN [msgs EXCEPT ![pc[w].msg].acked = TRUE]
                       ELSE msgs
            /\ \/ /\ Go(w, IF GoalLock THEN "complete" ELSE "read")   \* the call succeeded
                  /\ perrs' = perrs
               \/ /\ perrs < MaxProviderErrors                          \* retryable error
                  /\ Go(w, "retry")
                  /\ perrs' = perrs + 1
            /\ UNCHANGED <<taskStatus, outputs, outbox, token>>
    /\ UNCHANGED <<delayed, zver, hb, disp, mover, faults>>

\* What finishing job j does to the goal, given which tasks the transaction SEES as succeeded:
\* every still-pending task whose dependencies all look succeeded gets its job and outbox row.
Ready(seen) == {t \in Tasks : taskStatus[t] = "pending" /\ Deps[t] \subseteq seen}

Finish(j, seen) ==
    LET q == Ready(seen \cup {j}) IN
    /\ outputs'    = [outputs EXCEPT ![j] = @ + 1]
    /\ taskStatus' = [t \in Tasks |-> IF t = j THEN "succeeded"
                                      ELSE IF t \in q THEN "queued" ELSE taskStatus[t]]
    /\ jobStatus'  = [t \in Tasks |-> IF t = j THEN "succeeded"
                                      ELSE IF t \in q THEN "queued" ELSE jobStatus[t]]
    /\ outbox'     = outbox \cup q
    /\ UNCHANGED <<lockedBy, lockTok, token>>

Succeeded == {t \in Tasks : taskStatus[t] = "succeeded"}

\* _complete with the goal lock: one transaction that sees every committed sibling.
Complete(w) ==
    LET j == JobOf(w) IN
    /\ pc[w].step = "complete"
    /\ IF jobStatus[j] = "succeeded"           \* WHERE status != 'succeeded' matched nothing
       THEN UNCHANGED dbVars
       ELSE Finish(j, Succeeded)
    /\ Go(w, "ack")
    /\ UNCHANGED <<msgs, delayed, zver, hb, disp, mover, faults, perrs>>

\* Without the goal lock the transaction reads its siblings (SELECT ... FROM tasks) and commits
\* later; a sibling that commits in between is not seen (READ COMMITTED).
CompleteRead(w) ==
    /\ pc[w].step = "read"
    /\ pc' = [pc EXCEPT ![w] = [@ EXCEPT !.step = "commit", !.seen = Succeeded]]
    /\ UNCHANGED <<dbVars, msgs, delayed, zver, hb, disp, mover, faults, perrs>>

CompleteCommit(w) ==
    LET j == JobOf(w) IN
    /\ pc[w].step = "commit"
    /\ IF jobStatus[j] = "succeeded" THEN UNCHANGED dbVars ELSE Finish(j, pc[w].seen)
    /\ Go(w, "ack")
    /\ UNCHANGED <<msgs, delayed, zver, hb, disp, mover, faults, perrs>>

ScheduleRetry(w) ==     \* _schedule_retry: UPDATE ... SET status = 'retrying' (guarded)
    LET j == JobOf(w) IN
    /\ pc[w].step = "retry"
    /\ IF jobStatus[j] = "succeeded"
       THEN /\ Go(w, "ack") /\ UNCHANGED dbVars
       ELSE /\ jobStatus' = [jobStatus EXCEPT ![j] = "retrying"]
            /\ Go(w, "zadd")
            /\ UNCHANGED <<taskStatus, lockedBy, lockTok, outputs, outbox, token>>
    /\ UNCHANGED <<msgs, delayed, zver, hb, disp, mover, faults, perrs>>

Zadd(w) ==
    /\ pc[w].step = "zadd"
    /\ delayed' = [delayed EXCEPT ![JobOf(w)] = zver + 1]
    /\ zver' = zver + 1
    /\ Go(w, "ack")
    /\ UNCHANGED <<dbVars, msgs, hb, disp, mover, faults, perrs>>

Ack(w) ==
    /\ pc[w].step = "ack"
    /\ msgs' = [msgs EXCEPT ![pc[w].msg].acked = TRUE]
    /\ pc' = [pc EXCEPT ![w] = Idle]
    /\ UNCHANGED <<dbVars, delayed, zver, hb, disp, mover, faults, perrs>>

(********************************* faults *********************************)
Crash(w) ==             \* SIGKILL, and the process manager starts it again under the SAME name
    \* The open transaction rolls back, the message stays pending, and the new incarnation has
    \* a new token and a fresh heartbeat. (A worker that stays down looks, to everyone else,
    \* like one whose heartbeat lapsed: see HeartbeatLapse.)
    /\ faults < MaxFaults
    /\ pc' = [pc EXCEPT ![w] = Idle]
    /\ hb' = [hb EXCEPT ![w] = TRUE]
    /\ token' = [token EXCEPT ![w] = @ + 1]
    /\ faults' = faults + 1
    /\ UNCHANGED <<taskStatus, jobStatus, lockedBy, lockTok, outputs, outbox,
                   msgs, delayed, zver, disp, mover, perrs>>

HeartbeatLapse(w) ==    \* the key expires although the process is still running
    /\ hb[w] /\ faults < MaxFaults
    /\ hb' = [hb EXCEPT ![w] = FALSE]
    /\ faults' = faults + 1
    /\ UNCHANGED <<dbVars, msgs, delayed, zver, pc, disp, mover, perrs>>

Beat(w) ==
    /\ ~hb[w]
    /\ hb' = [hb EXCEPT ![w] = TRUE]
    /\ UNCHANGED <<dbVars, msgs, delayed, zver, pc, disp, mover, faults, perrs>>

(************************************************************************)
WorkerStep(w) ==
    \/ \E i \in DOMAIN msgs : Read(w, i) \/ Reclaim(w, i)
    \/ Claim(w) \/ Complete(w) \/ CompleteRead(w) \/ CompleteCommit(w)
    \/ ScheduleRetry(w) \/ Zadd(w) \/ Ack(w) \/ Beat(w)

Faults ==
    \/ \E w \in Workers : Crash(w) \/ HeartbeatLapse(w)
    \/ DispatcherCrash \/ MoverCrash

\* Everything finished and quiet: the only state where nothing can happen (TLC reports any other
\* state without a successor as a deadlock).
Terminated ==
    /\ \A t \in Tasks : jobStatus[t] = "succeeded"
    /\ \A i \in DOMAIN msgs : msgs[i].acked
    /\ \A w \in Workers : pc[w].step = "idle"
    /\ \A t \in Tasks : delayed[t] = 0
    /\ outbox = {} /\ disp = None /\ mover = NoMove
    /\ UNCHANGED vars

Next ==
    \/ Terminated
    \/ \E w \in Workers : WorkerStep(w)
    \/ DispatchXadd \/ DispatchCommit \/ MoverXadd \/ MoverZrem
    \/ Faults

\* Every correct component keeps taking steps: a worker that can do something eventually does
\* something, and so do the dispatcher and the mover. Faults are allowed, never required.
Fairness ==
    /\ \A w \in Workers : WF_vars(WorkerStep(w))
    /\ WF_vars(DispatchXadd \/ DispatchCommit)
    /\ WF_vars(MoverXadd \/ MoverZrem)

Spec == Init /\ [][Next]_vars /\ Fairness

(****************************** properties ******************************)
TypeOK ==
    /\ taskStatus \in [Tasks -> {"pending", "queued", "succeeded"}]
    /\ jobStatus \in [Tasks -> {"none", "queued", "running", "retrying", "succeeded"}]
    /\ outputs \in [Tasks -> Nat]
    /\ outbox \subseteq Tasks

\* At most one stored result per job (the unique job_outputs row is never needed as a backstop).
NoDuplicateOutput == \A j \in Tasks : outputs[j] <= 1

\* A job has a result exactly when it is succeeded.
OutputIffSucceeded == \A j \in Tasks : (outputs[j] = 1) <=> (jobStatus[j] = "succeeded")

\* A task's job exists only after every dependency succeeded.
DependencyOrder ==
    \A t \in Tasks : jobStatus[t] # "none" => \A d \in Deps[t] : taskStatus[d] = "succeeded"

\* No lost job: an unfinished job always has something that will run it again: an unpublished
\* outbox row, an entry in the delayed set, an unacknowledged stream message, or a live worker
\* that is working on it right now.
Recoverable(j) ==
    \/ j \in outbox
    \/ delayed[j] > 0
    \/ \E i \in DOMAIN msgs : msgs[i].job = j /\ ~msgs[i].acked
    \/ \E w \in Workers : pc[w].step # "idle" /\ JobOf(w) = j

NoLostJob == \A j \in Tasks : jobStatus[j] \in {"queued", "running", "retrying"} => Recoverable(j)

\* No stuck dependent: a pending task always has a dependency that has not succeeded yet.
NoStuckTask ==
    \A t \in Tasks : taskStatus[t] = "pending" => \E d \in Deps[t] : taskStatus[d] # "succeeded"

BoundedStream == Len(msgs) <= MaxMsgs

\* Liveness: whatever the faults (within the budgets), the goal finishes.
GoalFinishes == <>(\A t \in Tasks : jobStatus[t] = "succeeded")

\* ...and the stream drains: every message is eventually acknowledged.
StreamDrains == <>[](\A i \in DOMAIN msgs : msgs[i].acked)
=============================================================================
