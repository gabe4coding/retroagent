"""The steps the cloud routine runs around Claude's writing. The routine follows scripts/pages-routine.md.

  kb pages start    pick the branch (main, or the bootstrap branch until the first build is merged), build the index
  kb pages plan     what to write this run: project pages and weekly retros, from the sessions changed since the
                    watermark (printed as JSON and kept in .kb/pages-plan.json for finish)
  kb pages digest   compact input for one page: the memories agents kept, then one block per session (summary,
                    decisions, outcome, files, PRs)
  kb pages finish   check the pages, move the watermark, commit with [skip ci], push
  kb pages due      for the trigger workflow: is there anything to write? (no LLM, about a second)

Everything that needs no judgement is decided here. Claude only writes the pages.

Terms:
- watermark: the commit sha in pages/.state.json ("sha") that the last finished run planned from. The next run reads
  only what changed after it.
- memory change: a memory file (memories/<host>/…, copied by kb sync) that is added, changed or removed. It counts as
  a change of its project, like a session. Claude Code's MEMORY.md files only index the other memories, so they are
  left out.
- grown session: a session that a page cites and that ended after the page was written. The page saw only its start.
- uncited session: a session that worked in a project with a page, but that the page does not cite.

When the routine runs:
- The routine's commits change only pages/ and carry [skip ci]. So they never start the trigger workflow
  (.github/workflows/pages-trigger.yml).
- The workflow fires the routine only when `kb pages due` says there is work.
- It fires the routine at most once every min_hours_between_fires hours, because routine runs are counted per day.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from kb import freshness, gitops, ledger, memedits
from kb.index import Index, connect_readonly, parse_memory
from kb.pages import WEEK_RE, page_rel, parse_page, section, set_fields
from kb.redact import redact
from kb.stats import signature_sessions
from kb.util import atomic_write, short_id, slug

CONFIG_REL = "pages/config.json"
STATE_REL = "pages/.state.json"
PLAN_FILE = "pages-plan.json"            # in .kb/ (ignored by git): written by plan, read by finish
DEFAULTS = {
    "min_sessions": 3,                   # a project gets a page from this many top-level sessions on
    "skip_projects": ["scratch", "unknown"],
    "batch_projects": 5,                 # pages written per run; the rest waits in the state's pending list
    "batch_retros": 2,
    "summary_wait_hours": 24,            # a session that will get a summary waits this long for it before it is used
    "timezone": "Europe/Rome",           # retro weeks run Monday to Sunday in this zone
    "retro_weeks_back": 4,               # closed weeks that get a retro when they have none
    "retro_late_days": 14,               # a retro is rewritten when new sessions of its week arrive this late
    "max_page_chars": 40000,
    "stale_days": freshness.STALE_DAYS,  # an aging bullet this much older than the page's newest moves to History
    "stale_days_current": freshness.STALE_DAYS_CURRENT,   # the same for "Current state"
    "stale_days_threads": freshness.STALE_DAYS_THREADS,   # the same for "Open threads"
    "max_current_bullets": 10,           # "Current state" of a written page: past this, finish asks to compact it
    "max_open_threads": 8,               # the same for "Open threads"
    "min_hours_between_fires": 3,        # the trigger workflow fires the routine at most this often
    "branch": "main",
    "bootstrap_branch": "claude/pages-bootstrap",
}
_AT_LEAST_ONE = ("stale_days", "stale_days_current", "stale_days_threads", "max_current_bullets", "max_open_threads")
# The "since" mode of _changes reads the commits made after (last run - SINCE_MARGIN). The margin also catches a
# commit that was made before the last run but pushed after it.
SINCE_MARGIN = dt.timedelta(days=2)
DIGEST_CHARS = 150_000
MEMORY_CHARS = 2_500                     # text of one memory in a digest; longer ones are cut (kb memory reads it all)
MEMORY_DIGEST_CHARS = 40_000             # memories in one digest; past this the rest is listed one line each
_FIRST_PROMPT_CHARS = 400                # first prompt shown
_DIGEST_FILES = 8                        # files listed per session
_MEMORY_LINE_CHARS = 200                 # one-line memory description
_SKIP_IDS_SHOWN = 5                      # ids per error
_REPORT_LINE_CHARS = 120                 # undated/moved bullet text
# Parent links followed up from a subagent to find its top-level session. A session still deeper is skipped.
_SUBAGENT_DEPTH = 5
# kb sync summarizes only top-level sessions with this many user turns or more. Keep it equal to the rule in sync.py.
_SUMMARY_MIN_USER_TURNS = 2
_UTC = dt.timezone.utc


class PagesError(Exception):
    pass


# ---- settings and state

def load_settings(root) -> dict:
    """DEFAULTS overridden by pages/config.json (the owner's file; keys it does not know are ignored)."""
    path = Path(root) / CONFIG_REL
    out = dict(DEFAULTS)
    if not path.is_file():
        return out
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise PagesError(f"{CONFIG_REL}: {e}") from None
    if not isinstance(raw, dict):
        raise PagesError(f"{CONFIG_REL}: must be a JSON object")
    for key, value in raw.items():
        if key not in DEFAULTS:
            continue
        want = type(DEFAULTS[key])
        if want is list:
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        elif want is int:
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= (1 if key in _AT_LEAST_ONE else 0)
        else:
            ok = isinstance(value, want) and bool(value)
        if not ok:
            if want is list:
                what = "a list of text"
            elif key in _AT_LEAST_ONE:
                what = "a whole number of at least 1"
            else:
                what = want.__name__
            raise PagesError(f"{CONFIG_REL}: {key} must be {what}")
        out[key] = value
    return out


def load_state(root):
    """The committed state, or None before the first build. A broken file counts as an empty state (no watermark)."""
    path = Path(root) / STATE_REL
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _strings(value) -> list:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


# ---- time

def _parse(iso: str):
    try:
        d = dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_UTC)


def _iso(d: dt.datetime) -> str:
    return d.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def zone(name: str):
    """The time zone, or UTC when this machine has no time zone data for it."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, or no tz database at all
        return _UTC


def week_of(iso: str, tz) -> str:
    """ISO week label ("2026-W41") of a UTC time, in the zone tz. "" if the time cannot be read."""
    d = _parse(iso)
    if d is None:
        return ""
    year, week, _ = d.astimezone(tz).isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(label: str, tz):
    """(start, end) aware datetimes of a week: Monday 00:00 to the next Monday 00:00 in tz."""
    monday = dt.date.fromisocalendar(int(label[:4]), int(label[6:]), 1)
    start = dt.datetime.combine(monday, dt.time(), tzinfo=tz)
    end = dt.datetime.combine(monday + dt.timedelta(days=7), dt.time(), tzinfo=tz)
    return start, end


def closed_weeks(now: dt.datetime, tz, settings) -> list:
    """The last retro_weeks_back weeks that ended at least summary_wait_hours ago, newest first."""
    wait = dt.timedelta(hours=settings["summary_wait_hours"])
    local = now.astimezone(tz)
    monday = local.date() - dt.timedelta(days=local.weekday())
    out = []
    for i in range(1, settings["retro_weeks_back"] + 2):
        day = monday - dt.timedelta(days=7 * i)
        year, week, _ = day.isocalendar()
        label = f"{year}-W{week:02d}"
        if week_bounds(label, tz)[1] + wait <= now:
            out.append(label)
    return out[: settings["retro_weeks_back"]]


# ---- git

def _git(root, *args, check=True, timeout=gitops.DEFAULT_TIMEOUT):
    return gitops.git(root, *args, check=check, timeout=timeout)


def _ok(root, *args) -> bool:
    return _git(root, *args, check=False).returncode == 0


def _has_state(root, ref: str) -> bool:
    return _ok(root, "cat-file", "-e", f"{ref}:{STATE_REL}")


def _md_paths(out: str) -> set:
    return {line for line in out.splitlines() if line.startswith("sessions/") and line.endswith(".md")}


def _is_index(path: str) -> bool:
    """Claude Code's memories/<host>/claude/<folder>/MEMORY.md: an index of the folder's other memories, no facts."""
    parts = path.split("/")
    return len(parts) == 5 and parts[2] == "claude" and parts[4] == "MEMORY.md"


def _memory_paths(out: str) -> set:
    return {line for line in out.splitlines()
            if line.startswith("memories/") and line.endswith(".md") and not _is_index(line)}


def _memory_changes(root, mode: str, base: str, head: str):
    """The memory files added, changed or removed since the watermark (see _changes); None means every memory. A file
    whose only change is its `modified` line (a sync that dates old memories) is no change: its facts are the same."""
    if mode == "incremental":
        paths = _memory_paths(_git(root, "diff", "--name-only", "--no-renames", base, head, "--", "memories/").stdout)
        return paths - _date_only(root, base, head)
    if mode == "since":
        paths = _memory_paths(_git(root, "log", f"--since={base}", "--name-only", "--format=", head, "--",
                                   "memories/").stdout)
        before = _git(root, "rev-list", "-1", f"--before={base}", head, check=False).stdout.strip()
        return paths - _date_only(root, before, head) if before else paths
    return None


_DATE_LINE = re.compile(r"^[+-]modified: ")


def _date_only(root, base: str, head: str) -> set:
    """The memory files whose every changed line between base and head is their `modified` front matter line."""
    out = _git(root, "diff", "-U0", "--no-renames", "--no-color", base, head, "--", "memories/").stdout
    files, path, only_date_changed = set(), None, False
    # A file is judged when the next "diff --git" header starts. The extra header at the end makes the last file judged
    # too.
    for line in out.splitlines() + ["diff --git end"]:
        if line.startswith("diff --git "):
            if path and only_date_changed:
                files.add(path)
            path, only_date_changed = None, True
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith(("+++ ", "--- ")):
            continue
        elif line.startswith(("+", "-")) and not _DATE_LINE.match(line):
            only_date_changed = False
    return files


def _removed_memory(root, head: str, path: str):
    """{ref, project, removed} of a memory file that HEAD no longer holds, from its last version. None when HEAD still
    holds it (a file the index could not read) or no old version can be read."""
    if _ok(root, "cat-file", "-e", f"{head}:{path}"):
        return None
    last = _git(root, "rev-list", "-1", head, "--", path, check=False).stdout.strip()
    old = _git(root, "show", f"{last}^:{path}", check=False) if last else None
    if old is None or old.returncode != 0:
        return None
    try:
        meta, _ = parse_memory(path, old.stdout)
    except ValueError:
        return None
    return {"ref": meta["ref"], "project": meta["project"], "removed": True}


def _changes(root, state, head: str):
    """(mode, base, paths): the session files changed since the watermark. paths None means every session.

    The mode says how the changes were found:
    - bootstrap: there is no state file yet (the first build). Every session; base is "".
    - incremental: the watermark is an ancestor of HEAD. The sessions added or changed between the watermark and HEAD;
      base is the watermark sha.
    - since: the watermark is not in the history (a squash merge, a rewritten history), but the state has a last_run
      time. The sessions of the commits made since last_run minus SINCE_MARGIN; base is that time.
    - rebuild: the state has no usable watermark and no last_run. Every session; base is "".
    """
    if state is None:
        return "bootstrap", "", None
    sha = state.get("sha") if isinstance(state.get("sha"), str) else ""
    if sha and _ok(root, "merge-base", "--is-ancestor", sha, head):
        out = _git(root, "diff", "--name-only", "--no-renames", "--diff-filter=AM", sha, head, "--", "sessions/").stdout
        return "incremental", sha, _md_paths(out)
    last = _parse(state.get("last_run") if isinstance(state.get("last_run"), str) else "")
    if last is not None:                 # the watermark is gone (squash merge, rewritten history): go by time
        since = _iso(last - SINCE_MARGIN)
        out = _git(root, "log", f"--since={since}", "--name-only", "--format=", head, "--", "sessions/").stdout
        return "since", since, _md_paths(out)
    return "rebuild", "", None


# ---- start

def start(root, index_path, settings) -> dict:
    """Check out the branch this run writes to and build the index.

    Until main holds pages/.state.json the run works on the bootstrap branch (continuing it when it exists, with main
    merged in), so the first build reaches main only through a reviewed pull request."""
    root = Path(root)
    main, boot = settings["branch"], settings["bootstrap_branch"]
    remote = gitops.has_remote(root)
    if remote:
        # a cloud checkout may be shallow (the watermark diff needs history) or hold only the default branch
        if _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            _git(root, "fetch", "--quiet", "--unshallow", "origin", timeout=gitops.PUSH_TIMEOUT)
        for b in (main, boot):
            _git(root, "fetch", "--quiet", "origin", f"+refs/heads/{b}:refs/remotes/origin/{b}", check=False,
                 timeout=gitops.PULL_TIMEOUT)
    if _has_state(root, f"origin/{main}" if remote else main):
        if gitops.current_branch(root) != main:
            _git(root, "checkout", "--quiet", main)
        if remote:
            _git(root, "merge", "--ff-only", "--quiet", f"origin/{main}")
        target = main
    else:
        target = boot
        if remote and _ok(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{boot}"):
            _git(root, "checkout", "--quiet", "-B", boot, f"origin/{boot}")
            if not _ok(root, "merge", "--quiet", "--no-edit", f"origin/{main}"):
                _git(root, "merge", "--abort", check=False)
                raise PagesError(f"cannot merge origin/{main} into {boot}; fix the branch by hand")
        elif gitops.current_branch(root) != boot:
            _git(root, "checkout", "--quiet", "-B", boot)
    idx = Index(index_path)
    try:
        idx.update(root)
        sessions = idx.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        pages = idx.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        memories = idx.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        idx.close()
    return {"branch": target, "bootstrap": target == boot, "sessions": sessions, "memories": memories, "pages": pages}


# ---- plan

def make_plan(root, idx: Index, settings, now=None) -> dict:
    """What this run writes, and what stays pending for later runs. Also saved to .kb/pages-plan.json."""
    root = Path(root)
    now = now or dt.datetime.now(_UTC)
    tz = zone(settings["timezone"])
    state = load_state(root)             # None before the first build: _changes needs to know that
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    mode, base, paths = _changes(root, state, head)
    state = state or {}

    rows = [dict(r) for r in idx.db.execute(
        "SELECT id, short, project, parent, started, ended, summary, user_turns, md_path, cwd, files, prs FROM sessions")]
    by_id = {r["id"]: r for r in rows}
    top = [r for r in rows if not r["parent"]]
    if paths is None:
        changed = list(top)
    else:
        by_path = {r["md_path"]: r for r in rows}
        changed = [by_path[p] for p in sorted(paths) if p in by_path]
    changed += [by_id[i] for i in _strings(state.get("waiting")) if i in by_id]
    ready, waiting = _ready_and_waiting(changed, by_id, now, dt.timedelta(hours=settings["summary_wait_hours"]))

    # memories: {path: {ref, project}} of those in the index; removed ones are read from git
    memories = {r["path"]: {"ref": r["ref"], "project": r["project"]}
                for r in idx.db.execute("SELECT path, ref, project FROM memories")}
    changed_memories = _memory_changes(root, mode, base, head)
    if changed_memories is None:
        changed_memories = {p for p in memories if not _is_index(p)}

    def memory(path):
        if path not in memories:
            memories[path] = _removed_memory(root, head, path)
        return memories[path]

    grown = _grown(root, top, set(waiting))
    uncited = _uncited(root, top, set(waiting))
    projects, left_projects, todo = _plan_projects(root, top, ready, state, settings, changed_memories, memory, grown,
                                                   uncited)
    retros, left_weeks = _plan_retros(root, top, ready, state, settings, tz, now, grown)
    plan = {"version": 1, "mode": mode, "base": base, "head": head, "branch": gitops.current_branch(root),
            "created": _iso(now), "projects": projects, "retros": retros,
            "pending": {"projects": left_projects, "weeks": left_weeks}, "waiting": sorted(waiting)}
    saved = {**plan, "todo": todo}       # what each planned project was for: finish keeps it pending if not written
    atomic_write(root / ".kb" / PLAN_FILE, (json.dumps(saved, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return plan


def _ready_and_waiting(changed: list, by_id: dict, now, wait):
    """({id: row} of the top-level sessions ready to be written about, [ids] of those that wait for a summary).

    A subagent counts as a change of its top-level session. A session with no summary yet waits up to `wait` after it
    ended, unless it will never get a summary."""
    ready, waiting = {}, []
    for r in changed:
        for _ in range(_SUBAGENT_DEPTH):
            if r is None or not r["parent"]:
                break
            r = by_id.get(r["parent"])
        if r is None or r["parent"] or r["id"] in ready or r["id"] in waiting:
            continue
        last_activity = _parse(r["ended"] or r["started"])
        will_get_summary = (r["user_turns"] or 0) >= _SUMMARY_MIN_USER_TURNS
        if not r["summary"] and will_get_summary and last_activity is not None and now - last_activity < wait:
            waiting.append(r["id"])
        else:
            ready[r["id"]] = r
    return ready, waiting


def _eligible(project: str, counts: dict, settings) -> bool:
    if not project or project.casefold() in {p.casefold() for p in settings["skip_projects"]}:
        return False
    if counts.get(project, 0) < settings["min_sessions"]:
        return False
    try:
        page_rel("project", project)
    except ValueError:
        return False
    return True


def _plan_projects(root: Path, top: list, ready: dict, state: dict, settings, changed_memories=(), memory=None,
                   grown=None, uncited=None):
    """(project items to write, {project: todo} that stays pending, {project: todo} of each planned project).

    A project's todo is the set of what changed in it. It holds session short ids and memory paths (they start with
    "memories/"). Only the first batch_projects projects, most recent activity first, are planned this run. The others
    stay pending for later runs.

    grown and uncited are {page rel: {short}} from _grown and _uncited. Their sessions are planned again. An item lists
    the grown ones as "grown", and the uncited ones of other projects as "related"."""
    memory = memory or (lambda path: None)
    grown, uncited = grown or {}, uncited or {}
    counts, latest, members = {}, {}, {}
    for r in top:
        p = r["project"]
        counts[p] = counts.get(p, 0) + 1
        latest[p] = max(latest.get(p, ""), r["started"] or "")
        members.setdefault(p, []).append(r)
    todo = _collect_todo(ready, state, changed_memories, memory, grown, uncited)
    todo = {p: s for p, s in todo.items() if s and _eligible(p, counts, settings)}  # the config may have changed
    order = sorted(todo, key=lambda p: (latest.get(p, ""), p), reverse=True)        # most recent activity first
    batch = order[: settings["batch_projects"]]
    started = {r["short"]: r["started"] or "" for r in top}
    items = [_project_item(root, p, todo[p], top, members, started, memory, grown) for p in batch]
    return items, {p: sorted(todo[p]) for p in order[len(batch):]}, {p: sorted(todo[p]) for p in batch}


def _collect_todo(ready: dict, state: dict, changed_memories, memory, grown: dict, uncited: dict) -> dict:
    """{project: todo}: the pending todo of the state, plus this run's changes. A memory whose file and old version are
    both gone is dropped."""
    pend = state.get("pending") if isinstance(state.get("pending"), dict) else {}
    old = pend.get("projects") if isinstance(pend.get("projects"), dict) else {}
    todo = {p: set(_strings(v)) for p, v in old.items()}
    for r in ready.values():
        todo.setdefault(r["project"], set()).add(r["short"])
    for found in (grown, uncited):
        for rel, shorts in found.items():
            if rel.startswith("pages/projects/"):
                todo.setdefault(rel[len("pages/projects/"):-3], set()).update(shorts)
    for path in changed_memories:
        m = memory(path)
        if m:
            todo.setdefault(m["project"], set()).add(path)
    for p in todo:
        todo[p] = {e for e in todo[p] if not e.startswith("memories/") or memory(e)}
    return todo


def _project_item(root: Path, p: str, todo: set, top: list, members: dict, started: dict, memory, grown: dict) -> dict:
    """The plan item of one project: an update of its page with what is in todo, or a new page."""
    rel = page_rel("project", p)
    if not (root / rel).is_file():       # a new page is written from the whole history of the project
        return {"name": p, "page": rel, "action": "create",
                "sessions": [r["short"] for r in sorted(members[p], key=lambda r: (r["started"] or "", r["id"]))]}
    shorts = [e for e in todo if not e.startswith("memories/")]
    item = {"name": p, "page": rel, "action": "update",
            "sessions": sorted(shorts, key=lambda s: (started.get(s, ""), s))}
    paths = sorted(e for e in todo if e.startswith("memories/"))
    kept = [e for e in paths if not _removed(memory(e))]
    if kept:
        item["memories"] = kept
    gone = sorted({memory(e)["ref"] for e in paths if _removed(memory(e))})
    if gone:
        item["memories_removed"] = gone
    if grown.get(rel):
        item["grown"] = sorted(grown[rel] & set(shorts), key=lambda s: (started.get(s, ""), s))
    other = {r["short"] for r in top if r["short"] in shorts and r["project"] != p}
    if other:
        item["related"] = sorted(other, key=lambda s: (started.get(s, ""), s))
    return item


def _removed(m) -> bool:
    return bool(m) and m.get("removed", False)


def _sources(path: Path) -> set:
    return set(_page_meta(path)[0])


def _page_meta(path: Path):
    """(sources, updated) of a page file, ([], "") when it cannot be read."""
    try:
        meta = parse_page(path.read_text(encoding="utf-8", errors="replace"))[0]
    except (OSError, ValueError):
        return [], ""
    return meta["sources"], meta["updated"]


_PR = re.compile(r"github\.com/[^/\s]+/([^/\s]+)/pull/\d+")


def _json_list(text) -> list:
    try:
        v = json.loads(text or "[]")
    except ValueError:
        return []
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _projects_of(top: list, names: set) -> dict:
    """{session id: the projects with a page that the session worked in}. names: the projects with a page.

    - A session works in its own project.
    - It also works in every project whose repo it changed files in or opened a PR in.
    - A project's repo folders are the working folders of its sessions that are named after it (~/Repositories/alpha).
    - A folder name alone is not enough: alpha/ inside another repo is not the alpha project.
    - For a cloud session, the repos are the folders of /home/user.
    - A PR's repo maps to the project with the same name.
    - Else it maps to the project whose sessions opened the most PRs in it (repo alpha → project alpha-app).
    """
    from kb.setup import CLOUD_HOME
    name_by_lower = {n.lower(): n for n in names}
    repo_dir_to_project, pr_repo_votes = {}, {}
    for r in top:
        cwd = os.path.normpath(r["cwd"] or "/")
        if r["project"] in names and os.path.basename(cwd).lower() == r["project"].lower():
            repo_dir_to_project.setdefault(cwd, r["project"])
        for url in _json_list(r["prs"]):
            m = _PR.search(url)
            if m and r["project"] in names:
                counts = pr_repo_votes.setdefault(slug(m.group(1)), {})
                counts[r["project"]] = counts.get(r["project"], 0) + 1
    repos = {repo: max(counts, key=lambda p: (counts[p], p)) for repo, counts in pr_repo_votes.items()}
    out = {}
    for r in top:
        found = {r["project"]} & names
        cwd = os.path.normpath(r["cwd"] or "/")
        for f in _json_list(r["files"]):
            path = os.path.normpath(f if f.startswith("/") else os.path.join(cwd, f))
            found.update(p for root_dir, p in repo_dir_to_project.items() if path.startswith(root_dir + "/"))
            if cwd == CLOUD_HOME:
                parts = os.path.relpath(path, CLOUD_HOME).split("/")
                if len(parts) >= 2 and parts[0].lower() in name_by_lower:
                    found.add(name_by_lower[parts[0].lower()])
        for url in _json_list(r["prs"]):
            m = _PR.search(url)
            repo = slug(m.group(1)) if m else ""
            if repo in name_by_lower or repo in repos:
                found.add(name_by_lower.get(repo) or repos[repo])
        if found:
            out[r["id"]] = found
    return out


def _uncited(root: Path, top: list, waiting=()) -> dict:
    """Uncited sessions: those that worked in a project with a page (_projects_of), but that its page does not cite.

    Returns {page rel: {short}}. The page is then brought in line with the data, whatever folder a session started in.
    A session still waiting for its summary is left for later."""
    folder = root / "pages" / "projects"
    cited = {path.stem: set(_page_meta(path)[0]) for path in sorted(folder.glob("*.md"))} if folder.is_dir() else {}
    out, shorts = {}, {r["id"]: r["short"] for r in top}
    for sid, projects in _projects_of([r for r in top if r["id"] not in waiting], set(cited)).items():
        short = shorts[sid]
        for p in projects:
            if short not in cited[p]:
                out.setdefault(f"pages/projects/{p}.md", set()).add(short)
    return out


def _grown(root: Path, top: list, waiting=()) -> dict:
    """Grown sessions: those a project page or retro cites that ended after the page was written.

    Returns {page rel: {short}}. The page saw only their start: it was written while they ran, or a run skipped their
    new part. Every page is checked, not only those of the sessions changed since the watermark. So a gap left by an
    earlier run is found too. A session still waiting for its summary is left for later."""
    ended = {r["short"]: r["ended"] or "" for r in top if r["id"] not in waiting}
    out = {}
    for folder in ("projects", "retro"):
        for path in sorted((root / "pages" / folder).glob("*.md")):
            sources, updated = _page_meta(path)
            if not updated:
                continue
            late = {s for s in sources if ended.get(s, "") > updated}
            if late:
                out[f"pages/{folder}/{path.name}"] = late
    return out


def _plan_retros(root: Path, top: list, ready: dict, state: dict, settings, tz, now, grown=None):
    weeks = {}
    for r in top:
        w = week_of(r["started"], tz)
        if w:
            weeks.setdefault(w, []).append(r)
    pend = state.get("pending") if isinstance(state.get("pending"), dict) else {}
    todo = {w for w in _strings(pend.get("weeks")) if WEEK_RE.match(w)}
    late = dt.timedelta(days=settings["retro_late_days"])
    unnumbered = set()                   # retros whose suggestions have no id (written before kb.ledger): once more
    for w in closed_weeks(now, tz, settings):
        rel = page_rel("retro", w)
        if not (root / rel).is_file():
            todo.add(w)
            continue
        if _unnumbered_suggestions(root / rel):
            unnumbered.add(w)
            todo.add(w)
        if now - week_bounds(w, tz)[1] <= late:
            new = {r["short"] for r in ready.values() if week_of(r["started"], tz) == w}
            if new - _sources(root / rel) or (grown or {}).get(rel):
                todo.add(w)
    order = sorted((w for w in todo if w in weeks), reverse=True)
    batch = order[: settings["batch_retros"]]
    items = []
    for w in batch:
        rel = page_rel("retro", w)
        start, end = week_bounds(w, tz)
        items.append({"week": w, "page": rel, "action": "update" if (root / rel).is_file() else "create",
                      "from": start.date().isoformat(), "to": (end.date() - dt.timedelta(days=1)).isoformat(),
                      "since": _iso(start), "until": _iso(end),
                      "sessions": [r["short"] for r in sorted(weeks[w], key=lambda r: (r["started"] or "", r["id"]))]})
        if (grown or {}).get(rel):
            items[-1]["grown"] = sorted((grown or {})[rel])
        if w in unnumbered:
            items[-1]["review_suggestions"] = True
    return items, order[len(batch):]


def _unnumbered_suggestions(path: Path) -> bool:
    """True when a retro has "Suggested changes" bullets without an id: written before the ledger (kb.ledger), so
    `kb suggestions` and `kb decide` do not know them."""
    try:
        body = parse_page(path.read_bytes().decode("utf-8", errors="replace"))[1]
    except (OSError, ValueError):
        return False
    return any(not it["id"] for it in ledger.items(body))


def due(plan: dict, settings) -> dict:
    """Whether the routine has anything to write, from a plan (make_plan). For the trigger workflow."""
    projects, retros = len(plan["projects"]), len(plan["retros"])
    left = len(plan["pending"]["projects"]) + len(plan["pending"]["weeks"])
    reason = (f"{projects} project page{'s' * (projects != 1)} and {retros} retro{'s' * (retros != 1)} to write"
              if projects or retros else f"nothing to write ({len(plan['waiting'])} sessions waiting for a summary)")
    return {"due": bool(projects or retros), "reason": reason, "mode": plan["mode"], "pending_after": left,
            "min_hours_between_fires": settings["min_hours_between_fires"]}


# ---- digest

def _block(r: dict, subagents: int) -> str:
    out = [f"### {r['short']} · {(r['started'] or '')[:10]} · {r['agent']} · {r['project']} · "
           f"outcome: {r['outcome'] or '-'}", f"title: {r['title']}"]
    if r["summary"]:
        out.append("summary: " + " ".join(r["summary"].split()))
    else:
        first_prompt = " ".join((r["first_prompt"] or "").split())
        out.append("summary: (none yet) · first prompt: " + first_prompt[:_FIRST_PROMPT_CHARS])
    out += [f"- decision: {d}" for d in json.loads(r["decisions"] or "[]")]
    tags = json.loads(r["tags"] or "[]")
    if tags:
        out.append("tags: " + ", ".join(tags))
    files = json.loads(r["files"] or "[]")
    if files:
        out.append("files: " + ", ".join(files[:_DIGEST_FILES])
                   + (f" (+{len(files) - _DIGEST_FILES})" if len(files) > _DIGEST_FILES else ""))
    out += [f"pr: {p}" for p in json.loads(r["prs"] or "[]")]
    if subagents:
        out.append(f"subagents: {subagents}")
    return "\n".join(out)


def _memory_rows(idx: Index, paths=(), project: str = "", since: str = "", until: str = "") -> list:
    """Memories by path, or all of a project, or those made in a time range (modified in it, or written by a session
    that started in it). Newest first; Claude Code's MEMORY.md index files are left out."""
    cols = ("m.path, m.ref, m.host, m.project, m.description, m.type, m.origin_session, m.modified, "
            "f.body AS body")
    sql = f"SELECT {cols} FROM memories m JOIN memories_fts f ON f.rowid = m.rowid"
    if paths:
        sql += f" WHERE m.path IN ({','.join('?' * len(paths))})"
        params = list(paths)
    elif project:
        sql += " WHERE m.project = ? COLLATE NOCASE"
        params = [project]
    else:
        lo, hi = since or "", until or "~"
        sql += (" LEFT JOIN sessions s ON s.id = m.origin_session AND s.parent = '' "
                "WHERE (m.modified >= ? AND m.modified < ?) OR (s.started >= ? AND s.started < ?)")
        params = [lo, hi, lo, hi]
    rows = idx.db.execute(sql + " ORDER BY m.modified DESC, m.path", params).fetchall()
    return [dict(r) for r in rows if not _is_index(r["path"])]


def _memory_block(m: dict) -> str:
    """A memory for the writer. Its text is quoted ("> "), so a heading inside it cannot pass for a digest block."""
    text = (m["body"] or "").strip()
    cut = [f"[… cut; the rest: kb memory {m['path']}]"] if len(text) > MEMORY_CHARS else []
    origin = short_id(m["origin_session"]) if m["origin_session"] else "-"
    return "\n".join([f"### memory {m['ref']} · {m['type'] or '-'} · {m['host']} · modified "
                      f"{(m['modified'] or '')[:10] or '?'} · from session {origin}",
                      "description: " + " ".join((m["description"] or "").split())]
                     + ["> " + line if line else ">" for line in text[:MEMORY_CHARS].splitlines()] + cut)


def _memory_section(mems: list) -> list:
    """Memory blocks up to MEMORY_DIGEST_CHARS; the rest as one line each."""
    out, used = [], 0
    for i, m in enumerate(mems):
        block = _memory_block(m)
        if used + len(block) > MEMORY_DIGEST_CHARS:
            out.append(f"{len(mems) - i} more memories (read one with kb memory <path>):\n" + "\n".join(
                f"- {x['ref']} · {x['type'] or '-'} · "
                f"{' '.join((x['description'] or '').split())[:_MEMORY_LINE_CHARS]} [{x['path']}]" for x in mems[i:]))
            break
        out.append(block)
        used += len(block)
    return out


def digest(idx: Index, shorts=(), project: str = "", since: str = "", until: str = "",
           limit_chars: int = DIGEST_CHARS, memories=()) -> str:
    """The memories first, then one block per top-level session, oldest first. Subagents are counted, not listed.

    Memories: the given paths; else every memory of `project`; else, for a time range without shorts, the memories
    made in it. The memories count against limit_chars before the sessions do."""
    if memories:
        mems = _memory_rows(idx, paths=memories)
    elif project:
        mems = _memory_rows(idx, project=project)
    elif (since or until) and not shorts:
        mems = _memory_rows(idx, since=since, until=until)
    else:
        mems = []
    if not (shorts or project or since or until):          # memories only
        return "\n\n".join([f"0 sessions, {len(mems)} memories"] + _memory_section(mems))
    clauses, params = ["parent = ''"], []
    if shorts:
        clauses.append(f"short IN ({','.join('?' * len(shorts))})")
        params += list(shorts)
    if project:
        clauses.append("project = ? COLLATE NOCASE")
        params.append(project)
    if since:
        clauses.append("started >= ?")
        params.append(since)
    if until:
        clauses.append("started < ?")
        params.append(until)
    rows = [dict(r) for r in idx.db.execute(
        f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} ORDER BY started, id", params)]
    kids = {r[0]: r[1] for r in idx.db.execute(
        "SELECT parent, COUNT(*) FROM sessions WHERE parent != '' GROUP BY parent")}
    out = [f"{len(rows)} sessions" + (f", {len(mems)} memories" if mems else "")] + _memory_section(mems)
    used = sum(len(x) for x in out[1:])
    for i, r in enumerate(rows):
        block = _block(r, kids.get(r["id"], 0))
        if used + len(block) > limit_chars:
            rest = [x["short"] for x in rows[i:]]
            out.append(f"[… cut at {limit_chars} characters; {len(rest)} sessions left: "
                       f"kb pages digest --only {','.join(rest)}]")
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


# ---- finish

def _status(root) -> list:
    """(XY code, path) of every changed or untracked file, renames split into delete + add."""
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames").stdout
    return [(e[:2], e[3:]) for e in out.split("\0") if e]


def check_page(root, rel: str, settings) -> list:
    """Problems that stop a page from being committed (empty when it is fine)."""
    text = (Path(root) / rel).read_bytes().decode("utf-8", errors="replace")
    try:
        meta, body = parse_page(text)
    except ValueError as e:
        return [f"{rel}: {e}"]
    out = []
    want = page_rel(meta["kind"], meta["name"])
    if want != rel:
        out.append(f"{rel}: a {meta['kind']} page named {meta['name']} belongs in {want}")
    if len(text) > settings["max_page_chars"]:
        out.append(f"{rel}: {len(text)} characters, the limit is {settings['max_page_chars']}; compact it")
    found = redact(text)[1]
    if found:
        out.append(f"{rel}: looks like it holds a secret ({', '.join(sorted(found))}); remove it")
    if meta["kind"] == "project":
        for heading, key, how in (("Current state", "max_current_bullets", "one bullet per area, as it is now"),
                                  ("Open threads", "max_open_threads", "close the finished ones")):
            n = sum(1 for line in (section(body, heading) or "").splitlines() if line.startswith("- "))
            if n > settings[key]:
                out.append(f"{rel}: {n} bullets in \"{heading}\", the limit is {settings[key]}; {how}, and move "
                           "the rest to History")
    return out


def finish(root, settings, now=None, push: bool = True, skip=(), index_path=None) -> dict:
    """Check and complete the pages this run wrote, then commit and push them. Raises PagesError on a problem.

    1. Check that the plan still matches HEAD and the branch (_load_checked_plan).
    2. Check every changed file, the retro suggestions, the decisions and the skips (_check_changes).
    3. Date the bullets of the written project pages and move the stale ones to History (kb.freshness). Number the
       suggestions of the written retros (kb.ledger). Set each page's updated time and session count
       (_rewrite_written_pages).
    4. Put each planned page that was not written back to pending, with what it was planned for, unless it is named
       in skip (_next_pending).
    5. Record the new state (it moves the watermark), commit and push. When nothing changed, do nothing.

    index_path: the index `kb pages plan` updated (default <root>/.kb/index.sqlite). Without it no bullet gets a
    date."""
    root = Path(root)
    now = now or dt.datetime.now(_UTC)
    plan, branch = _load_checked_plan(root, settings, push)
    changes = _status(root)
    index_path = Path(index_path) if index_path else root / ".kb" / "index.sqlite"
    known = ledger.load(root)
    decided, edits = _check_changes(root, plan, settings, skip, changes, known, index_path, now)

    written = {rel for _, rel in changes if rel not in (ledger.DECISIONS_REL, memedits.REL)}
    suggestions, undated, moved = _rewrite_written_pages(root, written, settings, now, known, index_path)
    if suggestions != known:
        ledger.save(root, suggestions)
    if decided is not None:
        ledger.save_decisions(root, decided)
    if edits is not None:
        memedits.save(root, edits)
    pending, returned = _next_pending(plan, written, skip)
    old = load_state(root) or {}
    state = {"version": 1, "sha": plan["head"], "last_run": _iso(now), "mode": plan["mode"],
             "pending": pending, "waiting": plan["waiting"]}
    result = {"branch": branch, "projects": sorted(r for r in written if r.startswith("pages/projects/")),
              "retros": sorted(r for r in written if r.startswith("pages/retro/")), "returned": returned,
              "undated": undated, "moved": moved, "decisions": decided is not None,
              "memory_fixes": sorted(set(edits or {}) - set(_committed_json(root, memedits.REL) or {})),
              "committed": False, "push": ""}
    nothing_changed = not (
        written                                       # pages were written
        or decided is not None                        # pages/decisions.json changed
        or edits is not None                          # pages/memory-edits.json changed
        or plan["projects"] or plan["retros"]         # the plan had pages to write (even if they were skipped)
        or pending != old.get("pending")              # the pending list changed
        or plan["waiting"] != old.get("waiting")      # the sessions that wait for a summary changed
        or not old                                    # no state yet: the first run records one
    )
    if nothing_changed:
        return result                    # keep the watermark, no empty commit

    atomic_write(root / STATE_REL, (json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
                 .encode("utf-8"))
    _git(root, "add", "-A", "--", "pages/")
    projects, retros = len(result["projects"]), len(result["retros"])
    more = (", decisions" if decided is not None else "") + (", memory fixes" if edits is not None else "")
    _git(root, "commit", "--quiet", "-m",
         f"pages: {projects} project page{'s' * (projects != 1)}, {retros} retro{'s' * (retros != 1)}{more} [skip ci]")
    result["committed"] = True
    if push:
        result["push"] = _push(root, branch)
    return result


def _load_checked_plan(root: Path, settings, push: bool):
    """(plan, branch): the saved plan, after a check that HEAD, the branch and the push target still fit it."""
    try:
        plan = json.loads((root / ".kb" / PLAN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PagesError("no plan: run `kb pages plan` first") from None
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    if plan.get("head") != head:
        raise PagesError("HEAD moved since the plan; run `kb pages plan` again")
    branch = gitops.current_branch(root)
    if branch != plan.get("branch"):
        raise PagesError(f"on branch {branch or '(detached)'}, the plan was made on {plan.get('branch')}")
    if push and branch == settings["branch"] and not _has_state(root, f"origin/{branch}"):
        raise PagesError(f"the first build goes to {settings['bootstrap_branch']} and reaches {branch} through a pull "
                         "request; run `kb pages start`")
    return plan, branch


def _check_changes(root: Path, plan: dict, settings, skip, changes: list, known: dict, index_path: Path, now):
    """(decisions, memory fixes) to write, each None when its file did not change. Raises PagesError with every
    problem found."""
    problems = []
    for code, rel in changes:
        if not rel.startswith("pages/"):
            problems.append(f"{rel}: outside pages/")
        elif rel in (CONFIG_REL, STATE_REL, ledger.SUGGESTIONS_REL):
            problems.append(f"{rel}: only {'the owner' if rel == CONFIG_REL else '`kb pages finish`'} writes it")
        elif rel in (ledger.DECISIONS_REL, memedits.REL):
            if "D" in code:
                problems.append(f"{rel}: the routine never deletes it")
        elif "D" in code:
            problems.append(f"{rel}: the routine never deletes pages")
        elif not rel.endswith(".md"):
            problems.append(f"{rel}: only .md pages may be written")
        else:
            problems += check_page(root, rel, settings)
    decided = edits = None
    if not problems:
        problems += _check_suggestions(root, sorted(rel for _, rel in changes), known, index_path)
        if any(rel == ledger.DECISIONS_REL for _, rel in changes):
            more, decided = _check_decisions(root, known, index_path)
            problems += more
        if any(rel == memedits.REL for _, rel in changes):
            more, edits = _check_memory_edits(root, index_path, now)
            problems += more
    problems += _check_skips(root, plan, skip, {rel for _, rel in changes})
    if problems:
        raise PagesError("refusing to commit:\n  " + "\n  ".join(problems))
    return decided, edits


def _rewrite_written_pages(root: Path, written: set, settings, now, known: dict, index_path: Path):
    """(suggestions, undated, moved): write into each written page the facts finish knows better than the writer.

    These are the bullet dates, the stale bullets moved to History, the retro suggestion ids, the updated time and the
    session count."""
    undated, moved = [], []
    suggestions = dict(known)
    idx = Index(index_path) if index_path.is_file() else None
    try:
        for rel in sorted(written):
            path = root / rel
            text = path.read_bytes().decode("utf-8", errors="replace")
            meta, body = parse_page(text)
            if meta["kind"] == "project":
                body, missing = freshness.stamp(body, freshness.index_lookup(idx) if idx else lambda ref: "")
                undated += [f"{rel}: {line[:_REPORT_LINE_CHARS]}" for line in missing]
                body, gone = freshness.sweep(body, settings["stale_days"], settings["stale_days_current"],
                                             settings["stale_days_threads"])
                # drop the leading "- " of each moved History line, then keep _REPORT_LINE_CHARS characters
                moved += [f"{rel}: {line[2:2 + _REPORT_LINE_CHARS]}" for line in gone]
            else:
                body, found = ledger.assign(body, meta["name"], suggestions)
                suggestions = ledger.record(suggestions, meta["name"], found)
            fields = {"updated": _iso(now), "sessions": len(set(meta["sources"]))}
            atomic_write(path, set_fields(text, fields, body).encode("utf-8"))
    finally:
        if idx:
            idx.close()
    return suggestions, undated, moved


def _next_pending(plan: dict, written: set, skip):
    """(pending, returned): the plan's pending lists, plus each planned page that was not written and not skipped."""
    pending = {"projects": dict(plan["pending"]["projects"]), "weeks": list(plan["pending"]["weeks"])}
    returned = []
    todo = plan.get("todo") if isinstance(plan.get("todo"), dict) else {}
    for item in plan["projects"]:
        if item["page"] not in written and item["name"] not in skip:
            back = set(_strings(todo.get(item["name"]))) or set(item["sessions"]) or set(item.get("memories", []))
            pending["projects"][item["name"]] = sorted(back | set(pending["projects"].get(item["name"], [])))
            returned.append(item["name"])
    for item in plan["retros"]:
        if item["page"] not in written and item["week"] not in skip and item["week"] not in pending["weeks"]:
            pending["weeks"].append(item["week"])
            returned.append(item["week"])
    return pending, returned


def _check_skips(root: Path, plan: dict, skip, changed: set) -> list:
    """A page to update may be skipped only when it already covers its planned sessions: each one cited, none grown
    (it went on after the page was written). Memories alone, or a page to create, may be skipped. A retro planned for
    its suggestions without ids (review_suggestions) may not be skipped."""
    out = []
    for item in plan["projects"] + plan["retros"]:
        name = item.get("name") or item.get("week")
        if name not in skip or item["action"] != "update" or item["page"] in changed:
            continue
        if item.get("review_suggestions"):
            out.append(f"{item['page']}: cannot be skipped, its suggested changes have no ids; write them with [new]")
            continue
        cited = _sources(root / item["page"])
        new = [s for s in item["sessions"] if s not in cited]
        late = item.get("grown", [])
        if new or late:
            why = (f"{len(new)} planned session(s) it does not cite ({', '.join(new[:_SKIP_IDS_SHOWN])})" if new else
                   f"session(s) that went on after it was written ({', '.join(late[:_SKIP_IDS_SHOWN])}; "
                   "read them with `kb show`)")
            out.append(f"{item['page']}: cannot be skipped, it misses {why}; update it")
    return out


def _check_suggestions(root: Path, rels: list, known: dict, index_path: Path) -> list:
    """Problems of the "Suggested changes" of the retros written in this run (kb.ledger)."""
    bodies = {}
    for rel in rels:
        if rel.startswith("pages/retro/"):
            bodies[rel] = parse_page((root / rel).read_bytes().decode("utf-8", errors="replace"))[1]
    signatures = None
    if any(it["signature"] for body in bodies.values() for it in ledger.items(body)) and index_path.is_file():
        con = connect_readonly(index_path)
        try:
            signatures = set(signature_sessions(con))
        finally:
            con.close()
    return [p for rel, body in bodies.items() for p in ledger.check(rel, body, known, signatures)]


def _check_decisions(root: Path, known: dict, index_path: Path):
    """(problems, the decisions to write) for the routine's change of pages/decisions.json (kb.ledger)."""
    rel = ledger.DECISIONS_REL
    text = (root / rel).read_bytes().decode("utf-8", errors="replace")
    found = redact(text)[1]
    if found:
        return [f"{rel}: looks like it holds a secret ({', '.join(sorted(found))}); remove it"], None
    try:
        new = json.loads(text)
    except ValueError as e:
        return [f"{rel}: not valid JSON ({e})"], None
    shown = _git(root, "show", f"HEAD:{rel}", check=False)
    try:
        old = json.loads(shown.stdout) if shown.returncode == 0 else None
    except ValueError:
        old = None                       # a broken committed file: every entry counts as the routine's new one
    if not index_path.is_file():
        return [f"{rel}: no index to check the sources; run `kb pages plan` first"], None
    idx = Index(index_path)
    try:
        return ledger.check_decisions(old, new, known, freshness.index_lookup(idx))
    finally:
        idx.close()


def _committed_json(root: Path, rel: str):
    """The JSON of rel at HEAD, or None when it is missing or broken."""
    shown = _git(root, "show", f"HEAD:{rel}", check=False)
    try:
        return json.loads(shown.stdout) if shown.returncode == 0 else None
    except ValueError:
        return None


def _check_memory_edits(root: Path, index_path: Path, now):
    """(problems, the proposals to write) for the routine's change of pages/memory-edits.json (kb.memedits)."""
    rel = memedits.REL
    text = (root / rel).read_bytes().decode("utf-8", errors="replace")
    found = redact(text)[1]
    if found:
        return [f"{rel}: looks like it holds a secret ({', '.join(sorted(found))}); remove it"], None
    try:
        new = json.loads(text)
    except ValueError as e:
        return [f"{rel}: not valid JSON ({e})"], None
    if not index_path.is_file():
        return [f"{rel}: no index to check the memories; run `kb pages plan` first"], None
    idx = Index(index_path)
    try:
        return memedits.check(root, _committed_json(root, rel), new, idx, now.astimezone(_UTC).date().isoformat())
    finally:
        idx.close()


def _push(root, branch: str) -> str:
    """'pushed', or 'lost' when another run pushed pages first (its state conflicts; the next run catches up)."""
    for _ in range(3):
        if _ok_push(root, branch):
            return "pushed"
        if _git(root, "fetch", "--quiet", "origin", branch, check=False, timeout=gitops.PULL_TIMEOUT).returncode != 0:
            raise PagesError(f"push to {branch} failed and origin/{branch} cannot be fetched")
        if not _ok(root, "rebase", "--quiet", "FETCH_HEAD"):
            _git(root, "rebase", "--abort", check=False)
            return "lost"
    raise PagesError(f"push to {branch} failed 3 times")


def _ok_push(root, branch: str) -> bool:
    return _git(root, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", check=False,
                timeout=gitops.PULL_TIMEOUT).returncode == 0
