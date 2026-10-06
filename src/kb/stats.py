"""Ready-made analytics reports (SQL on the index). Top-level sessions only unless the name says otherwise."""
REPORTS = {
    "overview": "SELECT agent, COUNT(*) AS sessions, SUM(turns) AS turns, SUM(user_turns) AS prompts, "
                "MIN(substr(started,1,10)) AS first, MAX(substr(started,1,10)) AS last "
                "FROM sessions WHERE parent='' GROUP BY agent ORDER BY sessions DESC",
    "projects": "SELECT project, COUNT(*) AS sessions, SUM(turns) AS turns, MAX(substr(started,1,10)) AS last "
                "FROM sessions WHERE parent='' GROUP BY project ORDER BY sessions DESC LIMIT 30",
    "agents": "SELECT agent, COALESCE(NULLIF(model,''),'(unknown)') AS model, COUNT(*) AS sessions "
              "FROM sessions WHERE parent='' GROUP BY agent, model ORDER BY sessions DESC",
    "outcomes": "SELECT COALESCE(NULLIF(outcome,''),'(none)') AS outcome, COUNT(*) AS sessions "
                "FROM sessions WHERE parent='' GROUP BY 1 ORDER BY sessions DESC",
    "tags": "SELECT j.value AS tag, COUNT(*) AS sessions FROM sessions, json_each(sessions.tags) AS j "
            "WHERE sessions.parent='' GROUP BY j.value ORDER BY sessions DESC LIMIT 40",
    "daily": "SELECT substr(started,1,10) AS day, COUNT(*) AS sessions, SUM(user_turns) AS prompts "
             "FROM sessions WHERE parent='' AND started >= date('now','-30 days') GROUP BY day ORDER BY day",
    "subagents": "SELECT agent, COUNT(*) AS subagents, COUNT(DISTINCT parent) AS parents "
                 "FROM sessions WHERE parent!='' GROUP BY agent",
}
