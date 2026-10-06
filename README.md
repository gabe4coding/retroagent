# sessions-kb

Private knowledge base of my Claude Code and Codex sessions, from every machine. Agents search it with the `kb` CLI.

## How it works
- A SessionStart hook (Claude Code and Codex) starts `kb sync` in the background. It prints nothing.
- `kb sync` turns each session that has been idle for 15 minutes into markdown (`sessions/<host>/…`), keeps a slim redacted raw copy (`raw/<host>/…`), asks Haiku for a 3-line summary, updates `catalog/<host>/…`, commits only this machine's folders, and pushes.
- Search is local: `kb` builds a SQLite FTS5 index in `.kb/` from the markdown.

## Set up a machine
```bash
gh repo clone gabe4coding/sessions-kb ~/Repositories/sessions-kb
~/Repositories/sessions-kb/install.sh
```
Then open Codex once and trust the `sessions-kb` hook with `/hooks`. `install.sh --no-sync` skips the first background sync.

Each machine needs its own `host` in the config. The default is the short hostname, so two machines with the same hostname must not both use it. Two machines with the same `host` write the same folders (`sessions/<host>/…`) and block each other. Set a different `host` in `~/.config/sessions-kb/config.json` on one of them before the first sync.

If the machine has `~/claude-tools`, also register `kb` in the local-tools plugin (symlink `~/claude-tools/plugins/local-tools/bin/kb` → `plugin/bin/kb`, README row, version bump, commit, `claude plugin update local-tools@local`).

## Use
`kb find`, `kb recent`, `kb summary`, `kb show`, `kb stats`, `kb sql`, `kb status`, `kb sync --now`. Run `kb --help`.

Those read commands open the local index read-only: they work in a sandbox where `.kb/` cannot be written and never wait for a running sync. Only a missing or outdated index must be built, which needs write access. If it cannot be built, they print `index not built yet; run: kb reindex` and exit 2. Other errors print one line, `kb: <message>`, and exit 2.

`kb sync --now --no-summaries` syncs fast. `kb backfill --summaries` writes the summaries of every session with no cap and no time limit (a normal run stops after 20 minutes). If another sync is running, `kb sync` prints `another sync is running` and exits 0.

## Config
`~/.config/sessions-kb/config.json`. Every key is optional:
`root` (`~/Repositories/sessions-kb`), `host` (short hostname; unique per machine), `quiet_minutes` (15), `debounce_minutes` (10), `summary_model` (`haiku`), `summary_cap_per_run` (30), `claude_dir` (`~/.claude/projects`), `codex_dirs` (`~/.codex/sessions`, `~/.codex/archived_sessions`), `codex_home` (`~/.codex`), `exclude_cwd_globs` (temp folders).

## Develop
`scripts/test` runs the suite on `/usr/bin/python3` with pytest from `uv`.
