---
name: kb-stats
description: Use when the user asks for numbers about their own Claude Code or Codex usage — how many sessions, which projects, Claude vs Codex, models, outcomes, top topics, activity per day — answered from the sessions KB with `kb stats` and `kb sql`.
---

# kb-stats

Ready-made reports:
- `kb stats overview` — sessions, turns, prompts (typed by the user; messages from other agents do not count) and date range per agent
- `kb stats projects` — top projects
- `kb stats agents` — sessions per agent and model
- `kb stats outcomes` — done / partial / abandoned / question
- `kb stats tags` — top summary tags
- `kb stats daily` — sessions and prompts per day, last 30 days
- `kb stats subagents` — subagent transcripts per agent
- `kb stats errors [--since 7d] [--project P]` — tool errors that came back in 2 or more sessions, with the session
  and turn where each was first seen (`kb show <short> --turn N`)

Custom questions: `kb sql "<SELECT …>"` (read-only). Cells over 80 chars end with `…`; add `--width 0` to see them whole.
Before you write a custom query, read `references/schema.md` in this skill's folder: the tables and columns.

Report the numbers exactly as returned and say which filters you used.
