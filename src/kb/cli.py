"""kb — search and analyze your Claude Code and Codex sessions (all projects, all machines).

  kb find <words…>     ranked pages, memories and sessions, one line each, with a snippet and the best turn
  kb page [name]       a project page or weekly retro written by the cloud routine (--section NAME for one part)
  kb memory [name]     a memory file Claude Code or Codex keeps between sessions (no name: list them)
  kb recent            latest sessions
  kb summary <id>      summary, decisions, outcome, files, PRs, subagents of one session
  kb show <id>         only the part of a session you need (--turn N --around K, --grep PATTERN)
  kb stats [report]    ready-made analytics; kb sql "<SELECT …>" for custom ones
  kb sync | backfill | status | reindex   maintenance
  kb repair            when every sync fails to pull: reset the data clone to the remote; the next sync makes this
                       host's work again
  kb pages start | plan | digest | finish | due   steps of the cloud routine that writes pages/ (scripts/pages-routine.md)
  kb enable | disable  switch automatic syncs (the SessionStart hook) on or off

Ids: the 8-character short id shown in lists (or any unique prefix of it or of the full id, 4 characters at least).
Add --help to any command for its flags.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys

from kb import config as config_mod
from kb import gitops
from kb.distill import parse_markdown, split_front_matter
from kb.index import MIN_PREFIX, AmbiguousId, Filters, Index, run_sql
from kb.pages import parse_page, section, sections
from kb.stats import REPORTS
from kb.util import short_id


SUBAGENT_LINES = 10
PAGE_HITS = 3                    # pages listed before the sessions in `kb find`
MEMORY_HITS = 3                  # memories listed after the pages, before the sessions


def parse_since(value: str) -> str:
    if not value:
        return ""
    m = re.fullmatch(r"(\d+)([dwmy])", value.strip())
    if m:
        days = int(m.group(1)) * {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2)]
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value.strip()


def table(cols, rows, width: int = 80) -> str:
    data = [["" if v is None else str(v).replace("\n", " ")[:width] for v in row] for row in rows]
    widths = [max([len(c)] + [len(r[i]) for r in data]) for i, c in enumerate(cols)]
    fmt = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    return "\n".join([fmt(cols)] + [fmt(r) for r in data])


def _row(r: dict) -> str:
    sub = f"↳{short_id(r['parent'])} " if r.get("parent") else ""
    return (f"{short_id(r['id'])} {(r.get('started') or '')[:10]} {(r.get('agent') or ''):6} "
            f"{(r.get('project') or ''):<18.18} {sub}{(r.get('title') or '')[:70]}")


class IndexNotBuilt(Exception):
    pass


def _open_index(cfg) -> Index:
    """The index for a read command. It is opened read-only, so reading works where nothing can be written (a sandbox)
    and never waits for a running sync. Only a missing or outdated index is built, which needs write access."""
    path = cfg.kb_dir / "index.sqlite"
    idx = Index.open_current(path)
    if idx is not None:
        return idx
    try:
        idx = Index(path)                       # new file, or dropped because another kb version wrote it
    except (sqlite3.Error, OSError):            # the folder or the file cannot be written
        raise IndexNotBuilt("index not built yet; run: kb reindex") from None
    try:
        if idx.rebuilt:
            idx.update(cfg.root)
    except BaseException:
        idx.close()
        raise
    return idx


def _filters(args, subagents: bool = True) -> Filters:
    get = lambda name: getattr(args, name, "") or ""
    return Filters(project=get("project"), agent=get("agent"), host=get("host"), since=parse_since(get("since")),
                   until=get("until"), tag=get("tag"), subagents=subagents)


def _get(idx: Index, prefix: str):
    try:
        r = idx.get(prefix)
    except AmbiguousId as e:
        ids = list(e.args[0])
        shorts = [short_id(i) for i in ids]
        print("ambiguous id, candidates: " + " ".join(shorts if len(set(shorts)) == len(shorts) else ids))
        return None
    except ValueError as e:
        print(str(e))
        return None
    if r is None:
        print(f"no session with id {prefix}" + (f" (use at least {MIN_PREFIX} characters)" if len(prefix) < MIN_PREFIX else ""))
    return r


def _page_row(r: dict) -> str:
    return f"{'page':8} {(r.get('updated') or '')[:10]} {r['kind']:7} {r['name']:<18.18} {(r.get('title') or '')[:70]}"


def _memory_row(r: dict) -> str:
    return (f"{'memory':8} {(r.get('modified') or '')[:10]:10} {(r.get('agent') or ''):7} "
            f"{(r.get('project') or ''):<18.18} {r.get('name') or ''}")


def cmd_find(args, cfg) -> int:
    query = " ".join(args.query)
    # pages have no agent, host, date or tag: those filters ask for sessions only; memories have no date or tag
    want_pages = not (args.no_pages or args.agent or args.host or args.since or args.until or args.tag)
    want_memories = not (args.no_memories or args.since or args.until or args.tag)
    idx = _open_index(cfg)
    try:
        pages = idx.find_pages(query, args.project or "", PAGE_HITS, raw=args.fts) if want_pages else []
        mems = idx.find_memories(query, _filters(args), MEMORY_HITS, raw=args.fts) if want_memories else []
        hits = idx.find(query, _filters(args, not args.no_subagents), args.limit, raw=args.fts)
    except ValueError as e:
        print(str(e))
        return 2
    finally:
        idx.close()
    if args.json:
        print(json.dumps([{**p, "kind": "page", "page_kind": p["kind"]} for p in pages]
                         + [{**m, "kind": "memory"} for m in mems]
                         + [{**h, "short": short_id(h["id"])} for h in hits], ensure_ascii=False))
        return 0 if hits or pages or mems else 1
    if not hits and not pages and not mems:
        print("no matches")
        return 1
    for p in pages:
        print(f"{_page_row(p)} · {p.get('snippet') or ''} [kb page {p['name']}]")
    for m in mems:
        print(f"{_memory_row(m)} · {m.get('snippet') or ''} [kb memory {m['ref']}]")
    for h in hits:
        turn = f" [turn {h['turn']}]" if h.get("turn") else ""
        print(f"{_row(h)} · {h.get('snippet') or ''}{turn}")
    return 0


def cmd_recent(args, cfg) -> int:
    idx = _open_index(cfg)
    rows = idx.recent(_filters(args, subagents=False), args.limit)
    idx.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    for r in rows:
        print(_row(r))
    return 0


def cmd_summary(args, cfg) -> int:
    idx = _open_index(cfg)
    r = _get(idx, args.id)
    if r is None:
        idx.close()
        return 1
    kids = idx.children(r["id"])
    idx.close()
    if args.json:
        print(json.dumps({**r, "subagents": kids}, ensure_ascii=False))
        return 0
    branch = f" ({r['branch']})" if r.get("branch") else ""
    start = (r.get("started") or "")[:16].replace("T", " ")
    print(f"{r['id']} · {r['agent']} · {r['project']}{branch} · {start} → {(r.get('ended') or '')[11:16]} · "
          f"{r['turns']} turns · {r.get('model') or '-'}")
    print(f"title: {r['title']}")
    if r.get("parent"):
        print(f"parent: {r['parent']}")
    if r.get("summary"):
        print("summary: " + r["summary"].replace("\n", "\n  "))
    tags = json.loads(r.get("tags") or "[]")
    if tags or r.get("outcome"):
        print(f"tags: {', '.join(tags) or '-'} · outcome: {r.get('outcome') or '-'}")
    for d in json.loads(r.get("decisions") or "[]"):
        print(f"decision: {d}")
    files = json.loads(r.get("files") or "[]")
    if files:
        more = f" (+{len(files) - 15})" if len(files) > 15 else ""
        print("files: " + ", ".join(files[:15]) + more)
    for p in json.loads(r.get("prs") or "[]"):
        print(f"pr: {p}")
    for k in kids[:SUBAGENT_LINES]:
        print(f"subagent: {short_id(k['id'])} {k['title']}")
    if len(kids) > SUBAGENT_LINES:
        print(f"subagents: … and {len(kids) - SUBAGENT_LINES} more")
    print(f"md: {r['md_path']}")
    return 0


def cmd_page(args, cfg) -> int:
    idx = _open_index(cfg)
    try:
        if not args.name:
            rows = idx.pages()
            if not rows:
                print("no pages yet (the cloud routine writes them; see scripts/pages-routine.md)")
            for r in rows:
                print(f"{_page_row(r)} · {r['sessions']} sessions")
            return 0
        try:
            r = idx.page(args.name)
        except AmbiguousId as e:
            print("ambiguous page, candidates: " + " ".join(e.args[0]))
            return 1
    finally:
        idx.close()
    if r is None:
        print(f"no page named {args.name}; `kb page` lists them")
        return 1
    try:
        text = (cfg.root / r["path"]).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(f"kb: page file missing: {r['path']}; run: kb reindex")
        return 2
    _, body = parse_page(text)
    if args.section:
        part = section(body, args.section)
        if part is None:
            print(f"no section {args.section!r}; sections: " + ", ".join(h for h, _ in sections(body)))
            return 1
        body = part
    out = f"{r['path']} · updated {(r['updated'] or '?')[:16].replace('T', ' ')} · {r['sessions']} sessions\n\n" + body
    if len(out) > args.max_chars:
        out = out[: args.max_chars] + f"\n[… cut at {args.max_chars} chars; use --section or raise --max-chars]"
    print(out.rstrip())
    return 0


def cmd_memory(args, cfg) -> int:
    idx = _open_index(cfg)
    try:
        if not args.name:
            rows = idx.memories(_filters(args))
            if not rows:
                print("no memories yet (kb sync copies them from ~/.claude/projects/*/memory and ~/.codex/memories)")
            for r in rows:
                print(f"{r['ref']:<44} {(r.get('host') or ''):<10} {(r.get('type') or ''):<9} "
                      f"{(r.get('description') or '')[:70]}")
            return 0
        try:
            r = idx.memory(args.name)
        except AmbiguousId as e:
            print("ambiguous memory; use the path of one of these:\n" + "\n".join(f"  {p}" for p in e.args[0]))
            return 1
    finally:
        idx.close()
    if r is None:
        print(f"no memory named {args.name}; `kb memory` lists them")
        return 1
    try:
        text = (cfg.root / r["path"]).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(f"kb: memory file missing: {r['path']}; run: kb reindex")
        return 2
    head = [r["path"], r.get("host") or "?", r.get("type") or "-"]
    if r.get("modified"):
        head.append(f"modified {r['modified'][:16].replace('T', ' ')}")
    if r.get("origin_session"):
        head.append(f"from session {short_id(r['origin_session'])}")
    desc = f"{r['description']}\n\n" if r.get("description") else ""
    out = " · ".join(head) + "\n" + desc + split_front_matter(text)[1]
    if len(out) > args.max_chars:
        out = out[: args.max_chars] + f"\n[… cut at {args.max_chars} chars; raise --max-chars]"
    print(out.rstrip())
    return 0


def cmd_pages(args, cfg) -> int:
    from kb import routine
    try:
        settings = routine.load_settings(cfg.root)
        if args.step == "start":
            print(json.dumps(routine.start(cfg.root, cfg.kb_dir / "index.sqlite", settings)))
            return 0
        if args.step == "finish":
            skip = [s for s in (args.skip or "").split(",") if s]
            print(json.dumps(routine.finish(cfg.root, settings, push=not args.no_push, skip=skip)))
            return 0
        idx = _open_index(cfg)
        try:
            if args.step == "plan":
                print(json.dumps(routine.make_plan(cfg.root, idx, settings), indent=2, ensure_ascii=False))
            elif args.step == "due":           # exit 0: fire the routine; 1: nothing to write
                d = routine.due(routine.make_plan(cfg.root, idx, settings), settings)
                print(json.dumps(d))
                return 0 if d["due"] else 1
            else:
                only = [s for s in (args.only or "").split(",") if s]
                mems = [s for s in (args.memories or "").split(",") if s]
                if not (only or mems or args.project or args.since or args.until):
                    print("digest needs --project, --only, --memories, or --since/--until")
                    return 2
                print(routine.digest(idx, only, args.project or "", args.since or "", args.until or "",
                                     args.max_chars, memories=mems))
        finally:
            idx.close()
    except (routine.PagesError, gitops.GitError) as e:
        print(f"kb pages {args.step}: {e}")
        return 2
    return 0


def select_turns(turns: list, turn=None, around: int = 0, grep=None) -> list:
    if turn is not None:
        return [t for t in turns if turn - around <= t["n"] <= turn + around]
    if grep:
        rx = re.compile(grep, re.I)
        keep = set()
        for n in [t["n"] for t in turns if rx.search(t["text"])][:5]:
            keep.update(range(n - around, n + around + 1))
        return [t for t in turns if t["n"] in keep]
    picked = [t for t in turns if t["role"] == "user"][:1] + [t for t in turns if t["role"] == "assistant"][-1:]
    return sorted({t["n"]: t for t in picked}.values(), key=lambda t: t["n"])


def cmd_show(args, cfg) -> int:
    if args.grep:
        try:
            re.compile(args.grep, re.I)
        except re.error as e:
            print(f"kb: bad --grep regex: {e}")
            return 2
    idx = _open_index(cfg)
    r = _get(idx, args.id)
    idx.close()
    if r is None:
        return 1
    path = cfg.root / r["md_path"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(f"kb: session file missing: {path}; run: kb reindex")
        return 2
    _, turns = parse_markdown(text)
    out = f"{short_id(r['id'])} · {r['title']} · {len(turns)} turns\n"
    for t in select_turns(turns, args.turn, args.around, args.grep):
        out += f"\n## [{t['n']}] {t['role']} · {t['time']}\n{t['text']}\n"
    if len(out) > args.max_chars:
        out = out[: args.max_chars] + (f"\n[… cut at {args.max_chars} chars; narrow with --turn/--around/--grep "
                                       f"or raise --max-chars]")
    print(out.rstrip())
    return 0


def cmd_stats(args, cfg) -> int:
    sql = REPORTS.get(args.report)
    if sql is None:
        print("reports: " + ", ".join(REPORTS))
        return 2
    _open_index(cfg).close()
    print(table(*run_sql(cfg.kb_dir / "index.sqlite", sql)))
    return 0


def cmd_sql(args, cfg) -> int:
    _open_index(cfg).close()
    try:
        cols, rows = run_sql(cfg.kb_dir / "index.sqlite", args.query)
    except ValueError as e:
        print(str(e))
        return 2
    except sqlite3.Error as e:
        print(f"sql error: {e}")
        return 2
    print(table(cols, rows))
    return 0


def dry_run_text(rep) -> str:
    mb = lambda n: f"{n / 1e6:.1f} MB"
    lines = [f"sessions: {rep.sessions}", f"memories to write or remove: {rep.memories}", f"markdown: {mb(rep.sizes['md'])} · raw.gz: {mb(rep.sizes['raw'])}",
             f"errors: {len(rep.errors)}"] + [f"  {e}" for e in rep.errors[:10]]
    lines.append("redactions: " + (", ".join(f"{k}={v}" for k, v in rep.redactions.most_common()) or "none"))
    lines.append("skipped record types: " + (", ".join(f"{k}={v}" for k, v in rep.skipped.most_common(25)) or "none"))
    return "\n".join(lines)


def cmd_sync(args, cfg) -> int:
    from kb.sync import now_iso, run_sync
    if getattr(args, "auto", False) and not cfg.auto_sync:     # the hook asks; the owner has not said yes yet. Plain `kb sync` always runs.
        return 0
    rep = run_sync(cfg, now=args.now, dry_run=args.dry_run, sample=args.sample,
                   summary_cap=0 if args.no_summaries else "default")
    if rep.locked_out:                      # the hook starts syncs freely: a second one is not an error
        print("another sync is running")
        return 0
    if args.dry_run:
        print(dry_run_text(rep))
        return 0
    if rep.happened:
        print(f"{now_iso()} {cfg.host}: {rep.line()}")
    return 1 if rep.errors else 0


def cmd_backfill(args, cfg) -> int:
    from kb.sync import foreign_host, now_iso, run_sync, summarize_other_host
    host = args.host or cfg.host
    if (args.host or args.force_host) and not args.summaries:
        print("kb: --host and --force-host work only with --summaries")
        return 2
    if host == cfg.host:
        if args.force_host:
            print(f"kb: --force-host needs --host with another machine's host (this machine's host is '{cfg.host}')")
            return 2
        rep = run_sync(cfg, now=True, summary_cap=None if args.summaries else 0)
    elif not args.force_host:              # only the owner writes sessions/<host>: see README, "One writer per host"
        print(f"kb: {foreign_host(cfg, host)} (--force-host overrides this; read the README first)")
        return 2
    else:
        rep = summarize_other_host(cfg, host)
    if rep.locked_out:
        print("another sync is running; try again later")
        return 1
    print(f"{now_iso()} {host}: {rep.line()}")
    if host != cfg.host:
        print(f"forced from host '{cfg.host}': not committed; review the changes under sessions/{host} and "
              f"catalog/{host}, send them as a PR, and if the next sync of '{host}' then fails to pull, run kb repair "
              f"on that machine")
    return 1 if rep.errors else 0


def cmd_repair(args, cfg) -> int:
    from kb.repair import RepairRefused, repair
    try:
        lines = repair(cfg)
    except RepairRefused as e:
        print(f"kb repair: {e}")
        return 2
    for line in lines:
        print(line)
    return 0


def _set_auto_sync(on: bool) -> int:
    path = config_mod.set_key("auto_sync", on)
    print(f"automatic syncs {'enabled' if on else 'disabled'} ({path})")
    return 0


def cmd_enable(args, cfg) -> int:
    return _set_auto_sync(True)


def cmd_disable(args, cfg) -> int:
    return _set_auto_sync(False)


def cmd_status(args, cfg) -> int:
    from kb.state import State
    from kb.sync import needs_summary, pending_units
    st = State.load(cfg.kb_dir / "sync-state.json")
    idx = _open_index(cfg)                  # first: without an index the command prints that one line and nothing else
    backlog = len(needs_summary(idx, cfg.host))
    idx.close()
    print(f"root: {cfg.root} · host: {cfg.host}")
    print("auto sync: on" if cfg.auto_sync else "auto sync: off (the hook does nothing; run: kb enable)")
    print(f"last sync: {st.last_ok or 'never'}" + (f" · {st.last_result}" if st.last_result else ""))
    if st.last_error:
        print(f"last error: {st.last_error}")
    print(f"pending sessions: {len(pending_units(cfg, st, now=True))}")
    print(f"summary backlog: {backlog}")
    if gitops.find_gitleaks(cfg.gitleaks_path):
        print("gitleaks: installed")
    elif cfg.require_gitleaks:
        print("gitleaks: not installed (required: nothing is committed until it is installed)")
    else:
        print("gitleaks: not installed (built-in redaction only)")
    print(_pages_status(cfg))
    if st.quarantine:
        print(f"quarantined: {len(st.quarantine)} file(s)")
        held = sorted(st.quarantine.items(), key=lambda kv: (kv[1], kv[0]))
        for path, since in held[:5]:
            print(f"  {path}" + (f" (since {since[:10]})" if since else ""))
        if len(held) > 5:
            print(f"  … and {len(held) - 5} more")
    return 0


def _pages_status(cfg) -> str:
    from kb.routine import load_state
    state = load_state(cfg.root)
    if state is None:
        return "pages: none yet (written by the cloud routine)"
    pend = state.get("pending") if isinstance(state.get("pending"), dict) else {}
    waiting = len(pend.get("projects") or {}) + len(pend.get("weeks") or [])
    line = f"pages: last run {state.get('last_run') or '?'} · {waiting} pending"
    sha = state.get("sha")
    if isinstance(sha, str) and sha:
        p = gitops.git(cfg.root, "rev-list", "--count", f"{sha}..HEAD", "--", "sessions/", check=False)
        if p.returncode == 0:
            line += f" · {p.stdout.strip()} session commits since"
    return line


def cmd_reindex(args, cfg) -> int:
    path = cfg.kb_dir / "index.sqlite"
    if path.exists():
        path.unlink()
    idx = Index(path)
    n = idx.update(cfg.root)
    pages, mems = idx.pages_changed, idx.memories_changed
    idx.close()
    print(f"indexed {n} sessions" + (f", {pages} pages" if pages else "") + (f", {mems} memories" if mems else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def filters(sp):
        sp.add_argument("--project", help="project name (case-insensitive, exact)")
        sp.add_argument("--agent", choices=["claude", "codex"])
        sp.add_argument("--host", help="machine name")
        sp.add_argument("--since", help="30d, 2w, 6m, 1y or an ISO date")
        sp.add_argument("--until", help="ISO date (exclusive)")

    f = sub.add_parser("find", help="ranked pages and sessions for some words")
    f.add_argument("query", nargs="+")
    filters(f)
    f.add_argument("--tag")
    f.add_argument("--limit", type=int, default=10)
    f.add_argument("--no-subagents", action="store_true", help="hide subagent transcripts")
    f.add_argument("--no-pages", action="store_true", help="hide pages")
    f.add_argument("--no-memories", action="store_true", help="hide memories")
    f.add_argument("--fts", action="store_true", help="pass the query to SQLite FTS5 unchanged")
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_find)

    pg = sub.add_parser("page", help="a project page or weekly retro (no name: list them)")
    pg.add_argument("name", nargs="?", help="project name, ISO week (2026-W41), path, or the start of a name")
    pg.add_argument("--section", help="only the section whose heading starts with this, e.g. 'Key decisions'")
    pg.add_argument("--max-chars", type=int, default=12000)
    pg.set_defaults(func=cmd_page)

    me = sub.add_parser("memory", help="a synced memory file (no name: list them)")
    me.add_argument("name", nargs="?", help="ref from the list (project/file), the memory's name, or its path")
    me.add_argument("--project", help="list: only this project (case-insensitive, exact)")
    me.add_argument("--agent", choices=["claude", "codex"], help="list: only this agent")
    me.add_argument("--host", help="list: only this machine")
    me.add_argument("--max-chars", type=int, default=12000)
    me.set_defaults(func=cmd_memory)

    ps = sub.add_parser("pages", help="steps of the cloud routine that writes pages/ (scripts/pages-routine.md)")
    ps.add_argument("step", choices=["start", "plan", "digest", "finish", "due"])
    ps.add_argument("--project", help="digest: every session of this project")
    ps.add_argument("--only", help="digest: these short ids, comma-separated")
    ps.add_argument("--memories", help="digest: these memory paths (from the plan), comma-separated")
    ps.add_argument("--since", help="digest: sessions started at or after this ISO time")
    ps.add_argument("--until", help="digest: sessions started before this ISO time")
    ps.add_argument("--max-chars", type=int, default=150000, help="digest: cut the output here")
    ps.add_argument("--skip", help="finish: planned names (projects or weeks) not to retry, comma-separated")
    ps.add_argument("--no-push", action="store_true", help="finish: commit but do not push")
    ps.set_defaults(func=cmd_pages)

    r = sub.add_parser("recent", help="latest sessions (no subagents)")
    filters(r)
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_recent)

    s = sub.add_parser("summary", help="summary card of one session")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_summary)

    sh = sub.add_parser("show", help="part of a session (default: first prompt + last answer)")
    sh.add_argument("id")
    sh.add_argument("--turn", type=int)
    sh.add_argument("--around", type=int, default=0)
    sh.add_argument("--grep", help="regex, case-insensitive; up to 5 matching turns")
    sh.add_argument("--max-chars", type=int, default=4000)
    sh.set_defaults(func=cmd_show)

    st = sub.add_parser("stats", help="reports: " + ", ".join(REPORTS))
    st.add_argument("report", nargs="?", default="overview")
    st.set_defaults(func=cmd_stats)

    q = sub.add_parser("sql", help="read-only SQL on the index (tables: sessions, turns, pages, memories)")
    q.add_argument("query")
    q.set_defaults(func=cmd_sql)

    sy = sub.add_parser("sync", help="sync local sessions into the KB, commit, push")
    sy.add_argument("--now", action="store_true", help="ignore the quiet period")
    sy.add_argument("--dry-run", action="store_true", help="write nothing outside .kb/; print counts")
    sy.add_argument("--sample", type=int, default=0,
                    help="with --dry-run: write N sessions to .kb/dry-run/ (the newest Claude session with subagents, "
                         "the newest Codex top-level and subagent session, then the newest others)")
    sy.add_argument("--no-summaries", action="store_true")
    sy.add_argument("--auto", action="store_true",
                    help="what the SessionStart hook runs: do nothing (silently) unless auto_sync is on")
    sy.set_defaults(func=cmd_sync)

    b = sub.add_parser("backfill", help="process every pending session now")
    b.add_argument("--summaries", action="store_true", help="also summarize everything (no per-run cap, no time limit)")
    b.add_argument("--host", help="with --summaries: the host to summarize; another machine's host is refused "
                                  "unless --force-host")
    b.add_argument("--force-host", action="store_true",
                   help="with --summaries --host: write another machine's summaries here (summaries only, nothing "
                        "committed; its owner may then need kb repair)")
    b.set_defaults(func=cmd_backfill)

    sub.add_parser("enable", help="turn automatic syncs on (auto_sync in the config)").set_defaults(func=cmd_enable)
    sub.add_parser("disable", help="turn automatic syncs off").set_defaults(func=cmd_disable)
    sub.add_parser("status", help="last sync, pending sessions, summary backlog").set_defaults(func=cmd_status)
    sub.add_parser("reindex", help="rebuild the local index from markdown").set_defaults(func=cmd_reindex)
    sub.add_parser("repair", help="reset the data clone to the remote when every sync fails to pull (refuses when "
                                  "that could lose anything but this host's own work)").set_defaults(func=cmd_repair)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args, config_mod.load())
    except IndexNotBuilt as e:
        print(e)
        return 2
    except BrokenPipeError:                     # `kb find … | head`: the reader left, which is not an error
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())     # no second error when Python flushes
        except (OSError, ValueError):
            pass
        return 0
    except (sqlite3.Error, OSError, re.error, ValueError) as e:
        print("kb: " + " ".join(str(e).split()))
        return 2
