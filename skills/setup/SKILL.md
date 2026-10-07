---
name: setup
description: Use when the user wants to install, set up, finish setting up or repair retroagent on a machine — pick or create the private repo their sessions sync to, install the plugins and the `kb` CLI, run the first backfill, turn automatic syncs and updates on — or to set up, later, the cloud routine that writes project pages and weekly retros. Also "move my sessions to another repo", "set up the pages routine".
---

# retroagent setup

Guide the user through the setup, one question at a time (AskUserQuestion in Claude Code, a plain question
elsewhere). Every optional step offers **now**, **later** (this skill again, any time) or **no**.

Rules:
- Ask before each step that changes something outside this machine (creating a repo, a push, a routine). Show the
  exact command first.
- Never ask for, read or type a token. The user sets secrets in their own terminal; `gh secret set` prompts for them.
- The data repo must be private: the sessions hold prompts, paths and tool output. Refuse a public repo.

## Steps

0. **State.** `kb setup check` (no `kb` on PATH: `"${CLAUDE_PLUGIN_ROOT}/bin/kb" setup check`). It prints JSON. Skip
   what is done; say in one line what is set up and what is left.
1. **Code clone.** install.sh, the shell's `kb`, Codex and updates run from a clone of the code. When `code_is_clone`
   is false and `~/.retroagent` does not exist: `git clone https://github.com/gabe4coding/retroagent ~/.retroagent`.
   `<code>` below is that clone (or `code` when `code_is_clone` is true).
2. **Data repo.** Ask: create a new private GitHub repo (suggest `<gh user>/retroagent-data`), use an existing one,
   or keep the current one (when `data_clone` is true).
   - New: `gh auth status` must pass. After a yes: `gh repo create <owner>/<name> --private --description
     "retroagent data: my Claude Code and Codex sessions"`.
   - Existing: ask for it; `gh repo view <owner>/<name> --json visibility -q .visibility` must not be `PUBLIC`.
   - Clone URL: `git@github.com:<owner>/<name>.git` when `gh config get git_protocol` is `ssh`, else
     `https://github.com/<owner>/<name>.git`.
3. **Host name.** This machine's name in the KB (default: `host` from step 0). It must be unique per machine; confirm
   it, or take the user's.
4. **Install.** `<code>/install.sh --repo <url> --host <host>` (add `--root <folder>` only if the user wants another
   folder than `~/.retroagent-data`). It clones the data repo, pushes the base files it lacks (README.md, AGENTS.md, …)
   in one commit, writes `~/.config/retroagent/config.json`, installs the Claude Code and Codex plugins and links
   `~/.local/bin/kb`. Relay its WARNING lines:
   - gitleaks missing: ask the user to `brew install gitleaks` (or their package manager) and run install.sh again.
     Every commit of sessions is scanned with it.
   - `host '<host>' belongs to another machine`: ask for another name and run it again.
5. **First sync.** Ask now or later. `kb backfill` processes every session and makes the first push (minutes; a long
   history can be hundreds of MB).
6. **Summaries.** Ask now or later. `kb backfill --summaries` writes a 3-line summary per session with Haiku (slow;
   uses the user's Claude quota). Start it in the background:
   `nohup kb backfill --summaries >> <data root>/.kb/backfill.log 2>&1 &`.
7. **Automatic syncs.** Ask. `kb enable` makes each new session start a background sync (`kb disable` stops it).
8. **Automatic updates.** Ask (skip when `auto_update` is true). `kb enable updates` pulls the code clone once a day (fast-forward only, only when it
   sits clean on its default branch) and refreshes the plugins on a new version. It runs new code from the retroagent
   repo without review; without it, the user runs `kb update` to update.
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
11. **Codex.** When `codex` is installed: tell the user to open Codex once and trust the retroagent hook with `/hooks`.
12. **Pages routine.** Before you offer it, read `references/pages-routine.md` in this skill's folder: how to
    explain it, then `kb setup routine` and the routine itself.
13. **Report.** A short list: data repo and folder, host, automatic syncs and updates on or off, semantic search on or
    off, routine state, and the next manual step if any.

## Move to another data repo

When the user wants their sessions in another data repo, read `references/move-data-repo.md` in this skill's
folder and follow it.
