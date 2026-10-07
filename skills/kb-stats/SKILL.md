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

Custom questions: `kb sql "<SELECT …>"` (read-only).
Before you write a custom query, read `references/schema.md` in this skill's folder: the tables and columns.

Report the numbers exactly as returned and say which filters you used.
