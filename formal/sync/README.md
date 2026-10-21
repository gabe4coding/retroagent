# Sync models (TLA+)

Two small TLA+ models of the sync. TLC checks every order of steps inside the bounds below. A model is not the code:
each finding counts only after a script in `repro/` shows it with the real code.

| Model | Code | Actors |
| --- | --- | --- |
| `Sync.tla` | `src/kb/sync.py` (`run_sync`, `commit_own`, `publish`), `src/kb/gitops.py` (`pull`, `push`, `repair`), `src/kb/lock.py` | 2 machines, one host folder each, one data repo |
| `CloudInbox.tla` | `src/kb/cloud.py` (`push`, `import_inbox`, `delete_branches`), `src/kb/setup.py` (`publish`) | 1 cloud session with its hooks, 1 importing machine |

Each action has a `\* src: path:lines` comment. One action is one atomic step of the code: one git call, or the
code between two of them.

## Properties

1. **No lost session history.** Every written session stays in the working tree, a local commit or the remote. A
   cloud transcript is on its inbox branch or on the importing machine when the session ends.
2. **No push of another host's files.** The sync of a machine never pushes a file outside its own host folders.
3. **A stale lock is always recovered.** A lock that a dead run holds becomes free again.
4. **Every sync finishes or reports.** A run that takes the lock always ends, with success or an error in its report.

## Run

```bash
formal/sync/run.sh                     # every config, about 1 minute
formal/sync/run.sh SyncMC CloudMC      # some configs
```

`run.sh` needs Java 11+ (or `JAVA_HOME`) and curl. It downloads `tla2tools.jar` v1.8.0 and two scripts of
[opum-ai/proof-skills](https://github.com/opum-ai/proof-skills) (MIT, commit `578749f`): `tlc.sh` and
`parse_tlc_trace.py`. It checks each file by sha256 before use. The jar sha256 is the `digest` that GitHub shows for
the release asset. Each run writes `.tlc-out/<config>/` with `tlc.log` and, on a failure, `trace.md`: the steps of
the counterexample with their code lines.

## Configs

Each model has three kinds of config, after the proof-skills rule. The fixed config passes. A bug config turns one
behaviour on and fails. The sanity config fails, so the checked states include the goal state.

| Config | Turns on | Expect |
| --- | --- | --- |
| `SyncMC` | push only checked commits (`PushChecked`) | pass |
| `SyncMC_unchecked` | the push on `main`: it sends the branch, not the checked commits | fail: property 2 |
| `SyncMC_bug_abort` | reset to the upstream after a rebase conflict | fail: property 1 |
| `SyncMC_bug_lock` | a lock file with no takeover, not `flock` | fail: property 3 |
| `SyncMC_bug_timeout` | git calls without a timeout | fail: property 4 |
| `SyncMC_sanity` | `NeverAllPushed` | fail |
| `CloudMC` | a busy push lock makes the next hook wait (`WaitForLock`) | pass |
| `CloudMC_giveup` | the hook on `main`: it gives up when the lock is busy | fail: property 1 |
| `CloudMC_giveup_rounds` | the same, with the short exit window merged into one step | fail: property 1 |
| `CloudMC_nolease` | delete the inbox branch without `--force-with-lease` | fail: property 1 |
| `CloudMC_sanity` | `NeverAllImported` | fail |

## Bounds

- `Sync.tla`: 2 machines, 2 sessions per machine, 2 runs per machine, 3 push tries (`gitops.push`). The environment
  does each fault at most once: a crash, a git hang, a hand commit of another host's files, a hand push to a host.
- `CloudInbox.tla`: 4 turns, 3 push rounds (`cloud.PUSH_ROUNDS`), 5 hook processes, 2 importer runs.

## Assumptions and gaps

- The remote branch update is atomic (compare and swap). A rebase conflicts only on a hand edit of this host's files.
- Fairness covers the steps of a running sync or hook only. Crashes, hangs and people get no fairness.
- A hook process runs to its end before the cloud container goes away.
- Not modelled: gitleaks quarantine and the stash, the index, summaries, the pages routine, `kb repair`, and
  sessions that change after they were written.
