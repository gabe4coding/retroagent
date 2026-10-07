"""kb sync: discover changed sessions, distill, copy memories, summarize, catalog, commit this host's folders, then
pull and push.

Order of a run (nothing is pulled before our own files are written and committed, so our own uncommitted
files never block a pull): lock, host check (another machine's marker: stop), git gate (branch, half-done rebase,
stale index.lock: otherwise skip git, keep processing), index, process sessions (headless one-prompt runs are
skipped; a raw copy waits until its session has been idle raw_settle_hours), memories (kb.memories), summaries,
catalog, host marker, stage + secrets check + commit, pull, index again (other machines' sessions), push (never
commits that touch anything outside this host's folders).

Only the machine that owns a host writes sessions/<host> and memories/<host>, summaries included: summarize_pending
refuses any other host (ForeignHost). A pull that conflicts because the remote changed this host's files anyway
points to `kb repair`.

With cloud_import on, the run also imports the cloud sessions' inbox (kb.cloud) after the git gate and processes them
as a second host, cloud_host, which this machine then owns: its sessions, summaries, catalog and vectors go through the
same steps. After a clean push, the inbox branches whose sessions all have their markdown are deleted.
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
from pathlib import Path

from kb import cloud, gitops, machine, memories
from kb.adapters import claude, codex
from kb.catalog import write_catalog
from kb.distill import update_front_matter
from kb.index import Index
from kb.lock import Lock
from kb.model import Unit
from kb.paths import md_rel, month_of
from kb.state import State
from kb.store import read_meta, write_session
from kb.summarize import SummaryUnavailable, summarize
from kb.util import short_id

SUMMARY_BUDGET_S = 20 * 60        # wall-clock time a capped run may spend on summaries (an uncapped run has none)
SUMMARY_REGROWTH = 4              # re-summarize a session that has this many more turns than its summary covers
SUMMARY_MAX_ATTEMPTS = 3          # unusable answers tolerated per session and turn count
SUMMARY_MAX_UNAVAILABLE = 3       # failed claude calls in a row that stop the summary pass
CHECKPOINT_EVERY = 50             # processed units between two saves of the state
REPAIR_HINT = "the remote changed this host's files too; run: kb repair"


class ForeignHost(Exception):
    """Summaries were asked for a host that this machine does not own."""


def one_line(text, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


@dataclass
class Report:
    sessions: int = 0
    summarized: int = 0
    memories: int = 0                                   # memory files written or removed
    errors: list = field(default_factory=list)
    redactions: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    sizes: Counter = field(default_factory=Counter)
    committed: bool = False
    pushed: bool = False
    locked_out: bool = False
    quarantined: list = field(default_factory=list)     # paths gitleaks holds back, after this run
    newly_quarantined: int = 0                          # of those, found by this run
    embedded: int = 0                                   # items given a vector for semantic search
    notes: list = field(default_factory=list)           # problems that do not fail the sync (semantic search)

    @property
    def happened(self) -> bool:
        return bool(self.sessions or self.summarized or self.memories or self.errors or self.committed
                    or self.pushed or self.newly_quarantined or self.embedded or self.notes)

    def line(self) -> str:
        """One line, whatever the error texts contain."""
        parts = [f"{self.sessions} sessions", f"{self.summarized} summaries"]
        if self.memories:
            parts.append(f"{self.memories} memories")
        if self.redactions:
            parts.append(f"{sum(self.redactions.values())} redactions")
        if self.quarantined:
            parts.append(f"{len(self.quarantined)} quarantined")
        if self.embedded:
            parts.append(f"{self.embedded} embedded")
        if self.errors:
            parts.append(f"{len(self.errors)} errors (first: {one_line(self.errors[0], 200)})")
        if self.notes:
            parts.append(one_line(self.notes[0], 120))
        parts.append("pushed" if self.pushed else ("committed" if self.committed else "nothing committed"))
        return ", ".join(parts)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def raw_due(cfg, newest: float, clock=time.time) -> bool:
    """A unit's raw copy is written once the unit has been idle raw_settle_hours (0: at every sync).

    A session that grows all day would otherwise write a new full raw copy at every pause; its markdown still follows
    every change after quiet_minutes."""
    return not cfg.raw_settle_hours or clock() - newest >= cfg.raw_settle_hours * 3600


def pending_units(cfg, state, now: bool = False, clock=time.time) -> list:
    """Changed units, newest first, as (unit, fingerprint, raw due). Without `now`, skip units modified in the last
    quiet_minutes. A unit whose markdown is current and whose raw copy waits is skipped until it settles."""
    ready = []
    for u in claude.discover(cfg.claude_dir) + codex.discover(cfg.codex_dirs):
        try:
            fp, newest = u.fingerprint(), u.newest_mtime()
        except OSError:
            continue
        if state.files.get(u.key) == fp:
            continue
        due = raw_due(cfg, newest, clock)
        if state.raw_pending.get(u.key) == fp and not due:
            continue
        if not now and clock() - newest < cfg.quiet_minutes * 60:
            continue
        ready.append((newest, u, fp, due))
    ready.sort(key=lambda x: -x[0])
    return [(u, fp, due) for _, u, fp, due in ready]


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


def _skip_headless(cfg, s) -> bool:
    """A `claude -p` / `codex exec` run with one prompt: a script's call, not a conversation. It is marked done like
    any skipped session and is taken later if it grows a second prompt (the unit's fingerprint changes)."""
    return cfg.skip_headless_single_prompt and s.headless and not s.parent and s.user_turns <= 1


def _taken(cfg, s) -> bool:
    """False for a session the sync skips: no turns, an excluded folder, a headless one-prompt run."""
    return bool(s.turns) and not excluded(cfg, s.cwd) and not _skip_headless(cfg, s)


def _kept(cfg, s, sub, unit, known) -> str:
    """The KB's file of this subagent when it belongs to a session that no longer holds it on disk, else ''.
    Claude Code deletes old transcripts: the file stays where it is and the sessions that still hold it link to it."""
    held = known.get(sub.id)
    if not held or held == md_rel(cfg.host, sub, parent=s):
        return ""
    meta = read_meta(cfg.root / held)
    parent = meta.get("parent")
    holders = {Path(main).stem for main in unit.shared.get(sub.id, {unit.main: ""})}
    return held if meta.get("id") == sub.id and isinstance(parent, str) and parent and parent not in holders else ""


def _owner_file(cfg, unit, sub) -> str:
    """'' when this session writes the subagent, else the md path of the session that does: the first holder in
    claude.owner_order that the sync does not skip."""
    copies = unit.shared.get(sub.id)
    for main in claude.owner_order(copies) if copies else []:
        if main == unit.main:
            return ""
        try:
            other = claude.parse_unit(Unit(key=main, agent="claude", paths=[main, copies[main]], main=main))
        except OSError:                             # gone since discover
            continue
        if _taken(cfg, other):
            return md_rel(cfg.host, other.subagents[0], parent=other)
    return ""


def place_subagents(cfg, s, unit, known) -> None:
    """A resumed or forked Claude session copies its parent's subagents, so one subagent can sit under several
    sessions. It is written once; every other holder links to that file (sub.elsewhere)."""
    for sub in s.subagents:
        sub.elsewhere = _kept(cfg, s, sub, unit, known) or _owner_file(cfg, unit, sub)


def _done(state, key: str, fp: str) -> None:
    state.files[key] = fp
    state.raw_pending.pop(key, None)


def process_unit(cfg, state, report, unit, fp, titles, seen, months, known, dry_run, picker=None,
                 raw: bool = True) -> None:
    """raw=False: write the markdown only and remember the unit in state.raw_pending until its raw copy is due."""
    try:
        s = claude.parse_unit(unit) if unit.agent == "claude" else codex.parse_unit(unit, titles)
        if s is None or s.id in seen or not _taken(cfg, s):
            if not dry_run:
                _done(state, unit.key, fp)
            return
        seen.add(s.id)
        report.skipped.update(s.skipped)
        for sub in s.subagents:
            report.skipped.update(sub.skipped)
        if unit.agent == "claude":
            place_subagents(cfg, s, unit, known)
        written, sizes = write_session(cfg.root, cfg.host, s, report.redactions, dry_run=dry_run, known=known,
                                       touched=months, raw=raw)
        if picker is not None:
            picker.offer(s)
        report.sizes.update(sizes)
        months.update(month_of(p) for p in written)
        report.sessions += 1
        if dry_run:
            return
        if raw:
            _done(state, unit.key, fp)
        else:
            state.raw_pending[unit.key] = fp
    except Exception as e:  # one bad session must not stop the sync
        report.errors.append(f"{unit.key}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- summaries

def needs_summary(idx, host: str) -> list:
    """Sessions of this host with no summary yet, or SUMMARY_REGROWTH more turns than their summary covers."""
    return idx.db.execute(
        "SELECT id, md_path, turns FROM sessions WHERE host=? AND parent='' AND user_turns>=2 "
        "AND (IFNULL(summary_turns, 0) = 0 OR turns - summary_turns >= ?) ORDER BY started DESC",
        (host, SUMMARY_REGROWTH)).fetchall()


def foreign_host(cfg, host: str) -> str:
    """Why this machine must not write the summaries of `host`, or "". Only the machine that owns sessions/<host>
    writes them: `host` must be the configured host, and no other machine's marker may claim it (see kb.machine)."""
    if host != cfg.host:
        return (f"host '{host}' is not this machine's host ('{cfg.host}'); only the machine that owns "
                f"sessions/{host} writes its summaries: run kb backfill --summaries there")
    return host_is_taken(cfg, machine.local_id(cfg.kb_dir))


def summarize_pending(cfg, idx, state, cap, runner, report, lock, clock=time.time, host=None, force_host=False):
    """One summary pass over `host` (default: the configured host). Returns (summaries written, months touched).

    Raises ForeignHost, before any call, when this machine does not own `host` (see foreign_host), unless
    `force_host`. A script that calls summarize() and update_front_matter() itself skips this check: do not.
    The cap counts every call. A capped run also stops after SUMMARY_BUDGET_S; without a cap there is no time limit.
    Unusable answers are counted per session and turn count (SUMMARY_MAX_ATTEMPTS);
    failed calls are not (claude itself is down): SUMMARY_MAX_UNAVAILABLE in a row end the pass.
    """
    host = host or cfg.host
    problem = "" if force_host else foreign_host(cfg, host)
    if problem:
        raise ForeignHost(problem)
    rows = needs_summary(idx, host)
    keys = {r["id"]: f"{r['id']}:{r['turns']}" for r in rows}
    # Attempts matter only for what still waits for a summary at its current size. This also drops old plain-id keys.
    # Another host's keys (the cloud host has its own pass) are kept.
    waiting = set(keys.values())
    mine = {row[0] for row in idx.db.execute("SELECT id FROM sessions WHERE host=?", (host,))}
    state.summary_attempts = {k: v for k, v in state.summary_attempts.items() if isinstance(v, int) and ":" in k
                              and (k in waiting or k.rpartition(":")[0] not in mine)}
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


def summarize_other_host(cfg, host: str, runner=subprocess.run, clock=time.time) -> Report:
    """`kb backfill --summaries --host H --force-host`: write the summaries of another machine's host H in this clone.

    Only summary fields under sessions/H and the catalog of H change. Nothing is ingested, staged, committed or
    pushed: the changes stay in the working tree for a person to review and send. The owner's next sync may then
    conflict on those files; `kb repair` on the owner fixes that. The owner's summary retry counts are not touched.
    """
    report = Report()
    lock = Lock(cfg.kb_dir / "lock")
    if not lock.acquire():
        report.locked_out = True
        return report
    idx = None
    try:
        idx = Index(cfg.kb_dir / "index.sqlite")
        idx.update(cfg.root)
        scratch = State(path=cfg.kb_dir / "forced-summaries-state.json")      # never saved
        report.summarized, months = summarize_pending(cfg, idx, scratch, None, runner, report, lock, clock,
                                                      host=host, force_host=True)
        for path, why in write_catalog(cfg.root, host, months):
            report.errors.append(f"catalog: {path}: {why}")
        if report.summarized:
            idx.update(cfg.root)
    except Exception as e:  # noqa: BLE001 - one line in the report, like a sync
        report.errors.append(f"summaries: {type(e).__name__}: {e}")
    finally:
        if idx is not None:
            idx.close()
        lock.release()
    return report


# ---------------------------------------------------------------- git

def _staged(root, paths) -> bool:
    rc = gitops.git(root, "diff", "--cached", "--quiet", "--", *paths, check=False).returncode
    if rc not in (0, 1):
        raise gitops.GitError(f"git diff --cached --quiet: exit {rc}")
    return rc == 1


def own_paths(cfg) -> list:
    """The only folders this machine ever stages, commits or pushes: its host's, and the cloud host's when it imports."""
    hosts = [cfg.host] + [c.host for c in [cloud.lane(cfg)] if c is not None]
    return [f"{top}/{h}" for h in hosts for top in ("sessions", "raw", "catalog", "memories", "vectors")]


def _within(path: str, folders) -> bool:
    return any(path == f or path.startswith(f + "/") for f in folders)


def _wrong_branch(cfg, report) -> bool:
    branch = gitops.current_branch(cfg.root)
    if branch == cfg.branch:
        return False
    report.errors.append(f"git: on branch {branch or '(detached HEAD)'}, expected {cfg.branch}; skipping git")
    return True


def git_gate(cfg, report):
    """Decide whether this run may touch git. Returns (git_ok, has_remote); a refusal is recorded in the report.

    The branch is checked first: on another branch nothing is repaired, staged, committed, pulled or pushed (the
    checkout may hold somebody's work). A half-done rebase of ours is aborted; that can put HEAD back on a branch,
    so the branch is checked again.
    """
    root = cfg.root
    if gitops.current_branch(root) not in ("", cfg.branch):
        _wrong_branch(cfg, report)
        return False, False
    remote = gitops.has_remote(root)
    if remote:
        problem = gitops.repair(root)
        if problem:
            report.errors.append(f"git: {problem}; no git step this run")
            return False, remote
    if _wrong_branch(cfg, report):
        return False, remote
    return True, remote


def host_is_taken(cfg, mine: str) -> str:
    """The error text if another machine owns this host (see kb.machine), else ''."""
    if machine.owned_by_another(cfg.root, cfg.host, mine, cfg.branch):
        return f"host '{cfg.host}' belongs to another machine; set a unique host in the config"
    return ""


def commit_own(cfg, state, report) -> None:
    """Stage this host's folders, scan them, commit what is clean.

    A scan that could not run commits nothing. Files gitleaks flags are kept out of the commit, stay on disk and
    are staged and scanned again on every run: once clean they are committed and leave the quarantine.
    Without any gitleaks: with `require_gitleaks` nothing is committed; otherwise the files that are already
    quarantined stay held back (the quarantine is never emptied by a missing scanner) and the rest is committed.
    """
    root = cfg.root
    own = own_paths(cfg)
    if not gitops.stage(root, own):
        state.quarantine = {}              # nothing differs from HEAD any more: nothing is held back
        return
    res = gitops.secrets_check(root, exe=cfg.gitleaks_path or None)
    if res.error:                          # fail closed
        gitops.unstage(root, own)
        report.errors.append(f"gitleaks: {res.error}")
        return
    if res.ran:
        stray = [f for f in res.files if not _within(f, own)]
        if stray:                          # a finding we cannot hold back precisely: hold back everything
            gitops.unstage(root, own)
            report.errors.append("gitleaks: finding in a path outside this host's folders: " + ", ".join(stray[:3]))
            return
        held = res.files
        first_seen = state.quarantine
        state.quarantine = {f: first_seen.get(f) or now_iso() for f in held}
        report.newly_quarantined = len([f for f in held if f not in first_seen])
    else:
        if cfg.require_gitleaks:           # fail closed: no scanner, no commit
            gitops.unstage(root, own)
            report.errors.append("gitleaks: required but not found")
            return
        held = [f for f in state.quarantine if _within(f, own)]
        if held:
            report.errors.append(f"gitleaks: not found; {len(held)} quarantined file(s) stay held back until it is back")
    gitops.unstage(root, held)
    if not _staged(root, own):
        return
    # `git commit -- <paths>` also takes unstaged changes of tracked files inside the paths, so name the held-back
    # files as exclusions; otherwise a flagged file that was committed before would go in with its new content.
    memories_part = f", {report.memories} memories" if report.memories else ""
    gitops.commit(root, f"sync({cfg.host}): {report.sessions} sessions, {report.summarized} summaries{memories_part}",
                  own + [f":(exclude,literal){f}" for f in held])
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
            taken = host_is_taken(cfg, machine.local_id(cfg.kb_dir))      # a clone that had not seen the other marker
            # first in the text, as the log line is cut; a host name clash needs a unique host, not kb repair
            hint = f"{REPAIR_HINT}; " if isinstance(e, gitops.PullConflict) and not taken else ""
            report.errors.append(f"pull: {hint}{e}{note}")
            if taken:
                report.errors.append(taken)
            return
        try:
            idx.update(root)                # other machines' sessions become searchable
        except Exception as e:  # noqa: BLE001 - the push matters more than a fresh index
            report.errors.append(f"index: {type(e).__name__}: {e}")
    unborn = gitops.git(root, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode != 0
    if report.committed or gitops.ahead(root) or (not has_upstream and not unborn):
        foreign = [p for p in gitops.unpushed_paths(root) if not _within(p, own_paths(cfg))]
        if foreign:                         # somebody's own commits are theirs to push
            report.errors.append(f"git: unpushed commits touch {', '.join(foreign[:3])}; push them by hand")
            return
        try:
            gitops.push(root, keep=tuple(state.quarantine))
            report.pushed = True
        except gitops.GitError as e:
            report.errors.append(f"{REPAIR_HINT}; {e}" if isinstance(e, gitops.PullConflict) else str(e))


def embed_own(cfg, idx, report, clock=time.time, hosts=()):
    """Semantic search, before the commit: vectors for what this sync added or changed, then this host's vector files
    (vectors/<host>/) so other machines and cloud sessions get them with the sessions. Runs only once the user turned
    it on (`kb embed`), so it may install a new pin after `kb update`. Any problem is a note, never a sync error.
    Returns (endpoint or None, deadline) for embed_rest."""
    from kb import embed, embed_runtime
    deadline = clock() + cfg.embed_sync_seconds
    try:
        ep = embed_runtime.ensure(cfg, wait=True)
    except Exception as e:  # noqa: BLE001 - semantic search must never cost a sync
        report.notes.append(f"embed: {one_line(str(e), 200)}")
        return None, deadline
    try:
        store = embed.Vectors(cfg.kb_dir / embed.STORE)
        try:
            rep = embed.run_embed(idx.db, store, ep, deadline=deadline, clock=clock)
            if not cfg.embed_url and ep.model == embed_runtime.MODEL:   # only the pinned model's vectors are shared
                for host in hosts or (cfg.host,):
                    embed.export_own(cfg.root, idx.db, store, ep.model, host)
        finally:
            store.close()
        report.embedded += rep.done
        if rep.error:
            report.notes.append(f"embed: {rep.error}")
        if not cfg.embed_url:
            embed_runtime.Server().touch()
    except Exception as e:  # noqa: BLE001
        report.notes.append(f"embed: {one_line(str(e), 200)}")
    return ep, deadline


def embed_rest(cfg, idx, report, endpoint, deadline, clock=time.time) -> None:
    """Semantic search, after the pull: import the vector files other machines committed (no model needed), then embed
    what is still missing (pages, items whose files lag) in the time left. Any problem is a note."""
    from kb import embed, embed_runtime
    model = endpoint.model if endpoint else embed_runtime.MODEL
    try:
        store = embed.Vectors(cfg.kb_dir / embed.STORE)
        try:
            embed.import_files(cfg.root, idx.db, store, model, cfg.host)
            if endpoint is not None and clock() < deadline:
                rep = embed.run_embed(idx.db, store, endpoint, deadline=deadline, clock=clock)
                report.embedded += rep.done
                if rep.error and not report.notes:
                    report.notes.append(f"embed: {rep.error}")
        finally:
            store.close()
    except Exception as e:  # noqa: BLE001
        report.notes.append(f"embed: {one_line(str(e), 200)}")


# ---------------------------------------------------------------- cloud sessions

def cloud_inbox(cfg, ccfg, git_ok: bool, report):
    """The cloud lane of this run and the inbox branches to delete after a clean push: (ccfg or None, branches).
    No lane when import is off or another machine owns the cloud host (a note: this machine's own sync goes on)."""
    if ccfg is None:
        return None, []
    why = cloud.taken(cfg, ccfg)
    if why:
        report.notes.append(why)
        return None, []
    if not git_ok:
        return ccfg, []
    try:
        _, errors, done = cloud.import_inbox(cfg, ccfg)
    except gitops.GitError as e:
        report.errors.append(f"cloud: {e}")
        return ccfg, []
    report.errors += errors
    return ccfg, done


def cloud_waiting(ccfg, state, clock=time.time) -> bool:
    """True while an imported cloud session has no current markdown yet (its raw copy may still wait)."""
    return any(state.raw_pending.get(u.key) != fp for u, fp, _ in pending_units(ccfg, state, now=True, clock=clock))


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
            git_ok, remote = False, False
            mine = ""
            ccfg, inbox_done = cloud.lane(cfg), []
            if not dry_run:
                mine = machine.local_id(cfg.kb_dir)
                taken = host_is_taken(cfg, mine)         # before any git step and any write under the host's folders
                if taken:
                    report.errors.append(taken)
                    return report
                git_ok, remote = git_gate(cfg, report)
                idx = Index(cfg.kb_dir / "index.sqlite")
                idx.update(cfg.root)
                ccfg, inbox_done = cloud_inbox(cfg, ccfg, git_ok and remote, report)
            lanes = [cfg] + ([ccfg] if ccfg is not None else [])
            titles = codex.load_titles(cfg.codex_home)
            months, seen = {c.host: set() for c in lanes}, set()
            picker = SamplePicker(sample) if dry_run and sample > 0 else None
            n = 0
            for c in lanes:
                known = idx.paths_by_id(c.host) if idx is not None else {}
                for unit, fp, due in pending_units(c, state, now, clock):
                    n += 1
                    lock.touch()
                    process_unit(c, state, report, unit, fp, titles, seen, months[c.host], known, dry_run, picker,
                                 raw=due)
                    if not dry_run and n % CHECKPOINT_EVERY == 0:
                        save_state(state, report)    # a crash later keeps what is already written and recorded
            skip_cwd = lambda cwd: excluded(cfg, cwd)
            if dry_run:
                report.memories = memories.sync_memories(cfg, None, report, skip_cwd, dry_run=True)
                if picker is not None:
                    write_samples(cfg, picker, report)
                return report
            idx.update(cfg.root)
            # after the index has this run's sessions: they tell the cwd of a memory folder whose transcripts are gone
            report.memories = memories.sync_memories(cfg, idx, report, skip_cwd)
            cap = cfg.summary_cap_per_run if summary_cap == "default" else summary_cap
            for c in lanes:                     # the cap counts the summaries of every lane
                left = None if cap is None else max(cap - report.summarized, 0)
                done, touched = summarize_pending(c, idx, state, left, runner, report, lock, clock)
                report.summarized += done
                months[c.host] |= touched
            if report.summarized or report.memories:
                idx.update(cfg.root)
            for c in lanes:
                if months[c.host]:
                    for path, why in write_catalog(cfg.root, c.host, months[c.host]):
                        report.errors.append(f"catalog: {path}: {why}")
            semantic = cfg.embed or cfg.embed_url
            if semantic:                        # before the commit, so this host's vector files go out with it
                endpoint, deadline = embed_own(cfg, idx, report, clock, hosts=[c.host for c in lanes])
            for c in lanes:
                machine.claim(cfg.root, c.host, mine)
            if git_ok:
                commit_own(cfg, state, report)
                if remote:
                    publish(cfg, idx, state, report)
                    if inbox_done and not report.errors and not cloud_waiting(ccfg, state, clock):
                        report.errors += cloud.delete_branches(cfg.root, inbox_done)
            if semantic:                        # after the pull: other machines' vector files, then what is left
                embed_rest(cfg, idx, report, endpoint, deadline, clock)
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
