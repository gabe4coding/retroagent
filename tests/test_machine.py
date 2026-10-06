import re

from kb import machine


def test_local_id_is_made_once_and_is_random_hex(tmp_path):
    kb = tmp_path / ".kb"
    first = machine.local_id(kb)
    assert re.fullmatch(r"[0-9a-f]{32}", first) and (kb / "machine-id").read_text() == first + "\n"
    assert machine.local_id(kb) == first
    assert machine.local_id(tmp_path / "other" / ".kb") != first               # two folders, two machines


def test_an_empty_id_file_is_replaced_and_a_given_id_is_kept(tmp_path):
    kb = tmp_path / ".kb"
    kb.mkdir()
    (kb / "machine-id").write_text("\n")
    made = machine.local_id(kb)
    assert re.fullmatch(r"[0-9a-f]{32}", made)
    (kb / "machine-id").write_text("  my-own-id \n")
    assert machine.local_id(kb) == "my-own-id"


def test_marker_path_and_read_marker(tmp_path):
    assert machine.marker_path(tmp_path, "h") == tmp_path / "sessions" / "h" / ".machine-id"
    assert machine.read_marker(tmp_path, "h") == ""
    machine.marker_path(tmp_path, "h").parent.mkdir(parents=True)
    machine.marker_path(tmp_path, "h").write_text("abc\n")
    assert machine.read_marker(tmp_path, "h") == "abc"
