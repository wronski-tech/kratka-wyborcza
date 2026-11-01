"""A fake OpenAI-compatible server for tests and the load test (no key, no network).

Chat completions return the content a ``reply`` callable gives for the request body; embeddings are a hash
of each text (stable, 32 dimensions). Point the API at it with ``OPENAI_BASE_URL`` and any ``OPENAI_API_KEY``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def start(reply: Callable[[dict[str, Any]], str]) -> tuple[ThreadingHTTPServer, str]:
    """Serve on a free local port in a daemon thread; return the server and its base URL (``.../v1``)."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/embeddings"):
                texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
                payload: dict[str, Any] = {"object": "list", "model": body["model"], "usage": USAGE, "data": [
                    {"object": "embedding", "index": i, "embedding": [b / 255 for b in hashlib.sha256(t.encode()).digest()]}
                    for i, t in enumerate(texts)]}
            else:
                payload = {"id": "fake", "object": "chat.completion", "created": 0, "model": body["model"], "usage": USAGE,
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": reply(body)}}]}
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"
