"""Shared test harness for tango_server.

Provides:
  - ServerThread: starts the HTTP server on an ephemeral port; tracks the
    JobManager + Scheduler so tests can poke at internal state.
  - submit(), wait_status(), http(): tiny HTTP helpers.
  - make_git_repo(): creates a temp git repo for tests.
"""
from __future__ import annotations

import contextlib
import http.client as _http_client
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Optional

# Tests always run the server against tests/fake_tango.py.
TESTS_DIR = pathlib.Path(__file__).parent
PROJECT_DIR = TESTS_DIR.parent
TANGO_SERVER = str(PROJECT_DIR / "tango_server.py")
FAKE_TANGO = str(TESTS_DIR / "fake_tango.py")
TANGO_PY = str(PROJECT_DIR / "tango.py")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_git_repo(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True,
    )
    # Initial commit so HEAD is valid.
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(path), check=True)
    return path


class _ServerCtx:
    """Object returned by start_server; acts as a context manager and
    exposes base_url / manager / scheduler for tests that need to poke."""

    def __init__(self, base_url: str, thread: threading.Thread, jobs_dir: pathlib.Path,
                 server_holder: dict):
        self.base_url = base_url
        self.thread = thread
        self.jobs_dir = jobs_dir
        self._server_holder = server_holder
        self._stopped = False

    def __enter__(self) -> "_ServerCtx":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Best-effort: find the running ThreadingHTTPServer in this process
        # and shut it down. The server stores itself on httpd references via
        # the global registry; simpler: we keep a registry in the holder.
        # But because each test starts a server in this process and the server
        # is in a daemon thread, the cleanest path is to look it up via the
        # scheduler object. The harness's run_server doesn't expose httpd
        # after start; we instead just kill the thread by closing all sockets.
        # Since this is a test and the process will exit, the simpler
        # approach: send SIGINT to the thread? Can't; signals are process-wide.
        # Instead: rely on a small internal API: we registered a shutdown
        # callable in server_holder.
        shutdown = self._server_holder.get("shutdown")
        if shutdown:
            try:
                shutdown()
            except Exception:
                pass
        # Give the thread a moment to exit.
        self.thread.join(timeout=2)


def start_server(
    jobs_dir: pathlib.Path,
    *,
    max_concurrent: int = 1,
    allowed_repos: Optional[list[str]] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> "_ServerCtx":
    """Start a tango server in a background thread; return a _ServerCtx.

    Use as `with start_server(...) as ctx:` — _ServerCtx is itself a context
    manager that calls shutdown() on exit.
    """
    import tango_server

    jobs_dir.mkdir(parents=True, exist_ok=True)
    _port = _free_port()

    import types
    args = types.SimpleNamespace(
        host="127.0.0.1",
        port=_port,
        jobs_dir=str(jobs_dir),
        max_concurrent=max_concurrent,
        allowed_repo=allowed_repos,
    )
    base_url = f"http://127.0.0.1:{_port}"
    server_holder: dict = {}
    thread_ref: dict = {}

    def _run() -> None:
        try:
            tango_server.run_server(args, _server_holder=server_holder)
        except SystemExit:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    thread_ref["t"] = t

    # Poll until the port is open.
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", _port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("server did not start")

    return _ServerCtx(base_url, t, jobs_dir, server_holder)


def http(
    method: str,
    url: str,
    body: Any = None,
    *,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict, bytes]:
    parsed = __import__("urllib.parse").parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query
    conn = _http_client.HTTPConnection(host, port, timeout=10)
    h = {"Connection": "close"}
    if headers:
        h.update(headers)
    if body is not None:
        if isinstance(body, (dict, list)):
            h.setdefault("Content-Type", "application/json")
            data = json.dumps(body).encode("utf-8")
        elif isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            raise TypeError(f"unsupported body type: {type(body)}")
    else:
        data = b""
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    hdict = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = None
    return resp.status, payload, raw


# Allow callers to do `http.client.HTTPConnection(...)` for low-level tests
# (e.g. sending a bad Content-Length) without a separate import.
http.client = _http_client


def wait_status(
    url: str,
    job_id: str,
    target: str | tuple[str, ...],
    *,
    timeout: float = 10.0,
    interval: float = 0.05,
) -> dict:
    targets = target if isinstance(target, tuple) else (target,)
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        s, p, _ = http("GET", f"{url}/jobs/{job_id}/status")
        if s == 200 and p is not None:
            last = p
            if last.get("status") in targets:
                return last
        time.sleep(interval)
    raise AssertionError(
        f"job {job_id} did not reach {targets} within {timeout}s; last={last}"
    )


def submit(url: str, body: dict) -> tuple[int, Optional[dict], bytes]:
    return http("POST", f"{url}/jobs", body=body, headers={"Content-Type": "application/json"})


def make_request_body(repo: str, **overrides: Any) -> dict:
    body = {
        "step": "plan",
        "phase": "1",
        "writer": "claude",
        "reviewer": "codex",
        "repo_dir": str(repo),
    }
    body.update(overrides)
    return body
