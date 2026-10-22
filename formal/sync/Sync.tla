------------------------------- MODULE Sync -------------------------------
(* retroagent `kb sync` on several machines that share one data repo.       *)
(* Each machine owns one host folder. One run: lock, git gate, write        *)
(* sessions, commit own folders, pull --rebase, foreign-path check, push    *)
(* with retries. The environment can crash a run, hang a git call, leave a  *)
(* stale index.lock, commit another host's files by hand in a clone, and    *)
(* push a hand edit of a host's files (kb backfill --force-host).           *)
(* Each action is one atomic step of the real code; `\* src:` names it.     *)
EXTENDS Integers, FiniteSets

CONSTANTS Machines,        \* model values; machine m owns host folder m
          MaxSessions,     \* sessions each machine writes
          Runs,            \* sync runs per machine
          Tries,           \* src: src/kb/gitops.py:479 push(tries=3)
          PushChecked,     \* FIX toggle: each push attempt sends only commits it checked
          UseFlock,        \* TRUE: src/kb/lock.py flock; FALSE: bug toggle, a lock file nobody takes over
          AbortOnConflict, \* TRUE: src/kb/gitops.py:207-212 rebase --abort; FALSE: bug toggle, reset to upstream
          GitTimeouts,     \* TRUE: src/kb/gitops.py:97-109 every git call has a timeout; FALSE: bug toggle
          UserCommits      \* TRUE: a person may commit another host's files in a clone

Rec(a, h, s) == [by |-> a, host |-> h, sid |-> s]
NoRec == Rec("none", "none", 0)
Phases == {"idle", "gate", "write", "commit", "pull", "check", "push", "retry", "release"}
GitPhases == {"pull", "push", "retry"}

VARIABLES pc, lockSt, ilock, hung, produced, wt, loc, snap, base, rv, remote,
          conflict, tries, runs, outcome, syncPushed, env
vars == <<pc, lockSt, ilock, hung, produced, wt, loc, snap, base, rv, remote,
          conflict, tries, runs, outcome, syncPushed, env>>

TypeOK ==
    /\ pc \in [Machines -> Phases]
    /\ lockSt \in [Machines -> {"free", "held", "stale"}]
    /\ ilock \in [Machines -> BOOLEAN] /\ hung \in [Machines -> BOOLEAN]
    /\ produced \in [Machines -> 0..MaxSessions]
    /\ outcome \in [Machines -> {"none", "ok", "reported", "lockedout"}]

Init ==
    /\ pc = [m \in Machines |-> "idle"] /\ lockSt = [m \in Machines |-> "free"]
    /\ ilock = [m \in Machines |-> FALSE] /\ hung = [m \in Machines |-> FALSE]
    /\ produced = [m \in Machines |-> 0] /\ wt = [m \in Machines |-> {}]
    /\ loc = [m \in Machines |-> {}] /\ snap = [m \in Machines |-> {}]
    /\ base = [m \in Machines |-> 0] /\ rv = 0 /\ remote = {}
    /\ conflict = [m \in Machines |-> FALSE] /\ tries = [m \in Machines |-> 0]
    /\ runs = [m \in Machines |-> 0] /\ outcome = [m \in Machines |-> "none"]
    /\ syncPushed = {} /\ env = [crash |-> 0, user |-> 0, hand |-> 0, hang |-> 0]

Go(m, ph) == pc' = [pc EXCEPT ![m] = ph]
Report(m) == outcome' = [outcome EXCEPT ![m] = "reported"] /\ Go(m, "release")
Foreign(m, S) == {r \in S : r.host # m}

\* src: src/kb/sync.py:730-733 Lock(...).acquire(); locked_out
Start(m) ==
    /\ pc[m] = "idle" /\ runs[m] < Runs
    /\ runs' = [runs EXCEPT ![m] = runs[m] + 1]
    /\ IF lockSt[m] = "free"
          THEN lockSt' = [lockSt EXCEPT ![m] = "held"] /\ Go(m, "gate") /\ UNCHANGED outcome
          ELSE outcome' = [outcome EXCEPT ![m] = "lockedout"] /\ UNCHANGED <<lockSt, pc>>
    /\ UNCHANGED <<ilock, hung, produced, wt, loc, snap, base, rv, remote, conflict, tries, syncPushed, env>>

\* src: src/kb/sync.py:473-493 git_gate -> gitops.repair -> _stale_index_lock
Gate(m) ==
    /\ pc[m] = "gate"
    /\ IF ilock[m] THEN Report(m) ELSE Go(m, "write") /\ UNCHANGED outcome
    /\ UNCHANGED <<lockSt, ilock, hung, produced, wt, loc, snap, base, rv, remote, conflict, tries, runs, syncPushed, env>>

\* src: src/kb/sync.py:754 _process_lanes (writes sessions/<host>/...)
Write(m) ==
    /\ pc[m] = "write"
    /\ IF produced[m] < MaxSessions
          THEN produced' = [produced EXCEPT ![m] = produced[m] + 1]
               /\ wt' = [wt EXCEPT ![m] = wt[m] \cup {produced[m] + 1}]
          ELSE UNCHANGED <<produced, wt>>
    /\ Go(m, "commit")
    /\ UNCHANGED <<lockSt, ilock, hung, loc, snap, base, rv, remote, conflict, tries, runs, outcome, syncPushed, env>>

\* src: src/kb/sync.py:503-552 commit_own (stages and commits own_paths only)
Commit(m) ==
    /\ pc[m] = "commit"
    /\ loc' = [loc EXCEPT ![m] = loc[m] \cup {Rec("sync", m, s) : s \in wt[m]}]
    /\ wt' = [wt EXCEPT ![m] = {}]
    /\ Go(m, "pull")
    /\ UNCHANGED <<lockSt, ilock, hung, produced, snap, base, rv, remote, conflict, tries, runs, outcome, syncPushed, env>>

\* src: src/kb/gitops.py:181-218 pull: one `git pull --rebase`. It conflicts
\* when a hand edit of this host's files meets our own commits.
PullStep(m, next) ==
    /\ ~hung[m]
    /\ IF conflict[m] /\ \E r \in loc[m] : r.by = "sync"
          THEN /\ loc' = [loc EXCEPT ![m] = IF AbortOnConflict THEN loc[m] ELSE {}]
               /\ Report(m) /\ UNCHANGED <<base, conflict>>
          ELSE /\ base' = [base EXCEPT ![m] = rv] /\ conflict' = [conflict EXCEPT ![m] = FALSE]
               /\ Go(m, next) /\ UNCHANGED <<loc, outcome>>

\* src: src/kb/sync.py:555-573 publish -> gitops.pull
Pull(m) ==
    /\ pc[m] = "pull" /\ PullStep(m, "check")
    /\ UNCHANGED <<lockSt, ilock, hung, produced, wt, snap, rv, remote, tries, runs, syncPushed, env>>

\* src: src/kb/sync.py:582-585 foreign_commits, run by gitops.push before each attempt (gitops.py:491-498)
Check(m) ==
    /\ pc[m] = "check"
    /\ IF loc[m] = {}
          THEN outcome' = [outcome EXCEPT ![m] = "ok"] /\ Go(m, "release") /\ UNCHANGED snap
          ELSE IF Foreign(m, loc[m]) # {}
                  THEN Report(m) /\ UNCHANGED snap
                  ELSE snap' = [snap EXCEPT ![m] = loc[m]] /\ Go(m, "push") /\ UNCHANGED outcome
    /\ UNCHANGED <<lockSt, ilock, hung, produced, wt, loc, base, rv, remote, conflict, tries, runs, syncPushed, env>>

\* src: src/kb/gitops.py:479-512 push: `git push` of the branch (code) or of
\* the checked commits (fix). The remote ref update is a compare-and-swap.
Push(m) ==
    /\ pc[m] = "push" /\ ~hung[m]
    /\ LET S == IF PushChecked THEN snap[m] ELSE loc[m] IN
       IF base[m] = rv
          THEN /\ remote' = remote \cup S /\ rv' = rv + 1
               /\ base' = [base EXCEPT ![m] = rv + 1]
               /\ loc' = [loc EXCEPT ![m] = loc[m] \ S]
               /\ syncPushed' = syncPushed \cup {<<m, r>> : r \in S}
               /\ outcome' = [outcome EXCEPT ![m] = "ok"] /\ Go(m, "release")
               /\ UNCHANGED tries
          ELSE /\ UNCHANGED <<remote, rv, base, loc, syncPushed>>
               /\ IF tries[m] + 1 < Tries
                     THEN tries' = [tries EXCEPT ![m] = tries[m] + 1] /\ Go(m, "retry") /\ UNCHANGED outcome
                     ELSE Report(m) /\ UNCHANGED tries
    /\ UNCHANGED <<lockSt, ilock, hung, produced, wt, snap, conflict, runs, env>>

\* src: src/kb/gitops.py:504-509 rejected push -> pull(), then a new attempt (before the fix: no new check)
Retry(m) ==
    /\ pc[m] = "retry" /\ PullStep(m, IF PushChecked THEN "check" ELSE "push")
    /\ UNCHANGED <<lockSt, ilock, hung, produced, wt, snap, rv, remote, tries, runs, syncPushed, env>>

\* src: src/kb/sync.py:798-799 finally: lock.release()
Release(m) ==
    /\ pc[m] = "release"
    /\ lockSt' = [lockSt EXCEPT ![m] = "free"] /\ tries' = [tries EXCEPT ![m] = 0] /\ Go(m, "idle")
    /\ UNCHANGED <<ilock, hung, produced, wt, loc, snap, base, rv, remote, conflict, runs, outcome, syncPushed, env>>

\* src: src/kb/gitops.py:101-105 TimeoutExpired -> GitError, reported by publish
Timeout(m) ==
    /\ GitTimeouts /\ hung[m]
    /\ hung' = [hung EXCEPT ![m] = FALSE] /\ Report(m)
    /\ UNCHANGED <<lockSt, ilock, produced, wt, loc, snap, base, rv, remote, conflict, tries, runs, syncPushed, env>>

\* ---------------------------------------------------------------- environment
\* src: src/kb/lock.py:11-15 the run dies (kill, reboot). flock frees the lock;
\* a killed git write leaves .git/index.lock behind.
Crash(m) ==
    /\ pc[m] # "idle" /\ env.crash < 1
    /\ lockSt' = [lockSt EXCEPT ![m] = IF UseFlock THEN "free" ELSE "stale"]
    /\ ilock' = [ilock EXCEPT ![m] = ilock[m] \/ pc[m] \in {"commit", "pull", "retry"}]
    /\ hung' = [hung EXCEPT ![m] = FALSE] /\ tries' = [tries EXCEPT ![m] = 0] /\ Go(m, "idle")
    /\ env' = [env EXCEPT !.crash = @ + 1]
    /\ UNCHANGED <<produced, wt, loc, snap, base, rv, remote, conflict, runs, outcome, syncPushed>>

\* A network git call stops answering.
Hang(m) ==
    /\ pc[m] \in GitPhases /\ ~hung[m] /\ env.hang < 1
    /\ hung' = [hung EXCEPT ![m] = TRUE] /\ env' = [env EXCEPT !.hang = @ + 1]
    /\ UNCHANGED <<pc, lockSt, ilock, produced, wt, loc, snap, base, rv, remote, conflict, tries, runs, outcome, syncPushed>>

\* A person commits another host's files in this clone (kb backfill --force-host, then git commit).
UserCommit(m) ==
    /\ UserCommits /\ env.user < 1
    /\ \E h \in Machines \ {m} : loc' = [loc EXCEPT ![m] = loc[m] \cup {Rec("user", h, 0)}]
    /\ env' = [env EXCEPT !.user = @ + 1]
    /\ UNCHANGED <<pc, lockSt, ilock, hung, produced, wt, snap, base, rv, remote, conflict, tries, runs, outcome, syncPushed>>

\* A person pushes a hand edit of host h's files from elsewhere.
HandPush(h) ==
    /\ env.hand < 1
    /\ remote' = remote \cup {Rec("hand", h, 0)} /\ rv' = rv + 1
    /\ conflict' = [conflict EXCEPT ![h] = TRUE] /\ env' = [env EXCEPT !.hand = @ + 1]
    /\ UNCHANGED <<pc, lockSt, ilock, hung, produced, wt, loc, snap, base, tries, runs, outcome, syncPushed>>

\* src: src/kb/gitops.py:221-231 a person removes a stale index.lock, as the report asks.
RemoveIndexLock(m) ==
    /\ ilock[m] /\ ilock' = [ilock EXCEPT ![m] = FALSE]
    /\ UNCHANGED <<pc, lockSt, hung, produced, wt, loc, snap, base, rv, remote, conflict, tries, runs, outcome, syncPushed, env>>

RunStep(m) == Gate(m) \/ Write(m) \/ Commit(m) \/ Pull(m) \/ Check(m) \/ Push(m) \/ Retry(m) \/ Release(m) \/ Timeout(m)
EnvStep == \E m \in Machines : Crash(m) \/ Hang(m) \/ UserCommit(m) \/ HandPush(m) \/ RemoveIndexLock(m)
Terminated == (\A m \in Machines : pc[m] = "idle") /\ UNCHANGED vars

Next == (\E m \in Machines : Start(m) \/ RunStep(m)) \/ EnvStep \/ Terminated
Spec == Init /\ [][Next]_vars /\ \A m \in Machines : WF_vars(RunStep(m))

-----------------------------------------------------------------------------
\* (1) No lost session history: each written session is in the working tree,
\*     in a local commit, or on the remote.
NoLostHistory == \A m \in Machines : \A s \in 1..produced[m] :
    s \in wt[m] \/ Rec("sync", m, s) \in loc[m] \cup remote

\* (2) The sync of machine m never pushes another host's files.
NoForeignPush == \A x \in syncPushed : x[2].host = x[1]

\* (3) A lock left by a dead run is always recovered.
StaleLockRecovered == \A m \in Machines : (lockSt[m] # "free") ~> (lockSt[m] = "free")

\* (4) Every sync run that took the lock finishes (ok or with a report).
SyncFinishes == \A m \in Machines : (pc[m] # "idle") ~> (pc[m] = "idle")


\* Sanity (must FAIL): every session of every machine reaches the remote.
NeverAllPushed == ~(\A m \in Machines : produced[m] = MaxSessions /\ \A s \in 1..MaxSessions : Rec("sync", m, s) \in remote)
=============================================================================
