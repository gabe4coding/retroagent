---
name: kb-stats
description: Use when the user asks for numbers about their own Claude Code or Codex usage — how many sessions, which projects, Claude vs Codex, models, outcomes, top topics, activity per day — answered from the sessions KB with `kb stats` and `kb sql`.
---

# kb-stats

Ready-made reports (fast, small output):
- `kb stats overview` — sessions, turns, prompts and date range per agent
- `kb stats projects` — top projects
- `kb stats agents` — sessions per agent and model
- `kb stats outcomes` — done / partial / abandoned / question
- `kb stats tags` — top summary tags
- `kb stats daily` — sessions and prompts per day, last 30 days
- `kb stats subagents` — subagent transcripts per agent

Custom questions: `kb sql "<SELECT …>"` (read-only).
- `sessions(id, agent, host, project, cwd, branch, started, ended, model, turns, user_turns, title, summary, tags, outcome, decisions, summary_turns, files, prs, parent, first_prompt, md_path, short)`. `short` is the 8-character id that `kb` prints and accepts. `parent = ''` means a top-level session. `tags`, `decisions`, `files`, `prs` are JSON arrays (`json_each(tags)`).
- `turns(session_id, n, role, time, text)`. Tool calls are lines in `text` that start with `- ToolName`.
- Dates are UTC ISO strings: `started >= date('now','-30 days')`.

Report the numbers exactly as returned and say which filters you used.
