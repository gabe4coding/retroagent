# sessions-kb

Personal knowledge base of Claude Code and Codex sessions, synced from every machine.

## Find information (cheap first)
0. `kb page <project>` — the project's page (state, decisions, files, errors → fixes, open threads); `kb page` lists
   pages, `kb page 2026-W41` is a weekly retro, `--section "Key decisions"` prints one part.
1. `kb find "<words>" [--project P] [--agent claude|codex] [--since 30d]` — one line per page or session.
2. `kb summary <short>` — summary, decisions, files, PRs, subagents.
3. `kb show <short> --turn N --around 1` or `--grep REGEX` (a regular expression) — only the part you need.

`<short>` is the 8-character id shown in every list: the last 8 characters of the session id, without dashes (the first 8 of a Codex id are a timestamp).

Analytics: `kb stats <report>`, `kb sql "SELECT …"`. Help: `kb --help`.

Reading needs no write access, except to build a missing or outdated index. If `kb` says `index not built yet; run: kb reindex`, run `kb reindex` once from a shell that can write to the KB folder. `kb sync --now --no-summaries` brings the KB up to date fast.

## Hard rules
- Use `kb` first. Never `cat` or read whole files in `sessions/`.
- Never read `raw/` unless the task is to fix or re-run the distiller.
- `sessions/`, `raw/` and `catalog/` are written by `kb sync`. Do not edit them by hand.
- `pages/` is written by the cloud routine (`scripts/pages-routine.md`) through `kb pages finish`. Only
  `pages/config.json` is edited by hand.
- The sync works in its own data clone (`~/.sessions-kb`). Do not edit or commit there by hand; change code in a separate checkout.

## Layout
- `sessions/<host>/<agent>/<YYYY>/<MM>/<date>_<project>_<short>.md` — distilled session: JSON front matter, then `## [N] role · HH:MM` turns.
- `catalog/<host>/<YYYY-MM>.jsonl` — one line per session (`rg` it if `kb` is not available).
- `pages/projects/<project>.md`, `pages/retro/<YYYY-Www>.md` — pages written from the sessions by the cloud routine;
  `pages/config.json` its settings, `pages/.state.json` its watermark.
- `raw/<host>/<agent>/<YYYY>/<MM>/<id>.jsonl.gz` — slim, redacted raw transcript.
- `src/kb/` — code (Python 3.9, stdlib only). Tests: `scripts/test`.
- `plugin/` — Claude Code / Codex plugin: hook, skills, `bin/kb`.
- `scripts/pages-routine.md` — what the routine does; `.github/workflows/pages-trigger.yml` fires it on each push.
