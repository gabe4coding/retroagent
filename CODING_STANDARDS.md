# Coding standards

Rules for changing code, tests, skills, docs and commits in this repo. `[test: x]` marks a rule that tests
enforce; the others rely on discipline.

## Code
- `src/kb/` targets Python 3.9 and the standard library only: `kb` runs on the stock `/usr/bin/python3` with nothing
  installed. `[test: tests/test_scaffold.py checks 3.9+; scripts/test installs only pytest, so a third-party import
  fails]`

## Tests and commits
- Run `scripts/test --all` before every commit; plain `scripts/test` (the fast tests, under 5 s) while you work. Both
  run in parallel on `/usr/bin/python3`, the runtime target, with pytest from `uv`. It already passes `-q`; adding
  another `-q` hides the summary line. CI (`.github/workflows/test.yml`) runs `scripts/test --all` on every push to
  `main` and every PR, on Linux with Python 3.9 and 3.13 (`KB_TEST_PYTHON` picks the interpreter). It has no macOS
  job: run `scripts/test --all` on a Mac before a commit that changes macOS behavior.
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

## Docs
User docs are `README.md` and `docs/*.mdx`. They are for humans first: a developer who installs retroagent, searches
past sessions or sets up the pages routine. Out of this set: `AGENTS.md`, this file, the skills and
`scripts/pages-routine.md` (they are for agents). `tests/test_docs.py` checks the rules marked `[test]`.

### Content
- `README.md` stays a short landing page: pitch, quick start, a few commands, links. User docs live in `docs/*.mdx`;
  when behavior changes, change the matching page.
- The code is the source of truth. Check every default, limit and behavior in `src/kb/` before you write it.
- Lead with what the reader wants to do and a short example; reference tables come after.
- Keep only facts that change what a user does or understands. Leave out function and module names unless the user
  types them, and history ("before the rename", "legacy").

### Format
- Each `docs/*.mdx` starts with front matter (`title`, one-sentence `description`) and an H1 equal to the title.
  `[test]`
- Plain Markdown, no JSX, imports or comments, so GitHub renders them. Keep `<…>` and `{…}` inside backticks or code
  blocks: MDX reads them as JSX and fails to compile. `[test]`
- Relative links use the `.mdx` name and the GitHub heading slug. When you rename a heading, fix every link to it.
  `[test: every link and anchor must resolve]`

### Language: ASD-STE100 (Simplified Technical English)
Technical names are allowed: commands, flags, config keys, file and product names.
- Instructions: imperative, one instruction per sentence, at most 20 words, condition first ("If X, do Y").
- Descriptions: at most 25 words per sentence `[test]`, one topic per sentence, at most 6 sentences per paragraph.
- Active voice, simple present. "can" for possibility, "must" for a requirement, "do not" for a prohibition.
- No should/may/might/would/could, contractions, semicolons, e.g./i.e./etc., or filler words (just, simply,
  easily) `[test]`. No -ing forms where a plain verb works, no phrasal verbs where one verb exists. Keep the
  articles.
- Vertical lists for three or more items; numbered lists when order matters. A warning starts with the
  instruction, then gives the reason.
- One word for one meaning, in all docs:

| Term | Meaning | Do not use |
| --- | --- | --- |
| session | One Claude Code or Codex conversation, from its transcript | conversation, chat |
| transcript | The file the agent writes for a session (`.jsonl`) | log |
| machine | A computer that runs `kb` | laptop, computer, box |
| host | The name a machine writes its folders under (`sessions/<host>/…`) | machine name |
| data repo | The private GitHub repo that holds the synced sessions | KB, knowledge base, data repository |
| data clone | The local copy of the data repo (`root` in the config) | clone (alone), checkout |
| code clone | The local copy of this repo that `kb` runs (`code` in the config) | install, installed copy |
| sync | One run of `kb sync`, or the act of it | upload, backup |
| summary | The 3-line summary of a session | abstract, recap |
| memory file | A file an agent keeps between sessions (`memory/*.md`) | note |
| pages routine | The cloud routine that writes project pages and retros | job, agent |
| project page | The page the pages routine writes for one project | wiki page |
| retro | The weekly retrospective the pages routine writes | review, report |
| cloud session | A session that runs in a cloud container, not on a machine | remote session |
