"""kb sync: discover changed sessions, distill, summarize, catalog, commit this host's folders, then pull and push.

Order of a run (nothing is pulled before our own files are written and committed, so our own uncommitted
files never block a pull): lock, repair a half-done rebase, index, process sessions, summaries, catalog,
stage + secrets check + commit, pull, index again (other machines' sessions), push.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import shutil
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
from kb.summarize import SummaryUnavailable, summarize
from kb.util import short_id

SUMMARY_BUDGET_S = 20 * 60        # wall-clock time a capped run may spend on summaries (an uncapped run has none)
SUMMARY_REGROWTH = 4              # re-summarize a session that has this many more turns than its summary covers
SUMMARY_MAX_ATTEMPTS = 3          # unusable answers tolerated per session and turn count
SUMMARY_MAX_UNAVAILABLE = 3       # failed claude calls in a row that stop the summary pass
CHECKPOINT_EVERY = 50             # processed units between two saves of the state


def one_line(text, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


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
    quarantined: list = field(default_factory=list)     # paths gitleaks holds back, after this run
    newly_quarantined: int = 0                          # of those, found by this run

    @property
    def happened(self) -> bool:
        return bool(self.sessions or self.summarized or self.errors or self.committed or self.pushed
                    or self.newly_quarantined)

    def line(self) -> str:
        """One line, whatever the error texts contain."""
        parts = [f"{self.sessions} sessions", f"{self.summarized} summaries"]
        if self.redactions:
            parts.append(f"{sum(self.redactions.values())} redactions")
        if self.quarantined:
            parts.append(f"{len(self.quarantined)} quarantined")
        if self.errors:
            parts.append(f"{len(self.errors)} errors (first: {one_line(self.errors[0], 200)})")
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


def save_state(state, report) -> None:
    """Save the state. A failure is reported (once) but never stops the run or leaves the lock held."""
    try:
        state.save()
    except Exception as e:  # noqa: BLE001
        if not any(x.startswith("state:") for x in report.errors):
            report.errors.append(f"state: cannot save: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- sessions

SAMPLE_KINDS = ("claude-subagents", "codex", "codex-subagent")


def sample_kind(s) -> str:
    """The kind a dry-run sample is picked by. "" = an ordinary session."""
    if s.agent == "codex":
        return "codex-subagent" if s.parent else "codex"
    return "claude-subagents" if s.subagents else ""


class SamplePicker:
    """--dry-run --sample N: a mix, not only the newest N. Sessions arrive newest first.

    Picks, in this order: the newest Claude session that has subagents, the newest Codex top-level session, the newest
    Codex subagent session. Then the newest others, up to N sessions in all. Holds at most N + 3 sessions in memory.
    """

    def __init__(self, n: int):
        self.n = max(n, 0)
        self.picks, self.others = {}, []

    def offer(self, s) -> None:
        if not self.n:
            return
        kind = sample_kind(s)
        if kind and kind not in self.picks:
            self.picks[kind] = s
        elif len(self.others) < self.n:
            self.others.append(s)

    def chosen(self) -> list:
        return ([self.picks[k] for k in SAMPLE_KINDS if k in self.picks] + self.others)[: self.n]


def write_samples(cfg, picker, report) -> None:
    for s in picker.chosen():
        try:
            write_session(cfg.kb_dir / "dry-run", cfg.host, s, Counter())     # counted already by the dry pass
        except Exception as e:  # noqa: BLE001 - one bad sample must not hide the others
            report.errors.append(f"sample {short_id(s.id)}: {type(e).__name__}: {e}")


def process_unit(cfg, state, report, unit, fp, titles, seen, months, known, dry_run, picker=None) -> None:
    try:
        s = claude.parse_unit(unit) if unit.agent == "claude" else codex.parse_unit(unit, titles)
        if s is None or not s.turns or s.id in seen or excluded(cfg, s.cwd):
            if not dry_run:
                state.files[unit.key] = fp
            return
        seen.add(s.id)
        report.skipped.update(s.skipped)
        for sub in s.subagents:
            report.skipped.update(sub.skipped)
        written, sizes = write_session(cfg.root, cfg.host, s, report.redactions, dry_run=dry_run, known=known,
                                       touched=months)
        if picker is not None:
            picker.offer(s)
        report.sizes.update(sizes)
        months.update(month_of(p) for p in written)
        report.sessions += 1
        if not dry_run:
            state.files[unit.key] = fp
    except Exception as e:  # one bad session must not stop the sync
        report.errors.append(f"{unit.key}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- summaries

def needs_summary(idx, host: str) -> list:
    """Sessions of this host with no summary yet, or SUMMARY_REGROWTH more turns than their summary covers."""
    return idx.db.execute(
        "SELECT id, md_path, turns FROM sessions WHERE host=? AND parent='' AND user_turns>=2 "
        "AND (IFNULL(summary_turns, 0) = 0 OR turns - summary_turns >= ?) ORDER BY started DESC",
        (host, SUMMARY_REGROWTH)).fetchall()


def summarize_pending(cfg, idx, state, cap, runner, report, lock, clock=time.time):
    """One summary pass. Returns (summaries written, months touched).

    The cap counts every call. A capped run also stops after SUMMARY_BUDGET_S; without a cap there is no time limit.
    Unusable answers are counted per session and turn count (SUMMARY_MAX_ATTEMPTS);
    failed calls are not (claude itself is down): SUMMARY_MAX_UNAVAILABLE in a row end the pass.
    """
    rows = needs_summary(idx, cfg.host)
    keys = {r["id"]: f"{r['id']}:{r['turns']}" for r in rows}
    # Attempts matter only for what still waits for a summary at its current size. This also drops old plain-id keys.
    waiting = set(keys.values())
    state.summary_attempts = {k: v for k, v in state.summary_attempts.items() if k in waiting and isinstance(v, int)}
    start, done, calls, down, months = clock(), 0, 0, 0, set()
    for r in rows:
        if cap is not None and calls >= cap:
            break
        if cap is not None and clock() - start >= SUMMARY_BUDGET_S:     # no cap (backfill --summaries): run to the end
            break
        key, name = keys[r["id"]], short_id(r["id"])
        attempts = state.summary_attempts.get(key, 0)
        if attempts >= SUMMARY_MAX_ATTEMPTS:
            continue
        lock.touch()
        path = cfg.root / r["md_path"]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as e:
            report.errors.append(f"summary {name}: cannot read {r['md_path']}: {type(e).__name__}")
            continue
        calls += 1
        child_dir = tempfile.mkdtemp(prefix="kb-summary-")
        try:
            res = summarize(text, cfg.summary_model, runner=runner, cwd=child_dir)
        except SummaryUnavailable as e:
            down += 1
            report.errors.append(f"summary {name}: {e}")
            if down >= SUMMARY_MAX_UNAVAILABLE:
                report.errors.append(f"summaries: stopped after {down} failed claude calls in a row")
                break
            continue
        finally:
            shutil.rmtree(child_dir, ignore_errors=True)
        down = 0
        if res is None:
            state.summary_attempts[key] = attempts + 1
            report.errors.append(f"summary {name}: unusable answer (attempt {attempts + 1} of {SUMMARY_MAX_ATTEMPTS})")
            continue
        res["summary_turns"] = r["turns"]
        try:
            update_front_matter(path, res)
        except OSError as e:
            report.errors.append(f"summary {name}: cannot write {r['md_path']}: {type(e).__name__}")
            continue
        state.summary_attempts.pop(key, None)
        months.add(month_of(r["md_path"]))
        done += 1
    return done, months


# ---------------------------------------------------------------- git

def _staged(root, paths) -> bool:
    rc = gitops.git(root, "diff", "--cached", "--quiet", "--", *paths, check=False).returncode
    if rc not in (0, 1):
        raise gitops.GitError(f"git diff --cached --quiet: exit {rc}")
    return rc == 1


def commit_own(cfg, state, report) -> None:
    """Stage this host's folders, scan them, commit what is clean.

    A scan that could not run commits nothing. Files gitleaks flags are kept out of the commit, stay on disk and
    are staged and scanned again on every run: once clean they are committed and leave the quarantine.
    """
    root = cfg.root
    own = [f"sessions/{cfg.host}", f"raw/{cfg.host}", f"catalog/{cfg.host}"]
    if not gitops.stage(root, own):
        state.quarantine = {}              # nothing differs from HEAD any more: nothing is held back
        return
    res = gitops.secrets_check(root)
    if res.error:                          # fail closed
        gitops.unstage(root, own)
        report.errors.append(f"gitleaks: {res.error}")
        return
    stray = [f for f in res.files if not any(f == o or f.startswith(o + "/") for o in own)]
    if stray:                              # a finding we cannot hold back precisely: hold back everything
        gitops.unstage(root, own)
        report.errors.append("gitleaks: finding in a path outside this host's folders: " + ", ".join(stray[:3]))
        return
    gitops.unstage(root, res.files)
    first_seen = state.quarantine
    state.quarantine = {f: first_seen.get(f) or now_iso() for f in res.files}
    report.newly_quarantined = len([f for f in res.files if f not in first_seen])
    if not _staged(root, own):
        return
    # `git commit -- <paths>` also takes unstaged changes of tracked files inside the paths, so name the held-back
    # files as exclusions; otherwise a flagged file that was committed before would go in with its new content.
    gitops.commit(root, f"sync({cfg.host}): {report.sessions} sessions, {report.summarized} summaries",
                  own + [f":(exclude,literal){f}" for f in res.files])
    report.committed = True


def publish(cfg, idx, state, report) -> None:
    """Pull (other machines' work), index it, push. A failed pull skips the push; the commit stays for next time."""
    root = cfg.root
    has_upstream = gitops.git(root, "rev-parse", "--abbrev-ref", "@{u}", check=False).returncode == 0
    if has_upstream:        # a first push into an empty remote has nothing to pull; push sets the upstream
        try:
            gitops.pull(root, keep=tuple(state.quarantine))     # held-back files are set aside during the pull
        except gitops.GitError as e:
            note = f" ({len(state.quarantine)} quarantined file(s) stay in the working tree; see kb status)" \
                if state.quarantine and "local changes" in str(e) else ""
            report.errors.append(f"pull: {e}{note}")
            return
        try:
            idx.update(root)                # other machines' sessions become searchable
        except Exception as e:  # noqa: BLE001 - the push matters more than a fresh index
            report.errors.append(f"index: {type(e).__name__}: {e}")
    unborn = gitops.git(root, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode != 0
    if report.committed or gitops.ahead(root) or (not has_upstream and not unborn):
        try:
            gitops.push(root, keep=tuple(state.quarantine))
            report.pushed = True
        except gitops.GitError as e:
            report.errors.append(str(e))


# ---------------------------------------------------------------- the run

def run_sync(cfg, now: bool = False, dry_run: bool = False, summary_cap="default", sample: int = 0,
             runner=subprocess.run, clock=time.time) -> Report:
    report = Report()
    lock = Lock(cfg.kb_dir / "lock")
    if not lock.acquire():
        report.locked_out = True
        return report
    try:
        state = State.load(cfg.kb_dir / "sync-state.json")
        idx = None
        try:
            git_ok, remote, known = False, False, {}
            if not dry_run:
                git_ok = True
                remote = gitops.has_remote(cfg.root)
                if remote:
                    problem = gitops.repair(cfg.root)
                    if problem:
                        report.errors.append(f"git: {problem}; no git step this run")
                        git_ok = False
                idx = Index(cfg.kb_dir / "index.sqlite")
                idx.update(cfg.root)
                known = idx.paths_by_id(cfg.host)
            titles = codex.load_titles(cfg.codex_home)
            months, seen = set(), set()
            picker = SamplePicker(sample) if dry_run and sample > 0 else None
            for n, (unit, fp) in enumerate(pending_units(cfg, state, now, clock), 1):
                lock.touch()
                process_unit(cfg, state, report, unit, fp, titles, seen, months, known, dry_run, picker)
                if not dry_run and n % CHECKPOINT_EVERY == 0:
                    save_state(state, report)        # a crash later keeps what is already written and recorded
            if dry_run:
                if picker is not None:
                    write_samples(cfg, picker, report)
                return report
            idx.update(cfg.root)
            cap = cfg.summary_cap_per_run if summary_cap == "default" else summary_cap
            report.summarized, touched = summarize_pending(cfg, idx, state, cap, runner, report, lock, clock)
            months |= touched
            if report.summarized:
                idx.update(cfg.root)
            if months:
                write_catalog(cfg.root, cfg.host, months)
            if git_ok:
                commit_own(cfg, state, report)
                if remote:
                    publish(cfg, idx, state, report)
            report.quarantined = sorted(state.quarantine)
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
                state.last_error = one_line(report.errors[0], 300) if report.errors else ""
                save_state(state, report)
    finally:
        lock.release()
