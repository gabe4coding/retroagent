---------------------------- MODULE CloudInbox ----------------------------
(* retroagent cloud sessions. A cloud session runs MaxTurns turns. After each  *)
(* turn a Stop hook, and at the end a SessionEnd hook, start `kb cloud push`   *)
(* in the background. One machine imports the inbox branch, then deletes it    *)
(* with --force-with-lease. The container is deleted when the session ends,    *)
(* so a transcript tail that no push sent is gone.                             *)
(* Each action is one atomic step of the real code; `\* src:` names it.        *)
EXTENDS Integers

CONSTANTS MaxTurns,     \* turns of the session; each ends with a Stop hook
          PushRounds,   \* src: src/kb/cloud.py:44 PUSH_ROUNDS
          ImportRuns,   \* syncs of the importing machine
          WaitForLock,  \* FIX toggle: a push that finds the lock busy waits for it
          UseLease,     \* src: src/kb/cloud.py:304-312 --force-with-lease (FALSE = bug toggle)
          ExitWindow    \* TRUE: the last fingerprint read and the lock release are two steps (as in
                        \* the code); FALSE merges them, so TLC must find the rounds-exhausted variant

Procs  == 1..(MaxTurns + 1)          \* one hook process per turn, plus SessionEnd
Absent == -1                         \* the inbox branch does not exist
Max(a, b) == IF a >= b THEN a ELSE b

VARIABLES len,        \* records in the transcript (one per turn)
          ended,      \* SessionEnd fired
          spawned,    \* hook processes started so far
          pc,         \* phase of each hook process
          seen,       \* transcript length a process read before its push
          round,      \* rounds a process used
          lock,       \* 0 or the process that holds .kb/cloud-push.lock
          pushedFp,   \* .kb/cloud-pushed.json: the length last pushed
          branch,     \* transcript length on the inbox branch, or Absent
          tip,        \* tip version of the inbox branch (one per push)
          ipc,        \* importing machine: "idle" or "fetched"
          fetchedTip, fetchedLen,
          imported,   \* longest copy the importing machine holds
          runs
vars == <<len, ended, spawned, pc, seen, round, lock, pushedFp, branch, tip,
          ipc, fetchedTip, fetchedLen, imported, runs>>

TypeOK ==
    /\ len \in 0..MaxTurns /\ ended \in BOOLEAN /\ spawned \in 0..(MaxTurns + 1)
    /\ pc \in [Procs -> {"none", "start", "read", "push", "release", "done"}]
    /\ seen \in [Procs -> 0..MaxTurns] /\ round \in [Procs -> 0..PushRounds]
    /\ lock \in 0..(MaxTurns + 1) /\ pushedFp \in 0..MaxTurns
    /\ branch \in {Absent} \cup 0..MaxTurns /\ ipc \in {"idle", "fetched"}
    /\ imported \in 0..MaxTurns /\ runs \in 0..ImportRuns

Init ==
    /\ len = 0 /\ ended = FALSE /\ spawned = 0
    /\ pc = [p \in Procs |-> "none"] /\ seen = [p \in Procs |-> 0] /\ round = [p \in Procs |-> 0]
    /\ lock = 0 /\ pushedFp = 0 /\ branch = Absent /\ tip = 0
    /\ ipc = "idle" /\ fetchedTip = 0 /\ fetchedLen = 0 /\ imported = 0 /\ runs = 0

Spawn == /\ spawned' = spawned + 1
         /\ pc' = [pc EXCEPT ![spawned + 1] = "start"]

importer == <<ipc, fetchedTip, fetchedLen, imported, runs>>

\* src: src/kb/cloud.py:147-162 hook(), run by the Stop hook after each turn
Turn ==
    /\ ~ended /\ len < MaxTurns
    /\ len' = len + 1 /\ Spawn
    /\ UNCHANGED <<ended, seen, round, lock, pushedFp, branch, tip>> /\ UNCHANGED importer

\* src: src/kb/cloud.py:45 HOOK_EVENTS, the SessionEnd hook
End ==
    /\ ~ended /\ len >= 1
    /\ ended' = TRUE /\ Spawn
    /\ UNCHANGED <<len, seen, round, lock, pushedFp, branch, tip>> /\ UNCHANGED importer

\* src: src/kb/cloud.py:117-120 lock.acquire(wait=True)
Acquire(p) ==
    /\ pc[p] = "start" /\ lock = 0
    /\ lock' = p /\ pc' = [pc EXCEPT ![p] = "read"]
    /\ UNCHANGED <<len, ended, spawned, seen, round, pushedFp, branch, tip>> /\ UNCHANGED importer

\* src: src/kb/cloud.py:117-120 before the wait fix: return "another push is running"
GiveUp(p) ==
    /\ ~WaitForLock /\ pc[p] = "start" /\ lock # 0
    /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<len, ended, spawned, seen, round, lock, pushedFp, branch, tip>> /\ UNCHANGED importer

\* src: src/kb/cloud.py:128-132 for _ in range(PUSH_ROUNDS): fingerprint; equal -> break
Read(p) ==
    /\ pc[p] = "read"
    /\ IF round[p] = PushRounds \/ pushedFp = len
          THEN /\ pc' = [pc EXCEPT ![p] = IF ExitWindow THEN "release" ELSE "done"]
               /\ lock' = (IF ExitWindow THEN lock ELSE 0) /\ UNCHANGED seen
          ELSE pc' = [pc EXCEPT ![p] = "push"] /\ seen' = [seen EXCEPT ![p] = len] /\ UNCHANGED lock
    /\ UNCHANGED <<len, ended, spawned, round, pushedFp, branch, tip>> /\ UNCHANGED importer

\* src: src/kb/cloud.py:133-141 slim + setup.publish (src/kb/setup.py:143-165), record the fingerprint
Push(p) ==
    /\ pc[p] = "push"
    /\ branch' = seen[p] /\ tip' = tip + 1 /\ pushedFp' = seen[p]
    /\ round' = [round EXCEPT ![p] = round[p] + 1] /\ pc' = [pc EXCEPT ![p] = "read"]
    /\ UNCHANGED <<len, ended, spawned, seen, lock>> /\ UNCHANGED importer

\* src: src/kb/cloud.py:143-144 finally: lock.release()
Release(p) ==
    /\ pc[p] = "release"
    /\ lock' = 0 /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<len, ended, spawned, seen, round, pushedFp, branch, tip>> /\ UNCHANGED importer

\* src: src/kb/cloud.py:267-301 import_inbox (fetch --prune, copy a longer blob)
Fetch ==
    /\ ipc = "idle" /\ runs < ImportRuns /\ branch # Absent
    /\ ipc' = "fetched" /\ runs' = runs + 1
    /\ fetchedTip' = tip /\ fetchedLen' = branch /\ imported' = Max(imported, branch)
    /\ UNCHANGED <<len, ended, spawned, pc, seen, round, lock, pushedFp, branch, tip>>

\* src: src/kb/sync.py:782-783 after a clean push; src/kb/cloud.py:304-312 delete_branches
Delete ==
    /\ ipc = "fetched"
    /\ ipc' = "idle"
    /\ branch' = (IF ~UseLease \/ tip = fetchedTip THEN Absent ELSE branch)
    /\ UNCHANGED <<len, ended, spawned, pc, seen, round, lock, pushedFp, tip, fetchedTip, fetchedLen, imported, runs>>

\* src: src/kb/sync.py:782 a run with errors deletes nothing
SkipDelete ==
    /\ ipc = "fetched" /\ ipc' = "idle"
    /\ UNCHANGED <<len, ended, spawned, pc, seen, round, lock, pushedFp, branch, tip, fetchedTip, fetchedLen, imported, runs>>

ProcStep(p) == Acquire(p) \/ GiveUp(p) \/ Read(p) \/ Push(p) \/ Release(p)
Quiescent == ended /\ \A p \in Procs : pc[p] \in {"none", "done"}
Terminated == Quiescent /\ ipc = "idle" /\ UNCHANGED vars

Next == Turn \/ End \/ (\E p \in Procs : ProcStep(p)) \/ Fetch \/ Delete \/ SkipDelete \/ Terminated
Spec == Init /\ [][Next]_vars /\ \A p \in Procs : WF_vars(ProcStep(p))

-----------------------------------------------------------------------------
Kept == Max(IF branch = Absent THEN 0 ELSE branch, imported)

\* (1) No lost session history: once the session ended and every hook finished,
\*     the inbox branch or the importing machine holds the whole transcript.
NoLostHistory == Quiescent => Kept = len

\* (4) Every started hook push finishes (pushes, gives up, or reports).
PushFinishes == \A p \in Procs : (pc[p] = "start") ~> (pc[p] = "done")

\* Sanity (must FAIL): a full session imported and finished is reachable.
NeverAllImported == ~(Quiescent /\ len = MaxTurns /\ imported = MaxTurns)
=============================================================================
