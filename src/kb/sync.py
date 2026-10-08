"""kb sync: write this machine's new and changed sessions into the data clone, commit them, then pull and push.

The steps of one run:
 1. Lock: only one sync runs at a time.
 2. Host check: if another machine's marker claims this host, stop (see kb.machine).
 3. Git gate: check the branch, abort a half-done rebase, look for a stale index.lock. On a problem, skip the git
    steps but keep processing.
 4. Index: bring the search index up to date with the data clone.
 5. Process sessions: write the markdown and the raw copy of each changed unit. Skip headless one-prompt runs.
    A raw copy waits until its session has been idle for raw_settle_hours.
 6. Memories: copy this machine's memory files (kb.memories).
 7. Summaries: ask claude for the summaries that are missing or out of date.
 8. Catalog: rebuild the catalog pages of the months that changed.
 9. Host marker: write sessions/<host>/.machine-id if it is missing.
10. Stage this host's folders, run the secrets check (gitleaks), commit what is clean.
11. Pull: get the commits of the other machines.
12. Index again: the sessions of the other machines become searchable.
13. Push: never push commits that touch files outside this host's folders.

WHY this order: our own files are committed before the pull, so our own uncommitted files never block it.

Terms:
- host: the name of one machine's folders in the data repo (sessions/<host>, raw/<host>, ...).
- unit: one transcript plus its subagent files on disk (kb.model.Unit). One unit gives one session.
- lane: a host this run processes. This machine's host is always a lane. With cloud import on, the cloud host is too.
- raw copy: the slim, redacted transcript under raw/<host>/, next to the markdown.
- quarantine: the files that gitleaks flagged. They stay out of the commits until a scan finds them clean.
- fingerprint: a hash of the paths, sizes and times of a unit's files. A new fingerprint means the unit changed.

Only the machine that owns a host writes sessions/<host> and memories/<host>, summaries included.
summarize_pending refuses any other host: it raises ForeignHost. A pull can conflict because the remote changed this
host's files anyway. The error then tells the user to run `kb repair`.

With cloud_import on, the run imports the inbox of the cloud sessions (kb.cloud) after the git gate. It processes them
as a second host, cloud_host, which this machine then owns. Their sessions, summaries, catalog and vectors go through
the same steps. After a clean push, the run deletes each inbox branch whose sessions all have their markdown.
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
SHOW_PATHS = 3                    # paths an error message names at most
ERROR_CHARS = 200                 # length of the first error in the report line, and of an embed note
NOTE_CHARS = 120                  # length of the first note in the report line
LAST_ERROR_CHARS = 300            # length of state.last_error, which `kb status` prints


class ForeignHost(Exception):
    """Summaries were asked for a host that this machine does not own."""


def one_line(text, limit: int) -> str:
    """The text on one line (each run of whitespace becomes one space), cut to `limit` characters."""
    return " ".join(str(text).split())[:limit]


@dataclass
class Report:
    """What one sync run did. `kb sync` prints line() when happened is True."""
    sessions: int = 0
    summarized: int = 0
    memories: int = 0                                   # memory files written or removed
    errors: list = field(default_factory=list)
    redactions: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)   # transcript record types the parsers skipped, with counts
    sizes: Counter = field(default_factory=Counter)     # bytes of output by kind: "md" and "raw"
    committed: bool = False
    pushed: bool = False
    locked_out: bool = False
    quarantined: list = field(default_factory=list)     # paths gitleaks holds back, after this run
    newly_quarantined: int = 0                          # of those, found by this run
    embedded: int = 0                                   # items given a vector for semantic search
    notes: list = field(default_factory=list)           # problems that do not fail the sync (semantic search)

    @property
    def happened(self) -> bool:
        """True if the run did or found something worth a log line. Files quarantined before do not count."""
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
            parts.append(f"{len(self.errors)} errors (first: {one_line(self.errors[0], ERROR_CHARS)})")
        if self.notes:
            parts.append(one_line(self.notes[0], NOTE_CHARS))
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
    """The units to process, newest first, as (unit, fingerprint, raw copy due now).

    A unit is skipped when its fingerprint is the one the state recorded as done. Without `now`, a unit changed in
    the last quiet_minutes is skipped: the session may still be running. A unit whose markdown is current and whose
    raw copy waits is skipped until the raw copy is due."""
    ready = []
    for unit in claude.discover(cfg.claude_dir) + codex.discover(cfg.codex_dirs):
        try:
            fingerprint, newest = unit.fingerprint(), unit.newest_mtime()
        except OSError:
            continue
        if state.files.get(unit.key) == fingerprint:
            continue
        due = raw_due(cfg, newest, clock)
        if state.raw_pending.get(unit.key) == fingerprint and not due:
            continue
        if not now and clock() - newest < cfg.quiet_minutes * 60:
            continue
        ready.append((newest, unit, fingerprint, due))
    ready.sort(key=lambda item: item[0], reverse=True)     # newest first; equal times keep the discovery order
    return [(unit, fingerprint, due) for _, unit, fingerprint, due in ready]


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


def _existing_subagent_file(cfg, session, sub, unit, paths_by_id) -> str:
    """The data clone's file of this subagent under a parent whose transcript is gone from disk, else ''.

    Claude Code deletes old transcripts. If the data clone already has this subagent's file under such a parent, the
    sessions that still hold the subagent link to that file instead of writing a second copy."""
    known_path = paths_by_id.get(sub.id)
    if not known_path or known_path == md_rel(cfg.host, sub, parent=session):
        return ""
    meta = read_meta(cfg.root / known_path)
    parent = meta.get("parent")
    parents_on_disk = {Path(main).stem for main in unit.shared.get(sub.id, {unit.main: ""})}
    if meta.get("id") == sub.id and isinstance(parent, str) and parent and parent not in parents_on_disk:
        return known_path
    return ""


def _owner_file(cfg, unit, sub) -> str:
    """Which session writes this subagent's file: '' for this session, else the md path of the session that does.

    The owner is the first holder in claude.owner_order that the sync does not skip."""
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


def place_subagents(cfg, session, unit, paths_by_id) -> None:
    """Set sub.elsewhere for each subagent that another file already holds, so it is written only once.

    A resumed or forked Claude session copies its parent's subagents, so one subagent can sit under several sessions.
    One session writes it; every other holder links to that file (sub.elsewhere)."""
    for sub in session.subagents:
        sub.elsewhere = (_existing_subagent_file(cfg, session, sub, unit, paths_by_id)
                         or _owner_file(cfg, unit, sub))


def _done(state, key: str, fingerprint: str) -> None:
    state.files[key] = fingerprint
    state.raw_pending.pop(key, None)


def process_unit(cfg, state, report, unit, fingerprint, titles, seen, months, paths_by_id, dry_run, picker=None,
                 raw: bool = True) -> None:
    """Parse one unit and write its session (markdown, and the raw copy when `raw`). Record it in the state.

    seen: the session ids this run already wrote; a second unit with the same id is skipped.
    months: the set of months whose files changed (for the catalog); this call adds to it.
    paths_by_id: session id -> its md path in the index, to find files that move or that another session holds.
    raw=False: write the markdown only and remember the unit in state.raw_pending until its raw copy is due.
    """
    try:
        session = claude.parse_unit(unit) if unit.agent == "claude" else codex.parse_unit(unit, titles)
        if session is None or session.id in seen or not _taken(cfg, session):
            if not dry_run:
                _done(state, unit.key, fingerprint)
            return
        seen.add(session.id)
        report.skipped.update(session.skipped)
        for sub in session.subagents:
            report.skipped.update(sub.skipped)
        if unit.agent == "claude":
            place_subagents(cfg, session, unit, paths_by_id)
        written, sizes = write_session(cfg.root, cfg.host, session, report.redactions, dry_run=dry_run,
                                       known=paths_by_id, touched=months, raw=raw)
        if picker is not None:
            picker.offer(session)
        report.sizes.update(sizes)
        months.update(month_of(p) for p in written)
        report.sessions += 1
        if dry_run:
            return
        if raw:
            _done(state, unit.key, fingerprint)
        else:
            state.raw_pending[unit.key] = fingerprint
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
    keys = {row["id"]: f"{row['id']}:{row['turns']}" for row in rows}
    _prune_summary_attempts(idx, state, host, set(keys.values()))
    start, done, calls, failures_in_a_row, months = clock(), 0, 0, 0, set()
    for row in rows:
        if cap is not None and calls >= cap:
            break
        if cap is not None and clock() - start >= SUMMARY_BUDGET_S:     # no cap (backfill --summaries): run to the end
            break
        key, name = keys[row["id"]], short_id(row["id"])
        attempts = state.summary_attempts.get(key, 0)
        if attempts >= SUMMARY_MAX_ATTEMPTS:
            continue
        lock.touch()
        path = cfg.root / row["md_path"]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as e:
            report.errors.append(f"summary {name}: cannot read {row['md_path']}: {type(e).__name__}")
            continue
        calls += 1
        child_dir = tempfile.mkdtemp(prefix="kb-summary-")
        try:
            summary = summarize(text, cfg.summary_model, runner=runner, cwd=child_dir)
        except SummaryUnavailable as e:
            failures_in_a_row += 1
            report.errors.append(f"summary {name}: {e}")
            if failures_in_a_row >= SUMMARY_MAX_UNAVAILABLE:
                report.errors.append(f"summaries: stopped after {failures_in_a_row} failed claude calls in a row")
                break
            continue
        finally:
            shutil.rmtree(child_dir, ignore_errors=True)
        failures_in_a_row = 0
        if summary is None:
            state.summary_attempts[key] = attempts + 1
            report.errors.append(f"summary {name}: unusable answer (attempt {attempts + 1} of {SUMMARY_MAX_ATTEMPTS})")
            continue
        summary["summary_turns"] = row["turns"]
        try:
            update_front_matter(path, summary)
        except OSError as e:
            report.errors.append(f"summary {name}: cannot write {row['md_path']}: {type(e).__name__}")
            continue
        state.summary_attempts.pop(key, None)
        months.add(month_of(row["md_path"]))
        done += 1
    return done, months


def _prune_summary_attempts(idx, state, host: str, waiting: set) -> None:
    """Drop the retry counts that no longer matter.

    Keep a retry count only while that session still waits for a summary at the same size ("<id>:<turns>" in
    `waiting`). Keep the counts of other hosts: the cloud host has its own pass. Drop keys without ":" or with a
    count that is not an int.
    """
    host_session_ids = {row[0] for row in idx.db.execute("SELECT id FROM sessions WHERE host=?", (host,))}
    state.summary_attempts = {k: v for k, v in state.summary_attempts.items() if isinstance(v, int) and ":" in k
                              and (k in waiting or k.rpartition(":")[0] not in host_session_ids)}


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

    The branch is checked first. On another branch, nothing is repaired, staged, committed, pulled or pushed: the
    checkout may hold somebody's work. A detached HEAD passes this first check, because a half-done rebase leaves
    HEAD detached. gitops.repair aborts such a rebase, which can put HEAD back on the branch. So the branch is
    checked again after the repair; a HEAD that is still detached is refused then.
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


def host_is_taken(cfg, machine_id: str) -> str:
    """The error text if another machine owns this host (see kb.machine), else ''."""
    if machine.owned_by_another(cfg.root, cfg.host, machine_id, cfg.branch):
        return f"host '{cfg.host}' belongs to another machine; set a unique host in the config"
    return ""


def commit_own(cfg, state, report) -> None:
    """Stage this host's folders, scan them with gitleaks, commit what is clean.

    The quarantine (state.quarantine) is the set of files gitleaks flagged. They stay on disk but out of the commit.
    Every run stages and scans them again; once clean, they are committed and leave the quarantine.

    Three cases:
    - gitleaks ran: the flagged files go to the quarantine; the rest is committed. A scan that failed commits nothing.
    - gitleaks is missing and `require_gitleaks` is on: nothing is committed.
    - gitleaks is missing and not required: the files already in the quarantine stay held back, and the rest is
      committed. A missing scanner never empties the quarantine.
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
            report.errors.append("gitleaks: finding in a path outside this host's folders: "
                                 + ", ".join(stray[:SHOW_PATHS]))
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
    memories_part = f", {report.memories} memories" if report.memories else ""
    # `git commit -- <paths>` also takes unstaged changes inside the paths. Exclude the held-back files by name, or a
    # flagged file that was committed before would go in with its new content.
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
            # The pull may have brought another machine's marker for this host, which this clone had not seen before.
            taken = host_is_taken(cfg, machine.local_id(cfg.kb_dir))
            # The repair hint goes first in the text, because the log line is cut. A host name clash needs a unique
            # host, not kb repair, so it gets no hint.
            hint = f"{REPAIR_HINT}; " if isinstance(e, gitops.PullConflict) and not taken else ""
            report.errors.append(f"pull: {hint}{e}{note}")
            if taken:
                report.errors.append(taken)
            return
        try:
            idx.update(root)                # other machines' sessions become searchable
        except Exception as e:  # noqa: BLE001 - the push matters more than a fresh index
            report.errors.append(f"index: {type(e).__name__}: {e}")
    no_commit_yet = gitops.git(root, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode != 0
    first_push = not has_upstream and not no_commit_yet
    # Push when this run committed, when older commits wait, or for the first push of a branch that has commits.
    if report.committed or gitops.ahead(root) or first_push:
        foreign = [p for p in gitops.unpushed_paths(root) if not _within(p, own_paths(cfg))]
        if foreign:                         # somebody's own commits are theirs to push
            report.errors.append(f"git: unpushed commits touch {', '.join(foreign[:SHOW_PATHS])}; push them by hand")
            return
        try:
            gitops.push(root, keep=tuple(state.quarantine))
            report.pushed = True
        except gitops.GitError as e:
            report.errors.append(f"{REPAIR_HINT}; {e}" if isinstance(e, gitops.PullConflict) else str(e))


def embed_own(cfg, idx, report, clock=time.time, hosts=()):
    """Semantic search, before the commit. Returns (endpoint or None, deadline) for embed_rest.

    1. Make vectors for what this sync added or changed.
    2. Write this host's vector files (vectors/<host>/), so other machines and cloud sessions get them with the
       sessions.
    It runs only after the user turned semantic search on (`kb embed`). The pin is the exact llama.cpp build and model
    that this code release names (embed_runtime). After `kb update` the pin can change, and ensure() installs the new
    one here. Any problem is a note, never a sync error.
    """
    from kb import embed, embed_runtime
    deadline = clock() + cfg.embed_sync_seconds
    try:
        ep = embed_runtime.ensure(cfg, wait=True)
    except Exception as e:  # noqa: BLE001 - semantic search must never cost a sync
        report.notes.append(f"embed: {one_line(str(e), ERROR_CHARS)}")
        return None, deadline
    try:
        store = embed.Vectors(cfg.kb_dir / embed.STORE)
        try:
            rep = embed.run_embed(idx.db, store, ep, deadline=deadline, clock=clock)
            # Export only the pinned model's vectors. A custom server (embed_url) may run another model, and vectors
            # of different models cannot be compared.
            if not cfg.embed_url and ep.model == embed_runtime.MODEL:
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
        report.notes.append(f"embed: {one_line(str(e), ERROR_CHARS)}")
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
        report.notes.append(f"embed: {one_line(str(e), ERROR_CHARS)}")


# ---------------------------------------------------------------- cloud sessions

def cloud_inbox(cfg, cloud_cfg, git_ok: bool, report):
    """The cloud lane of this run and the inbox branches to delete after a clean push: (cloud_cfg or None, branches).

    No lane when import is off, or when another machine owns the cloud host. The second case is a note, not an
    error: this machine's own sync goes on."""
    if cloud_cfg is None:
        return None, []
    why = cloud.taken(cfg, cloud_cfg)
    if why:
        report.notes.append(why)
        return None, []
    if not git_ok:
        return cloud_cfg, []
    try:
        _, errors, done = cloud.import_inbox(cfg, cloud_cfg)
    except gitops.GitError as e:
        report.errors.append(f"cloud: {e}")
        return cloud_cfg, []
    report.errors += errors
    return cloud_cfg, done


def cloud_waiting(cloud_cfg, state, clock=time.time) -> bool:
    """True while an imported cloud session has no current markdown yet (its raw copy may still wait)."""
    return any(state.raw_pending.get(unit.key) != fingerprint
               for unit, fingerprint, _ in pending_units(cloud_cfg, state, now=True, clock=clock))


# ---------------------------------------------------------------- the run

def _process_lanes(cfg, lanes, state, idx, report, lock, months, now, dry_run, picker, clock) -> None:
    """Step 5: write the markdown and raw copies of the changed units of every lane."""
    titles = codex.load_titles(cfg.codex_home)
    seen = set()
    processed = 0
    for lane_cfg in lanes:
        paths_by_id = idx.paths_by_id(lane_cfg.host) if idx is not None else {}
        for unit, fingerprint, raw_due_now in pending_units(lane_cfg, state, now, clock):
            processed += 1
            lock.touch()
            process_unit(lane_cfg, state, report, unit, fingerprint, titles, seen, months[lane_cfg.host],
                         paths_by_id, dry_run, picker, raw=raw_due_now)
            if not dry_run and processed % CHECKPOINT_EVERY == 0:
                save_state(state, report)    # a crash later keeps what is already written and recorded


def _summarize_lanes(lanes, idx, state, cap, runner, report, lock, months, clock) -> None:
    """Step 7: the summary pass of every lane. The cap counts the summaries of all lanes together."""
    for lane_cfg in lanes:
        left = None if cap is None else max(cap - report.summarized, 0)
        done, touched = summarize_pending(lane_cfg, idx, state, left, runner, report, lock, clock)
        report.summarized += done
        months[lane_cfg.host] |= touched


def _write_catalogs(cfg, lanes, months, report) -> None:
    """Step 8: rebuild the catalog of each lane's changed months."""
    for lane_cfg in lanes:
        if months[lane_cfg.host]:
            for path, why in write_catalog(cfg.root, lane_cfg.host, months[lane_cfg.host]):
                report.errors.append(f"catalog: {path}: {why}")


def _record_success(cfg, state, report) -> None:
    """Record a finished run: the quarantine in the report, the time and line in the state, and the last-ok file."""
    report.quarantined = sorted(state.quarantine)
    state.last_ok = now_iso()
    state.last_result = report.line()
    (cfg.kb_dir / "last-ok").touch()


def run_sync(cfg, now: bool = False, dry_run: bool = False, summary_cap="default", sample: int = 0,
             runner=subprocess.run, clock=time.time) -> Report:
    """One sync run; the module docstring lists its steps. Every problem goes into the returned Report."""
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
            machine_id = ""
            cloud_cfg, inbox_done = cloud.lane(cfg), []
            if not dry_run:
                machine_id = machine.local_id(cfg.kb_dir)
                taken = host_is_taken(cfg, machine_id)   # before any git step and any write under the host's folders
                if taken:
                    report.errors.append(taken)
                    return report
                git_ok, remote = git_gate(cfg, report)
                idx = Index(cfg.kb_dir / "index.sqlite")
                idx.update(cfg.root)
                cloud_cfg, inbox_done = cloud_inbox(cfg, cloud_cfg, git_ok and remote, report)
            lanes = [cfg] + ([cloud_cfg] if cloud_cfg is not None else [])
            months = {lane_cfg.host: set() for lane_cfg in lanes}
            picker = SamplePicker(sample) if dry_run and sample > 0 else None
            _process_lanes(cfg, lanes, state, idx, report, lock, months, now, dry_run, picker, clock)
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
            _summarize_lanes(lanes, idx, state, cap, runner, report, lock, months, clock)
            if report.summarized or report.memories:
                idx.update(cfg.root)
            _write_catalogs(cfg, lanes, months, report)
            semantic = cfg.embed or cfg.embed_url
            if semantic:                        # before the commit, so this host's vector files go out with it
                endpoint, deadline = embed_own(cfg, idx, report, clock, hosts=[lane_cfg.host for lane_cfg in lanes])
            for lane_cfg in lanes:
                machine.claim(cfg.root, lane_cfg.host, machine_id)
            if git_ok:
                commit_own(cfg, state, report)
                if remote:
                    publish(cfg, idx, state, report)
                    if inbox_done and not report.errors and not cloud_waiting(cloud_cfg, state, clock):
                        report.errors += cloud.delete_branches(cfg.root, inbox_done)
            if semantic:                        # after the pull: other machines' vector files, then what is left
                embed_rest(cfg, idx, report, endpoint, deadline, clock)
            _record_success(cfg, state, report)
            return report
        except Exception as e:
            report.errors.append(f"sync: {type(e).__name__}: {e}")
            return report
        finally:
            if idx is not None:
                idx.close()
            if not dry_run:
                state.last_error = one_line(report.errors[0], LAST_ERROR_CHARS) if report.errors else ""
                save_state(state, report)
    finally:
        lock.release()
