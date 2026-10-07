# retroagent

Sync your Claude Code and Codex sessions, from every machine, into a **private Git repo you choose**. Then search
them with the `kb` CLI at a tiny token cost, and let a cloud routine keep one page per project and one retrospective
per week.

- **Sync**: each session becomes a short markdown file (plus a slim, redacted raw copy) in your data repo. Memory files
  agents keep between sessions are copied too. Secrets are redacted and every commit is scanned with gitleaks.
- **Search**: `kb find`, `kb summary`, `kb show`, `kb stats`, `kb sql`, on a local SQLite index. Skills teach Claude
  Code and Codex to look there first ("how did I fix…", "what did I decide about…").
- **Pages and retros** (optional): a Claude Code routine writes `pages/projects/<project>.md` (state, decisions, files,
  errors → fixes, open threads) and `pages/retro/<YYYY-Www>.md`, every bullet linked to its source session.

Two repos are involved: this one (the code, public) and your **data repo** (private, yours). The code never holds data.

## Install

You need git, Python 3.9+ with SQLite FTS5 (macOS's `/usr/bin/python3` works), [gitleaks](https://github.com/gitleaks/gitleaks)
(strongly recommended: `brew install gitleaks`), and the GitHub CLI `gh` to create the data repo and the routine's
secrets.

In Claude Code:
```
/plugin marketplace add gabe4coding/retroagent
/plugin install retroagent@retroagent
/retroagent:setup
```
The `setup` skill asks a few questions (new or existing data repo, this machine's name, first sync now or later,
automatic syncs and updates, the pages routine now or later) and runs the steps below for you. Run it again at any
time to finish or change the setup.

By hand, or for Codex only:
```bash
git clone https://github.com/gabe4coding/retroagent ~/.retroagent
gh repo create <you>/retroagent-data --private          # or use an existing private repo
~/.retroagent/install.sh --repo git@github.com:<you>/retroagent-data.git --host <unique-name>
kb backfill                   # process every session; the first push
kb backfill --summaries       # 3-line summaries with Haiku (slow; uses your Claude quota)
kb enable                     # sync automatically when a session starts
```
`install.sh` clones the data repo into `~/.retroagent-data` (`--root DIR` changes that), pushes the base files it
lacks (README.md, AGENTS.md, CLAUDE.md, .gitignore) in one commit, writes `~/.config/retroagent/config.json` with
`auto_sync: false`, installs the Claude Code and Codex plugins from the code clone, links `kb` into `~/.local/bin`, and
builds the index. It starts no sync. Run it again at any time; it keeps your other config keys. In Codex, open it once
and trust the retroagent hook with `/hooks`.

Each machine needs its own `host` (default: the short hostname). Two machines with the same `host` would write the same
folders. The first sync commits `sessions/<host>/.machine-id`, a copy of the random id in `.kb/machine-id`; a sync or
install on a machine with another id stops with `host '<host>' belongs to another machine`. Never delete
`.kb/machine-id`. A machine that wrote `sessions/<host>` before markers existed claims it once with
`install.sh --force-host`.

## Update

`kb update` pulls the code clone (fast-forward only) and runs `install.sh` again (plugins, link, config).
`kb enable updates` does the pull once a day from the background sync instead, and refreshes the plugins when their
version changes. Both run the latest `main` of the code repo: read what changed if you care.

Every entry point runs one copy of the code: `~/.local/bin/kb` links to the clone, and a plugin's cached copy (Claude
Code, Codex) forwards to the clone recorded as `code` in the config.

## How the sync works
- A SessionStart hook (Claude Code and Codex) runs `kb sync --auto` in the background. It prints nothing. It does
  nothing until you run `kb enable` (`auto_sync` in the config).
- `kb sync` turns each session that has been idle for 15 minutes into markdown (`sessions/<host>/…`), keeps a slim
  redacted raw copy (`raw/<host>/…`) once the session has been idle for 24 hours, asks Haiku for a 3-line summary,
  updates `catalog/<host>/…`, scans with gitleaks, commits only this machine's folders, and pushes. Headless
  one-prompt sessions (`claude -p`, `codex exec`) are skipped.
- It also copies the memory files agents keep: Claude Code's `~/.claude/projects/*/memory/*.md` and Codex's
  `~/.codex/memories/**/*.md`, to `memories/<host>/…` (redacted, scanned, committed with the sessions). A memory
  deleted or renamed on the machine leaves the KB too; a memory folder that is gone entirely keeps its copies.
  Projects whose folder matches `exclude_cwd_globs` are left out.
- A resumed or forked Claude session can copy its parent's subagents. Such a subagent is written once, under the
  session its own records name first; the others link to that file.
- The sync works in its own data clone. Nobody edits it by hand. It only touches git on the configured `branch`
  (`main`), and it never pushes commits that touch anything outside this machine's folders.
- Search is local: `kb` builds a SQLite FTS5 index in `.kb/` of the data clone.
- gitleaks uses the data repo's own `.gitleaks.toml` when it has one, else the one shipped here (it needs gitleaks
  8.25 or newer).

## One writer per host
Only the machine that owns a host writes `sessions/<host>`, `raw/<host>`, `catalog/<host>` and `memories/<host>`,
summaries included: make them on that machine with `kb backfill --summaries`.

Do not make them on another machine (a cloud session, a second laptop) and merge them by pull request. The owner's
sync writes the same files from its local transcripts; after the merge its pull conflicts, and every later sync fails
the same way.

- `kb backfill --summaries --host <other host>` refuses, and so does the library (`summarize_pending` raises
  `ForeignHost`).
- `--force-host` overrides this: it writes only the summary fields and the catalog of that host, and syncs, commits
  and pushes nothing. Use it only when the owner cannot run the summaries itself, send the result as a pull request,
  and expect to run `kb repair` on the owner after the merge.
- A script that calls `summarize()`, `update_front_matter()` or `write_catalog()` itself skips every check. Don't.

## Repair
If every sync fails with `pull: the remote changed this host's files too; run: kb repair`, run on that machine:
```bash
kb repair
kb sync --now --no-summaries
```
`kb repair` fetches, resets the data clone to its upstream and clears the fingerprints (`files` in
`.kb/sync-state.json`). The next sync renders every session again on top of the remote files, keeps the summaries it
finds there, commits only real changes and pushes. Only this host's own work is dropped, and the sync makes it again
from the local transcripts. Dropped commits stay in the clone as `refs/kb/repair/<time>`.

`kb repair` refuses, and changes nothing, when a sync runs, the clone is not on the configured branch, has no upstream
or cannot fetch, or when a local-only commit or an uncommitted change touches a file outside this host's folders.

## Use
`kb find`, `kb page`, `kb memory`, `kb recent`, `kb summary`, `kb show`, `kb stats`, `kb sql`, `kb status`,
`kb sync --now`, `kb enable [updates]`, `kb disable [updates]`, `kb repair`, `kb update`. Run `kb --help`.

The read commands open the local index read-only: they work in a sandbox where `.kb/` cannot be written and never wait
for a running sync. Only a missing or outdated index must be built, which needs write access; if it cannot be built
they print `index not built yet; run: kb reindex` and exit 2. Other errors print one line, `kb: <message>`, and exit 2.

`kb sync --now --no-summaries` syncs fast. `kb backfill --summaries` writes the summaries of every session with no cap
and no time limit (a normal run stops after 20 minutes). Plain `kb sync` and `kb backfill` always run; only
`kb sync --auto` (the hook) obeys `auto_sync`.

## Pages routine
A cloud routine keeps `pages/` of the data repo up to date: one page per project and one retrospective per closed
week. Read them with `kb page [name] [--section NAME]`; `kb find` lists matching pages first; `kb status` shows the
routine's last run.

- The routine runs on claude.ai with two repos checked out side by side, your data repo and this code, and follows
  `scripts/pages-routine.md`. Code does the parts that need no judgement: `kb pages start` (branch, index),
  `kb pages plan` (what to write, from the sessions and memories changed since the watermark in
  `pages/.state.json`), `kb pages digest` (compact input), `kb pages finish` (refuses files outside `pages/`, bad
  front matter, oversized pages and anything that looks like a secret; records the watermark; commits; pushes,
  rebasing over session pushes).
- `.github/workflows/pages-trigger.yml` in the data repo decides when to fire it, because routine runs are counted per
  day. On each push to `main`, and every 2 hours, it fires only if `kb pages due` finds pages to write (a second, no
  LLM) and nothing fired it in the last `min_hours_between_fires` (3). The routine's own commits change only `pages/`
  and carry `[skip ci]`, so they never start it. Run it by hand with the `force` input to fire at once.
- Until `main` holds `pages/.state.json`, runs write to `claude/pages-bootstrap` only. Review that branch and merge it
  to start the normal runs on `main`.
- Settings: `pages/config.json` in the data repo (projects to skip, minimum sessions for a page, batch sizes, retro
  time zone). Local `kb sync` never touches `pages/`.

Set it up with the `setup` skill, or by hand: `kb setup routine` pushes `pages/config.json` and the workflow to the
data repo and prints the routine to create (no schedule, no connectors: it reads untrusted session text) and the two
repository secrets to set from the routine's API trigger, `PAGES_ROUTINE_FIRE_URL` and `PAGES_ROUTINE_FIRE_TOKEN`.

## Config
`~/.config/retroagent/config.json` (an install from before the rename keeps reading `~/.config/sessions-kb/`). Every
key is optional:
`root` (the data clone; `~/.retroagent-data`), `code` (the code clone plugin caches forward to; install.sh writes it),
`host` (short hostname; unique per machine), `auto_sync` (true when the key is missing; `install.sh` writes false;
`kb enable` / `kb disable`), `auto_update` (false; `kb enable updates`), `branch` (`main`; on any other branch the sync
touches no git), `raw_settle_hours` (24), `skip_headless_single_prompt` (true), `gitleaks_path` (found on PATH, then
`/opt/homebrew/bin`, `/usr/local/bin`), `require_gitleaks` (false; `install.sh` sets true when it finds gitleaks: with
no scanner nothing is committed), `quiet_minutes` (15), `debounce_minutes` (10), `summary_model` (`haiku`),
`summary_cap_per_run` (30), `claude_dir` (`~/.claude/projects`), `codex_dirs` (`~/.codex/sessions`,
`~/.codex/archived_sessions`), `codex_home` (`~/.codex`), `exclude_cwd_globs` (temp folders).

`raw_settle_hours`: the raw copy of a session is written once the session has been idle that many hours; until then
only its markdown follows each change. `0` writes the raw copy at every sync. `--now` does not skip the wait.

## Develop
The whole repo is the plugin (`.claude-plugin/`, `.codex-plugin/`, `bin/`, `hooks/`, `skills/`); the Python package
is `src/kb` (stdlib only, Python 3.9). `templates/data/` holds the files `kb setup` writes into data repos.
`scripts/test` runs the fast tests in parallel on `/usr/bin/python3` with pytest from `uv` (a few seconds);
`scripts/test --all` adds the tests marked `slow`, which start real git and other processes. When skills or hooks
change, bump `version` in both plugin manifests: Claude Code and Codex cache plugins per version.

## License
MIT. See `LICENSE`.
