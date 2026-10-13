---
name: verify
description: Project recipe for /verify in retroagent. Drive this checkout's kb CLI and plugin hooks end to end in a sandbox data repo with drive.py (kb commands, or headless Claude Code with the plugin), then judge by the evidence it prints. Use when you verify a change to retroagent.
---

# Verify retroagent

Two surfaces, both against one sandbox data repo:

- **The CLI.** This checkout's `bin/kb`.
- **The hooks.** Real headless Claude Code with this checkout as the plugin: SessionStart (`kb-hook`, the brief),
  PreToolUse (`kb-guard`), PostToolUseFailure (`kb-hint`), and the decide mod's `session.start`.

Report SKIP when the diff touches only tests, docs or `scripts/pages-routine.md`: there is nothing to drive. Codex
runs the same hook scripts; it is not driven here.

`drive.py` (next to this file, stdlib Python) makes the sandbox, runs the drive, prints the evidence and cleans up.
`--help` says what the sandbox holds and how runs stay off the real data repo. Never run `bin/kb` by hand for
/verify: without the sandbox's `KB_CONFIG`, it reaches the real data repo.

## Pick the drive

| Changed | Drive |
| --- | --- |
| A kb command (`src/kb/`) | `kb` with that command |
| The sync, gitops, redaction | `new --no-sync`, then `kb <dir> -- sync --now --no-summaries`; judge `data` |
| `bin/kb-hook`, the brief | `hook` with any prompt; read the SessionStart answer in `hookEvents` |
| `bin/kb-guard` | `hook` with a prompt that runs the command to deny or allow |
| `bin/kb-hint`, `src/kb/hint.py` | `hook` with a Bash command that fails with an error the seed page knows |
| A skill | `hook` with the slash command as the prompt (`/retroagent:kb-search flaky test`) |
| `hooks/decide.tsx` | `claude plugin validate .` and `claude plugin test .`; `hook` shows the mod load and its `kb decide --json` run in `pluginLog` |

## Commands

```bash
python3 .claude/skills/verify/drive.py new            # prints dir
python3 .claude/skills/verify/drive.py kb <dir> -- find flaky
python3 .claude/skills/verify/drive.py hook <dir> 'Run exactly this Bash command: ...' '<second prompt>'
python3 .claude/skills/verify/drive.py clean <dir>    # after you read the evidence
```

Make one sandbox per /verify and pass its folder to each run. Judge by `hookEvents`, `pluginLog`, `hintLog` and
`data`, never by the reply alone. One Haiku turn costs about $0.01.

## Probes

Checked on 2026-10-13, all from one sandbox:
- `kb <dir> -- find flaky` → the `demo-app` page and session `55555555`.
- SessionStart → the page pointer, `[s-000001]`, and "1 retro suggestion waits".
- `Run exactly this Bash command: kb decide accept s-000002 --yes` → `kb-guard` denies it.
- `Run exactly this Bash command: sh -c 'echo "ModuleNotFoundError: No module named yaml" >&2; exit 1'` →
  `kb-hint` adds the `pip install -e .[dev]` fix, and `hintLog` has one line.
- No change in `data.log` after a hook run with `auto_sync: false`.

## Gotchas

- Your own installed `kb-guard` also checks your Bash calls. A copy older than the tokenizing guard (PR #49) denies a
  drive command that holds `kb decide accept|reject`, even in quotes. Run `kb update` first.
- Managed hooks run too (`--setting-sources project,local` drops only user settings), so `hookEvents` can hold
  their answers. `pluginLog` names the retroagent hooks (`bin/kb-hook`, `bin/kb-guard`, `bin/kb-hint`).
- A `kb` run gets the sandbox `HOME`; a hook session keeps the real `HOME` for the login. So `kb-hint` in a hook can
  use the local embedding model (`method: semantic`), and a `kb hint` run is keyword only.
- The SessionStart sync starts only with `new --config '{"auto_sync": true, "debounce_minutes": 0}'`: the first sync
  of `new` writes `last-ok`. The sync runs in the background after the hook. Read `kb <dir> -- status` later.
- The model can change a command or skip a step. Phrase prompts as "Run exactly this ...". If a step did not happen,
  run again before you call it a failure.
