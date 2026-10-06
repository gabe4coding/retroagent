---
name: kb-search
description: Use when the user asks about past work in any Claude Code or Codex session — "how did I fix…", "what did I decide about…", "last time we…", "did I already…", "find the session where…" — or when an earlier session may already hold the answer (an error seen before, a past design choice, a command that worked). Searches the personal sessions KB with the `kb` CLI at very low token cost.
---

# kb-search

`kb` searches every synced Claude Code and Codex session (all projects, all machines). Output is short on purpose. Go in this order and stop as soon as you have the answer.

1. **Find:** `kb find "<2-5 distinctive words>" [--project NAME] [--agent claude|codex] [--since 30d] [--tag TAG]`
   - One line per session: `short date agent project title · «snippet» [turn N]`. `short` is the 8-character session id (the last 8 characters, no dashes). Lines with `↳parent` are subagent transcripts.
   - Use distinctive words: error text, file names, service or tool names. Add filters when you know them.
   - No match: try synonyms or fewer words. `kb recent --project NAME` lists the latest sessions.
2. **Summarize:** `kb summary <short>` on the 1–3 best hits: summary, decisions, outcome, files, PRs, subagents.
3. **Read only what you need:** `kb show <short> --turn N --around 1` or `kb show <short> --grep "pattern"`. Output stops at `--max-chars` (default 4000).

Rules:
- Never `cat`, `Read` or `rg` whole files under `sessions/` or `raw/`. Use `kb`.
- When you report a finding, cite it as `short [turn N]` so the user can open it.
- The KB lags a little: a session is synced after it has been idle 15 minutes. `kb sync --now` syncs now. `kb status` shows the last sync and its error, if any.
- All flags: `kb <command> --help`.
