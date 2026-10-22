---
name: verify-sync-model
description: Check a change to the sync, git steps, locks or cloud inbox of retroagent against the TLA+ models in formal/sync. Use when a diff touches src/kb/sync.py, gitops.py, cloud.py, lock.py or setup.publish, or the models themselves.
---

# Verify the sync models

`formal/sync/` holds two TLA+ models and the scripts that check them. Read `formal/sync/README.md` for the
properties, bounds and configs.

## When

Run it when a diff changes one of the covered functions below. Skip it for other files.

| Model | Covered code |
| --- | --- |
| `Sync.tla` | `src/kb/sync.py`: `run_sync`, `git_gate`, `commit_own`, `publish`. `src/kb/gitops.py`: `pull`, `push`, `repair`, `unpushed_paths`, `_run`. `src/kb/lock.py`: `Lock` |
| `CloudInbox.tla` | `src/kb/cloud.py`: `push`, `import_inbox`, `delete_branches`. `src/kb/setup.py`: `publish`. `src/kb/lock.py`: `Lock` |

Last confirmed against: `2fcc23e` (main) with the fixes of issue #61 on top. Move this commit only after you read the covered code at the new commit
and confirm that each `\* src:` comment still names the right lines.

## Steps

1. Run `formal/sync/run.sh`. Every line must show the expected result.
2. If the code change alters a modelled step (an order, a check, a retry, a lock), change the matching action in the
   model first. Keep one action per atomic step of the code, and update its `\* src:` comment.
3. Run the repro scripts: `python3 formal/sync/repro/foreign_push.py` and `python3 formal/sync/repro/cloud_tail.py`.
   Exit 0 means the bug did not happen.
4. A config with an unexpected result is a finding until the trace shows a model error. Read
   `.tlc-out/<config>/trace.md`. Tell the trace in the code's words. Do not weaken a property or a bound to get a pass.

## Real bug or model error

- Real bug: a script that runs the real code shows the same steps and the same harm. Add a repro script for it.
- Model error: an action merges two steps that the code runs apart, or allows a step that the code never takes.
  Fix the model and say so in the PR.
