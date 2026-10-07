"""Ready-made analytics reports (SQL on the index). Top-level sessions only unless the name says otherwise."""
import re

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

# `kb stats errors` groups tool errors by a signature, which SQL cannot build, so it is Python, not a REPORTS entry.
ERROR_MARK = " → ERROR: "
_ERROR_LINE = re.compile(r"^- (\S+).*? → ERROR: (.*)$")         # a tool call line, as distill writes it
_PATH = re.compile(r"(?:~|\.{1,2})?(?:/[\w.@~+-]+)+/?")
_NOISE = re.compile(r"^(?:exit code \d+\s*/?\s*)|</?tool_use_error>|traceback \(most recent call last\):\s*/?"
                    r"|file \"[^\"]*\", line \d+(?:, in \S+)?\s*/?", re.I)
_STOP = set("the and for with not this that from are was has have you your its use path error".split())
SIGNATURE_WORDS = 7


def error_signature(err: str) -> str:
    """The same error from two sessions, with paths, numbers, ids and quoted values left out. Empty: no message."""
    e = _NOISE.sub(" ", err)
    e = _PATH.sub(" ", e)
    e = re.sub(r"`[^`]*`|'[^']*'|\"[^\"]*\"", " ", e)
    e = re.sub(r"\b[0-9a-f]{7,}\b|\d+", " ", e)
    words = [w for w in re.findall(r"[a-z_]{3,}", e.lower()) if w not in _STOP]
    return " ".join(words[:SIGNATURE_WORDS]) if len(words) >= 2 else ""


def repeated_errors(con, since: str = "", project: str = "", limit: int = 30):
    """Tool errors that came back in two or more top-level sessions (a subagent's count for its parent), most sessions
    first. Errors with no message (a grep that found nothing) are left out. Returns (columns, rows)."""
    sql = ("SELECT s.id, s.parent, s.short, s.started, t.n, t.text FROM turns t JOIN sessions s ON s.id = t.session_id "
           "WHERE t.text LIKE ? AND s.started >= ?" + (" AND s.project = ?" if project else "")
           + " ORDER BY s.started, t.n")
    args = ["%" + ERROR_MARK + "%", since] + ([project] if project else [])
    groups = {}
    for sid, parent, short, started, n, text in con.execute(sql, args):
        for line in text.splitlines():
            m = _ERROR_LINE.match(line)
            sig = error_signature(m.group(2)) if m else ""
            if not sig:
                continue
            g = groups.setdefault(sig, {"roots": set(), "errors": 0, "first": started, "last": started,
                                        "tool": m.group(1), "where": f"{short} [turn {n}]", "example": m.group(2)})
            g["roots"].add(parent or sid)
            g["errors"] += 1
            g["last"] = started
    rows = [(len(g["roots"]), g["errors"], g["first"][:10], g["last"][:10], sig, g["where"], g["tool"], g["example"])
            for sig, g in groups.items() if len(g["roots"]) >= 2]
    rows.sort(key=lambda r: (-r[0], -r[1], r[4]))
    # first_seen, tool and example are from the first time the error was seen
    return ["sessions", "errors", "first", "last", "signature", "first_seen", "tool", "example"], rows[:limit]
