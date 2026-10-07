import pytest

from kb import vecfile


def test_round_trip_within_half_precision():
    items = [("session", "a-1", "a" * 40), ("turn", "a-1#3", "0123456789abcdef0123456789abcdef01234567")]
    vectors = [[0.5, -0.25, 0.123456, 1.0], [-1.0, 0.0, 0.333333, 0.000123]]
    data = vecfile.dumps("m/4", 4, items, vectors)
    assert len(data) < 200
    model, dim, got_items, got = vecfile.loads(data)
    assert (model, dim, got_items) == ("m/4", 4, items)
    for v, w in zip(vectors, got):
        assert w == pytest.approx(v, abs=1e-3)


def test_empty_file_is_valid():
    assert vecfile.loads(vecfile.dumps("m/4", 4, [], [])) == ("m/4", 4, [], [])


@pytest.mark.parametrize("data", [b"", b"nope\n{}\n", vecfile.MAGIC + b"{not json\n",
                                  vecfile.MAGIC + b'{"model": "m", "dim": 2, "dtype": "f32", "items": []}\n',
                                  vecfile.MAGIC + b'{"model": "m", "dim": 2, "dtype": "f16", "items": [["a"]]}\n',
                                  vecfile.MAGIC + b'{"model": "m", "dim": 2, "dtype": "f16", "items": [["a", "b"]]}\n',
                                  vecfile.MAGIC])
def test_broken_files_raise_one_line(data):
    with pytest.raises(vecfile.VecFileError):
        vecfile.loads(data)


def test_size_is_checked_and_mismatch_refused():
    data = vecfile.dumps("m/2", 2, [("session", "a", "b" * 40)], [[0.1, 0.2]])
    with pytest.raises(vecfile.VecFileError, match="wrong size"):
        vecfile.loads(data[:-1])
    with pytest.raises(vecfile.VecFileError):
        vecfile.dumps("m/2", 2, [("session", "a", "b" * 40)], [[0.1, 0.2, 0.3]])
    with pytest.raises(vecfile.VecFileError, match="sha"):
        vecfile.dumps("m/2", 2, [("session", "a", "not-hex")], [[0.1, 0.2]])


def test_the_header_holds_no_hash_next_to_a_key():
    sha = "c08077f20a4cc82e3673054cf0d7176a1b2c3d4e"
    data = vecfile.dumps("m/2", 2, [("memory", "memories/h/claude/x/context-token-guideline.md", sha)], [[0.1, 0.2]])
    assert sha.encode() not in data and sha not in data.split(b"\n")[1].decode()
    assert vecfile.loads(data)[2][0][2] == sha
