#!/usr/bin/env python3
"""auto_router_demo.py -- a live, offline proof that the Auto Router routes each
task to the right backend.

It spins up a mock multi-backend server (a cheap classifier + three candidate
models with different price/capability), starts the REAL codex-shim server with
the router enabled, then sends a series of real Codex ``/v1/responses`` requests
of varying difficulty and prints which backend each one actually hit.

No network, no API keys, standard library only (the shim itself needs aiohttp):

    python3 examples/auto_router_demo.py

Expected: trivial -> cheapest model, medium -> mid model, hard -> strong model,
image task -> the only image-capable model, and a repeat task served from cache
without re-calling the classifier.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK_PORT = int(os.environ.get("DEMO_MOCK_PORT", "8799"))
SHIM_PORT = int(os.environ.get("DEMO_SHIM_PORT", "8766"))

# Relative costs only matter for the cheapest-among-viable tie-break.
COST = {"cheap-real": 0.3, "mid-real": 1.0, "strong-real": 5.0}
ROUTED = {"cheap-real": "cheap", "mid-real": "mid", "strong-real": "strong"}


def _score_task(task: str):
    """Stand-in for a real classifier: score each candidate 0-1 from the task.
    A real run uses your configured classifier model; the routing logic the
    shim applies to these scores is identical."""
    t = task.lower()
    hard = any(k in t for k in ("refactor", "debug", "concurren", "architecture", "race condition", "across"))
    medium = any(k in t for k in ("endpoint", "crud", "parse", "implement", "api", "migrate", "tests"))
    if hard:
        return {"cheap": 0.40, "mid": 0.55, "strong": 0.95}
    if medium:
        return {"cheap": 0.50, "mid": 0.85, "strong": 0.95}
    return {"cheap": 0.90, "mid": 0.92, "strong": 0.95}


class Mock(BaseHTTPRequestHandler):
    classifier_calls = 0
    last_scores = {}
    last_backend = None

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        b = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) if n else b"{}")
        if not self.path.split("?")[0].endswith("/v1/chat/completions"):
            self._json(404, {"e": "nope"})
            return
        model = body.get("model")
        if model == "classifier-real":
            user = "\n".join(
                m.get("content", "")
                for m in body.get("messages", [])
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            )
            task = ""
            for line in user.splitlines():
                if line.strip().startswith("current_task") or task:
                    task += line + " "
            scores = _score_task(task or user)
            Mock.classifier_calls += 1
            Mock.last_scores = scores
            self._json(200, {
                "choices": [{"message": {"role": "assistant", "content": json.dumps({"scores": scores, "reasoning": "demo"})}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            })
            return
        Mock.last_backend = model
        self._json(200, {
            "choices": [{"message": {"role": "assistant", "content": "Handled by %s." % model}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        })


def _post_response(task, with_image=False):
    if with_image:
        content = [
            {"type": "input_text", "text": task},
            {"type": "input_image", "image_url": "data:image/png;base64,xx"},
        ]
        payload = {"model": "codex-auto", "input": [{"role": "user", "content": content}]}
    else:
        payload = {"model": "codex-auto", "input": task}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/responses" % SHIM_PORT,
        data=data, method="POST", headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30).read()


def main():
    mock = "http://127.0.0.1:%d" % MOCK_PORT
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    settings = {
        "models": [
            {"slug": "cheap", "model": "cheap-real", "display_name": "Cheap", "provider": "openai", "base_url": mock + "/v1", "api_key": "k"},
            {"slug": "mid", "model": "mid-real", "display_name": "Mid", "provider": "openai", "base_url": mock + "/v1", "api_key": "k"},
            {"slug": "strong", "model": "strong-real", "display_name": "Strong", "provider": "openai", "base_url": mock + "/v1", "api_key": "k"},
            {"slug": "classifier", "model": "classifier-real", "display_name": "Classifier", "provider": "openai", "base_url": mock + "/v1", "api_key": "k"},
        ],
        "router": {
            "enabled": True,
            "slug": "codex-auto",
            "classifier": "classifier",
            "threshold": 0.7,
            "default": "cheap",
            "cache": True,
            "candidates": [
                {"slug": "cheap", "cost": 0.3, "supports_images": False, "card": "Very cheap, fast. Single-file edits, codegen, simple changes."},
                {"slug": "mid", "cost": 1.0, "supports_images": False, "card": "Cheap generalist. Standard servers/CRUD, data processing, moderate multi-file edits."},
                {"slug": "strong", "cost": 5.0, "supports_images": True, "card": "Frontier. Big multi-file refactors, hard debugging, architecture, and image tasks."},
            ],
        },
    }
    cfg_f = os.path.join(REPO, "_demo_models.json")
    with open(cfg_f, "w") as fh:
        fh.write(json.dumps(settings))

    env = dict(
        os.environ,
        PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""),
        CODEX_SHIM_DISABLE_CHATGPT="1",
        CODEX_SHIM_DISABLE_CURSOR="1",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "codex_shim.server", "--settings", cfg_f, "--port", str(SHIM_PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    def fmt_scores(s):
        return "cheap=%.2f mid=%.2f strong=%.2f" % (s.get("cheap", 0), s.get("mid", 0), s.get("strong", 0))

    try:
        for _ in range(80):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % SHIM_PORT, timeout=2).read()
                break
            except Exception:
                time.sleep(0.1)

        print("=" * 92)
        print("codex-shim Auto Router -- live routing proof (offline, real server)")
        print("=" * 92)
        print("Candidates:  cheap ($0.3, no images) | mid ($1.0, no images) | strong ($5.0, images)")
        print("Rule: cheapest candidate scoring >= 0.70 wins; image tasks skip models that can't see.\n")
        print("%-2s %-44s %-32s %-14s %s" % ("#", "Task", "Classifier scores", "Routed to", "Cost"))
        print("-" * 92)

        cases = [
            ("add a docstring to the foo() helper", False, "cheap-real"),
            ("write a CRUD REST endpoint with tests", False, "mid-real"),
            ("refactor the auth module across 8 files and fix the race condition", False, "strong-real"),
            ("what does this screenshot show?", True, "strong-real"),
        ]
        ok = True
        for i, (task, img, expected) in enumerate(cases, 1):
            Mock.last_backend = None
            _post_response(task, with_image=img)
            backend = Mock.last_backend
            ok = ok and backend == expected
            note = "  <- only image-capable model" if img else ""
            shown = (task[:41] + "...") if len(task) > 44 else task
            print("%-2d %-44s %-32s %-14s $%-4s%s" % (
                i, shown, fmt_scores(Mock.last_scores), ROUTED.get(backend, "?"), COST.get(backend, "?"), note))

        # Caching: repeat case #1; the classifier must NOT be called again.
        calls_before = Mock.classifier_calls
        Mock.last_backend = None
        _post_response(cases[0][0], with_image=False)
        cached = Mock.classifier_calls == calls_before
        ok = ok and cached and Mock.last_backend == "cheap-real"
        print("%-2s %-44s %-32s %-14s $%-4s  <- %s" % (
            "5", "(repeat task #1)", "served from cache" if cached else "RE-SCORED (bug!)",
            ROUTED.get(Mock.last_backend, "?"), COST.get(Mock.last_backend, "?"),
            "cache hit: classifier not re-called" if cached else "cache MISS"))

        print("-" * 92)
        print("Classifier was called %d times for 5 requests (caching saved 1)." % Mock.classifier_calls)
        print("RESULT: PASS" if ok else "RESULT: FAIL")
        return 0 if ok else 1
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        srv.shutdown()
        try:
            os.remove(cfg_f)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
