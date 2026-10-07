# sessions-kb

Private knowledge base of my Claude Code and Codex sessions, from every machine. Agents search it with the `kb` CLI.

## How it works
- A SessionStart hook (Claude Code and Codex) runs `kb sync --auto` in the background. It prints nothing. It does nothing until you run `kb enable` (`auto_sync` in the config).
- `kb sync` turns each session that has been idle for 15 minutes into markdown (`sessions/<host>/…`), keeps a slim redacted raw copy (`raw/<host>/…`), asks Haiku for a 3-line summary, updates `catalog/<host>/…`, scans with gitleaks, commits only this machine's folders, and pushes. Headless one-prompt sessions (`claude -p`, `codex exec`) are skipped.
- A resumed or forked Claude session can copy its parent's subagents. Such a subagent is written once, under the session that its own records name first (the one it ran under); the other sessions link to that file. A file whose session is gone from `~/.claude` stays where it is.
- The sync works in its own data clone, `~/.sessions-kb`. Nobody edits it by hand; develop in a separate checkout. It only touches git on the configured `branch` (`main`), and it never pushes commits that touch anything outside this machine's folders.
- Search is local: `kb` builds a SQLite FTS5 index in `.kb/` from the markdown.

## Set up a machine
```bash
brew install gitleaks        # required: every commit is scanned; without it nothing is committed
gh repo clone gabe4coding/sessions-kb ~/Repositories/sessions-kb      # your development checkout
~/Repositories/sessions-kb/install.sh --host <unique-name>
```
`install.sh` clones the repo into `~/.sessions-kb` (`--root DIR` changes that), writes `~/.config/sessions-kb/config.json` with `auto_sync: false` (an explicit value that is already there is kept), installs the Claude Code and Codex plugins, links `kb` into `~/.local/bin`, and builds the index. It starts no sync. Run it again at any time; it keeps your other config keys. It refuses to run if python3 is older than 3.9 or has no SQLite FTS5.

Then, in this order:
1. `kb backfill` processes every session and makes the first data push.
2. `kb backfill --summaries` writes the summaries (slow, uses your Claude quota). Run it on this machine: see "One writer per host".
3. `kb enable` lets the SessionStart hook sync automatically (`kb disable` stops it).

Open Codex once and trust the `sessions-kb` hook with `/hooks`.

Each machine needs its own `host` (default: the short hostname). Two machines with the same `host` would write the same folders. The first sync commits `sessions/<host>/.machine-id`, a copy of the random id in `.kb/machine-id`. If the committed marker differs from the local id, `kb sync` stops with `host '<host>' belongs to another machine` and writes nothing under that host; `install.sh` refuses too. Never delete `.kb/machine-id`: it looks like a new machine. The machine that already wrote `sessions/<host>` before markers existed claims it once with `install.sh --host <host> --force-host`.

If the machine has `~/claude-tools`, also register `kb` in the local-tools plugin (symlink `~/claude-tools/plugins/local-tools/bin/kb` → `plugin/bin/kb`, README row, version bump, commit, `claude plugin update local-tools@local`).

## One writer per host
Only the machine that owns a host writes `sessions/<host>`, `raw/<host>` and `catalog/<host>`. That includes the summaries: make them on that machine with `kb backfill --summaries`.

Do not make them on another machine (a cloud session, a second laptop) and merge them by PR. The owner's sync writes the same files from its local transcripts. After the merge its pull conflicts, and every later sync fails the same way. This happened once: PR #1 added 169 summaries for `laptop-1` from a cloud session (host `vm`).

- `kb backfill --summaries --host <other host>` refuses. The library refuses too: `summarize_pending` raises `ForeignHost` when the host is not the configured host, or when another machine's `.machine-id` marker claims it.
- `--force-host` overrides this. `kb backfill --summaries --host <other host> --force-host` writes only the summary fields and the catalog of that host. It syncs, commits and pushes nothing. Use it only when the owner cannot run the summaries itself. Review the changes, send them as a PR, and expect to run `kb repair` on the owner after the merge.
- A script that calls `summarize()`, `update_front_matter()` or `write_catalog()` itself skips every check. Do not write one; call `kb backfill --summaries` on the owner.

## Repair
If every sync fails with `pull: the remote changed this host's files too; run: kb repair`, run on that machine:
```bash
kb repair
kb sync --now --no-summaries
```
`kb repair` fetches, resets the data clone to its upstream (`origin/main`) and clears the fingerprints (`files` in `.kb/sync-state.json`). The next sync renders every session again on top of the remote files. It keeps the summaries it finds in them, commits only real changes and pushes.

Only this host's own work is dropped, and the sync makes it again from the local transcripts. Dropped commits stay in the clone as `refs/kb/repair/<time>`: a session whose transcript is gone from this machine can be taken back from there. The quarantine is kept.

`kb repair` refuses, and changes nothing, when a sync runs, the clone is not on the configured branch, has no upstream or cannot fetch, or when a local-only commit or an uncommitted change touches a file outside this host's folders.

## Use
`kb find`, `kb page`, `kb recent`, `kb summary`, `kb show`, `kb stats`, `kb sql`, `kb status`, `kb sync --now`, `kb enable`, `kb disable`, `kb repair`. Run `kb --help`.

Those read commands open the local index read-only: they work in a sandbox where `.kb/` cannot be written and never wait for a running sync. Only a missing or outdated index must be built, which needs write access. If it cannot be built, they print `index not built yet; run: kb reindex` and exit 2. Other errors print one line, `kb: <message>`, and exit 2.

`kb sync --now --no-summaries` syncs fast. `kb backfill --summaries` writes the summaries of every session with no cap and no time limit (a normal run stops after 20 minutes). If another sync is running, `kb sync` prints `another sync is running` and exits 0. Plain `kb sync` and `kb backfill` always run; only `kb sync --auto` (the hook) obeys `auto_sync`.

## Pages
A cloud routine keeps `pages/` up to date from the sessions: one page per project (`pages/projects/<project>.md`: state, decisions, files, errors → fixes, open threads) and one retrospective per closed week (`pages/retro/<YYYY-Www>.md`). Every bullet names its source session. Read them with `kb page [name] [--section NAME]`; `kb find` lists matching pages before sessions; `kb status` shows the routine's last run.

- `.github/workflows/pages-trigger.yml` decides when to fire the routine, because routine runs are counted per day. On each push to `main`, and every 2 hours, it fires only if `kb pages due` finds pages to write (a second on a sparse checkout, no LLM) and nothing fired it in the last `min_hours_between_fires` (3). The routine's own commits change only `pages/` and carry `[skip ci]`, so they never start it. Run it by hand with the `force` input to fire at once.
- The routine follows `scripts/pages-routine.md`. Code does the parts that need no judgement: `kb pages start` (branch, index), `kb pages plan` (what to write, from the sessions changed since the watermark in `pages/.state.json`), `kb pages digest` (compact input), `kb pages finish` (refuses files outside `pages/`, bad front matter, oversized pages and anything that looks like a secret; records the watermark; commits; pushes, rebasing over session pushes; a run that loses a race with another run drops its work and the next run catches up).
- Until `main` holds `pages/.state.json`, runs write to `claude/pages-bootstrap` only. Review that branch and merge it to start the normal runs on `main`.
- Settings: `pages/config.json` (projects to skip, minimum sessions for a page, batch sizes, retro time zone). Local `kb sync` never touches `pages/`.

Set up once: create the routine (repo `gabe4coding/sessions-kb`, prompt: follow `scripts/pages-routine.md`, no schedule, no connectors), add an API trigger at claude.ai/code/routines, and store its URL and token as the repository secrets `PAGES_ROUTINE_FIRE_URL` and `PAGES_ROUTINE_FIRE_TOKEN`.

## Config
`~/.config/sessions-kb/config.json`. Every key is optional:
`root` (`~/Repositories/sessions-kb`; `install.sh` writes `~/.sessions-kb`), `host` (short hostname; unique per machine), `auto_sync` (true when the key is missing; `install.sh` writes false; `kb enable` / `kb disable` change it), `branch` (`main`; on any other branch the sync touches no git), `skip_headless_single_prompt` (true), `gitleaks_path` (found on PATH, then `/opt/homebrew/bin`, `/usr/local/bin`), `require_gitleaks` (false; `install.sh` sets true when it finds gitleaks: with no scanner nothing is committed), `quiet_minutes` (15), `debounce_minutes` (10), `summary_model` (`haiku`), `summary_cap_per_run` (30), `claude_dir` (`~/.claude/projects`), `codex_dirs` (`~/.codex/sessions`, `~/.codex/archived_sessions`), `codex_home` (`~/.codex`), `exclude_cwd_globs` (temp folders).

Without gitleaks and with `require_gitleaks` off, files that gitleaks already held back stay held back, and the rest is committed with the built-in redaction only.

The repo's `.gitleaks.toml` (`disabledRules`, `[[allowlists]]`) needs gitleaks 8.25 or newer (`brew upgrade gitleaks`).

## Develop
`scripts/test` runs the suite on `/usr/bin/python3` with pytest from `uv`.
