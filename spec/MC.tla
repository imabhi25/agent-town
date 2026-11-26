------------------------------- MODULE MC -------------------------------
(* The model TLC checks: 2 workers and a goal of 3 tasks where t3 needs both t1 and t2, the     *)
(* smallest graph with two siblings that finish into one dependent (the race the goal lock     *)
(* prevents). The budgets are set in the .cfg files.                                           *)
EXTENDS AgentTown

CONSTANTS w1, w2   \* model values: worker names
MCWorkers == {w1, w2}
MCTasks == {"t1", "t2", "t3"}
MCDeps == [t \in MCTasks |-> IF t = "t3" THEN {"t1", "t2"} ELSE {}]
=============================================================================
