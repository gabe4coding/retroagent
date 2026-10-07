"""Fakes for the embedding tests: a deterministic toy embedder, an in-thread /v1/embeddings server (fast tests) and
the source of a fake llama-server program (slow tests, which start it as a process)."""
import hashlib
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 64
# words that mean the same thing land on the same axis, so a paraphrase can match without a shared word
SYNONYMS = {"cheaper": "cost", "price": "cost", "spend": "cost", "tokens": "cost",
            "broken": "flaky", "unstable": "flaky", "voli": "flight", "flights": "flight"}


def fake_vector(text: str) -> list:
    """Bag of words hashed onto DIM axes, after the synonym map. Prompt prefixes are dropped first."""
    text = re.sub(r"^(task: [^|]*\| query: |title: [^|]*\| text: )", "", text)
    v = [0.0] * DIM
    for w in re.findall(r"\w+", text.lower()):
        w = SYNONYMS.get(w, w)
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeEmbedServer:
    """A /v1/embeddings server on 127.0.0.1 in a thread. calls counts requests; fail=True answers 500."""

    def __init__(self, key: str = "", port: int = 0, thread: bool = True):
        self.key, self.calls, self.fail, self.inputs = key, 0, False, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self):
                return not outer.key or self.headers.get("Authorization") == f"Bearer {outer.key}"

            def do_GET(self):
                code = 200 if self.path in ("/health", "/v1/models") and self._auth() else 401
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                outer.calls += 1
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if outer.fail or not self._auth():
                    self.send_response(500 if outer.fail else 401)
                    self.end_headers()
                    return
                texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
                outer.inputs.extend(texts)
                data = [{"index": i, "embedding": [x * 3 for x in fake_vector(t)]} for i, t in enumerate(texts)]
                out = json.dumps({"data": list(reversed(data))}).encode()     # out of order on purpose
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        if thread:
            threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


FAKE_LLAMA_SERVER = r'''#!{python}
"""Fake llama-server: --port, --api-key-file; serves /health, /v1/models and /v1/embeddings like the real one."""
import sys
sys.path.insert(0, {tests!r})
import embed_fakes
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
key = open(args[args.index("--api-key-file") + 1]).read().strip()
embed_fakes.FakeEmbedServer(key, port, thread=False).httpd.serve_forever()
'''
