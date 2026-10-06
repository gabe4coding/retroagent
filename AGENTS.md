# sessions-kb

Personal knowledge base of Claude Code and Codex sessions, synced from every machine.

## Find information (cheap first)
1. `kb find "<words>" [--project P] [--agent claude|codex] [--since 30d]` — one line per session.
2. `kb summary <id8>` — summary, decisions, files, PRs, subagents.
3. `kb show <id8> --turn N --around 1` or `--grep PATTERN` — only the part you need.

Analytics: `kb stats <report>`, `kb sql "SELECT …"`. Help: `kb --help`.

## Hard rules
- Use `kb` first. Never `cat` or read whole files in `sessions/`.
- Never read `raw/` unless the task is to fix or re-run the distiller.
- `sessions/`, `raw/` and `catalog/` are written by `kb sync`. Do not edit them by hand.

## Layout
- `sessions/<host>/<agent>/<YYYY>/<MM>/<date>_<project>_<id8>.md` — distilled session: JSON front matter, then `## [N] role · HH:MM` turns.
- `catalog/<host>/<YYYY-MM>.jsonl` — one line per session (`rg` it if `kb` is not available).
- `raw/<host>/<agent>/<YYYY>/<MM>/<id>.jsonl.gz` — slim, redacted raw transcript.
- `src/kb/` — code (Python 3.9, stdlib only). Tests: `scripts/test`.
- `plugin/` — Claude Code / Codex plugin: hook, skills, `bin/kb`.
