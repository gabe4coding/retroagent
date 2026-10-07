# retroagent

The code of retroagent: it syncs Claude Code and Codex sessions into a private data repo the user chooses, searches
them with the `kb` CLI, and runs the cloud routine that writes project pages and weekly retros. This repo holds no
data; each user's data lives in their own data repo.

## Read first, by task
- Code, tests, skills or docs → `CODING_STANDARDS.md`. User docs: `README.md` (pitch, quick start) and
  `docs/*.mdx`. The whole repo is the Claude Code / Codex plugin (marketplace source `./`); `bin/kb` runs
  `python3 -m kb` from `src/kb/`.
- Install or setup → `skills/setup/SKILL.md` and `install.sh`.
- The cloud routine → `scripts/pages-routine.md`.
- Cloud sessions: the push from a cloud container and the import by one machine's sync → `src/kb/cloud.py`.
- The data repo: layout and rules, what `kb setup init` / `kb setup routine` push (with `{{CODE_REPO}}` filled in)
  → `templates/data/`, `AGENTS.md` there first. How much each sync commit grows a data clone → `scripts/raw-growth`
  (read-only).

## Rules
- Develop in a checkout of its own. The installed copy (`code` in `~/.config/retroagent/config.json`, usually
  `~/.retroagent`) is what every `kb` on the machine runs; change it only with `kb update`. To try new code first,
  run this checkout's `bin/kb` (it runs the code next to it); keep to read-only commands such as `find`, `show`, `sql`.
- Never write to a data clone by hand (`root` in the config). Use `kb` to read it: `kb find`, `kb summary`, `kb show`.
- Only the machine that owns a host writes its folders in a data repo (`own_paths` in `src/kb/sync.py`), summaries
  included. Never write summaries from another machine or a cloud session, never with `--force-host` unless the owner
  asks, and never with a script that calls `summarize`, `update_front_matter` or `write_catalog` directly: the
  owner's next sync then conflicts.
