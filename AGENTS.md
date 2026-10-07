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
- `scripts/pages-routine.md` — what the cloud routine does; `scripts/codex_marketplace.py` — Codex marketplace
  entry; `scripts/raw-growth` — read-only report of how much each sync commit grows a data clone.
- `tests/` — `scripts/test` runs the fast ones in parallel on `/usr/bin/python3` with pytest from `uv`;
  `scripts/test --all` runs them all. Shared builders live in
  `tests/fixtures.py`: fake Claude/Codex transcripts (`write_claude_session`, `write_codex_unit`, `make_*_tree`),
  `age()` to make files look idle, git remotes and clones (`init_remote`, `clone`), and `make_config`. A session's
  short id is the last 8 hex digits of its id, so fixture ids must differ there.

## Rules
- Run `scripts/test --all` before every commit; plain `scripts/test` (under 5 s) while you work. Tests never touch
  the real home, config or data clone (`tests/conftest.py`). There is no CI: `scripts/test --all` is the only check.
  It already passes `-q`; adding another `-q` hides the summary line.
- A test that starts a process (git, gitleaks, sh, python) must be marked `@pytest.mark.slow` (or its module
  `pytestmark = pytest.mark.slow`); `tests/conftest.py` fails it otherwise. Keep the fast run under 5 s.
- Never put real session data, real project or company names, or real ids in tests or docs: this repo is public.
- When `skills/` or `hooks/` change, bump `version` in both `.claude-plugin/plugin.json` and
  `.codex-plugin/plugin.json` (tests check they agree): Claude Code and Codex cache plugins per version.
- Develop in a checkout of its own. The installed copy (`code` in `~/.config/retroagent/config.json`, usually
  `~/.retroagent`) is what every `kb` on the machine runs; change it only with `kb update`. To try new code first,
  run this checkout's `bin/kb` (it runs the code next to it); keep to read-only commands such as `find`, `show`, `sql`.
- Never write to a data clone by hand (`root` in the config). Use `kb` to read it: `kb find`, `kb summary`, `kb show`.
- Only the machine that owns a host writes its folders in a data repo (`own_paths` in `src/kb/sync.py`), summaries
  included. Never write summaries from another machine or a cloud session, never with `--force-host` unless the owner
  asks, and never with a script that calls `summarize`, `update_front_matter` or `write_catalog` directly: the
  owner's next sync then conflicts.
