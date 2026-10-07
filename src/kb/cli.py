"""kb — search and analyze your Claude Code and Codex sessions (all projects, all machines).

  kb find <words…>     ranked pages, memories and sessions, one line each, with a snippet and the best turn
  kb page [name]       a project page or weekly retro written by the cloud routine (--section NAME for one part)
  kb memory [name]     a memory file Claude Code or Codex keeps between sessions (no name: list them)
  kb recent            latest sessions
  kb summary <id>      summary, decisions, outcome, files, PRs, subagents of one session
  kb show <id>         only the part of a session you need (--turn N --around K, --grep PATTERN)
  kb hint --event error  a past fix for a failed tool call (the PostToolUseFailure hook runs it; "hints": true)
  kb stats [report]    ready-made analytics (errors: tool errors that came back); kb sql "<SELECT …>" for custom ones
  kb sync | backfill | status | reindex   maintenance
  kb embed             turn on semantic search: kb installs and runs a local embedding model (--status, --off)
  kb repair            when every sync fails to pull: reset the data clone to the remote; the next sync makes this
                       host's work again
  kb pages start | plan | digest | finish | due   steps of the cloud routine that writes pages/ (scripts/pages-routine.md)
  kb enable | disable  switch automatic syncs (the SessionStart hook) on or off; `kb enable updates` pulls the code
                       once a day
  kb setup init | routine | cloud | check   set up the data repo, the cloud routine, cloud environments (the setup
                       skill drives these)
  kb cloud push | hook | install | import   get Claude Code cloud sessions into the KB: the cloud pushes them to
                       an inbox, one machine imports them as host `cloud`
  kb update            pull the retroagent code and refresh the plugins

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
from kb.index import MIN_PREFIX, AmbiguousId, Filters, Index, connect_readonly, run_sql
from kb.pages import parse_page, section, sections
from kb.stats import REPORTS, repeated_errors
from kb.util import short_id


SUBAGENT_LINES = 10
STATS = list(REPORTS) + ["errors"]  # errors: tool errors that came back in 2+ sessions (kb.stats)
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
    """Rows as aligned text. A cell longer than `width` chars is cut and ends with "…"; width 0 cuts nothing."""
    cut = lambda v: v[:width - 1] + "…" if 0 < width < len(v) else v
    data = [[cut("" if v is None else str(v).replace("\n", " ")) for v in row] for row in rows]
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
                   until=get("until"), tag=get("tag"), subagents=subagents, role=get("role"))


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
    # pages have no agent, host, date, tag or role: those filters ask for sessions only; memories have no date, tag or role
    want_pages = not (args.no_pages or args.agent or args.host or args.since or args.until or args.tag or args.role)
    want_memories = not (args.no_memories or args.since or args.until or args.tag or args.role)
    idx = _open_index(cfg)
    try:
        dense = _dense_rankings(cfg, args, idx, query, want_pages, want_memories) or {}
        pages = idx.find_pages(query, args.project or "", PAGE_HITS, raw=args.fts,
                               dense=dense.get("page")) if want_pages else []
        mems = idx.find_memories(query, _filters(args), MEMORY_HITS, raw=args.fts,
                                 dense=dense.get("memory")) if want_memories else []
        hits = idx.find(query, _filters(args, not args.no_subagents), args.limit, raw=args.fts,
                        dense=dense.get("session"))
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


def _dense_rankings(cfg, args, idx, query: str, want_pages: bool, want_memories: bool):
    """Embedding rankings per kind for `kb find`, or None: then BM25 alone. Never raises, never downloads, and waits
    at most embed_runtime.PROBE for the model. A stopped server is started in the background for the next call."""
    if args.fts or args.no_embed or not (cfg.embed or cfg.embed_url):
        return None
    from kb import embed, embed_runtime
    store = embed.Vectors.open_readonly(cfg.kb_dir / embed.STORE)
    if store is None:
        started = _maybe_fill(cfg, idx, None)
        return _no_dense(args, "no vectors yet; " + ("filling them in the background" if started else "run: kb embed"))
    try:
        _maybe_fill(cfg, idx, store)
        ep = embed_runtime.ensure(cfg, wait=False)
        if ep is None:
            return _no_dense(args, "the embedding model is starting")
        q = embed.embed([embed.query_text(query)], ep.url, ep.key, timeout=embed_runtime.PROBE)[0]
        if not cfg.embed_url:
            embed_runtime.Server().touch()
        f = _filters(args, not args.no_subagents)
        allowed = None if f == Filters(role=f.role) else idx.keys("session", f)      # no filter: skip the check
        out = {"session": embed.rank(store, ep.model, "session", q, allowed)}
        if want_pages:
            out["page"] = embed.rank(store, ep.model, "page", q, idx.keys("page", project=args.project or ""))
        if want_memories:
            out["memory"] = embed.rank(store, ep.model, "memory", q, idx.keys("memory", _filters(args)))
        return out
    except (embed.EmbedError, embed_runtime.EmbedUnavailable, sqlite3.Error, OSError) as e:
        return _no_dense(args, str(e))
    finally:
        store.close()


FILL_EVERY = 600                 # seconds between two background fills started by `kb find`


def _maybe_fill(cfg, idx, store) -> bool:
    """Start one background `kb embed --quiet` when the vectors are behind the index: no store yet (a new clone, a
    cloud session) or fewer vectors than items. At most once per FILL_EVERY seconds, and only when the model can run
    (an embed_url, or the installed runtime). Cheap when it does nothing. Returns True when it started one."""
    import time

    from kb import embed, embed_runtime as er
    try:
        stamp = er.cache_dir() / "last-fill"
        if stamp.exists() and time.time() - stamp.stat().st_mtime < FILL_EVERY:
            return False
        if not (cfg.embed_url or er.installed()):
            return False
        if store is not None:
            model = embed.model_key("url:" + cfg.embed_url if cfg.embed_url else er.MODEL)
            have = sum(store.counts(model).values())
            want = sum(idx.db.execute(sql, args).fetchone()[0] for sql, args in (
                ("SELECT COUNT(*) FROM sessions", ()), ("SELECT COUNT(*) FROM pages", ()),
                ("SELECT COUNT(*) FROM memories", ()),
                ("SELECT COUNT(*) FROM turns WHERE role = 'user' AND length(text) >= ?", (embed.TURN_MIN,))))
            if have >= want:
                return False
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        _spawn_fill()
        return True
    except (OSError, sqlite3.Error):
        return False


def _spawn_fill() -> None:
    """`kb embed --quiet` in its own session, detached from this command."""
    import subprocess
    from pathlib import Path
    src = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ, PYTHONPATH=src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.Popen([sys.executable, "-m", "kb", "embed", "--quiet"], env=env, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _no_dense(args, why: str):
    if getattr(args, "verbose", False):
        print(f"kb: keyword search only: {why}", file=sys.stderr)
    return None


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
            print(json.dumps(routine.finish(cfg.root, settings, push=not args.no_push, skip=skip,
                                            index_path=cfg.kb_dir / "index.sqlite")))
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
    if args.report == "errors":
        _open_index(cfg).close()
        con = connect_readonly(cfg.kb_dir / "index.sqlite")
        try:
            cols, rows = repeated_errors(con, parse_since(args.since), args.project or "")
        finally:
            con.close()
        print(table(cols, rows, args.width) if rows else "no error came back in two sessions")
        return 0
    if args.since or args.project:
        print("--since and --project work only with: kb stats errors")
        return 2
    sql = REPORTS.get(args.report)
    if sql is None:
        print("reports: " + ", ".join(STATS))
        return 2
    _open_index(cfg).close()
    print(table(*run_sql(cfg.kb_dir / "index.sqlite", sql), args.width))
    return 0


def cmd_hint(args, cfg) -> int:
    """One past fix for a failed tool call (the hook event JSON on stdin), or nothing. Never fails: a hook calls it."""
    from kb import hint
    try:
        event = json.loads(sys.stdin.read() or "{}")
        line = hint.run(cfg, event)
    except Exception:  # noqa: BLE001 - a hint is never worth breaking the agent's turn
        return 0
    if not line:
        return 0
    if args.hook:
        name = event.get("hook_event_name") if isinstance(event.get("hook_event_name"), str) else ""
        line = json.dumps({"hookSpecificOutput": {"hookEventName": name or "PostToolUseFailure",
                                                  "additionalContext": line}}, ensure_ascii=False)
    print(line)
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
    print(table(cols, rows, args.width))
    if 0 < args.width < max((len(str(v)) for row in rows for v in row if v is not None), default=0):
        print(f"[cells cut at {args.width} chars; --width 0 shows them whole]")
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
    if getattr(args, "auto", False) and cfg.auto_update:
        from kb.setup import auto_update
        line = auto_update(cfg)
        if line:
            print(f"{now_iso()} {cfg.host}: {line}")
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


def _switch(what: str, on: bool) -> int:
    key, label = {"sync": ("auto_sync", "automatic syncs"), "updates": ("auto_update", "automatic code updates")}[what]
    path = config_mod.set_key(key, on)
    print(f"{label} {'enabled' if on else 'disabled'} ({path})")
    return 0


def cmd_enable(args, cfg) -> int:
    return _switch(getattr(args, "what", "sync"), True)


def cmd_disable(args, cfg) -> int:
    return _switch(getattr(args, "what", "sync"), False)


def cmd_status(args, cfg) -> int:
    from kb.state import State
    from kb.sync import needs_summary, pending_units
    st = State.load(cfg.kb_dir / "sync-state.json")
    idx = _open_index(cfg)                  # first: without an index the command prints that one line and nothing else
    backlog = len(needs_summary(idx, cfg.host))
    idx.close()
    print(f"root: {cfg.root} · host: {cfg.host}")
    print("auto sync: on" if cfg.auto_sync else "auto sync: off (the hook does nothing; run: kb enable)")
    print("auto update: on (the code clone is pulled once a day)" if cfg.auto_update
          else "auto update: off (run: kb update; or kb enable updates)")
    print(f"last sync: {st.last_ok or 'never'}" + (f" · {st.last_result}" if st.last_result else ""))
    if st.last_error:
        print(f"last error: {st.last_error}")
    print(f"pending sessions: {len(pending_units(cfg, st, now=True))}")
    if cfg.cloud_import:
        from kb.cloud import lane
        ccfg = lane(cfg)
        waiting = len(pending_units(ccfg, st, now=True)) if ccfg is not None else 0
        print(f"cloud import: on (host {cfg.cloud_host}) · {waiting} imported session(s) pending")
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


def cmd_setup(args, cfg) -> int:
    from kb import setup
    try:
        if args.step == "init":
            print(setup.dumps(setup.init(cfg.root, cfg.branch)))
        elif args.step == "cloud":
            print(setup.cloud(cfg.root, code=args.code_repo or ""), end="")
        elif args.step == "routine":
            print(setup.dumps(setup.routine(cfg.root, cfg.branch, code=args.code_repo or "", model=args.model,
                                            environment=args.environment or "", push=not args.no_push)))
        else:
            print(setup.dumps(setup.check(cfg, config_mod.config_path())))
    except (setup.SetupError, gitops.GitError) as e:
        print(f"kb: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_cloud(args, cfg) -> int:
    from kb import cloud, setup
    from kb.config import CODE_ROOT
    try:
        if args.step == "hook":
            cloud.hook(cfg, sys.stdin)
        elif args.step == "install":
            print(f"cloud sessions: Stop and SessionEnd hooks in {cloud.install(str(CODE_ROOT / 'bin' / 'kb'))}")
        elif args.step == "import":
            if args.on or args.off:
                path = config_mod.set_key("cloud_import", bool(args.on))
                print(f"cloud import {'on' if args.on else 'off'} ({path})")
            else:
                print(f"cloud import: {'on' if cfg.cloud_import else 'off'} · host: {cfg.cloud_host}")
        else:
            transcript = args.transcript or cloud.newest_transcript(cfg.claude_dir)
            if not transcript:
                print(f"kb: no transcript in {cfg.claude_dir}", file=sys.stderr)
                return 2
            print(f"{transcript}: {cloud.push(cfg, transcript)}")
    except (cloud.CloudError, setup.SetupError, config_mod.ConfigError, gitops.GitError) as e:
        print(f"kb: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_update(args, cfg) -> int:
    from kb import setup
    try:
        return setup.update()
    except (setup.SetupError, gitops.GitError) as e:
        print(f"kb: {e}", file=sys.stderr)
        return 2


def cmd_embed(args, cfg) -> int:
    import shutil

    from kb import embed, embed_runtime as er
    srv = er.Server()
    store_path = cfg.kb_dir / embed.STORE
    if args.status:
        return _embed_status(cfg, srv, store_path)
    if args.stop:
        print("embedding server stopped" if srv.stop() else "embedding server was not running")
        return 0
    if args.off or args.remove:
        config_mod.set_key("embed", False)
        srv.stop()
        if args.remove:
            shutil.rmtree(er.cache_dir(), ignore_errors=True)
            _drop_store(store_path)
        print("semantic search is off" + (f"; removed {er.cache_dir()} and the vectors" if args.remove else
                                           "; kb embed turns it on again"))
        return 0
    if args.install:
        return _embed_install(args)
    if args.quiet:
        return _embed_quiet(cfg, store_path)
    try:
        ep = er.ensure(cfg, wait=True, progress=_progress())
    except er.EmbedUnavailable as e:
        print(f"kb: {e}")
        return 2
    if args.rebuild:
        _drop_store(store_path)
    idx = _open_index(cfg)
    store = embed.Vectors(store_path)
    try:
        imported = embed.import_files(cfg.root, idx.db, store, ep.model, cfg.host)   # other machines' vectors first
        rep = embed.run_embed(idx.db, store, ep, limit=args.limit)
        counts = store.counts(embed.model_key(ep.model))
    finally:
        store.close()
        idx.close()
    if not cfg.embed_url:
        srv.touch()
    if not cfg.embed:
        config_mod.set_key("embed", True)
    print(f"embedded {rep.done} items" + (f", imported {imported}" if imported else "")
          + f" ({embed.describe(counts)}; {rep.left} left)" + (f"; stopped: {rep.error}" if rep.error else ""))
    return 1 if rep.error else 0


def _embed_install(args) -> int:
    """Download and check the runtime and model, turn semantic search on, and stop: no server, no embedding. For a
    cloud environment's setup script, whose files are cached for later sessions (`kb setup cloud` prints it)."""
    from pathlib import Path

    from kb import embed_runtime as er
    try:
        er.install(progress=_progress())
    except er.EmbedUnavailable as e:
        print(f"kb: {e}")
        return 2
    if args.root:
        config_mod.set_key("root", str(Path(args.root).expanduser().resolve()))
    config_mod.set_key("embed", True)
    print(f"semantic search installed ({er.cache_dir()}) and on; kb find starts the model when it needs it")
    return 0


def _embed_quiet(cfg, store_path) -> int:
    """Maintenance: when semantic search is on, import other machines' vectors and embed what is missing, within
    embed_sync_seconds, without output. Nothing when it is off or another fill runs. What `kb find` starts in the
    background, and what the pages routine runs."""
    import time

    from kb import embed, embed_runtime as er
    from kb.lock import Lock
    if not (cfg.embed or cfg.embed_url):
        return 0
    lock = Lock(er.cache_dir() / "fill.lock")
    if not lock.acquire():
        return 0
    try:
        deadline = time.time() + cfg.embed_sync_seconds
        ep = er.ensure(cfg, wait=True)
        idx = _open_index(cfg)
        store = embed.Vectors(store_path)
        try:
            embed.import_files(cfg.root, idx.db, store, ep.model, cfg.host)
            embed.run_embed(idx.db, store, ep, deadline=deadline)
        finally:
            store.close()
            idx.close()
        if not cfg.embed_url:
            er.Server().touch()
    except (er.EmbedUnavailable, IndexNotBuilt, sqlite3.Error, OSError):
        pass
    finally:
        lock.release()
    return 0


def _drop_store(path) -> None:
    """Delete the vector store with its WAL files (a stale -wal next to a new file would be read as its log) and its
    bit index."""
    from kb import embed
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"),
              path.with_name(embed.BITS)):
        if p.exists():
            p.unlink()


def _progress():
    """A download progress printer: one line per 10%, on stderr."""
    shown = {}

    def show(got: int, size: int) -> None:
        step = got * 10 // size if size else 10
        if shown.get(size) != step:
            shown[size] = step
            print(f"downloading {size // 1_000_000} MB: {step * 10}%", file=sys.stderr)
    return show


def _embed_status(cfg, srv, store_path) -> int:
    import time

    from kb import embed, embed_runtime as er
    print(f"semantic search: {'on' if cfg.embed or cfg.embed_url else 'off'}")
    if cfg.embed_url:
        print(f"server: {cfg.embed_url} (embed_url; kb does not manage it)")
        model = "url:" + cfg.embed_url
    else:
        model = er.MODEL
        key = er.platform_key()
        print(f"runtime: llama.cpp {er.ASSETS['llama_build']} for {key or 'this platform: not available'}, "
              f"{'installed' if er.installed() else 'not installed'} ({er.cache_dir()})")
        s = srv.state()
        if s and srv.alive(timeout=1.0):
            idle = int((time.time() - float(s.get("last_used") or 0)) // 60)
            print(f"server: running, pid {s['pid']}, port {s['port']}, last used {idle} min ago; log {srv.log_path}")
        else:
            print(f"server: stopped; log {srv.log_path}")
    store = embed.Vectors.open_readonly(store_path)
    if store is None:
        print("vectors: none yet")
        return 0
    try:
        counts = store.counts(embed.model_key(model))
    finally:
        store.close()
    print(f"vectors ({embed.model_key(model)}): {embed.describe(counts)}")
    return 0


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

    f = sub.add_parser("find", help="ranked pages and sessions for some words; the exact phrase ranks first")
    f.add_argument("query", nargs="+")
    filters(f)
    f.add_argument("--tag")
    f.add_argument("--limit", type=int, default=10)
    f.add_argument("--role", choices=("user", "assistant"), help="match only turns by this role (user: your own messages)")
    f.add_argument("--no-subagents", action="store_true", help="hide subagent transcripts")
    f.add_argument("--no-pages", action="store_true", help="hide pages")
    f.add_argument("--no-memories", action="store_true", help="hide memories")
    f.add_argument("--fts", action="store_true", help="pass the query to SQLite FTS5 unchanged")
    f.add_argument("--json", action="store_true")
    f.add_argument("--no-embed", action="store_true", help="keyword search only, even with semantic search on")
    f.add_argument("-v", "--verbose", action="store_true", help="say on stderr why semantic search was not used")
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

    st = sub.add_parser("stats", help="reports: " + ", ".join(STATS))
    st.add_argument("report", nargs="?", default="overview")
    st.add_argument("--since", help="errors: only sessions started since (30d, 2w, 6m, 1y or an ISO date)")
    st.add_argument("--project", help="errors: only this project")
    st.add_argument("--width", type=int, default=80, help="cut cells longer than this many chars (0: never cut)")
    st.set_defaults(func=cmd_stats)

    hi = sub.add_parser("hint", help="a past fix for a failed tool call, from the project page (hook event JSON on "
                                     "stdin; prints one line or nothing; needs \"hints\": true in the config)")
    hi.add_argument("--event", choices=["error"], required=True, help="the kind of event: error (a tool call failed)")
    hi.add_argument("--hook", action="store_true", help="print the hook output JSON (additionalContext) instead")
    hi.set_defaults(func=cmd_hint)

    q = sub.add_parser("sql", help="read-only SQL on the index (tables: sessions, turns, pages, memories)")
    q.add_argument("query")
    q.add_argument("--width", type=int, default=80, help="cut cells longer than this many chars (0: never cut)")
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

    for name, func, verb in (("enable", cmd_enable, "on"), ("disable", cmd_disable, "off")):
        sw = sub.add_parser(name, help=f"turn automatic syncs (or with 'updates': daily code updates) {verb}")
        sw.add_argument("what", nargs="?", choices=["sync", "updates"], default="sync",
                        help="sync: auto_sync, the SessionStart hook (default) · updates: auto_update, a daily "
                             "fast-forward of the code clone")
        sw.set_defaults(func=func)
    sub.add_parser("status", help="last sync, pending sessions, summary backlog").set_defaults(func=cmd_status)
    sub.add_parser("reindex", help="rebuild the local index from markdown").set_defaults(func=cmd_reindex)
    em = sub.add_parser("embed", help="semantic search: install and run a local embedding model, embed what is new")
    em.add_argument("--rebuild", action="store_true", help="embed everything again")
    em.add_argument("--limit", type=int, help="embed at most N items")
    em.add_argument("--status", action="store_true", help="what is installed, running and embedded")
    em.add_argument("--stop", action="store_true", help="stop the embedding server now (it starts again on use)")
    em.add_argument("--off", action="store_true", help="turn semantic search off; keep the files")
    em.add_argument("--remove", action="store_true", help="turn it off and delete the runtime, model and vectors")
    em.add_argument("--install", action="store_true", help="only download and check the runtime and model, and turn "
                                                             "it on (a cloud setup script)")
    em.add_argument("--root", help="with --install: the data clone to use (written to the config)")
    em.add_argument("--quiet", action="store_true", help="when it is on: import and embed what is missing, within "
                                                          "embed_sync_seconds, without output")
    em.set_defaults(func=cmd_embed)
    su = sub.add_parser("setup", help="set up the data repo and the cloud routine")
    su.add_argument("step", choices=["init", "routine", "cloud", "check"],
                    help="init: base files of the data repo · routine: files and spec of the pages routine · "
                         "cloud: network and setup script of a cloud environment · check: what is set up (JSON)")
    su.add_argument("--code-repo", help="owner/name of the retroagent code the routine, workflow and cloud sessions "
                                        "use (default: this code's origin)")
    su.add_argument("--model", default="claude-sonnet-5-5", help="model of the routine")
    su.add_argument("--environment", help="environment id for the routine spec")
    su.add_argument("--no-push", action="store_true", help="routine: print the spec only, push no files")
    su.set_defaults(func=cmd_setup)
    cl = sub.add_parser("cloud", help="Claude Code cloud sessions: push them to the data repo's inbox (in the cloud), "
                                      "import them (on one machine)")
    cl.add_argument("step", choices=["push", "hook", "install", "import"],
                    help="push: this session's transcript to the inbox on the data clone's branch · hook: what the "
                         "Stop hook runs (a push in the background, only in a cloud session) · install: put that hook "
                         "in ~/.claude/settings.json (the cloud setup script) · import: show or switch (--on, --off) "
                         "the import of the inbox by this machine's sync")
    cl.add_argument("--transcript", help="push: the transcript (default: the newest one)")
    onoff = cl.add_mutually_exclusive_group()
    onoff.add_argument("--on", action="store_true", help="import: this machine imports the cloud sessions")
    onoff.add_argument("--off", action="store_true", help="import: stop importing them here")
    cl.set_defaults(func=cmd_cloud)
    sub.add_parser("update", help="pull the retroagent code and run install.sh again").set_defaults(func=cmd_update)
    sub.add_parser("repair", help="reset the data clone to the remote when every sync fails to pull (refuses when "
                                  "that could lose anything but this host's own work)").set_defaults(func=cmd_repair)
    return p


def _reap_embed_server() -> None:
    """Stop the embedding server kb started when nobody used it for an hour. Must never break a command."""
    try:
        from kb.embed_runtime import Server
        Server().reap_if_idle()
    except Exception:  # noqa: BLE001 - a cleanup must not cost the command
        pass


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    _reap_embed_server()
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
