"""The two things the console's end-to-end test stands in for: the model
router and the review services.

Both are tiny threaded HTTP servers in the test process. The agent under
test is the real server in its own process; it reaches these exactly as it
reaches the real ones, over HTTP at the addresses its environment names
(MODEL_ROUTER_URL, REVIEW_SERVICE_PORT, REVIEW_CONTROL_PORT).

FakeRouter is an OpenAI-compatible /v1/chat/completions that plays one
deterministic build: the first coordinator turn writes one file, the turn
after the tool result says it is done. Every other caller (the classifier,
a summary, a background job) gets a short text answer, which each of them
treats as best-effort.

FakeReview is the reviewer (control port: /check) and the review dashboard
(service port: /api/review/status, merge, restart) in one: every branch it
is asked to check is READY at its current commit, and a merge is a real
`git merge --ff-only` in the live checkout, so what the agent sees after the
merge is what the real merge would have left.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

# What the fake model builds. The test asserts these land on live main.
BUILD_PATH = "GREETING.md"
BUILD_CONTENT = "Hello from the end-to-end test.\n"
DONE_TEXT = f"Created {BUILD_PATH} with a one-line greeting. Nothing else changed."


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake/1"

    def log_message(self, fmt, *args):  # noqa: D401 - quiet
        return

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _json(self, code: int, payload) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler]):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------------------
# The model router
# ---------------------------------------------------------------------------

def _usage() -> dict:
    return {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}


class _RouterHandler(_Handler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            return self._json(200, {"status": "ok"})
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": []})
        return self._json(404, {"error": "not here"})

    def do_POST(self):  # noqa: N802
        body = self._body()
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": f"no fake for {self.path}"})
        router: FakeRouter = self.server.owner  # type: ignore[attr-defined]
        message = router.reply(body)
        model = body.get("model") or "fake"
        if body.get("stream"):
            return self._stream(model, message)
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        return self._json(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": _usage(),
        })

    def _stream(self, model: str, message: dict) -> None:
        """Server-sent events, for the callers that stream (no tools bound)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        base = {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model}
        chunks = [
            {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": message.get("content") or ""},
                                  "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**base, "choices": [], "usage": _usage()},
        ]
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class FakeRouter(_Server):
    def __init__(self):
        super().__init__(_RouterHandler)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def reply(self, body: dict) -> dict:
        tools = {(t.get("function") or {}).get("name") for t in body.get("tools") or []}
        messages = body.get("messages") or []
        with self._lock:
            self.calls.append({"model": body.get("model"), "tools": sorted(t for t in tools if t),
                               "stream": bool(body.get("stream"))})
        if "write" in tools:
            # The build seat. One write, then done once its result is back.
            if any(m.get("role") == "tool" for m in messages):
                return {"role": "assistant", "content": DONE_TEXT}
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": "write",
                             "arguments": json.dumps({"path": BUILD_PATH, "content": BUILD_CONTENT})},
            }]}
        # The classifier (structured output it will not parse, so it falls
        # back to its default), a summary, a background job: all best-effort.
        return {"role": "assistant", "content": "ok"}

    def build_calls(self) -> int:
        with self._lock:
            return sum(1 for c in self.calls if "write" in c["tools"])


# ---------------------------------------------------------------------------
# The review services
# ---------------------------------------------------------------------------

def _git(cwd: str, *args: str) -> str:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


class _ReviewHandler(_Handler):
    def do_GET(self):  # noqa: N802
        review: FakeReview = self.server.owner.review  # type: ignore[attr-defined]
        if self.path.startswith("/health"):
            return self._json(200, {"ok": True})
        if self.path.startswith("/api/review/status"):
            return self._json(200, review.status())
        # /projects (checks autodetection): "cannot confirm", so nothing is
        # written into the test's projects.json.
        return self._json(404, {"error": "not here"})

    def do_POST(self):  # noqa: N802
        review: FakeReview = self.server.owner.review  # type: ignore[attr-defined]
        body = self._body()
        url = urlsplit(self.path)
        path = url.path
        if path.startswith("/check/"):
            branch = (parse_qs(url.query).get("branch") or [""])[0]
            review.checked(unquote(path[len("/check/"):]), branch)
            return self._json(200, {"queued": True})
        if path.endswith("/merge"):
            ok, detail = review.merge(body.get("branch", ""))
            return self._json(200 if ok else 409, {"ok": ok, **detail})
        if path.endswith("/restart"):
            review.restarts += 1
            return self._json(200, {"ok": True})
        return self._json(404, {"error": f"no fake for {path}"})


class FakeReview:
    """Both review ports, sharing one state."""

    def __init__(self, project: str, live: str):
        self.project = project
        self.live = live
        self.branches: set[str] = set()
        self.merged: list[str] = []
        self.restarts = 0
        self._lock = threading.Lock()
        self.service = _Server(_ReviewHandler)
        self.control = _Server(_ReviewHandler)
        for s in (self.service, self.control):
            s.review = self  # type: ignore[attr-defined]

    def start(self):
        self.service.start()
        self.control.start()
        return self

    def stop(self):
        self.service.stop()
        self.control.stop()

    def checked(self, project: str, branch: str) -> None:
        if project == self.project and branch:
            with self._lock:
                self.branches.add(branch)

    def status(self) -> dict:
        with self._lock:
            branches = sorted(self.branches)
        out = {}
        for b in branches:
            try:
                sha = _git(self.live, "rev-parse", f"refs/heads/{b}")
            except subprocess.CalledProcessError:
                continue
            out[b] = {"lastReviewedSha": sha, "verdict": "READY",
                      "summary": "Fake review: the change is fine.", "findings": []}
        return {self.project: {"branches": out}}

    def merge(self, branch: str) -> tuple[bool, dict]:
        try:
            sha = _git(self.live, "rev-parse", f"refs/heads/{branch}")
            _git(self.live, "merge", "--ff-only", "-q", sha)
        except subprocess.CalledProcessError as e:
            return False, {"error": (e.stderr or str(e)).strip()}
        with self._lock:
            self.merged.append(branch)
        return True, {"mergedFrom": sha}
