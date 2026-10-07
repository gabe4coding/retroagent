"""Vector files committed to the data repo (vectors/<host>/…), so other machines and cloud sessions get the embeddings
of a host's sessions and memories without running the model on them.

A file holds the vectors of one session (its summary document and its user turns) or of one memory:

    KBVEC2\\n
    {"model": "<model key>", "dim": 256, "dtype": "f16", "items": [["session", "<id>"], ...]}\\n
    <count * 20 bytes: the sha1 of each item's text>
    <count * dim little-endian float16 values>

The text hashes sit in the binary part, not next to the keys in the header: a key like "token-guideline.md" followed
by a 40-character hex string reads as an API key to secret scanners (gitleaks' generic rule), and the sync would hold
the file back. Half precision halves the size; for unit vectors it changes no ranking that matters. Stdlib only.
"""
from __future__ import annotations

import json
import struct

MAGIC = b"KBVEC2\n"
SHA = 20                     # bytes of a sha1 digest


class VecFileError(ValueError):
    """Not a valid vector file. One line."""


def dumps(model: str, dim: int, items: list, vectors: list) -> bytes:
    """items: (kind, key, sha1 hex) per vector, in the order of vectors."""
    if len(items) != len(vectors) or any(len(v) != dim for v in vectors):
        raise VecFileError("vector file: items and vectors do not match")
    head = json.dumps({"model": model, "dim": dim, "dtype": "f16", "items": [[kind, key] for kind, key, _ in items]},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        shas = b"".join(bytes.fromhex(sha) for _, _, sha in items)
    except ValueError:
        raise VecFileError("vector file: a sha is not hex") from None
    if len(shas) != SHA * len(items):
        raise VecFileError("vector file: a sha is not a sha1")
    flat = [x for v in vectors for x in v]
    return MAGIC + head + b"\n" + shas + struct.pack(f"<{len(flat)}e", *flat)


def loads(data: bytes):
    """(model, dim, items, vectors) of a vector file; VecFileError when it is not one."""
    if not data.startswith(MAGIC):
        raise VecFileError("vector file: bad magic")
    nl = data.find(b"\n", len(MAGIC))
    if nl < 0:
        raise VecFileError("vector file: no header")
    try:
        head = json.loads(data[len(MAGIC):nl])
        model, dim, keys = head["model"], int(head["dim"]), [tuple(i) for i in head["items"]]
    except (ValueError, KeyError, TypeError) as e:
        raise VecFileError(f"vector file: bad header ({e})") from None
    if head.get("dtype") != "f16" or dim <= 0 or any(len(i) != 2 for i in keys):
        raise VecFileError("vector file: unsupported header")
    n, body = len(keys), data[nl + 1:]
    if len(body) != n * (SHA + dim * 2):
        raise VecFileError("vector file: wrong size")
    items = [(kind, key, body[i * SHA:(i + 1) * SHA].hex()) for i, (kind, key) in enumerate(keys)]
    flat = struct.unpack(f"<{n * dim}e", body[n * SHA:])
    return model, dim, items, [list(flat[i * dim:(i + 1) * dim]) for i in range(n)]
