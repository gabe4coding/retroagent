# Coding standards

Rules for changing code, tests, skills, docs and commits in this repo. `[test: x]` marks a rule that tests
enforce; the others rely on discipline.

## Code
- `src/kb/` targets Python 3.9 and the standard library only: `kb` runs on the stock `/usr/bin/python3` with nothing
  installed. `[test: tests/test_scaffold.py checks 3.9+; scripts/test installs only pytest, so a third-party import
  fails]`

## Tests and commits
- Run `scripts/test --all` before every commit; plain `scripts/test` (the fast tests, under 5 s) while you work. Both
  run in parallel on `/usr/bin/python3`, the runtime target, with pytest from `uv`. There is no CI:
  `scripts/test --all` is the only check. It already passes `-q`; adding another `-q` hides the summary line.
- A test that starts a process (git, gitleaks, sh, python) must be marked `@pytest.mark.slow` (or its module
  `pytestmark = pytest.mark.slow`). Keep the fast run under 5 s. `[test: tests/conftest.py fails an unmarked one]`
- Build test data with the shared builders in `tests/fixtures.py`: fake Claude/Codex transcripts
  (`write_claude_session`, `write_codex_unit`, `make_*_tree`), `age()` to make files look idle, git remotes and clones
  (`init_remote`, `clone`), and `make_config`. A session's short id is the last 8 hex digits of its id, so fixture ids
  must differ there.
- Tests never touch the real home, config or data clone: `tests/conftest.py` isolates `HOME`, `KB_CONFIG`, `KB_ROOT`
  and git config for every test. `[test: tests/conftest.py, autouse]`
- Never put real session data, real project or company names, or real ids in tests or docs: this repo is public.

## Plugin and skills
- When `skills/` or `hooks/` change, bump `version` in both `.claude-plugin/plugin.json` and
  `.codex-plugin/plugin.json`: Claude Code and Codex cache plugins per version. `[test: tests/test_plugin.py checks
  the two agree, not that you bumped]`
- Each `skills/<name>/SKILL.md` starts with front matter whose first keys are `name: <folder name>` then
  `description:`, and its body stays under a line limit: tests/test_skills.py enforces the limits.
  `[test: tests/test_skills.py]`
- Detail a skill needs only sometimes goes in `skills/<name>/references/<file>.md`, plain Markdown (Codex reads the
  same folder). `SKILL.md` names each file as `references/<file>.md` and says when to read it; a file it does not
  name is an orphan. `[test: tests/test_skills.py]`
- A new skill folder must be added to the list in `tests/test_skills.py`, so it gets the same checks.
  `[test: tests/test_skills.py]`
- `skills/setup/SKILL.md` names only `kb` and `install.sh` commands that exist: an agent runs them as written.
  `[test: tests/test_skills.py checks a fixed list]`
