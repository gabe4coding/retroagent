# retroagent data

Knowledge base of Claude Code and Codex sessions, synced from every machine by [retroagent](https://github.com/gabe4coding/retroagent).

## Find information (cheap first)
0. `kb page <project>` — the project's page (state, decisions, files, errors → fixes, open threads); `kb page` lists
   pages, `kb page 2026-W41` is a weekly retro, `--section "Key decisions"` prints one part.
1. `kb find "<words>" [--project P] [--agent claude|codex] [--since 30d]` — one line per page, memory or session.
   `kb memory [ref]` lists the memory files agents kept (all machines) or prints one.
2. `kb summary <short>` — summary, decisions, files, PRs, subagents.
3. `kb show <short> --turn N --around 1` or `--grep REGEX` (a regular expression) — only the part you need.

`<short>` is the 8-character id shown in every list: the last 8 characters of the session id, without dashes (the
first 8 of a Codex id are a timestamp).

Analytics: `kb stats <report>`, `kb sql "SELECT …"`. Help: `kb --help`.

Reading needs no write access, except to build a missing or outdated index. If `kb` says
`index not built yet; run: kb reindex`, run `kb reindex` once from a shell that can write to the data clone.
`kb sync --now --no-summaries` brings the KB up to date fast.

## Hard rules
- Use `kb` first. Never `cat` or read whole files in `sessions/`.
- Never read `raw/` unless the task is to fix or re-run the distiller.
- `sessions/`, `raw/`, `catalog/` and `memories/` are written by `kb sync`. Do not edit them by hand.
- `pages/` is written by the cloud routine through `kb pages finish`. Only `pages/config.json` is edited by hand.
- The sync works in its own data clone (the `root` of `~/.config/retroagent/config.json`). Do not edit or commit there
  by hand. The code lives in the retroagent repo; change it there.
- Only the machine that owns a host writes `sessions/<host>`, `raw/<host>`, `catalog/<host>` and `memories/<host>`,
  summaries included. Make summaries on that machine with `kb backfill --summaries`; never from another machine or a
  cloud session, never with `--force-host` unless the owner asks. Otherwise the owner's next sync conflicts.
- If every sync fails with `run: kb repair`, run `kb repair` on that machine, then `kb sync --now --no-summaries`.

## Layout
- `sessions/<host>/<agent>/<YYYY>/<MM>/<date>_<project>_<short>.md` — distilled session: JSON front matter, then
  `## [N] role · HH:MM` turns.
- `catalog/<host>/<YYYY-MM>.jsonl` — one line per session (`rg` it if `kb` is not available).
- `memories/<host>/claude/<encoded cwd>/<file>.md`, `memories/<host>/codex/<file>.md` — copies of the memory files.
- `pages/projects/<project>.md`, `pages/retro/<YYYY-Www>.md` — written by the cloud routine; `pages/config.json` its
  settings, `pages/.state.json` its watermark.
- `raw/<host>/<agent>/<YYYY>/<MM>/<id>.jsonl.gz` — slim, redacted raw transcript.
