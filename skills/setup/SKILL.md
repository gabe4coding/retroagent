---
name: setup
description: Use when the user wants to install, set up, finish setting up or repair retroagent on a machine — pick or create the private repo their sessions sync to, install the plugins and the `kb` CLI, run the first backfill, turn automatic syncs and updates on — or to set up, later, the cloud routine that writes project pages and weekly retros. Also "move my sessions to another repo", "set up the pages routine".
---

# retroagent setup

Guide the user through the setup with as few questions as possible (AskUserQuestion in Claude Code, a plain question
elsewhere). Optional steps offer **now**, **later** (this skill again, any time) or **no**.

Rules:
- Show the exact commands before anything that changes something outside this machine (creating a repo, a push, a
  routine), and run them only after a yes. One yes can cover a list of commands shown together.
- Never ask for, read or type a token. The user sets secrets in their own terminal; `gh secret set` prompts for them.
- The data repo must be private: the sessions hold prompts, paths and tool output. Refuse a public repo.
- `kb` below means `kb` on PATH, else `<code>/bin/kb` (not on PATH yet in this session).

## Steps

0. **State.** `kb setup check` (no `kb` anywhere yet: `"${CLAUDE_PLUGIN_ROOT}/bin/kb" setup check`). It prints JSON.
   Skip what is done; say in one line what is set up and what is left.
1. **Code clone.** install.sh, the shell's `kb`, Codex and updates run from one clone of the code. When `code_is_clone`
   is false and `~/.retroagent` does not exist, clone it without a question (it changes nothing outside this machine):
   `git clone https://github.com/gabe4coding/retroagent ~/.retroagent`. `<code>` below is that clone (or `code` when
   `code_is_clone` is true). install.sh moves the plugin to this clone's marketplace.
2. **Recommended or Customize.** When the data repo is not set up yet, ask one question: **Recommended** or
   **Customize**. Recommended is: a new private `<gh user>/retroagent-data` (step 3), the host from step 0, install.sh,
   the first sync with the recent sessions first, summaries in the background, and automatic syncs on when the first
   sync is done. Semantic search, automatic updates, cloud sessions and the pages routine stay for later.
   - Recommended: show steps 3–6 as one list of commands, run them after one yes, then go to step 12.
   - Customize: do steps 3–11, one question each.
3. **Data repo.** New private repo, an existing one, or keep the current one (when `data_clone` is true).
   - With `gh` (`gh auth status` passes): `gh repo create <owner>/<name> --private --description "retroagent data: my
     Claude Code and Codex sessions"`. An existing one: `gh repo view <owner>/<name> --json visibility -q .visibility`
     must not be `PUBLIC`. Clone URL: `git@github.com:<owner>/<name>.git` when `gh config get git_protocol` is `ssh`,
     else `https://github.com/<owner>/<name>.git`.
   - Without `gh`, or not logged in: give the user `https://github.com/new?name=retroagent-data&visibility=private`
     and ask for the clone URL of the repo they made (or an existing one). Then `kb setup repo-check <url>`:
     `reachable` must be true (else show `error`: usually the git login), and `public` must not be true.
4. **Host name.** This machine's name in the KB (default: `host` from step 0). It must be unique per machine. In
   Customize, confirm it or take the user's.
5. **Install.** `<code>/install.sh --repo <url> --host <host>` (add `--root <folder>` only if the user wants another
   folder than `~/.retroagent-data`). It clones the data repo, downloads a pinned gitleaks when there is none, pushes
   the base files the repo lacks (README.md, AGENTS.md, …) in one commit, writes `~/.config/retroagent/config.json`,
   installs the Claude Code and Codex plugins and links `~/.local/bin/kb`. Relay its WARNING lines:
   - gitleaks: the download failed. Ask the user to `brew install gitleaks` (or their package manager) and run
     install.sh again. Every commit of sessions is scanned with it.
   - `host '<host>' belongs to another machine`: ask whether this machine wrote those sessions (a new clone, a lost
     `.kb/machine-id`). If yes, run it again with `--force-host`; else ask for another name and run it again.
6. **First sync.** `kb backfill --recent 14` indexes the sessions of the last 14 days in about a minute, with no push:
   `kb find` works at once. Then the rest, the first push (a long history can be hundreds of MB) and the summaries
   (Haiku; uses the user's Claude quota) in the background:
   `nohup sh -c 'kb backfill && kb backfill --summaries' >> <data root>/.kb/backfill.log 2>&1 &`.
   In Customize, ask about the summaries; without them run `kb backfill` alone in the background.
7. **Automatic syncs.** The first full `kb backfill` turns them on (install.sh leaves `auto_sync_pending` for it).
   Until then each new session starts that backfill again, so a stopped one goes on. In Customize, the user can
   choose now: `kb enable` (on at once) or `kb disable` (off; nothing turns it on later).
8. **Automatic updates.** Ask (skip when `auto_update` is true). `kb enable updates` pulls the code clone once a day
   (fast-forward only, only when it sits clean on its default branch) and refreshes the plugins on a new version. It
   runs new code from the retroagent repo without review; without it, the user runs `kb update` to update.
9. **Semantic search.** Ask (skip when `kb embed --status` says it is on). `kb embed` downloads a local embedding
   model and its runtime (about 330 MB, checked by sha256, into `~/.cache/retroagent/embed`), embeds every session,
   the user's messages, every page and memory, and turns it on: `kb find` then also matches paraphrases and other
   languages. Everything runs on this machine; the model uses about 500 MB of RAM while loaded and stops when idle.
   `kb embed --off` turns it off.
10. **Cloud sessions.** Only when the user runs Claude Code cloud sessions or routines: `kb setup cloud` prints the
    network allowlist and the setup script of a cloud environment. Only the user can apply them, in the environment
    settings at claude.ai/code; show the output and say so. The script puts `kb` and semantic search in the cloud and
    a hook that pushes each cloud session to an inbox. Ask which one machine imports them (as host `cloud`), and run
    `kb cloud import --on` there only.
11. **Pages routine.** Before you offer it, read `references/pages-routine.md` in this skill's folder: how to
    explain it, then `kb setup routine` and the routine itself.
12. **Report.** A short list: data repo and folder, host, the first sync (recent sessions indexed; the rest runs in
    the background, `kb status` shows what is left), automatic syncs and updates, semantic search, routine state.
    Then the next steps, each on its own line:
    - Start a new session: the plugin, its skills and its hook load only then.
    - When `kb_on_path` is false: `kb` is in `~/.local/bin`; tell the user to add `export PATH="$HOME/.local/bin:$PATH"`
      to their shell file (`~/.zshrc` or `~/.bashrc`). Do not edit it yourself.
    - When `codex` is installed: open Codex once and trust the retroagent hook with `/hooks`.
    - What stays for later (semantic search, updates, cloud sessions, the pages routine): run this skill again.

## Move to another data repo

When the user wants their sessions in another data repo, read `references/move-data-repo.md` in this skill's
folder and follow it.
