"""tango_server -- local HTTP job server for Tango.

Wraps the existing `tango.py` CLI as subprocesses. Stdlib only.
The server never imports or invokes tango's mutable workflow internals; it
re-uses tango.py via subprocess, sharing only a few pure constants
(AGENT_NAMES, CLAUDE_EFFORTS, CODEX_EFFORTS) for validation and /help.

Sections in this file (in order):
  1. Constants
  2. Request schema (single source of truth for validation + /help)
  3. ID generation
  4. Atomic write helpers
  5. Job dataclass
  6. JobManager
  7. Scheduler
  8. HTTPRequestHandler
  9. run_server entry point
 10. Signal handlers
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import tango
from tango import (
    AGENT_NAMES,
    CLAUDE_EFFORTS,
    CODEX_EFFORTS,
    MAX_ITERS_DEFAULT,
    MAX_ITERS_MIN,
    MAX_ITERS_MAX,
)


# ---------------------------------------------------------------------------
# 1. Constants
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_JOBS_DIR = "~/.tango/jobs"
DEFAULT_MAX_CONCURRENT = 1
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_LOG_LIMIT = 65536
MAX_LOG_LIMIT = 1024 * 1024  # 1 MiB
STOP_GRACE_SECONDS = 10
LOG_TAIL_FOR_ERROR = 2000  # bytes from end of log to include in launch-failure error

JOB_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"

# All known job status values. Used by the recovery logic and /help.
ALL_STATUSES = ("queued", "running", "succeeded", "failed", "stopping", "stopped", "interrupted")
TERMINAL_STATUSES = ("succeeded", "failed", "stopped", "interrupted")
ACTIVE_STATUSES = ("running", "stopping")
RETRYABLE_STATUSES = ("succeeded", "failed", "stopped")

# Server's own resolved path to its entry script (tango.py).
# Tests can override via TANGO_SERVER_CHILD_SCRIPT env var to redirect to a
# fake executable; the server itself only relies on argv[0] being an executable
# Python script. This is test-only — production runs always use tango.py.
TANGO_SCRIPT_PATH = os.environ.get("TANGO_SERVER_CHILD_SCRIPT", str(pathlib.Path(__file__).parent / "tango.py"))


# ---------------------------------------------------------------------------
# 2. Request schema
# ---------------------------------------------------------------------------
# Single source of truth for request validation and the /help document.
# Adding/removing a field here updates both the validator and the help doc.

REQUEST_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "step",
        "type": "string",
        "required": True,
        "choices": ["plan", "implement", "phase"],
    },
    {"name": "phase", "type": "string", "required": True, "min_length": 1},
    {"name": "writer", "type": "string", "required": True, "choices": list(AGENT_NAMES)},
    {"name": "reviewer", "type": "string", "required": True, "choices": list(AGENT_NAMES)},
    {"name": "repo_dir", "type": "string", "required": True},
    {"name": "spec", "type": "string", "required": False, "nullable": True, "default": None},
    {"name": "plan", "type": "string", "required": False, "nullable": True, "default": None},
    {"name": "config", "type": "string", "required": False, "nullable": True, "default": None},
    {"name": "base_sha", "type": "string", "required": False, "nullable": True, "default": None},
    {
        "name": "max_iters",
        "type": "integer",
        "required": False,
        "nullable": True,
        "default": MAX_ITERS_DEFAULT,
        "range": [MAX_ITERS_MIN, MAX_ITERS_MAX],
    },
    {"name": "claude_model", "type": "string", "required": False, "nullable": True, "default": None},
    {"name": "codex_model", "type": "string", "required": False, "nullable": True, "default": None},
    {
        "name": "claude_effort",
        "type": "string",
        "required": False,
        "nullable": True,
        "default": None,
        "choices": list(CLAUDE_EFFORTS),
    },
    {
        "name": "codex_effort",
        "type": "string",
        "required": False,
        "nullable": True,
        "default": None,
        "choices": list(CODEX_EFFORTS),
    },
    {"name": "no_stream", "type": "boolean", "required": False, "default": False},
    {"name": "dry_run", "type": "boolean", "required": False, "default": False},
    {"name": "reset", "type": "boolean", "required": False, "default": False},
    {"name": "resume_claude", "type": "string", "required": False, "nullable": True, "default": None},
    {"name": "resume_codex", "type": "string", "required": False, "nullable": True, "default": None},
]


# ---------------------------------------------------------------------------
# 3. ID generation
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().strftime(ISO_FMT)


def generate_job_id() -> str:
    """Format: job-YYYYMMDD-HHMMSS-<4hex>. Collisions retry via fresh suffix."""
    while True:
        bucket = datetime.datetime.now().strftime("job-%Y%m%d-%H%M%S")
        suffix = secrets.token_hex(2)
        candidate = f"{bucket}-{suffix}"
        if JOB_ID_PATTERN.match(candidate):
            return candidate


def validate_job_id(job_id: str) -> bool:
    return bool(JOB_ID_PATTERN.match(job_id))


# ---------------------------------------------------------------------------
# 4. Atomic write helpers
# ---------------------------------------------------------------------------


def _chmod(path: pathlib.Path, mode: int) -> None:
    """Best-effort chmod; non-POSIX platforms ignore unsupported bits."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def atomic_write_json(path: pathlib.Path, payload: Any) -> None:
    """Write JSON atomically: tempfile + flush + os.replace.

    temp file is in the same directory as `path` so os.replace is atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{secrets.token_hex(4)}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)
    _chmod(path, 0o600)


# ---------------------------------------------------------------------------
# 5. Job dataclass
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    request: dict
    status: dict
    job_dir: pathlib.Path
    log_path: pathlib.Path
    process: Optional[subprocess.Popen] = None
    canonical_repo: str = ""
    created_at: str = field(default_factory=_now_iso)
    state_lock: threading.Lock = field(default_factory=threading.Lock)


# ---------------------------------------------------------------------------
# 6. JobManager
# ---------------------------------------------------------------------------


class JobManager:
    """Owns in-memory job state + atomic on-disk persistence."""

    def __init__(self, jobs_dir: pathlib.Path):
        self.jobs_dir = jobs_dir
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _job_dir(self, job_id: str) -> pathlib.Path:
        return self.jobs_dir / job_id

    def submit(self, request: dict) -> Job:
        """Create a new job, persist it, return it. Caller is the scheduler."""
        job_id = generate_job_id()
        # Resubmit once on the (vanishingly rare) ID collision.
        while (self.jobs_dir / job_id).exists():
            job_id = generate_job_id()
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        _chmod(job_dir, 0o700)
        log_path = job_dir / "output.log"
        # Pre-create log file (spec §13.6 / §16.4).
        log_path.touch(exist_ok=True)
        _chmod(log_path, 0o600)
        status = {
            "id": job_id,
            "status": "queued",
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "pid": None,
            "retry_of": None,
            "error": None,
        }
        atomic_write_json(job_dir / "request.json", request)
        atomic_write_json(job_dir / "status.json", status)
        job = Job(
            id=job_id,
            request=request,
            status=status,
            job_dir=job_dir,
            log_path=log_path,
        )
        with self._lock:
            self.jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self.jobs.get(job_id)

    def save_status(self, job: Job) -> None:
        atomic_write_json(job.job_dir / "status.json", job.status)

    def load_all(self) -> list[Job]:
        """Scan jobs_dir; load every parseable job. Skip malformed with warning."""
        loaded: list[Job] = []
        if not self.jobs_dir.exists():
            return loaded
        for entry in sorted(self.jobs_dir.iterdir()):
            if not entry.is_dir():
                continue
            if not validate_job_id(entry.name):
                print(f"[tango-server] WARNING: skipping job dir with invalid id: {entry.name}", file=sys.stderr)
                continue
            req_path = entry / "request.json"
            stat_path = entry / "status.json"
            if not (req_path.exists() and stat_path.exists()):
                print(f"[tango-server] WARNING: skipping job {entry.name}: missing request/status", file=sys.stderr)
                continue
            try:
                request = json.loads(req_path.read_text())
                status = json.loads(stat_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[tango-server] WARNING: skipping job {entry.name}: {exc}", file=sys.stderr)
                continue
            log_path = entry / "output.log"
            if not log_path.exists():
                log_path.touch(exist_ok=True)
            job = Job(
                id=entry.name,
                request=request,
                status=status,
                job_dir=entry,
                log_path=log_path,
            )
            with self._lock:
                self.jobs[entry.name] = job
            loaded.append(job)
        return loaded


# ---------------------------------------------------------------------------
# 7. Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Single thread that promotes queued jobs when capacity + repo lock allow.

    Globals:
      - self.max_concurrent: cap on concurrent active jobs across all repos
      - self.active_repos:   canonical_repo -> job_id (running or stopping)
      - self.queued:         list of job_ids waiting for capacity/repo
    """

    def __init__(self, jobs_dir: pathlib.Path, max_concurrent: int, manager: JobManager):
        self.jobs_dir = jobs_dir
        self.max_concurrent = max(1, max_concurrent)
        self.manager = manager
        self.queued: list[str] = []
        self.active_repos: dict[str, str] = {}
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake_pending = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tango-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    def enqueue(self, job: Job) -> None:
        with self._cond:
            if job.id not in self.queued and job.id not in self.active_repos.values():
                self.queued.append(job.id)
            self._cond.notify()

    def wakeup(self) -> None:
        with self._cond:
            self._cond.notify()

    def release_repo(self, canonical_repo: str) -> None:
        with self._cond:
            if canonical_repo in self.active_repos:
                del self.active_repos[canonical_repo]
            self._cond.notify()

    def acquire_repo(self, canonical_repo: str, job_id: str) -> bool:
        with self._cond:
            if canonical_repo in self.active_repos:
                return False
            self.active_repos[canonical_repo] = job_id
            return True

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                self._cond.wait(timeout=0.5)
                self._tick()
            # _tick may have started Popen; yield to other threads briefly.

    def _tick(self) -> None:
        # Walk the queue in created_at order; skip entries that can't run.
        if not self.queued:
            return
        active_count = len(self.active_repos)
        new_queue: list[str] = []
        # Snapshot the jobs we will promote so we can start them after releasing
        # the cond lock. (Holding the cond through Popen would block the HTTP
        # thread that wants to enqueue new jobs.)
        to_start: list[Job] = []
        for jid in self.queued:
            if active_count >= self.max_concurrent:
                new_queue.append(jid)
                continue
            job = self.manager.get(jid)
            if job is None:
                continue
            with job.state_lock:
                if job.status.get("status") != "queued":
                    # Job was stopped (or otherwise moved) while sitting in queue.
                    continue
            canonical = job.canonical_repo
            if canonical in self.active_repos:
                new_queue.append(jid)
                continue
            # Promote.
            self.active_repos[canonical] = jid
            active_count += 1
            with job.state_lock:
                job.status["status"] = "running"
                job.status["started_at"] = _now_iso()
            self.manager.save_status(job)
            to_start.append(job)
        # Replace queue (preserving FIFO order for the remainder).
        self.queued = new_queue
        for job in to_start:
            threading.Thread(target=_start_job, args=(job, self), daemon=True).start()


def _start_job(job: Job, scheduler: Scheduler) -> None:
    """Build argv, launch the subprocess, watch it to completion."""
    request = job.request
    argv = [sys.executable, TANGO_SCRIPT_PATH, request["step"]]
    # Always pass repo_dir with the canonical root.
    argv += ["--repo-dir", job.canonical_repo]
    # Phase
    argv += ["--phase", str(request["phase"])]
    if request.get("spec"):
        argv += ["--spec", request["spec"]]
    if request.get("plan"):
        argv += ["--plan", request["plan"]]
    if request.get("config"):
        argv += ["--config", request["config"]]
    if request.get("base_sha"):
        argv += ["--base-sha", request["base_sha"]]
    if request.get("max_iters") is not None and request["max_iters"] != MAX_ITERS_DEFAULT:
        argv += ["--max-iters", str(request["max_iters"])]
    if request.get("claude_model"):
        argv += ["--claude-model", request["claude_model"]]
    if request.get("codex_model"):
        argv += ["--codex-model", request["codex_model"]]
    if request.get("claude_effort"):
        argv += ["--claude-effort", request["claude_effort"]]
    if request.get("codex_effort"):
        argv += ["--codex-effort", request["codex_effort"]]
    if request.get("no_stream"):
        argv += ["--no-stream"]
    if request.get("dry_run"):
        argv += ["--dry-run"]
    if request.get("reset"):
        argv += ["--reset"]
    if request.get("resume_claude"):
        argv += ["--resume-claude", request["resume_claude"]]
    if request.get("resume_codex"):
        argv += ["--resume-codex", request["resume_codex"]]
    argv += ["--writer", request["writer"], "--reviewer", request["reviewer"]]

    # Write the server-generated log header.
    try:
        with open(job.log_path, "ab", buffering=0) as log_file:
            header = (
                f"[tango-server] Job: {job.id}\n"
                f"[tango-server] Started: {_now_iso()}\n"
                f"[tango-server] Command: {' '.join(_redact_argv(argv))}\n"
            )
            log_file.write(header.encode("utf-8"))
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=job.canonical_repo,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={**os.environ, "TANGO_SERVER_JOB_ID": job.id},
                )
            except OSError as exc:
                # Read whatever we wrote to the log so we can include a snippet.
                log_file.flush()
                tail = _read_log_tail(job.log_path, LOG_TAIL_FOR_ERROR)
                msg = f"Unable to start the Tango child process: {exc.__class__.__name__}"
                with job.state_lock:
                    job.status["status"] = "failed"
                    job.status["finished_at"] = _now_iso()
                    job.status["exit_code"] = None
                    job.status["pid"] = None
                    job.status["error"] = {
                        "code": "process_start_failed",
                        "message": msg,
                    }
                job.manager = job.manager  # silence attr checker; manager lives on JobManager
                # Append sanitized error to log so the operator can see it.
                with open(job.log_path, "ab", buffering=0) as lf:
                    lf.write(f"[tango-server] ERROR: {msg}\n".encode("utf-8"))
                    if tail:
                        lf.write(b"[tango-server] Log tail at failure:\n")
                        lf.write(tail)
                # Persist and release.
                job.manager_ref.save_status(job)
                scheduler.release_repo(job.canonical_repo)
                return
            with job.state_lock:
                job.process = proc
                job.status["pid"] = proc.pid
            job.manager_ref.save_status(job)
    except OSError as exc:
        with job.state_lock:
            job.status["status"] = "failed"
            job.status["finished_at"] = _now_iso()
            job.status["error"] = {"code": "process_start_failed", "message": str(exc)}
        job.manager_ref.save_status(job)
        scheduler.release_repo(job.canonical_repo)
        return

    # Wait for the process to complete, but also notice a stop signal.
    _watch_process(job, scheduler, proc)


def _redact_argv(argv: list[str]) -> list[str]:
    """Return a copy of argv with potential secret values redacted.

    Spec §16.4: redaction should be centralized even when no secret fields
    exist yet.
    """
    redacted = []
    redact_next = False
    for tok in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if tok in ("--resume-claude", "--resume-codex"):
            redacted.append(tok)
            redact_next = True
            continue
        redacted.append(tok)
    return redacted


def _read_log_tail(path: pathlib.Path, n: int) -> bytes:
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    with open(path, "rb") as f:
        if size > n:
            f.seek(size - n)
        return f.read()


def _watch_process(job: Job, scheduler: Scheduler, proc: subprocess.Popen) -> None:
    """Block on proc.wait(); on exit, transition to terminal status."""
    try:
        rc = proc.wait()
    except Exception as exc:  # pragma: no cover
        rc = -1
    # Acquire the job lock to decide terminal status atomically.
    with job.state_lock:
        current = job.status.get("status")
        if current == "stopping":
            new_status = "stopped"
        elif current == "running":
            new_status = "succeeded" if rc == 0 else "failed"
        else:
            new_status = current or "failed"
        job.status["status"] = new_status
        job.status["finished_at"] = _now_iso()
        job.status["exit_code"] = rc
        job.status["pid"] = None
    job.manager_ref.save_status(job)
    scheduler.release_repo(job.canonical_repo)


# Attach a back-reference on Job so helpers can save_status.
Job.manager_ref = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Stop helpers
# ---------------------------------------------------------------------------


def _stop_job(job: Job, manager: JobManager, scheduler: Scheduler) -> dict:
    """Idempotent stop. Returns the response payload (status only)."""
    with job.state_lock:
        current = job.status.get("status")
        if current in TERMINAL_STATUSES:
            return {"id": job.id, "status": current}
        if current == "stopping":
            return {"id": job.id, "status": "stopping"}
        if current == "queued":
            # Drop from queue if present.
            if job.id in scheduler.queued:
                scheduler.queued = [j for j in scheduler.queued if j != job.id]
            job.status["status"] = "stopped"
            job.status["finished_at"] = _now_iso()
            job.status["exit_code"] = None
            job.status["pid"] = None
            manager.save_status(job)
            # No repo lock held (never started).
            return {"id": job.id, "status": "stopped"}
        # current == "running"
        proc = job.process
        if proc is None:
            # Transitioning queued -> running; the process hasn't been spawned yet.
            # Should not normally happen; treat as stopping -> stopped via race.
            job.status["status"] = "stopping"
            manager.save_status(job)
            return {"id": job.id, "status": "stopping"}
        job.status["status"] = "stopping"
        manager.save_status(job)
        pid = proc.pid

    # Outside the lock: signal the process group.
    _kill_process_group(pid)

    # Wait for reap with a grace period; if still alive, SIGKILL.
    deadline = time.time() + STOP_GRACE_SECONDS
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        _kill_process_group(pid, force=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    # Finalize status. If completion handler already ran, this is a no-op
    # in effect; we still re-save to ensure status is consistent on disk.
    with job.state_lock:
        if job.status.get("status") == "stopping":
            rc = proc.poll()
            job.status["status"] = "stopped"
            job.status["finished_at"] = _now_iso()
            job.status["exit_code"] = rc
            job.status["pid"] = None
    manager.save_status(job)
    scheduler.release_repo(job.canonical_repo)
    scheduler.wakeup()
    return {"id": job.id, "status": job.status["status"]}


def _kill_process_group(pid: int, force: bool = False) -> None:
    """Send SIGTERM (or SIGKILL) to the process group; fall back to proc.terminate/kill."""
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, sig)
        return
    except (OSError, NotImplementedError):
        pass
    # Fallback: best-effort, no PG.
    try:
        os.kill(pid, sig)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_request(payload: Any) -> tuple[Optional[dict], Optional[tuple[dict, HTTPStatus]]]:
    """Returns (validated_request, None) on success, or (None, (err, status))."""
    if not isinstance(payload, dict):
        return None, ({"error": {"code": "invalid_request", "message": "Request body must be a JSON object."}}, HTTPStatus.BAD_REQUEST)
    known = {f["name"] for f in REQUEST_SCHEMA}
    unknown = set(payload.keys()) - known
    if unknown:
        f = sorted(unknown)[0]
        return None, ({"error": {"code": "unknown_field", "message": f"Unknown request field: {f}.", "details": {"field": f}}}, HTTPStatus.BAD_REQUEST)
    missing = [f["name"] for f in REQUEST_SCHEMA if f.get("required") and f["name"] not in payload]
    if missing:
        f = missing[0]
        return None, ({"error": {"code": "missing_field", "message": f"Missing required field: {f}.", "details": {"field": f}}}, HTTPStatus.BAD_REQUEST)

    out: dict = {}
    for f in REQUEST_SCHEMA:
        name = f["name"]
        if name not in payload:
            out[name] = f.get("default")
            continue
        value = payload[name]
        if value is None:
            if not f.get("nullable", False):
                return None, ({"error": {"code": "invalid_field", "message": f"Field {name} cannot be null.", "details": {"field": name}}}, HTTPStatus.BAD_REQUEST)
            out[name] = None
            continue
        if f["type"] == "string":
            if not isinstance(value, str):
                return None, ({"error": {"code": "invalid_field", "message": f"Field {name} must be a string.", "details": {"field": name}}}, HTTPStatus.BAD_REQUEST)
            if "choices" in f and value not in f["choices"]:
                return None, ({"error": {"code": "invalid_field", "message": f"Field {name} must be one of {f['choices']}.", "details": {"field": name}}}, HTTPStatus.BAD_REQUEST)
            if "min_length" in f and len(value) < f["min_length"]:
                return None, ({"error": {"code": "invalid_field", "message": f"Field {name} must be non-empty.", "details": {"field": name}}}, HTTPStatus.BAD_REQUEST)
            out[name] = value
        elif f["type"] == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return None, ({"error": {"code": "invalid_field", "message": f"Field {name} must be an integer.", "details": {"field": name}}}, HTTPStatus.BAD_REQUEST)
            if "range" in f:
                lo, hi = f["range"]
                if value < lo or value > hi:
                    return None, ({"error": {"code": "invalid_field", "message": f"Field {name} must be in [{lo}, {hi}].", "details": {"field": name, "value": value}}}, HTTPStatus.BAD_REQUEST)
            out[name] = value
        elif f["type"] == "boolean":
            if not isinstance(value, bool):
                return None, ({"error": {"code": "invalid_field", "message": f"Field {name} must be a boolean.", "details": {"field": name}}}, HTTPStatus.BAD_REQUEST)
            out[name] = value
        else:
            out[name] = value
    return out, None


def _resolve_repo(repo_dir: str, allowed: list[str]) -> tuple[Optional[str], Optional[tuple[dict, HTTPStatus]]]:
    """Returns (canonical_root, None) or (None, (err, status))."""
    try:
        resolved = pathlib.Path(repo_dir).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return None, ({"error": {"code": "invalid_path", "message": f"Could not resolve path: {exc}"}}, HTTPStatus.BAD_REQUEST)
    if not resolved.exists():
        return None, ({"error": {"code": "repo_not_found", "message": f"Repository path does not exist: {resolved}."}}, HTTPStatus.BAD_REQUEST)
    if not resolved.is_dir():
        return None, ({"error": {"code": "invalid_path", "message": f"Path is not a directory: {resolved}."}}, HTTPStatus.BAD_REQUEST)
    # Git root resolution.
    try:
        r = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, ({"error": {"code": "internal_error", "message": f"git rev-parse failed: {exc}"}}, HTTPStatus.INTERNAL_SERVER_ERROR)
    if r.returncode != 0:
        return None, ({"error": {"code": "not_a_git_repo", "message": f"Path is not inside a Git repository: {resolved}."}}, HTTPStatus.BAD_REQUEST)
    canonical = r.stdout.strip()
    # Allowlist check.
    if allowed:
        ok = False
        for a in allowed:
            if os.path.commonpath([a, canonical]) == a:
                ok = True
                break
        if not ok:
            return None, ({"error": {"code": "repo_not_allowed", "message": f"Repository {canonical} is not in the allowed list."}}, HTTPStatus.FORBIDDEN)
    return canonical, None


def _validate_base_sha(canonical_repo: str, base_sha: str) -> Optional[tuple[dict, HTTPStatus]]:
    if not base_sha:
        return None
    # Reject anything that smells like a shell fragment.
    if not re.match(r"^[0-9a-fA-F]{4,64}$", base_sha):
        return {"error": {"code": "invalid_git_revision", "message": f"base_sha has invalid characters: {base_sha}."}}, HTTPStatus.BAD_REQUEST
    try:
        r = subprocess.run(
            ["git", "-C", canonical_repo, "rev-parse", "--verify", f"{base_sha}^{{commit}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": {"code": "internal_error", "message": f"git rev-parse failed: {exc}"}}, HTTPStatus.INTERNAL_SERVER_ERROR
    if r.returncode != 0:
        return {"error": {"code": "invalid_git_revision", "message": f"Revision not found in repository: {base_sha}."}}, HTTPStatus.BAD_REQUEST
    return None


def _validate_retry_body(body: Any) -> tuple[Optional[dict], Optional[tuple[dict, HTTPStatus]]]:
    """Retry body is either empty, {}, or {"reset": bool}."""
    if body is None or body == "":
        return {"reset": None}, None
    if not isinstance(body, dict):
        return None, ({"error": {"code": "invalid_request", "message": "Retry body must be a JSON object."}}, HTTPStatus.BAD_REQUEST)
    unknown = set(body.keys()) - {"reset"}
    if unknown:
        f = sorted(unknown)[0]
        return None, ({"error": {"code": "unknown_field", "message": f"Unknown retry field: {f}.", "details": {"field": f}}}, HTTPStatus.BAD_REQUEST)
    reset = body.get("reset")
    if reset is not None and not isinstance(reset, bool):
        return None, ({"error": {"code": "invalid_field", "message": "reset must be a boolean.", "details": {"field": "reset"}}}, HTTPStatus.BAD_REQUEST)
    return {"reset": reset}, None


# ---------------------------------------------------------------------------
# /help document
# ---------------------------------------------------------------------------


def build_help_document(base_url: str) -> dict:
    """Derive /help from REQUEST_SCHEMA and the live server base_url."""
    fields = []
    for f in REQUEST_SCHEMA:
        d: dict = {
            "type": _schema_type_name(f["type"]),
            "required": f.get("required", False),
        }
        if "choices" in f:
            d["values"] = f["choices"]
        if "range" in f:
            d["range"] = f["range"]
        if "default" in f and f.get("default") is not None:
            d["default"] = f["default"]
        if f.get("nullable"):
            d["nullable"] = True
        fields.append({f["name"]: d})

    # Convert to a single nested dict matching the spec example shape.
    field_dict: dict = {}
    for entry in fields:
        field_dict.update(entry)

    return {
        "name": "tango-server",
        "description": (
            "Tango runs an adversarial coding workflow against a local Git repository: a writer agent "
            "(Claude or Codex) drafts a plan or writes code for one phase, and a reviewer agent (the other model) "
            "checks it in a read-only sandbox and returns a machine-parseable verdict; writer and reviewer repeat "
            "until the reviewer approves or max_iters is reached. This server runs that workflow as background jobs "
            "over HTTP instead of a blocking terminal command, so a calling agent can start a job, poll its status "
            "and logs, stop it, or retry it without holding a process open."
        ),
        "workflow_steps": {
            "plan": "Writer drafts an implementation plan for the phase into plans/phase-N.md; reviewer approves or requests changes. Does not write code.",
            "implement": "Writer codes the already-approved plan for the phase and commits; reviewer inspects the diff and approves or requests changes. Requires an approved plan to already exist.",
            "phase": "Runs plan then implement for the phase back to back.",
        },
        "base_url": base_url,
        "endpoints": [
            {
                "method": "POST",
                "path": "/jobs",
                "description": "Start a new Tango workflow job.",
                "request_fields": field_dict,
                "response": "202 Accepted with job id, status, status_url, logs_url",
                "example_request": {
                    "step": "phase",
                    "phase": "3",
                    "writer": "claude",
                    "reviewer": "codex",
                    "repo_dir": "/Users/me/code/project",
                },
            },
            {"method": "GET", "path": "/jobs/{id}", "description": "Get full job status."},
            {"method": "GET", "path": "/jobs/{id}/status", "description": "Alias of GET /jobs/{id}."},
            {
                "method": "GET",
                "path": "/jobs/{id}/logs",
                "description": "Read combined stdout/stderr incrementally.",
                "query_params": {
                    "offset": "byte offset, default 0",
                    "limit": "max bytes, default 65536, max 1048576",
                },
            },
            {"method": "POST", "path": "/jobs/{id}/stop", "description": "Stop a queued or running job."},
            {
                "method": "POST",
                "path": "/jobs/{id}/retry",
                "description": "Retry a failed, stopped, or succeeded job as a new job.",
                "request_fields": {"reset": {"type": "boolean", "required": False}},
            },
            {"method": "GET", "path": "/health", "description": "Liveness check."},
            {"method": "GET", "path": "/help", "description": "This document."},
        ],
        "job_statuses": list(ALL_STATUSES),
        "error_shape": {"error": {"code": "string", "message": "string", "details": {}}},
        "notes": [
            "One active job per canonical Git repository root at a time.",
            "Jobs run as detached subprocess groups; stdin is closed, so interactive prompts fail the job rather than blocking.",
            "Poll /jobs/{id}/logs with next_offset for incremental output.",
        ],
    }


def _schema_type_name(t: str) -> str:
    return {"string": "string", "integer": "integer", "boolean": "boolean"}.get(t, t)


# ---------------------------------------------------------------------------
# HTTPRequestHandler
# ---------------------------------------------------------------------------


class HTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "tango-server/1.0"

    # Suppress default access logging; we keep the request line via stderr sparingly.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature imposed by stdlib
        return

    # Helpers ------------------------------------------------------------

    def _write_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, indent=2, sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _write_error(self, status: int, code: str, message: str, details: Any = None) -> None:
        err: dict = {"error": {"code": code, "message": message}}
        if details is not None:
            err["error"]["details"] = details
        self._write_json(status, err)

    def _read_body(self) -> tuple[Optional[Any], Optional[tuple[dict, HTTPStatus]]]:
        length = self.headers.get("Content-Length")
        if length is None:
            return None, ({"error": {"code": "invalid_request", "message": "Missing Content-Length."}}, HTTPStatus.BAD_REQUEST)
        try:
            n = int(length)
        except ValueError:
            return None, ({"error": {"code": "invalid_request", "message": "Invalid Content-Length."}}, HTTPStatus.BAD_REQUEST)
        if n < 0:
            return None, ({"error": {"code": "invalid_request", "message": "Negative Content-Length."}}, HTTPStatus.BAD_REQUEST)
        if n > MAX_BODY_BYTES:
            # Drain the oversized body so the client doesn't get a connection
            # reset mid-send; we still refuse to buffer it in memory.
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            return None, ({"error": {"code": "payload_too_large", "message": f"Request body exceeds {MAX_BODY_BYTES} bytes."}}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        if n == 0:
            raw = b""
        else:
            try:
                raw = self.rfile.read(n)
            except OSError:
                return None, ({"error": {"code": "invalid_request", "message": "Failed to read request body."}}, HTTPStatus.BAD_REQUEST)
        # Parse JSON.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            return None, ({"error": {"code": "invalid_request", "message": f"Unsupported Content-Type: {ctype}."}}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        if not raw:
            return None, None
        try:
            return json.loads(raw.decode("utf-8")), None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, ({"error": {"code": "invalid_json", "message": f"Malformed JSON: {exc.msg if hasattr(exc, 'msg') else exc}"}}, HTTPStatus.BAD_REQUEST)

    # Routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if path == "/help":
            base_url = f"http://{self.headers.get('Host') or (self.server.server_address[0] + ':' + str(self.server.server_address[1]))}"
            self._write_json(200, build_help_document(base_url))
            return
        if path in ("/jobs", "/jobs/start"):
            self._write_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", f"GET is not supported on {path}; use POST.")
            return
        # /jobs/{id} or /jobs/{id}/status or /jobs/{id}/logs
        m = re.match(r"^/jobs/([a-zA-Z0-9][a-zA-Z0-9_-]{0,127})(/status|/logs)?$", path)
        if not m:
            if path.startswith("/jobs/"):
                self._write_error(HTTPStatus.NOT_FOUND, "job_not_found", f"Unknown path: {path}")
            else:
                self._write_error(HTTPStatus.NOT_FOUND, "invalid_request", f"Unknown path: {path}")
            return
        job_id, suffix = m.group(1), m.group(2)
        manager: JobManager = self.server.manager  # type: ignore[attr-defined]
        job = manager.get(job_id)
        if job is None:
            self._write_error(HTTPStatus.NOT_FOUND, "job_not_found", f"Job '{job_id}' was not found.")
            return
        if suffix == "/logs":
            self._handle_logs(job, parsed.query)
            return
        # Default + /status: return status
        self._write_json(200, job.status)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # /jobs or /jobs/start
        if path in ("/jobs", "/jobs/start"):
            self._handle_submit()
            return
        # /jobs/{id}/stop, /jobs/{id}/retry
        m = re.match(r"^/jobs/([a-zA-Z0-9][a-zA-Z0-9_-]{0,127})/(stop|retry)$", path)
        if not m:
            self._write_error(HTTPStatus.NOT_FOUND, "invalid_request", f"Unknown path: {path}")
            return
        job_id = m.group(1)
        action = m.group(2)
        manager: JobManager = self.server.manager  # type: ignore[attr-defined]
        scheduler: Scheduler = self.server.scheduler  # type: ignore[attr-defined]
        job = manager.get(job_id)
        if job is None:
            self._write_error(HTTPStatus.NOT_FOUND, "job_not_found", f"Job '{job_id}' was not found.")
            return
        if action == "stop":
            self._handle_stop(job, manager, scheduler)
            return
        if action == "retry":
            self._handle_retry(job, manager, scheduler)
            return

    def do_PUT(self) -> None:  # noqa: N802
        self._write_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "PUT is not supported on this path.")

    def do_DELETE(self) -> None:  # noqa: N802
        self._write_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "DELETE is not supported on this path.")

    def do_PATCH(self) -> None:  # noqa: N802
        self._write_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "PATCH is not supported on this path.")

    # Handlers -----------------------------------------------------------

    def _handle_submit(self) -> None:
        # Read body
        body, err = self._read_body()
        if err is not None:
            self._write_json(err[1], err[0])
            return
        # Validate
        request, err = _validate_request(body)
        if err is not None:
            self._write_json(err[1], err[0])
            return
        # Resolve repo
        allowed = self.server.allowed_repos  # type: ignore[attr-defined]
        canonical, err = _resolve_repo(request["repo_dir"], allowed)
        if err is not None:
            self._write_json(err[1], err[0])
            return
        request["repo_dir"] = canonical
        # base_sha
        if request.get("base_sha"):
            err = _validate_base_sha(canonical, request["base_sha"])
            if err is not None:
                self._write_json(err[1], err[0])
                return
        # Submit
        manager: JobManager = self.server.manager  # type: ignore[attr-defined]
        scheduler: Scheduler = self.server.scheduler  # type: ignore[attr-defined]
        job = manager.submit(request)
        job.canonical_repo = canonical
        job.manager_ref = manager  # back-ref for helpers
        scheduler.enqueue(job)
        self._write_json(HTTPStatus.ACCEPTED, {
            "id": job.id,
            "status": job.status["status"],
            "created_at": job.status["created_at"],
            "status_url": f"/jobs/{job.id}/status",
            "logs_url": f"/jobs/{job.id}/logs",
        })

    def _handle_stop(self, job: Job, manager: JobManager, scheduler: Scheduler) -> None:
        # Optional body: ignored if empty/unknown, so just read and discard.
        length = self.headers.get("Content-Length")
        if length and length != "0":
            self._read_body()  # validate; ignore contents
        result = _stop_job(job, manager, scheduler)
        self._write_json(HTTPStatus.OK, result)

    def _handle_retry(self, job: Job, manager: JobManager, scheduler: Scheduler) -> None:
        with job.state_lock:
            current = job.status.get("status")
        if current not in RETRYABLE_STATUSES:
            self._write_error(
                HTTPStatus.CONFLICT,
                "job_not_retryable",
                f"Job in status '{current}' cannot be retried.",
            )
            return
        body, err = self._read_body()
        if err is not None:
            self._write_json(err[1], err[0])
            return
        overrides, err = _validate_retry_body(body)
        if err is not None:
            self._write_json(err[1], err[0])
            return
        new_request = dict(job.request)
        if overrides["reset"] is not None:
            new_request["reset"] = overrides["reset"]
        # No need to revalidate repo: original was validated. But re-derive the
        # canonical repo in case the working tree moved. We use the stored
        # canonical_repo from the original job; it's the resolved form.
        allowed = self.server.allowed_repos  # type: ignore[attr-defined]
        canonical, err = _resolve_repo(job.request["repo_dir"], allowed)
        if err is not None:
            self._write_json(err[1], err[0])
            return
        new_job = manager.submit(new_request)
        new_job.canonical_repo = canonical
        new_job.status["retry_of"] = job.id
        new_job.manager_ref = manager
        manager.save_status(new_job)
        scheduler.enqueue(new_job)
        self._write_json(HTTPStatus.ACCEPTED, {
            "id": new_job.id,
            "status": new_job.status["status"],
            "retry_of": job.id,
            "status_url": f"/jobs/{new_job.id}/status",
            "logs_url": f"/jobs/{new_job.id}/logs",
        })

    def _handle_logs(self, job: Job, query: str) -> None:
        params = urllib.parse.parse_qs(query) if query else {}
        offset_str = (params.get("offset") or ["0"])[0]
        limit_str = (params.get("limit") or [str(DEFAULT_LOG_LIMIT)])[0]
        try:
            offset = int(offset_str)
            limit = int(limit_str)
        except ValueError:
            self._write_error(HTTPStatus.BAD_REQUEST, "invalid_request", "offset/limit must be integers.")
            return
        if offset < 0:
            self._write_error(HTTPStatus.BAD_REQUEST, "invalid_request", "offset must be >= 0.")
            return
        if limit <= 0 or limit > MAX_LOG_LIMIT:
            self._write_error(HTTPStatus.BAD_REQUEST, "invalid_request", f"limit must be in [1, {MAX_LOG_LIMIT}].")
            return
        try:
            size = job.log_path.stat().st_size
        except OSError:
            self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Log file is unavailable.")
            return
        # Beyond EOF: empty content, next_offset = size.
        if offset >= size:
            self._write_json(200, {
                "id": job.id,
                "offset": offset,
                "next_offset": size,
                "complete": job.status.get("status") in TERMINAL_STATUSES and size <= offset,
                "content": "",
            })
            return
        with open(job.log_path, "rb") as f:
            f.seek(offset)
            data = f.read(limit)
        next_offset = offset + len(data)
        complete = (job.status.get("status") in TERMINAL_STATUSES) and (next_offset >= size)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            content = data.decode("utf-8", errors="replace")
        self._write_json(200, {
            "id": job.id,
            "offset": offset,
            "next_offset": next_offset,
            "complete": complete,
            "content": content,
        })


# ---------------------------------------------------------------------------
# 9. run_server
# ---------------------------------------------------------------------------


def run_server(args: argparse.Namespace, _server_holder: dict = None) -> None:
    host = args.host or DEFAULT_HOST
    port = args.port if args.port is not None else DEFAULT_PORT
    if not (1 <= port <= 65535):
        print(f"[tango-server] invalid port: {port}", file=sys.stderr)
        sys.exit(2)
    jobs_dir = pathlib.Path(args.jobs_dir or DEFAULT_JOBS_DIR).expanduser().resolve()
    if not jobs_dir.exists():
        try:
            jobs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[tango-server] cannot create jobs dir {jobs_dir}: {exc}", file=sys.stderr)
            sys.exit(2)
    _chmod(jobs_dir, 0o700)
    max_concurrent = args.max_concurrent if args.max_concurrent is not None else DEFAULT_MAX_CONCURRENT
    if max_concurrent < 1:
        print(f"[tango-server] invalid --max-concurrent: {max_concurrent}", file=sys.stderr)
        sys.exit(2)
    # Resolve allowed repos at startup.
    allowed_repos: list[str] = []
    if args.allowed_repo:
        for r in args.allowed_repo:
            try:
                allowed_repos.append(str(pathlib.Path(r).expanduser().resolve()))
            except (OSError, RuntimeError) as exc:
                print(f"[tango-server] invalid --allowed-repo {r}: {exc}", file=sys.stderr)
                sys.exit(2)
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(f"[tango-server] WARNING: binding to {host} -- this API can launch coding agents with repository write access.", file=sys.stderr)

    manager = JobManager(jobs_dir)
    loaded = manager.load_all()
    print(f"[tango-server] jobs dir: {jobs_dir}", file=sys.stderr)
    print(f"[tango-server] loaded {len(loaded)} persisted job(s)", file=sys.stderr)

    scheduler = Scheduler(jobs_dir, max_concurrent, manager)
    # Tag every loaded job with manager back-ref and apply recovery.
    now = _now_iso()
    queued_resume: list[Job] = []
    for job in loaded:
        job.manager_ref = manager
        with job.state_lock:
            current = job.status.get("status")
            if current in ("running", "stopping"):
                job.status["status"] = "interrupted"
                job.status["finished_at"] = now
                job.status["pid"] = None
                job.status["exit_code"] = None
                if not job.status.get("error"):
                    job.status["error"] = {
                        "code": "server_restart",
                        "message": "The server restarted while this job was running.",
                    }
                manager.save_status(job)
            elif current == "queued":
                queued_resume.append(job)
            # else: terminal, leave as-is.

    # Resolve canonical_repo for every loaded job (we need it for scheduling
    # AND for retrying). If a path can't be resolved, the job stays in its
    # current terminal state; scheduling is skipped.
    for job in loaded:
        if job.status.get("status") == "interrupted":
            # No need to resolve repo for a terminal job; skip.
            continue
        repo_dir = job.request.get("repo_dir")
        if not repo_dir:
            continue
        canonical, err = _resolve_repo(repo_dir, allowed_repos)
        if err is not None:
            print(f"[tango-server] could not resolve repo for job {job.id}; leaving as-is", file=sys.stderr)
            continue
        job.canonical_repo = canonical
        # Re-validate allowlist post-load: jobs in allowed-only mode might
        # no longer be allowed.
    for job in queued_resume:
        if not job.canonical_repo:
            continue
        scheduler.enqueue(job)

    # Patch Job class with manager back-ref.
    Job.manager_ref = manager  # type: ignore[attr-defined]

    # Build the HTTP server.
    try:
        httpd = ThreadingHTTPServer((host, port), HTTPRequestHandler)
    except OSError as exc:
        print(f"[tango-server] cannot bind {host}:{port}: {exc}", file=sys.stderr)
        sys.exit(2)
    httpd.manager = manager  # type: ignore[attr-defined]
    httpd.scheduler = scheduler  # type: ignore[attr-defined]
    httpd.allowed_repos = allowed_repos  # type: ignore[attr-defined]
    httpd.jobs_dir = jobs_dir  # type: ignore[attr-defined]

    # Test hook: caller can supply a mutable dict; we register a shutdown
    # callable that terminates serve_forever() and tears down child jobs.
    if _server_holder is not None:
        def _test_shutdown() -> None:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        _server_holder["shutdown"] = _test_shutdown
        _server_holder["httpd"] = httpd
        _server_holder["manager"] = manager
        _server_holder["scheduler"] = scheduler

    # Shutdown coordination.
    shutdown_state = {"shutting_down": False, "second_signal": False}
    scheduler.start()
    print(f"[tango-server] listening on http://{host}:{port}", file=sys.stderr)

    def _shutdown(signum: int, frame: Any) -> None:  # noqa: ARG001
        if shutdown_state["shutting_down"]:
            shutdown_state["second_signal"] = True
            # Force-exit ASAP.
            os._exit(1)
        shutdown_state["shutting_down"] = True
        # server.shutdown must be called from another thread.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        httpd.serve_forever()
    finally:
        # Best-effort graceful shutdown of all active jobs.
        print("[tango-server] shutting down -- terminating active jobs", file=sys.stderr)
        scheduler.stop()
        with manager._lock:
            active = [j for j in manager.jobs.values() if j.status.get("status") in ACTIVE_STATUSES]
        for j in active:
            try:
                _stop_job(j, manager, scheduler)
            except Exception as exc:  # pragma: no cover
                print(f"[tango-server] shutdown error on {j.id}: {exc}", file=sys.stderr)
        httpd.server_close()
        print("[tango-server] bye", file=sys.stderr)


if __name__ == "__main__":
    # Allow running the server directly for testing.
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--host", default=DEFAULT_HOST)
    _p.add_argument("--port", type=int, default=DEFAULT_PORT)
    _p.add_argument("--jobs-dir", default=DEFAULT_JOBS_DIR)
    _p.add_argument("--max-concurrent", type=int, default=DEFAULT_MAX_CONCURRENT)
    _p.add_argument("--allowed-repo", action="append", default=None)
    run_server(_p.parse_args())
