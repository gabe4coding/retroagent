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

If the machine has `~/claude-tools`, also register `kb` in the local-tools plugin (symlink `~/claude-tools/plugins/local-tools/bin/kb` → `plugin/bin/kb`, README row, version bump, commit, `claude plugin update local-tools@local`).

## Use
`kb find`, `kb recent`, `kb summary`, `kb show`, `kb stats`, `kb sql`, `kb status`, `kb sync --now`. Run `kb --help`.

## Config
`~/.config/sessions-kb/config.json`. Every key is optional:
`root` (`~/Repositories/sessions-kb`), `host` (short hostname), `quiet_minutes` (15), `debounce_minutes` (10), `summary_model` (`haiku`), `summary_cap_per_run` (30), `claude_dir` (`~/.claude/projects`), `codex_dirs` (`~/.codex/sessions`, `~/.codex/archived_sessions`), `codex_home` (`~/.codex`), `exclude_cwd_globs` (temp folders).

## Develop
`scripts/test` runs the suite on `/usr/bin/python3` with pytest from `uv`.
