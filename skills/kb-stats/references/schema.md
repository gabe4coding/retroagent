# `kb sql` schema

- `sessions(id, agent, host, project, cwd, branch, started, ended, model, turns, user_turns, title, summary, tags, outcome, decisions, summary_turns, files, prs, parent, first_prompt, md_path, short)`. `short` is the 8-character id that `kb` prints and accepts. `parent = ''` means a top-level session. `tags`, `decisions`, `files`, `prs` are JSON arrays (`json_each(tags)`).
- `turns(session_id, n, role, time, text)`. Tool calls are lines in `text` that start with `- ToolName`.
- `pages(path, kind, name, title, updated, sessions)`: pages written by the cloud routine. `kind` is `project` or
  `retro`; `sessions` is how many sessions the page cites.
- `memories(path, ref, agent, host, project, name, description, type, origin_session, modified)`: memory files.
  `type` is the memory's type when it has one (`user`, `feedback`, `project`, `reference`); `origin_session` is the
  session that wrote it.
- Dates are UTC ISO strings: `started >= date('now','-30 days')`.
