# retroagent

The code of retroagent: it syncs Claude Code and Codex sessions into a private data repo the user chooses, searches
them with the `kb` CLI, and runs the cloud routine that writes project pages and weekly retros. This repo holds no
data; each user's data lives in their own data repo (its layout and rules: `templates/data/AGENTS.md`).

## Layout
- `src/kb/` — the Python package (3.9, stdlib only). `kb` runs it: `bin/kb` → `python3 -m kb`.
- `.claude-plugin/`, `.codex-plugin/`, `bin/`, `hooks/`, `skills/` — the whole repo is the Claude Code / Codex plugin
  (marketplace source `./`). `skills/setup` drives `install.sh` and `kb setup`.
- `install.sh` — sets up a machine from a clone of this repo; `--repo` names the data repo.
- `templates/data/` — files `kb setup init` / `kb setup routine` push to a data repo (base files, `pages/config.json`,
  the trigger workflow with `{{CODE_REPO}}`).
- `scripts/pages-routine.md` — what the cloud routine does; `scripts/codex_marketplace.py` — Codex marketplace entry.
- `tests/` — `scripts/test` runs them on `/usr/bin/python3` with pytest from `uv`.

## Rules
- Run `scripts/test` before every commit. Tests never touch the real home, config or data clone (`tests/conftest.py`).
- Never put real session data, real project or company names, or real ids in tests or docs: this repo is public.
- When `skills/` or `hooks/` change, bump `version` in both `.claude-plugin/plugin.json` and
  `.codex-plugin/plugin.json` (tests check they agree): Claude Code and Codex cache plugins per version.
- Develop in a checkout of its own. The installed copy (`code` in `~/.config/retroagent/config.json`, usually
  `~/.retroagent`) is what every `kb` on the machine runs; change it only with `kb update`.
- Never write to a data clone by hand (`root` in the config). Use `kb` to read it: `kb find`, `kb summary`, `kb show`.
- Only the machine that owns a host writes its folders in a data repo, summaries included. Never write summaries
  from another machine or a cloud session, never with `--force-host` unless the owner asks, and never with a script
  that calls `summarize`, `update_front_matter` or `write_catalog` directly: the owner's next sync then conflicts.
