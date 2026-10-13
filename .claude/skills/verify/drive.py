#!/usr/bin/env python3
"""Drives retroagent end to end for /verify and prints the evidence as JSON (SKILL.md reads it).

  new    a sandbox: a bare remote, a data clone with the pages of seed/ (project demo-app), the fake Claude Code and
         Codex transcripts of tests/fixtures.py, a config with auto_sync off and no summaries, and one
         `kb sync --now --no-summaries`; prints its folder
  kb     one command of this checkout's bin/kb in a sandbox, with the sandbox's KB_CONFIG, KB_ROOT and HOME
  hook   one headless Claude Code session with this checkout as the plugin, in <dir>/work/demo-app, with the sandbox's
         KB_CONFIG and KB_ROOT (it keeps the real HOME for the login); the session is not saved. With --cached, the
         plugin is a copy in <dir>/plugins/cache/, like the one Claude Code runs, so its bin/kb and bin/kb-guard hand
         over to "code" in the sandbox config (set it with new --config)
  clean  removes a sandbox

kb, hook and clean refuse a folder without the sandbox marker, so no run reaches the real data repo. Git runs with
no global or system config.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
KB = ROOT / "bin" / "kb"
SEED = Path(__file__).resolve().parent / "seed"
PREFIX = "ra-verify-"
MARKER = ".ra-verify"
PROJECT = "demo-app"                    # the seeded project page; sessions run in <dir>/work/demo-app
KEEP = {"assistant", "user", "result"}  # the stream messages the evidence reads
PLUGIN_PARTS = (".claude-plugin", ".codex-plugin", "bin", "hooks", "skills", "src", "types")  # what --cached copies
# debug lines that only repeat a hook's output, which hookEvents already holds
ECHO = re.compile(r"Checking first line|Parsed initial response|success:\\n|returned permissionDecision|Keeping /")

# every process of a run, the fixtures' git included: no kb settings of the caller, no git config of the machine
for key in ("KB_CONFIG", "KB_ROOT", "KB_CHILD", "XDG_CACHE_HOME"):
    os.environ.pop(key, None)
os.environ.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "verify", "GIT_AUTHOR_EMAIL": "verify@example.com",
                   "GIT_COMMITTER_NAME": "verify", "GIT_COMMITTER_EMAIL": "verify@example.com"})

sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "src")]
import fixtures  # noqa: E402 - needs the path above
from fixtures import git  # noqa: E402

from kb.adapters.common import iter_records  # noqa: E402


def cut(text: str, n: int = 800) -> str:
    return text if len(text) <= n else f"{text[:n]}… ({len(text)} chars)"


def cut_values(data: dict, n: int = 400) -> dict:
    return {k: cut(v, n) if isinstance(v, str) else v for k, v in data.items()}


def read_jsonl(path: Path) -> list:
    return list(iter_records(path, Counter())) if path.is_file() else []


def sandbox(path: str) -> Path:
    """The sandbox at `path`, or exit: it must carry the marker that `new` writes."""
    d = Path(path).resolve()
    if not d.name.startswith(PREFIX) or not (d / MARKER).is_file():
        raise SystemExit(f"not a sandbox: {d}")
    return d


def kb_env(d: Path, home: bool = True) -> dict:
    env = {**os.environ, "KB_CONFIG": str(d / "config.json"), "KB_ROOT": str(d / "data")}
    if home:
        env["HOME"] = str(d / "home")
    return env


def run_kb(d: Path, args: list, timeout: float = 300) -> dict:
    t0 = time.perf_counter()
    try:
        p = subprocess.run([str(KB), *args], cwd=d / "work" / PROJECT, env=kb_env(d), capture_output=True, text=True,
                           timeout=timeout)
        exit, stdout, stderr = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:  # its output is bytes even with text=True
        exit = "timeout"
        stdout, stderr = (x.decode(errors="replace") if isinstance(x, bytes) else x or "" for x in (e.stdout, e.stderr))
    return {"argv": ["kb", *args], "exit": exit, "seconds": round(time.perf_counter() - t0, 1),
            "stdout": cut(stdout, 4000), "stderr": cut(stderr, 2000)}


def data_state(d: Path) -> dict:
    """The data clone and its remote: what a run committed, pushed or left uncommitted."""
    data = d / "data"
    return {"status": git("status", "--short", cwd=data).stdout.splitlines(),
            "log": git("log", "--oneline", "-3", cwd=data).stdout.splitlines(),
            "remoteLog": git("log", "--oneline", "-3", cwd=d / "remote.git").stdout.splitlines(),
            "kbFiles": sorted(str(p.relative_to(data / ".kb")) for p in (data / ".kb").rglob("*") if p.is_file())}


def cmd_new(args) -> dict:
    d = Path(tempfile.mkdtemp(prefix=PREFIX)).resolve()
    (d / MARKER).write_text("retroagent /verify sandbox\n")
    data = fixtures.clone(fixtures.init_remote(d), d / "data")
    shutil.copytree(SEED / "pages", data / "pages")
    git("add", "pages", cwd=data)
    git("commit", "-q", "-m", "seed pages", cwd=data)
    git("push", "-q", "origin", "main", cwd=data)
    fixtures.make_claude_tree(d / "agents")
    codex_sessions, codex_home = fixtures.make_codex_tree(d / "agents")
    (d / "home").mkdir()
    (d / "work" / PROJECT).mkdir(parents=True)
    cfg = {"root": str(data), "host": "verify", "claude_dir": str(d / "agents" / "projects"),
           "codex_dirs": [str(codex_sessions)], "codex_home": str(codex_home),
           "auto_sync": False,              # the SessionStart hook starts no sync unless --config turns it on
           "summary_cap_per_run": 0}        # no `claude -p` summaries
    cfg.update(json.loads(args.config or "{}"))
    (d / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    res = {"dir": str(d), "config": cfg}
    if not args.no_sync:
        res["sync"] = run_kb(d, ["sync", "--now", "--no-summaries"])
    return res


def cmd_kb(args) -> dict:
    d = sandbox(args.dir)
    return {"dir": str(d), **run_kb(d, args.kb_args, args.timeout), "data": data_state(d)}


def cached_plugin(d: Path) -> Path:
    """A fresh copy of the plugin where Claude Code caches it, under the version of the manifest."""
    version = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    dest = d / "plugins" / "cache" / "retroagent" / "retroagent" / version
    shutil.rmtree(dest, ignore_errors=True)
    for part in PLUGIN_PARTS:
        shutil.copytree(ROOT / part, dest / part, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def run_session(d: Path, args, run: str) -> tuple:
    """One session; its files in the sandbox are named by `run`, so parallel runs do not mix."""
    argv = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-hook-events", "--no-session-persistence",
            "--plugin-dir", str(cached_plugin(d) if args.cached else ROOT),
            # only this plugin's hooks: no user settings (their hooks, plugins, permission mode)
            "--setting-sources", "project,local", "--strict-mcp-config",
            "--allowedTools", args.allow,
            "--permission-prompts", "none",  # a prompt becomes a denial, never a hang
            "--debug-file", str(d / f"debug-{run}.log"),
            "--session-id", run, "--max-budget-usd", str(args.budget),
            "--model", args.model]
    stream: list = []
    results = 0
    with (d / f"stderr-{run}.txt").open("w") as err:
        child = subprocess.Popen(argv, cwd=d / "work" / PROJECT, env=kb_env(d, home=False), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=err, text=True)
    timer = threading.Timer(args.timeout, child.kill)
    timer.daemon = True
    timer.start()

    def ask(text: str) -> None:
        try:
            child.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": text}]}}) + "\n")
            child.stdin.flush()
        except BrokenPipeError:
            pass                        # claude has exited: its exit code and stderr-<run>.txt tell why

    with (d / f"out-{run}.jsonl").open("w", encoding="utf-8") as out:
        ask(args.prompts[0])
        for line in child.stdout:
            out.write(line)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") in KEEP or msg.get("subtype") == "hook_response":
                stream.append(msg)
            if msg.get("type") != "result":
                continue
            results += 1
            if results < len(args.prompts):
                ask(args.prompts[results])
            else:
                child.stdin.close()
    code = child.wait()
    killed = not timer.is_alive()
    timer.cancel()
    return ("timeout" if killed else code), stream


def plugin_log(debug: Path) -> list:
    """What Claude Code said about the plugin: that it loaded, each of its hooks with status and time, and errors.
    Each line once, without its time stamp."""
    if not debug.is_file():
        return []
    with debug.open(encoding="utf-8") as fh:
        lines = (line.rstrip("\n").split(" ", 1)[-1].replace(str(ROOT) + "/", "") for line in fh
                 if "retroagent" in line and not ECHO.search(line))
        return list(dict.fromkeys(lines))[:60]


def hook_events(stream: list) -> list:
    """Each hook answer that says something, as Claude Code got it. Managed hooks run too, and the stream does not
    name the plugin of a hook: pluginLog does."""
    events = [m for m in stream if m.get("subtype") == "hook_response"]
    return [cut_values({k: m[k] for k in ("hook_event", "hook_name", "exit_code", "outcome", "stdout", "stderr")
                        if m.get(k) not in (None, "")}, 600)
            for m in events if m.get("outcome") != "success" or (m.get("stdout") or "").strip() not in ("", "{}")]


def cmd_hook(args) -> dict:
    d = sandbox(args.dir)
    run = str(uuid.uuid4())             # also the session id
    exit, stream = run_session(d, args, run)
    blocks = [b for m in stream if m.get("type") in ("assistant", "user")
              for b in (m.get("message") or {}).get("content") or [] if isinstance(b, dict)]

    def text_of(content) -> str:
        return content if isinstance(content, str) else "\n".join(c.get("text", "") for c in content or [])

    results = [m for m in stream if m.get("type") == "result"]
    sync_log = d / "data" / ".kb" / "sync.log"
    return {
        "dir": str(d),
        "exit": exit,
        "turns": [{"prompt": p, "isError": r.get("is_error"), "result": cut(str(r.get("result") or "")),
                   "totalCostUsd": r.get("total_cost_usd")} for p, r in zip(args.prompts, results)],
        "toolUses": [{"name": b["name"], "input": cut_values(b.get("input") or {})}
                     for b in blocks if b.get("type") == "tool_use"],
        "toolResults": [{"isError": bool(b.get("is_error")), "content": cut(text_of(b.get("content")))}
                        for b in blocks if b.get("type") == "tool_result"],
        "hookEvents": hook_events(stream),
        "hintLog": read_jsonl(d / "data" / ".kb" / "hints" / "log.jsonl"),
        "syncLog": sync_log.read_text(encoding="utf-8").splitlines()[-20:] if sync_log.is_file() else [],
        "data": data_state(d),
        "pluginLog": plugin_log(d / f"debug-{run}.log"),
    }


def cmd_clean(args) -> dict:
    d = sandbox(args.dir)
    shutil.rmtree(d)
    return {"removed": str(d)}


def main() -> int:
    p = argparse.ArgumentParser(prog="drive.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="a sandbox; prints its folder and the first sync")
    n.add_argument("--config", help="JSON merged into the config, like '{\"auto_sync\": true}'")
    n.add_argument("--no-sync", action="store_true", help="skip the first `kb sync`")
    n.set_defaults(fn=cmd_new)
    k = sub.add_parser("kb", help="one kb command in a sandbox")
    k.add_argument("dir", help="a sandbox from `new`")
    k.add_argument("--timeout", type=float, default=300)
    k.set_defaults(fn=cmd_kb)
    k.epilog = "The kb arguments come after --, like: drive.py kb <dir> -- find flaky"
    h = sub.add_parser("hook", help="one headless Claude Code session with the plugin")
    h.add_argument("dir", help="a sandbox from `new`")
    h.add_argument("prompts", nargs="+", help="each one goes in after the result of the one before")
    h.add_argument("--allow", default="Bash,Read", help="tools the session may use without a prompt (default %(default)s)")
    h.add_argument("--model", default="haiku")
    h.add_argument("--budget", type=float, default=0.5, help="--max-budget-usd of the session")
    h.add_argument("--timeout", type=float, default=600, help="seconds before the session is killed")
    h.add_argument("--cached", action="store_true", help="run the plugin from a copy in <dir>/plugins/cache/")
    h.set_defaults(fn=cmd_hook)
    c = sub.add_parser("clean", help="remove a sandbox")
    c.add_argument("dir")
    c.set_defaults(fn=cmd_clean)
    argv, kb_args = sys.argv[1:], []
    if argv[:1] == ["kb"] and "--" in argv:     # split first: drive.py options can then come after <dir>
        i = argv.index("--")
        argv, kb_args = argv[:i], argv[i + 1:]
    args = p.parse_args(argv)
    if args.cmd == "kb":
        if not kb_args:
            p.error("kb needs the kb arguments after --")
        args.kb_args = kb_args
    print(json.dumps(args.fn(args), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
