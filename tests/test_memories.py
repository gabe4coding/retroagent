import datetime as dt
import json
import os
from pathlib import Path

import pytest
from fixtures import (CWD, GH_TOKEN, SID, clone, init_remote, make_claude_tree, make_codex_tree, make_config,
                      write_jsonl)

from kb import gitops
from kb import memories as mem
from kb.cli import main
from kb.distill import split_front_matter
from kb.index import AmbiguousId, Filters, Index
from kb.sync import Report, own_paths, run_sync

ENC = "-" + CWD.strip("/").replace("/", "-")          # the project folder make_claude_tree writes
NOTE = """---
name: prefer-small-prs
description: "User prefers small pull requests"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0
  modified: 2026-10-07T08:10:59.984Z
---

Keep each pull request to one change.

**Why:** small changes are easier to review.
"""


def write_memory(projects, name="prefer-small-prs.md", text=NOTE, folder=ENC):
    path = Path(projects) / folder / "memory" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def cfg(tmp_path):
    projects = make_claude_tree(tmp_path / "src")
    sessions, home = make_codex_tree(tmp_path / "src")
    root = tmp_path / "kb"
    root.mkdir()
    return make_config(root, "h", projects, sessions, home)


def sync(cfg, idx=None, excluded=lambda cwd: False, dry_run=False):
    report = Report()
    n = mem.sync_memories(cfg, idx, report, excluded, dry_run)
    return n, report


def kb_file(cfg, name="prefer-small-prs.md", folder=ENC, agent="claude"):
    return cfg.root / "memories" / "h" / agent / folder / name if folder else cfg.root / "memories/h" / agent / name


# ---------------------------------------------------------------- front matter

def test_split_yaml_lifts_nested_fields_and_unquotes():
    fields, body = mem.split_yaml(NOTE)
    assert fields["name"] == "prefer-small-prs" and fields["type"] == "feedback"
    assert fields["description"] == "User prefers small pull requests"
    assert fields["originSessionId"] == "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"
    assert body.startswith("Keep each pull request")


def test_split_yaml_top_level_wins_and_odd_values():
    text = "---\ntype: user\nmetadata:\n  type: feedback\ndescription: >\n  folded\nname: 'it''s'\n---\nbody\n"
    fields, body = mem.split_yaml(text)
    assert fields == {"type": "user", "name": "it's"} and body == "body\n"


@pytest.mark.parametrize("text", ["no front matter\n", "---\nname: x\nnever closed\n", "---\nname: x\n---tail\n"])
def test_split_yaml_without_a_closed_front_matter_keeps_the_text(text):
    assert mem.split_yaml(text) == ({}, text)


def test_encode_cwd_matches_claude_folder_names():
    assert mem.encode_cwd("/Users/me/Repositories/sessions-kb/.claude/worktrees/x_y") == \
        "-Users-me-Repositories-sessions-kb--claude-worktrees-x-y"
    assert mem.encode_cwd(CWD) == ENC


# ---------------------------------------------------------------- copying

def test_memory_is_copied_with_json_front_matter(cfg):
    write_memory(cfg.claude_dir)
    n, report = sync(cfg)
    assert n == 1 and report.errors == []
    meta, body = split_front_matter(kb_file(cfg).read_text())
    assert meta == {"kind": "memory", "agent": "claude", "host": "h", "project": "demo", "cwd": CWD, "folder": ENC,
                    "file": "prefer-small-prs.md", "name": "prefer-small-prs",
                    "description": "User prefers small pull requests", "type": "feedback",
                    "origin_session": "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0", "modified": "2026-10-07T08:10:59.984Z"}
    assert body.startswith("Keep each pull request") and body.endswith("easier to review.\n") and "metadata:" not in body
    assert sync(cfg)[0] == 0                                    # unchanged: nothing written


def utime(path, iso):
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    os.utime(path, (t, t))


def modified(cfg, name="prefer-small-prs.md"):
    return split_front_matter(kb_file(cfg, name).read_text())[0]["modified"]


def test_a_memory_without_its_own_modified_is_dated_by_the_file_mtime(cfg):
    src = write_memory(cfg.claude_dir, "plain.md", "a fact\n")
    utime(src, "2026-03-04T05:06:07Z")
    utime(write_memory(cfg.claude_dir), "2026-03-04T05:06:07Z")
    assert sync(cfg)[0] == 2
    assert modified(cfg, "plain.md") == "2026-03-04T05:06:07Z"
    assert modified(cfg) == "2026-10-07T08:10:59.984Z"           # its own front matter wins


def test_a_touched_memory_keeps_its_date_and_an_edited_one_takes_the_new_mtime(cfg):
    src = write_memory(cfg.claude_dir, "plain.md", "a fact\n")
    utime(src, "2026-03-04T05:06:07Z")
    sync(cfg)
    utime(src, "2026-05-01T00:00:00Z")                          # touched, same text: nothing to commit
    assert sync(cfg)[0] == 0 and modified(cfg, "plain.md") == "2026-03-04T05:06:07Z"
    src.write_text("a changed fact\n")
    utime(src, "2026-06-01T00:00:00Z")
    assert sync(cfg)[0] == 1 and modified(cfg, "plain.md") == "2026-06-01T00:00:00Z"
    assert sync(cfg)[0] == 0


def test_an_old_copy_without_modified_is_dated_once(cfg):
    src = write_memory(cfg.claude_dir, "plain.md", "a fact\n")
    sync(cfg)
    target = kb_file(cfg, "plain.md")
    target.write_text(target.read_text().replace(f'modified: "{modified(cfg, "plain.md")}"', 'modified: ""'))
    utime(src, "2026-03-04T05:06:07Z")
    assert sync(cfg, dry_run=True)[0] == 1
    assert sync(cfg)[0] == 1 and modified(cfg, "plain.md") == "2026-03-04T05:06:07Z"
    assert sync(cfg)[0] == 0


def test_index_file_without_front_matter_and_secrets_are_redacted(cfg):
    write_memory(cfg.claude_dir, "MEMORY.md", f"- [Note](note.md) — token {GH_TOKEN}\n")
    n, report = sync(cfg)
    text = kb_file(cfg, "MEMORY.md").read_text()
    assert n == 1 and GH_TOKEN not in text and "[REDACTED" in text and sum(report.redactions.values()) >= 1
    assert split_front_matter(text)[0]["name"] == "MEMORY"


def test_deleted_or_renamed_memory_leaves_the_kb(cfg):
    src = write_memory(cfg.claude_dir)
    write_memory(cfg.claude_dir, "keep.md", "kept\n")
    sync(cfg)
    src.rename(src.with_name("renamed.md"))
    n, _ = sync(cfg)
    assert n == 2 and not kb_file(cfg).exists() and kb_file(cfg, "renamed.md").exists()
    assert kb_file(cfg, "keep.md").exists()


def test_a_gone_memory_folder_keeps_its_kb_copies(cfg):
    src = write_memory(cfg.claude_dir)
    sync(cfg)
    src.unlink()
    src.parent.rmdir()
    assert sync(cfg)[0] == 0 and kb_file(cfg).exists()


def test_excluded_cwd_is_not_copied_and_its_old_copies_go(cfg):
    write_memory(cfg.claude_dir)
    sync(cfg)
    n, _ = sync(cfg, excluded=lambda cwd: cwd == CWD)
    assert n == 1 and not kb_file(cfg).exists()


def test_unreadable_or_oversized_memory_keeps_the_old_copy(cfg):
    src = write_memory(cfg.claude_dir)
    sync(cfg)
    src.write_text(NOTE + "x" * mem.MAX_BYTES)
    n, report = sync(cfg)
    assert n == 0 and kb_file(cfg).exists() and "more than" in report.errors[0]


def test_dry_run_writes_and_removes_nothing(cfg):
    write_memory(cfg.claude_dir)
    n, _ = sync(cfg, dry_run=True)
    assert n == 1 and not (cfg.root / "memories").exists()


def test_symlinks_and_hidden_files_are_left_out(cfg, tmp_path):
    secret = tmp_path / "elsewhere.md"
    secret.write_text("not a memory\n")
    folder = write_memory(cfg.claude_dir).parent
    (folder / "link.md").symlink_to(secret)
    (folder / ".hidden.md").write_text("hidden\n")
    sync(cfg)
    assert sorted(p.name for p in kb_file(cfg).parent.iterdir()) == ["prefer-small-prs.md"]


def test_cwd_comes_from_the_index_when_the_transcripts_are_gone(cfg):
    folder = "-Users-me-Repositories-old"
    write_memory(cfg.claude_dir, folder=folder)

    class Idx:
        class db:
            @staticmethod
            def execute(sql, params):
                return [("/Users/me/Repositories/old/.claude/worktrees/feature",)]

    sync(cfg, idx=Idx())
    meta, _ = split_front_matter(kb_file(cfg, folder=folder).read_text())
    assert meta["cwd"] == "/Users/me/Repositories/old" and meta["project"] == "old"


def test_a_session_moved_from_a_scratch_folder_gives_the_project_folder_its_cwd(cfg):
    folder = "-Users-me-Repositories-moved"
    scratch = "/Users/me/Library/Application Support/Claude/scratch-workspaces/x/scratch-1"
    write_jsonl(Path(cfg.claude_dir) / folder / f"{SID}.jsonl", [
        {"type": "attachment", "cwd": scratch},
        {"type": "user", "cwd": "/Users/me/elsewhere"},
        {"type": "relocated", "relocatedCwd": "/Users/me/Repositories/moved"}])
    write_memory(cfg.claude_dir, folder=folder)
    sync(cfg)
    meta, _ = split_front_matter(kb_file(cfg, folder=folder).read_text())
    assert meta["cwd"] == "/Users/me/Repositories/moved" and meta["project"] == "moved"


def test_unknown_cwd_still_copies(cfg):
    write_memory(cfg.claude_dir, folder="-Users-me-nowhere")
    sync(cfg)
    meta, _ = split_front_matter(kb_file(cfg, folder="-Users-me-nowhere").read_text())
    assert meta["cwd"] == "" and meta["project"] == "unknown"


def test_codex_memories(cfg):
    folder = Path(cfg.codex_home) / "memories"
    (folder / "rollout_summaries").mkdir(parents=True)
    (folder / "MEMORY.md").write_text("Codex notes\n")
    (folder / "rollout_summaries" / "a.md").write_text("summary\n")
    n, _ = sync(cfg)
    assert n == 2 and kb_file(cfg, "MEMORY.md", folder="", agent="codex").exists()
    meta, _ = split_front_matter((cfg.root / "memories/h/codex/rollout_summaries/a.md").read_text())
    assert meta["agent"] == "codex" and meta["project"] == "" and meta["file"] == "rollout_summaries/a.md"
    (folder / "rollout_summaries" / "a.md").unlink()
    assert sync(cfg)[0] == 1 and not (cfg.root / "memories/h/codex/rollout_summaries/a.md").exists()


# ---------------------------------------------------------------- index and CLI

@pytest.fixture
def indexed(cfg):
    write_memory(cfg.claude_dir)
    write_memory(cfg.claude_dir, "MEMORY.md", "- [Small PRs](prefer-small-prs.md) — one change per PR\n")
    sync(cfg)
    idx = Index(cfg.kb_dir / "index.sqlite")
    idx.update(cfg.root)
    yield cfg, idx
    idx.close()


def test_index_finds_and_looks_up_memories(indexed):
    cfg, idx = indexed
    assert idx.memories_changed == 2
    hits = idx.find_memories("easier review")
    assert hits[0]["ref"] == "demo/prefer-small-prs" and "«" in hits[0]["snippet"]
    assert idx.find_memories("easier", Filters(project="other")) == []
    assert idx.memory("demo/prefer-small-prs")["type"] == "feedback"
    assert idx.memory("prefer-small-prs")["origin_session"].startswith("0f1e2d3c")
    assert idx.memory("demo/MEM")["name"] == "MEMORY"
    with pytest.raises(AmbiguousId):
        idx.memory("demo/")
    assert idx.memory("nothing") is None


def test_index_drops_deleted_memories_and_rejects_misplaced_ones(indexed):
    cfg, idx = indexed
    kb_file(cfg, "MEMORY.md").unlink()
    wrong = cfg.root / "memories/other-host/claude/x/note.md"
    wrong.parent.mkdir(parents=True)
    wrong.write_text(kb_file(cfg).read_text())
    idx.update(cfg.root)
    assert idx.memories_changed == 1 and [m["name"] for m in idx.memories()] == ["prefer-small-prs"]
    assert idx.errors and idx.errors[0][0] == "memories/other-host/claude/x/note.md"


def test_cli_find_and_memory(indexed, capsys, monkeypatch):
    cfg, idx = indexed
    idx.close()
    conf = cfg.root.parent / "config.json"
    conf.write_text(json.dumps({"root": str(cfg.root), "host": "h", "claude_dir": str(cfg.claude_dir),
                                "codex_dirs": [], "codex_home": str(cfg.codex_home)}))
    monkeypatch.setenv("KB_CONFIG", str(conf))
    assert main(["find", "easier"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("memory ") and "[kb memory demo/prefer-small-prs]" in out
    assert main(["find", "easier", "--no-memories"]) == 1
    capsys.readouterr()
    assert main(["memory"]) == 0
    assert "demo/prefer-small-prs" in capsys.readouterr().out
    assert main(["memory", "prefer-small-prs"]) == 0
    out = capsys.readouterr().out
    assert "from session c3d2e1f0" in out and "Keep each pull request" in out
    assert "kind:" not in out
    assert main(["memory", "nope"]) == 1


# ---------------------------------------------------------------- sync, two machines

@pytest.mark.slow
def test_memories_are_committed_pushed_and_searchable_on_the_other_host(tmp_path):
    remote = init_remote(tmp_path)
    a = make_config(clone(remote, tmp_path / "a"), "host-a", make_claude_tree(tmp_path / "sa"),
                    *make_codex_tree(tmp_path / "sa"))
    b = make_config(clone(remote, tmp_path / "b"), "host-b", tmp_path / "none", tmp_path / "none", tmp_path / "none")
    write_memory(a.claude_dir)
    ra = run_sync(a, summary_cap=0)
    assert ra.errors == [] and ra.memories == 1 and ra.pushed
    assert "memories/host-a" in own_paths(a)
    subject = gitops.git(a.root, "log", "-1", "--format=%s").stdout.strip()
    assert subject.endswith(", 1 memories")
    assert gitops.git(a.root, "ls-files", "memories/host-a").stdout.strip() == \
        f"memories/host-a/claude/{ENC}/prefer-small-prs.md"
    rb = run_sync(b, summary_cap=0)
    assert rb.errors == []
    idx = Index(b.kb_dir / "index.sqlite")
    try:
        assert idx.memory("demo/prefer-small-prs")["host"] == "host-a"
    finally:
        idx.close()
    write_memory(a.claude_dir).unlink()
    ra = run_sync(a, summary_cap=0)
    assert ra.memories == 1 and ra.pushed
    assert gitops.git(a.root, "ls-files", "memories/host-a").stdout.strip() == ""
