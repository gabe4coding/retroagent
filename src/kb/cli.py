"""kb — search and analyze your Claude Code and Codex sessions (all projects, all machines).

  kb find <words…>     ranked sessions, one line each, with a snippet and the best turn
  kb recent            latest sessions
  kb summary <id>      summary, decisions, outcome, files, PRs, subagents of one session
  kb show <id>         only the part of a session you need (--turn N --around K, --grep PATTERN)
  kb stats [report]    ready-made analytics; kb sql "<SELECT …>" for custom ones
  kb sync | backfill | status | reindex   maintenance

Ids: any unique prefix (8 characters is normal). Add --help to any command for its flags.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3

from kb import config as config_mod
from kb.distill import parse_markdown
from kb.index import AmbiguousId, Filters, Index, run_sql
from kb.stats import REPORTS


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
    sub = f"↳{r['parent'][:8]} " if r.get("parent") else ""
    return (f"{r['id'][:8]} {(r.get('started') or '')[:10]} {(r.get('agent') or ''):6} "
            f"{(r.get('project') or ''):<18.18} {sub}{(r.get('title') or '')[:70]}")


def _open_index(cfg) -> Index:
    idx = Index(cfg.kb_dir / "index.sqlite")
    if idx.rebuilt:                         # new file, or dropped because another kb version wrote it
        idx.update(cfg.root)
    return idx


def _filters(args, subagents: bool = True) -> Filters:
    get = lambda name: getattr(args, name, "") or ""
    return Filters(project=get("project"), agent=get("agent"), host=get("host"), since=parse_since(get("since")),
                   until=get("until"), tag=get("tag"), subagents=subagents)


def _get(idx: Index, prefix: str):
    try:
        r = idx.get(prefix)
    except AmbiguousId as e:
        print("ambiguous id, candidates: " + " ".join(i[:12] for i in e.args[0]))
        return None
    except ValueError as e:
        print(str(e))
        return None
    if r is None:
        print(f"no session with id {prefix}")
    return r


def cmd_find(args, cfg) -> int:
    idx = _open_index(cfg)
    try:
        hits = idx.find(" ".join(args.query), _filters(args, not args.no_subagents), args.limit, raw=args.fts)
    except ValueError as e:
        print(str(e))
        return 2
    finally:
        idx.close()
    if args.json:
        print(json.dumps(hits, ensure_ascii=False))
        return 0 if hits else 1
    if not hits:
        print("no matches")
        return 1
    for h in hits:
        snip = " ".join((h.get("snippet") or "").split())
        turn = f" [turn {h['turn']}]" if h.get("turn") else ""
        print(f"{_row(h)} · {snip}{turn}")
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
    for k in kids:
        print(f"subagent: {k['id'][:8]} {k['title']}")
    print(f"md: {r['md_path']}")
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
    idx = _open_index(cfg)
    r = _get(idx, args.id)
    idx.close()
    if r is None:
        return 1
    _, turns = parse_markdown((cfg.root / r["md_path"]).read_text(encoding="utf-8"))
    out = f"{r['id'][:8]} · {r['title']} · {len(turns)} turns\n"
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
    lines = [f"sessions: {rep.sessions}", f"markdown: {mb(rep.sizes['md'])} · raw.gz: {mb(rep.sizes['raw'])}",
             f"errors: {len(rep.errors)}"] + [f"  {e}" for e in rep.errors[:10]]
    lines.append("redactions: " + (", ".join(f"{k}={v}" for k, v in rep.redactions.most_common()) or "none"))
    lines.append("skipped record types: " + (", ".join(f"{k}={v}" for k, v in rep.skipped.most_common(25)) or "none"))
    return "\n".join(lines)


def cmd_sync(args, cfg) -> int:
    from kb.sync import now_iso, run_sync
    rep = run_sync(cfg, now=args.now, dry_run=args.dry_run, sample=args.sample,
                   summary_cap=0 if args.no_summaries else "default")
    if args.dry_run:
        print(dry_run_text(rep))
        return 0
    if rep.happened:
        print(f"{now_iso()} {cfg.host}: {rep.line()}")
    return 1 if rep.errors else 0


def cmd_backfill(args, cfg) -> int:
    from kb.sync import now_iso, run_sync
    rep = run_sync(cfg, now=True, summary_cap=None if args.summaries else 0)
    if rep.locked_out:
        print("another sync is running; try again later")
        return 1
    print(f"{now_iso()} {cfg.host}: {rep.line()}")
    return 1 if rep.errors else 0


def cmd_status(args, cfg) -> int:
    from kb.state import State
    from kb.sync import pending_units
    st = State.load(cfg.kb_dir / "sync-state.json")
    print(f"root: {cfg.root} · host: {cfg.host}")
    print(f"last sync: {st.last_ok or 'never'}" + (f" · {st.last_result}" if st.last_result else ""))
    if st.last_error:
        print(f"last error: {st.last_error}")
    print(f"pending sessions: {len(pending_units(cfg, st, now=True))}")
    idx = _open_index(cfg)
    backlog = idx.db.execute("SELECT COUNT(*) FROM sessions WHERE host=? AND parent='' AND user_turns>=2 "
                             "AND IFNULL(summary_turns, 0) != turns", (cfg.host,)).fetchone()[0]
    idx.close()
    print(f"summary backlog: {backlog}")
    return 0


def cmd_reindex(args, cfg) -> int:
    path = cfg.kb_dir / "index.sqlite"
    if path.exists():
        path.unlink()
    idx = Index(path)
    n = idx.update(cfg.root)
    idx.close()
    print(f"indexed {n} sessions")
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

    f = sub.add_parser("find", help="ranked sessions for some words")
    f.add_argument("query", nargs="+")
    filters(f)
    f.add_argument("--tag")
    f.add_argument("--limit", type=int, default=10)
    f.add_argument("--no-subagents", action="store_true", help="hide subagent transcripts")
    f.add_argument("--fts", action="store_true", help="pass the query to SQLite FTS5 unchanged")
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_find)

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

    q = sub.add_parser("sql", help="read-only SQL on the index (tables: sessions, turns)")
    q.add_argument("query")
    q.set_defaults(func=cmd_sql)

    sy = sub.add_parser("sync", help="sync local sessions into the KB, commit, push")
    sy.add_argument("--now", action="store_true", help="ignore the quiet period")
    sy.add_argument("--dry-run", action="store_true", help="write nothing outside .kb/; print counts")
    sy.add_argument("--sample", type=int, default=0, help="with --dry-run: write N sessions to .kb/dry-run/")
    sy.add_argument("--no-summaries", action="store_true")
    sy.set_defaults(func=cmd_sync)

    b = sub.add_parser("backfill", help="process every pending session now")
    b.add_argument("--summaries", action="store_true", help="also summarize everything (no per-run cap)")
    b.set_defaults(func=cmd_backfill)

    sub.add_parser("status", help="last sync, pending sessions, summary backlog").set_defaults(func=cmd_status)
    sub.add_parser("reindex", help="rebuild the local index from markdown").set_defaults(func=cmd_reindex)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args, config_mod.load())
