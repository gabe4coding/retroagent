---
name: kb-search
description: Use when the user asks about past work in any Claude Code or Codex session — "how did I fix…", "what did I decide about…", "last time we…", "did I already…", "find the session where…" — or when an earlier session may already hold the answer (an error seen before, a past design choice, a command that worked). Searches the personal sessions KB with the `kb` CLI at very low token cost.
---

# kb-search

`kb` searches every synced Claude Code and Codex session and memory file (all projects, all machines). Output is short on purpose. Go in this order and stop as soon as you have the answer.

0. **Project page:** for a question about one project, `kb page <project> [--section "Key decisions"]` first: its
   state, decisions, files, errors → fixes and open threads, each with the short id of its session. `kb page` lists the
   pages; weekly retros are named like `2026-W41`.
1. **Find:** `kb find "<2-5 distinctive words>" [--project NAME] [--agent claude|codex] [--since 30d] [--tag TAG]`
   - Matching pages come first (`page date kind name title`), then memories (`memory date agent project name`;
     read one with `kb memory <ref>`, the ref is in brackets), then one line per session: `short date agent project title · «snippet» [turn N]`. `short` is the 8-character session id (the last 8 characters, no dashes). Lines with `↳parent` are subagent transcripts.
   - Use distinctive words: error text, file names, service or tool names. Add filters when you know them.
   - No match: try synonyms or fewer words. `kb recent --project NAME` lists the latest sessions.
2. **Summarize:** `kb summary <short>` on the 1–3 best hits: summary, decisions, outcome, files, PRs, subagents.
3. **Read only what you need:** `kb show <short> --turn N --around 1` or `kb show <short> --grep "regex"`. `--grep` takes a regular expression (case-insensitive, up to 5 matching turns): write `\(` for a literal parenthesis. Output stops at `--max-chars` (default 4000).

Rules:
- Never `cat`, `Read` or `rg` whole files under `sessions/`, `raw/` or `memories/`. Use `kb`.
- Memories are the notes agents kept between sessions (user preferences, project facts, feedback). `kb memory
  --project NAME` lists one project's; a memory names the session it came from (`kb summary <short>`).
- When you report a finding, cite it as `short [turn N]` so the user can open it.
- The KB lags a little: a session is synced after it has been idle 15 minutes. `kb sync --now --no-summaries` syncs now and is fast (plain `kb sync --now` also writes summaries and can take minutes). `kb status` shows the last sync and its error, if any.
- Reading needs no write access, except to build a missing or outdated index. If `kb` answers `index not built yet; run: kb reindex`, run that once from a shell that can write to the KB folder.
- All flags: `kb <command> --help`.
