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
2. `kb backfill --summaries` writes the summaries (slow, uses your Claude quota).
3. `kb enable` lets the SessionStart hook sync automatically (`kb disable` stops it).

Open Codex once and trust the `sessions-kb` hook with `/hooks`.

Each machine needs its own `host` (default: the short hostname). Two machines with the same `host` would write the same folders. The first sync commits `sessions/<host>/.machine-id`, a copy of the random id in `.kb/machine-id`. If the committed marker differs from the local id, `kb sync` stops with `host '<host>' belongs to another machine` and writes nothing under that host; `install.sh` refuses too. Never delete `.kb/machine-id`: it looks like a new machine. The machine that already wrote `sessions/<host>` before markers existed claims it once with `install.sh --host <host> --force-host`.

If the machine has `~/claude-tools`, also register `kb` in the local-tools plugin (symlink `~/claude-tools/plugins/local-tools/bin/kb` → `plugin/bin/kb`, README row, version bump, commit, `claude plugin update local-tools@local`).

## Use
`kb find`, `kb recent`, `kb summary`, `kb show`, `kb stats`, `kb sql`, `kb status`, `kb sync --now`, `kb enable`, `kb disable`. Run `kb --help`.

Those read commands open the local index read-only: they work in a sandbox where `.kb/` cannot be written and never wait for a running sync. Only a missing or outdated index must be built, which needs write access. If it cannot be built, they print `index not built yet; run: kb reindex` and exit 2. Other errors print one line, `kb: <message>`, and exit 2.

`kb sync --now --no-summaries` syncs fast. `kb backfill --summaries` writes the summaries of every session with no cap and no time limit (a normal run stops after 20 minutes). If another sync is running, `kb sync` prints `another sync is running` and exits 0. Plain `kb sync` and `kb backfill` always run; only `kb sync --auto` (the hook) obeys `auto_sync`.

## Config
`~/.config/sessions-kb/config.json`. Every key is optional:
`root` (`~/Repositories/sessions-kb`; `install.sh` writes `~/.sessions-kb`), `host` (short hostname; unique per machine), `auto_sync` (true when the key is missing; `install.sh` writes false; `kb enable` / `kb disable` change it), `branch` (`main`; on any other branch the sync touches no git), `skip_headless_single_prompt` (true), `gitleaks_path` (found on PATH, then `/opt/homebrew/bin`, `/usr/local/bin`), `require_gitleaks` (false; `install.sh` sets true when it finds gitleaks: with no scanner nothing is committed), `quiet_minutes` (15), `debounce_minutes` (10), `summary_model` (`haiku`), `summary_cap_per_run` (30), `claude_dir` (`~/.claude/projects`), `codex_dirs` (`~/.codex/sessions`, `~/.codex/archived_sessions`), `codex_home` (`~/.codex`), `exclude_cwd_globs` (temp folders).

Without gitleaks and with `require_gitleaks` off, files that gitleaks already held back stay held back, and the rest is committed with the built-in redaction only.

The repo's `.gitleaks.toml` (`disabledRules`, `[[allowlists]]`) needs gitleaks 8.25 or newer (`brew upgrade gitleaks`).

## Develop
`scripts/test` runs the suite on `/usr/bin/python3` with pytest from `uv`.
