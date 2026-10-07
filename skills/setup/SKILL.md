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
8. **Automatic updates.** Ask. `kb enable updates` pulls the code clone once a day (fast-forward only, only when it
   sits clean on its default branch) and refreshes the plugins on a new version. It runs new code from the retroagent
   repo without review; without it, the user runs `kb update` to update.
9. **Codex.** When `codex` is installed: tell the user to open Codex once and trust the retroagent hook with `/hooks`.
10. **Pages routine.** Explain in two lines: a cloud routine on claude.ai writes one page per project and one retro
    per closed week into the data repo, fired by a GitHub workflow in the data repo at most every 3 hours (routine
    runs count against the account's daily routine limit). Ask now, later or no. For now:
    - `kb setup routine` pushes `pages/config.json` (kept when present) and the trigger workflow, and prints JSON:
      `routine` (the create body), `secrets`, `test`.
    - Claude Code: `RemoteTrigger list`. If a routine already has this data repo in its sources, offer to update it
      (`RemoteTrigger update` with the spec's `job_config`). Else take `environment_id` from an existing routine (ask
      when there are several, or ask the user to create any routine at claude.ai/code/routines first), show name,
      repos and model, and after a yes `RemoteTrigger create` with the spec. Other agents: give the user the values to
      create it at https://claude.ai/code/routines (both repos, the prompt, the model, no schedule, no connectors).
    - The user opens the routine page, adds an **API** trigger and copies its URL and token. Then, in their own
      terminal, they run the two `secrets` commands and paste the values when asked.
    - Test, after a yes: the `test` command fires it once. The first run writes to the `claude/pages-bootstrap`
      branch and keeps a draft pull request; when it is ready, the user reviews and merges it, and later runs write
      to `main`.
11. **Report.** A short list: data repo and folder, host, automatic syncs and updates on or off, routine state, and
    the next manual step if any.

## Move to another data repo

`kb disable`, and wait until `kb status` shows no running sync. Then either rename the repo on GitHub
(`gh repo rename`; then `git -C <data root> remote set-url origin <new url>`), or copy it to a new private repo:
create it (step 2), then after a yes `git -C <data root> push <new url> main` and
`git -C <data root> remote set-url origin <new url>`. Run `kb setup routine` again and update the routine's sources;
a new repo also needs the two secrets again. `kb enable` at the end.
