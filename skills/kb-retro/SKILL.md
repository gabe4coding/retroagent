---
name: kb-retro
description: Use when the user asks for a retrospective, review or lessons learned over their own past Claude Code or Codex sessions — "retro of last week", "what went wrong on project X", "where do I lose time with agents", "what do I keep correcting" — built from the sessions KB with the `kb` CLI.
---

# kb-retro

Goal: changes to the agent's **environment** (steering files, checks, tools, docs) that make the next sessions better,
ranked by severity. Every claim points to evidence (`short [turn N]`). A diary of what happened is not a retro.

1. **Scope.** Period (default: last 7 days) and project (default: all), or one session if the user names it. Ask
   only if the request is unclear.
2. **Overview (cheap).**
   - `kb stats outcomes` and `kb stats projects`
   - `kb sql "SELECT short, substr(started,1,10) day, project, turns, outcome, title, summary FROM sessions WHERE parent='' AND started >= date('now','-7 days') ORDER BY started"`
   - Corrections saved as memories: `kb sql "SELECT project, name, description FROM memories WHERE type='feedback' AND modified >= date('now','-7 days')"`
3. **Pick at most 8 sessions** to look at closer: `abandoned` or `partial` outcomes, the longest ones, repeated topics.
4. **Find patterns with targeted queries, not full reads.**
   - User corrections: `kb find "actually" --since 7d`, `kb find "not what I asked" --since 7d`, `kb find "wrong" --since 7d`.
   - Errors that come back: `kb sql "SELECT (SELECT short FROM sessions WHERE id = session_id) short, n, substr(text, instr(text,'→ ERROR'), 160) err FROM turns WHERE text LIKE '%→ ERROR%' AND session_id IN (SELECT id FROM sessions WHERE started >= date('now','-7 days')) LIMIT 60"`, then group similar errors.
   - Heavy tool use: `kb sql "SELECT s.short, s.turns, SUM((length(t.text)-length(replace(t.text, char(10)||'- ', '')))/3) calls FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.role='assistant' AND s.parent='' AND s.started >= date('now','-7 days') GROUP BY s.id ORDER BY calls DESC LIMIT 5"`
   - Details only with `kb show <short> --turn N --around 1`.
5. **Sort findings into categories.** Each has a "use when" test; skip a category with no evidence.
   - **Navigation** — the agent took long to find a file or fact. Fix: a short pointer where it looked first.
   - **Automated checks** — the agent made a mistake a linter, type check, test or hook could catch. Before you
     propose one, read the repo's own check command and CI: a check that exists but is not wired, or is broken, is
     the finding. A repo with no pre-commit hook and no CI check is a finding too.
   - **Rules** — the user corrected the same thing more than once. A mechanical rule (banned API, file location,
     fixed pattern) becomes a check, not text. Only a judgement call becomes a written rule, and it goes where it is
     read: review rules in a coding-standards or review file, not in the always-loaded steering file.
   - **Steering bloat and no-ops** — `CLAUDE.md` / `AGENTS.md` (repo or global) is large, or has lines that did not
     change what the agent did. Fix: delete them, or move them to a doc, a skill or a check, leaving a pointer.
   - **Tool economy** — many or expensive tool calls for little result: whole-file reads, repeated searches, a CLI
     with long output. Fix: a cheaper command, a flag, or a small tool.
   - **Information access** — the agent lacked a key fact (server logs, a third-party service, a ticket). Fix: a way
     to read it, read-only.
6. **Check before you propose.** Read the file you want to change (`CLAUDE.md`, `AGENTS.md`, lint config, CI
   workflow, skill). Do not propose a rule that already exists: then the finding is why it did not work.
7. **Write the retro** (under one page, short sentences):
   - Findings ranked by severity (time or rework lost, how often it came back). Each: category, what happened with
     evidence, root cause (not the symptom), and the concrete change: which file, which check, which command.
   - What went well (1–3 bullets), only if it is worth keeping on purpose.
   - Do not apply the changes. The user picks which to make.
