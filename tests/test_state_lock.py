import os
import time

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


def test_lock_excludes_second_holder(tmp_path):
    a, b = Lock(tmp_path / "lock"), Lock(tmp_path / "lock")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_stale_lock_is_taken_over(tmp_path):
    a = Lock(tmp_path / "lock")
    assert a.acquire()
    old = time.time() - 3600
    os.utime(tmp_path / "lock", (old, old))
    b = Lock(tmp_path / "lock", stale_seconds=1800)
    assert b.acquire()
    a.touch()
    b.release()
