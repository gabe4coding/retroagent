---
name: kb-retro
description: Use when the user asks for a retrospective, review or lessons learned over their own past Claude Code or Codex sessions — "retro of last week", "what went wrong on project X", "where do I lose time with agents", "what do I keep correcting" — built from the sessions KB with the `kb` CLI.
---

# kb-retro

Goal: a short retrospective where every claim points to evidence (`short [turn N]`).

1. **Scope.** Period (default: last 7 days) and project (default: all). Ask only if the request is unclear.
2. **Overview (cheap).**
   - `kb stats outcomes` and `kb stats projects`
   - `kb sql "SELECT short, substr(started,1,10) day, project, turns, outcome, title, summary FROM sessions WHERE parent='' AND started >= date('now','-7 days') ORDER BY started"`
3. **Pick at most 8 sessions** to look at closer: `abandoned` or `partial` outcomes, the longest ones, repeated topics.
4. **Find patterns with targeted queries, not full reads.**
   - User corrections: `kb find "actually" --since 7d`, `kb find "not what I asked" --since 7d`, `kb find "wrong" --since 7d`.
   - Errors that come back: `kb sql "SELECT (SELECT short FROM sessions WHERE id = session_id) short, n, substr(text, instr(text,'→ ERROR'), 160) err FROM turns WHERE text LIKE '%→ ERROR%' AND session_id IN (SELECT id FROM sessions WHERE started >= date('now','-7 days')) LIMIT 60"`, then group similar errors.
   - Details only with `kb show <short> --turn N --around 1`.
5. **Write the retro** (under one page, short sentences):
   - What went well (2–4 bullets).
   - What cost time, with the root cause, not the symptom (2–5 bullets).
   - Corrections the user repeated: candidate rules for CLAUDE.md, AGENTS.md or a skill.
   - 1–3 concrete changes to try next, each linked to evidence.
