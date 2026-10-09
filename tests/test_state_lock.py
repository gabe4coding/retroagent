import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import kb.lock
from kb.lock import Lock
from kb.state import State


def test_state_round_trip_and_corruption(tmp_path):
    p = tmp_path / ".kb" / "sync-state.json"
    st = State.load(p)
    assert st.files == {} and st.last_ok == ""
    st.files["k"] = "fp"
    st.summary_attempts["id"] = 2
    st.last_ok = "2026-10-06T10:00:00Z"
    st.save()
    st2 = State.load(p)
    assert st2.files == {"k": "fp"} and st2.summary_attempts == {"id": 2} and st2.last_ok.startswith("2026")
    p.write_text("{broken")
    assert State.load(p).files == {}


@pytest.mark.parametrize("content", ["[]", '"x"', "7", "null", "[1, 2]"])
def test_state_with_non_object_json_is_empty(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_text(content)
    st = State.load(p)
    assert st.files == {} and st.summary_attempts == {} and st.last_ok == "" and st.last_error == ""


def test_state_with_wrong_field_shapes_is_sanitized(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"files": ["a"], "summary_attempts": "x", "last_ok": 5, "last_result": None,
                             "last_error": ["e"]}))
    st = State.load(p)
    assert st.files == {} and st.summary_attempts == {}
    assert (st.last_ok, st.last_result, st.last_error) == ("", "", "")
    p.write_text(json.dumps({"files": {"k": "fp"}, "summary_attempts": {"i": 2}, "last_ok": "t"}))
    st = State.load(p)
    assert st.files == {"k": "fp"} and st.summary_attempts == {"i": 2} and st.last_ok == "t"


def test_state_raw_pending_round_trip_and_tolerant_load(tmp_path):
    p = tmp_path / "s.json"
    st = State.load(p)
    assert st.raw_pending == {}
    st.raw_pending["unit"] = "fp"
    st.save()
    assert State.load(p).raw_pending == {"unit": "fp"}
    p.write_text(json.dumps({"files": {"k": "fp"}, "raw_pending": ["x"]}))
    assert State.load(p).raw_pending == {} and State.load(p).files == {"k": "fp"}


def test_state_unreadable_file_is_empty(tmp_path):
    p = tmp_path / "s.json"
    p.mkdir()  # reading a directory raises OSError
    assert State.load(p).files == {}


def test_lock_excludes_second_holder(tmp_path):
    a, b = Lock(tmp_path / "lock"), Lock(tmp_path / "lock")
    assert a.acquire() and not b.acquire()
    old = time.time() - 3600
    os.utime(tmp_path / "lock", (old, old))
    assert not Lock(tmp_path / "lock").acquire()  # age never frees a held lock
    a.release()
    assert b.acquire()
    b.release()


def test_lock_file_is_kept_and_holds_informational_pid(tmp_path):
    path = tmp_path / "sub" / "lock"
    a = Lock(path)
    assert a.acquire()
    info = json.loads(path.read_text())
    assert info["pid"] == os.getpid() and "started" in info
    a.release()
    assert path.exists()


def test_release_without_acquire_and_double_release_are_safe(tmp_path):
    a = Lock(tmp_path / "lock")
    a.release()
    assert a.acquire() and a.acquire()
    a.release()
    a.release()
    assert Lock(tmp_path / "lock").acquire()


@pytest.mark.slow
def test_lock_held_by_another_process_blocks_until_it_dies(tmp_path):
    path = tmp_path / "lock"
    src = str(Path(kb.lock.__file__).resolve().parents[1])
    code = ("import sys, time\nfrom kb.lock import Lock\n"
            "l = Lock(sys.argv[1])\nassert l.acquire()\nprint('ready', flush=True)\ntime.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "PYTHONPATH": src})
    try:
        assert child.stdout.readline().strip() == "ready"
        assert not Lock(path).acquire()
        child.kill()
        child.wait(timeout=10)
        mine = Lock(path)
        deadline = time.time() + 5
        while not mine.acquire() and time.time() < deadline:
            time.sleep(0.05)
        assert mine.held
        mine.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
