"""kb sync: discover changed sessions, distill, summarize, catalog, commit this host's folders, push."""
from __future__ import annotations

import datetime as dt
import fnmatch
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field

from kb import gitops
from kb.adapters import claude, codex
from kb.catalog import write_catalog
from kb.distill import update_front_matter
from kb.index import Index
from kb.lock import Lock
from kb.paths import month_of
from kb.state import State
from kb.store import write_session
from kb.summarize import summarize


@dataclass
class Report:
    sessions: int = 0
    summarized: int = 0
    errors: list = field(default_factory=list)
    redactions: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    sizes: Counter = field(default_factory=Counter)
    committed: bool = False
    pushed: bool = False
    locked_out: bool = False

    @property
    def happened(self) -> bool:
        return bool(self.sessions or self.summarized or self.errors or self.pushed)

    def line(self) -> str:
        parts = [f"{self.sessions} sessions", f"{self.summarized} summaries"]
        if self.redactions:
            parts.append(f"{sum(self.redactions.values())} redactions")
        if self.errors:
            parts.append(f"{len(self.errors)} errors (first: {self.errors[0][:200]})")
        parts.append("pushed" if self.pushed else ("committed" if self.committed else "nothing committed"))
        return ", ".join(parts)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pending_units(cfg, state, now: bool = False, clock=time.time) -> list:
    """Changed units, newest first. Without `now`, skip units modified in the last quiet_minutes."""
    ready = []
    for u in claude.discover(cfg.claude_dir) + codex.discover(cfg.codex_dirs):
        try:
            fp, newest = u.fingerprint(), u.newest_mtime()
        except OSError:
            continue
        if state.files.get(u.key) == fp:
            continue
        if not now and clock() - newest < cfg.quiet_minutes * 60:
            continue
        ready.append((newest, u, fp))
    ready.sort(key=lambda x: -x[0])
    return [(u, fp) for _, u, fp in ready]


def excluded(cfg, cwd: str) -> bool:
    return any(fnmatch.fnmatch(cwd or "", g) for g in cfg.exclude_cwd_globs)


def summarize_pending(cfg, idx, state, cap, runner, report, lock):
    rows = idx.db.execute(
        "SELECT id, md_path, turns FROM sessions WHERE host=? AND parent='' AND user_turns>=2 "
        "AND IFNULL(summary_turns, 0) != turns ORDER BY started DESC", (cfg.host,)).fetchall()
    done, months, cwd = 0, set(), tempfile.gettempdir()
    for r in rows:
        if cap is not None and done >= cap:
            break
        if state.summary_attempts.get(r["id"], 0) >= 3:
            continue
        lock.touch()
        path = cfg.root / r["md_path"]
        res = summarize(path.read_text(encoding="utf-8"), cfg.summary_model, runner=runner, cwd=cwd)
        if res is None:
            state.summary_attempts[r["id"]] = state.summary_attempts.get(r["id"], 0) + 1
            report.errors.append(f"summary {r['id'][:8]} failed")
            continue
        res["summary_turns"] = r["turns"]
        update_front_matter(path, res)
        state.summary_attempts.pop(r["id"], None)
        months.add(month_of(r["md_path"]))
        done += 1
    return done, months


def run_sync(cfg, now: bool = False, dry_run: bool = False, summary_cap="default", sample: int = 0,
             runner=subprocess.run, clock=time.time) -> Report:
    report = Report()
    lock = Lock(cfg.kb_dir / "lock")
    if not lock.acquire():
        report.locked_out = True
        return report
    state = State.load(cfg.kb_dir / "sync-state.json")
    idx = None
    try:
        remote = gitops.has_remote(cfg.root)
        if remote and not dry_run:
            try:
                gitops.pull(cfg.root)
            except gitops.GitError as e:
                report.errors.append(f"pull: {e}")
        known = {}
        if not dry_run:
            idx = Index(cfg.kb_dir / "index.sqlite")
            idx.update(cfg.root)
            known = idx.paths_by_id(cfg.host)
        titles = codex.load_titles(cfg.codex_home)
        months, seen = set(), set()
        for unit, fp in pending_units(cfg, state, now, clock):
            lock.touch()
            try:
                s = claude.parse_unit(unit) if unit.agent == "claude" else codex.parse_unit(unit, titles)
                if s is None or not s.turns or s.id in seen or excluded(cfg, s.cwd):
                    if not dry_run:
                        state.files[unit.key] = fp
                    continue
                seen.add(s.id)
                report.skipped.update(s.skipped)
                for sub in s.subagents:
                    report.skipped.update(sub.skipped)
                if dry_run and report.sessions < sample:
                    written, sizes = write_session(cfg.kb_dir / "dry-run", cfg.host, s, report.redactions)
                else:
                    written, sizes = write_session(cfg.root, cfg.host, s, report.redactions,
                                                   dry_run=dry_run, known=known)
                report.sizes.update(sizes)
                months.update(month_of(p) for p in written)
                report.sessions += 1
                if not dry_run:
                    state.files[unit.key] = fp
            except Exception as e:  # one bad session must not stop the sync
                report.errors.append(f"{unit.key}: {type(e).__name__}: {e}")
        if dry_run:
            return report
        idx.update(cfg.root)
        cap = cfg.summary_cap_per_run if summary_cap == "default" else summary_cap
        report.summarized, touched = summarize_pending(cfg, idx, state, cap, runner, report, lock)
        months |= touched
        if report.summarized:
            idx.update(cfg.root)
        if months:
            write_catalog(cfg.root, cfg.host, months)
        if gitops.stage(cfg.root, [f"sessions/{cfg.host}", f"raw/{cfg.host}", f"catalog/{cfg.host}"]):
            finding = gitops.secrets_check(cfg.root)
            if finding:
                gitops.git(cfg.root, "reset", "--quiet")
                report.errors.append(f"gitleaks: {finding}")
            else:
                gitops.commit(cfg.root, f"sync({cfg.host}): {report.sessions} sessions, {report.summarized} summaries")
                report.committed = True
        if remote and (report.committed or gitops.ahead(cfg.root)):
            try:
                gitops.push(cfg.root)
                report.pushed = True
            except gitops.GitError as e:
                report.errors.append(str(e))
        state.last_ok = now_iso()
        state.last_result = report.line()
        (cfg.kb_dir / "last-ok").touch()
        return report
    except Exception as e:
        report.errors.append(f"sync: {type(e).__name__}: {e}")
        return report
    finally:
        if idx is not None:
            idx.close()
        if not dry_run:
            state.last_error = report.errors[0] if report.errors else ""
            state.save()
        lock.release()
