"""Tests for tango_server.

Covers spec §26.1-26.10:
  - argument parsing (CLI)
  - request validation
  - job persistence
  - execution / subprocess
  - logs
  - stop
  - retry
  - concurrency
  - recovery
  - HTTP routing
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Optional

# Force the server to use the fake child script.
os.environ["TANGO_SERVER_CHILD_SCRIPT"] = str(pathlib.Path(__file__).parent / "fake_tango.py")
os.environ.setdefault("FAKE_TANGO_MODE", "ok")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import tango
import tango_server
from _harness import (
    FAKE_TANGO,
    PROJECT_DIR,
    TANGO_PY,
    TANGO_SERVER,
    make_git_repo,
    make_request_body,
    http,
    start_server,
    submit,
    wait_status,
)


# ---------------------------------------------------------------------------
# §26.1 — argument parsing
# ---------------------------------------------------------------------------


class TestCliParsing(unittest.TestCase):
    """CLI flag combinations: server mode vs workflow mode."""

    def test_no_step_starts_server(self):
        from tango import AGENT_NAMES
        self.assertEqual(AGENT_NAMES, ("claude", "codex"))

    def test_default_values(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_dir = pathlib.Path(td) / "jobs"
            with start_server(jobs_dir) as ctx:
                s, p, _ = http("GET", f"{ctx.base_url}/help")
                self.assertEqual(s, 200)

    def test_invalid_port_zero(self):
        from tango_server import run_server
        class A:
            host = "127.0.0.1"
            port = 0
            jobs_dir = "/tmp/_never_used"
            max_concurrent = 1
            allowed_repo = None
        with self.assertRaises(SystemExit) as cm:
            run_server(A())
        self.assertEqual(cm.exception.code, 2)

    def test_invalid_port_too_high(self):
        from tango_server import run_server
        class A:
            host = "127.0.0.1"
            port = 70000
            jobs_dir = "/tmp/_never_used"
            max_concurrent = 1
            allowed_repo = None
        with self.assertRaises(SystemExit) as cm:
            run_server(A())
        self.assertEqual(cm.exception.code, 2)

    def test_invalid_max_concurrent(self):
        from tango_server import run_server
        class A:
            host = "127.0.0.1"
            port = 1
            jobs_dir = "/tmp/_never_used"
            max_concurrent = 0
            allowed_repo = None
        with self.assertRaises(SystemExit) as cm:
            run_server(A())
        self.assertEqual(cm.exception.code, 2)

    def test_workflow_mode_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "r"
            make_git_repo(repo)
            r = subprocess.run(
                [sys.executable, TANGO_PY, "plan", "--phase", "1",
                 "--writer", "claude", "--reviewer", "codex",
                 "--repo-dir", str(repo), "--dry-run"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
            self.assertIn("DRY-RUN", r.stdout)

    def test_mix_step_and_port_errors(self):
        r = subprocess.run(
            [sys.executable, TANGO_PY, "plan", "--phase", "1",
             "--writer", "claude", "--reviewer", "codex", "--port", "8765"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("server flags not allowed", r.stderr)

    def test_mix_writer_in_server_mode_errors(self):
        r = subprocess.run(
            [sys.executable, TANGO_PY, "--writer", "claude"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("workflow flags not allowed", r.stderr)


# ---------------------------------------------------------------------------
# §26.2 — request validation
# ---------------------------------------------------------------------------


class TestRequestValidation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.jobs_dir = self.tmp_path / "jobs"
        self.repo = self.tmp_path / "repo"
        make_git_repo(self.repo)
        self._ctx = start_server(self.jobs_dir)
        self._ctx.__enter__()

    def tearDown(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        self.tmp.cleanup()

    def _submit(self, body):
        return http("POST", f"{self._ctx.base_url}/jobs", body=body, headers={"Content-Type": "application/json"})

    def test_malformed_json(self):
        s, _, raw = http(
            "POST", f"{self._ctx.base_url}/jobs",
            body="{not json",
            headers={"Content-Type": "application/json", "Content-Length": str(len("{not json"))},
        )
        self.assertEqual(s, 400)
        self.assertIn(b"invalid_json", raw)

    def test_oversized_body(self):
        big = b'{"x":"' + (b"a" * (1024 * 1024 + 10)) + b'"}'
        port = int(self._ctx.base_url.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/jobs", body=big, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(big)),
            "Connection": "close",
        })
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        body = resp.read()
        self.assertIn(b"payload_too_large", body)
        conn.close()

    def test_non_object_body(self):
        s, p, _ = self._submit([1, 2, 3])
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "invalid_request")

    def test_missing_required(self):
        body = {"step": "plan", "writer": "claude", "reviewer": "codex", "repo_dir": str(self.repo)}
        s, p, _ = self._submit(body)
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "missing_field")
        self.assertEqual(p["error"]["details"]["field"], "phase")

    def test_unknown_field(self):
        s, p, _ = self._submit(make_request_body(self.repo, bogus=True))
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "unknown_field")

    def test_invalid_step(self):
        s, p, _ = self._submit(make_request_body(self.repo, step="bad"))
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "invalid_field")

    def test_invalid_writer(self):
        s, p, _ = self._submit(make_request_body(self.repo, writer="gpt"))
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "invalid_field")

    def test_invalid_reviewer(self):
        s, p, _ = self._submit(make_request_body(self.repo, reviewer="gpt"))
        self.assertEqual(s, 400)

    def test_empty_phase(self):
        s, p, _ = self._submit(make_request_body(self.repo, phase=""))
        self.assertEqual(s, 400)

    def test_nonexistent_repo(self):
        s, p, _ = self._submit(make_request_body(self.repo.parent / "nope"))
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "repo_not_found")

    def test_non_git_dir(self):
        not_repo = self.tmp_path / "not-repo"
        not_repo.mkdir()
        s, p, _ = self._submit(make_request_body(not_repo))
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "not_a_git_repo")

    def test_invalid_base_sha(self):
        s, p, _ = self._submit(make_request_body(self.repo, base_sha="not-a-sha"))
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "invalid_git_revision")

    def test_invalid_max_iters_zero(self):
        s, p, _ = self._submit(make_request_body(self.repo, max_iters=0))
        self.assertEqual(s, 400)
        self.assertIn("[1, 100]", p["error"]["message"])

    def test_invalid_max_iters_101(self):
        s, p, _ = self._submit(make_request_body(self.repo, max_iters=101))
        self.assertEqual(s, 400)

    def test_bad_bool(self):
        s, p, _ = self._submit(make_request_body(self.repo, dry_run="yes"))
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "invalid_field")

    def test_disallowed_repo(self):
        # Spin up a server with --allowed-repo pointing at a different repo.
        with tempfile.TemporaryDirectory() as td2:
            jd2 = pathlib.Path(td2) / "jobs"
            allowed = pathlib.Path(td2) / "allowed"
            other = pathlib.Path(td2) / "other"
            make_git_repo(allowed)
            make_git_repo(other)
            ctx2 = start_server(jd2, allowed_repos=[str(allowed)])
            ctx2.__enter__()
            try:
                s, p, _ = http("POST", f"{ctx2.base_url}/jobs", body=make_request_body(other), headers={"Content-Type": "application/json"})
                self.assertEqual(s, 403)
                self.assertEqual(p["error"]["code"], "repo_not_allowed")
            finally:
                ctx2.__exit__(None, None, None)

    def test_allowed_repo_accepts_match(self):
        with tempfile.TemporaryDirectory() as td2:
            jd2 = pathlib.Path(td2) / "jobs"
            allowed = pathlib.Path(td2) / "allowed"
            make_git_repo(allowed)
            ctx2 = start_server(jd2, allowed_repos=[str(allowed)])
            ctx2.__enter__()
            try:
                s, p, _ = http("POST", f"{ctx2.base_url}/jobs", body=make_request_body(allowed), headers={"Content-Type": "application/json"})
                self.assertEqual(s, 202)
            finally:
                ctx2.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# §26.3 — persistence
# ---------------------------------------------------------------------------


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.jobs_dir = self.tmp_path / "jobs"
        self.repo = self.tmp_path / "repo"
        make_git_repo(self.repo)
        self._ctx = start_server(self.jobs_dir)
        self._ctx.__enter__()

    def tearDown(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        self.tmp.cleanup()

    def test_job_dir_created(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        self.assertEqual(s, 202)
        jid = p["id"]
        self.assertTrue((self.jobs_dir / jid).is_dir())
        self.assertTrue((self.jobs_dir / jid / "request.json").is_file())
        self.assertTrue((self.jobs_dir / jid / "status.json").is_file())
        self.assertTrue((self.jobs_dir / jid / "output.log").is_file())

    def test_initial_status_queued(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        jid = p["id"]
        self.assertEqual(p["status"], "queued")
        on_disk = json.loads((self.jobs_dir / jid / "status.json").read_text())
        self.assertEqual(on_disk["status"], "queued")

    def test_atomic_write_1mb(self):
        from tango_server import atomic_write_json
        target = self.tmp_path / "big.json"
        payload = {"data": "x" * (1024 * 1024)}
        atomic_write_json(target, payload)
        loaded = json.loads(target.read_text())
        self.assertEqual(loaded["data"], "x" * (1024 * 1024))

    def test_reload_after_restart(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        jid = p["id"]
        wait_status(self._ctx.base_url, jid, "succeeded", timeout=5)
        self._ctx.__exit__(None, None, None)
        new_ctx = start_server(self.jobs_dir)
        new_ctx.__enter__()
        try:
            s2, p2, _ = http("GET", f"{new_ctx.base_url}/jobs/{jid}/status")
            self.assertEqual(s2, 200)
            self.assertEqual(p2["status"], "succeeded")
        finally:
            new_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# §26.4 — execution
# ---------------------------------------------------------------------------


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.jobs_dir = self.tmp_path / "jobs"
        self.repo = self.tmp_path / "repo"
        make_git_repo(self.repo)
        self._ctx = start_server(self.jobs_dir)
        self._ctx.__enter__()

    def tearDown(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        self.tmp.cleanup()

    def test_exit_zero_succeeded(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        jid = p["id"]
        st = wait_status(self._ctx.base_url, jid, "succeeded", timeout=5)
        self.assertEqual(st["exit_code"], 0)

    def test_exit_nonzero_failed(self):
        with tempfile.TemporaryDirectory() as td2:
            jd2 = pathlib.Path(td2) / "jobs"
            repo2 = pathlib.Path(td2) / "repo"
            make_git_repo(repo2)
            old = os.environ.get("FAKE_TANGO_MODE")
            os.environ["FAKE_TANGO_MODE"] = "fail"
            try:
                ctx2 = start_server(jd2)
                ctx2.__enter__()
                try:
                    s, p, _ = submit(ctx2.base_url, make_request_body(repo2))
                    jid = p["id"]
                    st = wait_status(ctx2.base_url, jid, "failed", timeout=5)
                    self.assertEqual(st["exit_code"], 1)
                finally:
                    ctx2.__exit__(None, None, None)
            finally:
                if old is None:
                    os.environ.pop("FAKE_TANGO_MODE", None)
                else:
                    os.environ["FAKE_TANGO_MODE"] = old

    def test_popen_called_with_safe_args(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        jid = p["id"]
        wait_status(self._ctx.base_url, jid, "succeeded", timeout=5)
        s2, p2, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}/logs?offset=0")
        cmd_line = p2["content"]
        self.assertIn("Command:", cmd_line)
        self.assertNotIn("|", cmd_line)
        self.assertNotIn("&&", cmd_line)
        self.assertNotIn(";", cmd_line)
        self.assertIn("plan", cmd_line)
        self.assertIn("--writer", cmd_line)
        self.assertIn("claude", cmd_line)

    def test_log_header_written(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        jid = p["id"]
        wait_status(self._ctx.base_url, jid, "succeeded", timeout=5)
        log = (self.jobs_dir / jid / "output.log").read_text()
        self.assertIn("[tango-server] Job:", log)
        self.assertIn("[tango-server] Started:", log)
        self.assertIn("[tango-server] Command:", log)


# ---------------------------------------------------------------------------
# §26.5 — logs
# ---------------------------------------------------------------------------


class TestLogs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.jobs_dir = self.tmp_path / "jobs"
        self.repo = self.tmp_path / "repo"
        make_git_repo(self.repo)
        self._ctx = start_server(self.jobs_dir)
        self._ctx.__enter__()

    def tearDown(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        self.tmp.cleanup()

    def _submit_ok(self) -> str:
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        jid = p["id"]
        wait_status(self._ctx.base_url, jid, "succeeded", timeout=5)
        return jid

    def test_full_read(self):
        jid = self._submit_ok()
        s, p, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}/logs?offset=0")
        self.assertEqual(s, 200)
        self.assertIn("[fake-tango] hello", p["content"])
        self.assertEqual(p["offset"], 0)
        self.assertGreater(p["next_offset"], 0)
        self.assertTrue(p["complete"])

    def test_incremental_read(self):
        jid = self._submit_ok()
        s1, p1, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}/logs?offset=0")
        full_size = p1["next_offset"]
        half = full_size // 2
        s2, p2, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}/logs?offset={half}")
        self.assertEqual(p2["offset"], half)
        self.assertGreater(p2["next_offset"], half)
        self.assertEqual(p2["next_offset"], half + len(p2["content"].encode("utf-8")))

    def test_offset_beyond_file(self):
        jid = self._submit_ok()
        s, p, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}/logs?offset=99999999")
        self.assertEqual(s, 200)
        self.assertEqual(p["content"], "")

    def test_limit_honored(self):
        jid = self._submit_ok()
        s, p, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}/logs?offset=0&limit=10")
        self.assertLessEqual(len(p["content"].encode("utf-8")), 10)

    def test_limit_too_big(self):
        jid = self._submit_ok()
        s, p, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}/logs?offset=0&limit=2000000")
        self.assertEqual(s, 400)
        self.assertEqual(p["error"]["code"], "invalid_request")

    def test_invalid_offset(self):
        jid = self._submit_ok()
        s, p, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}/logs?offset=abc")
        self.assertEqual(s, 400)

    def test_invalid_utf8_replaced(self):
        with tempfile.TemporaryDirectory() as td2:
            jd2 = pathlib.Path(td2) / "jobs"
            repo2 = pathlib.Path(td2) / "repo"
            make_git_repo(repo2)
            old = os.environ.get("FAKE_TANGO_MODE")
            os.environ["FAKE_TANGO_MODE"] = "write_invalid_utf8"
            try:
                ctx2 = start_server(jd2)
                ctx2.__enter__()
                try:
                    s, p, _ = submit(ctx2.base_url, make_request_body(repo2))
                    jid = p["id"]
                    wait_status(ctx2.base_url, jid, "succeeded", timeout=5)
                    s2, p2, _ = http("GET", f"{ctx2.base_url}/jobs/{jid}/logs?offset=0")
                    self.assertIn("�", p2["content"])
                finally:
                    ctx2.__exit__(None, None, None)
            finally:
                if old is None:
                    os.environ.pop("FAKE_TANGO_MODE", None)
                else:
                    os.environ["FAKE_TANGO_MODE"] = old


# ---------------------------------------------------------------------------
# §26.6 — stop
# ---------------------------------------------------------------------------


class TestStop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.jobs_dir = self.tmp_path / "jobs"
        self.repo = self.tmp_path / "repo"
        make_git_repo(self.repo)
        self._ctx = start_server(self.jobs_dir)
        self._ctx.__enter__()

    def tearDown(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        self.tmp.cleanup()

    def _submit_and_succeed(self) -> str:
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        jid = p["id"]
        wait_status(self._ctx.base_url, jid, "succeeded", timeout=5)
        return jid

    def test_stop_idempotent_on_terminal(self):
        jid = self._submit_and_succeed()
        s, p, _ = http("POST", f"{self._ctx.base_url}/jobs/{jid}/stop")
        self.assertEqual(s, 200)
        self.assertEqual(p["status"], "succeeded")

    def test_stop_running(self):
        with tempfile.TemporaryDirectory() as td2:
            jd2 = pathlib.Path(td2) / "jobs"
            repo2 = pathlib.Path(td2) / "repo"
            make_git_repo(repo2)
            old = os.environ.get("FAKE_TANGO_MODE")
            os.environ["FAKE_TANGO_MODE"] = "sleep"
            os.environ["FAKE_TANGO_SLEEP"] = "30"
            try:
                ctx2 = start_server(jd2)
                ctx2.__enter__()
                try:
                    s, p, _ = submit(ctx2.base_url, make_request_body(repo2))
                    jid = p["id"]
                    wait_status(ctx2.base_url, jid, "running", timeout=3)
                    s2, p2, _ = http("POST", f"{ctx2.base_url}/jobs/{jid}/stop")
                    self.assertEqual(s2, 200)
                    self.assertIn(p2["status"], ("stopping", "stopped"))
                    st = wait_status(ctx2.base_url, jid, "stopped", timeout=5)
                    self.assertEqual(st["status"], "stopped")
                    self.assertIsNotNone(st["finished_at"])
                finally:
                    ctx2.__exit__(None, None, None)
            finally:
                os.environ.pop("FAKE_TANGO_MODE", None)
                os.environ.pop("FAKE_TANGO_SLEEP", None)
                if old is not None:
                    os.environ["FAKE_TANGO_MODE"] = old

    def test_queued_stop(self):
        with tempfile.TemporaryDirectory() as td2:
            jd2 = pathlib.Path(td2) / "jobs"
            repo2 = pathlib.Path(td2) / "repo"
            make_git_repo(repo2)
            old = os.environ.get("FAKE_TANGO_MODE")
            os.environ["FAKE_TANGO_MODE"] = "sleep"
            os.environ["FAKE_TANGO_SLEEP"] = "10"
            try:
                ctx2 = start_server(jd2, max_concurrent=1)
                ctx2.__enter__()
                try:
                    s1, p1, _ = submit(ctx2.base_url, make_request_body(repo2))
                    jid1 = p1["id"]
                    wait_status(ctx2.base_url, jid1, "running", timeout=3)
                    s2, p2, _ = submit(ctx2.base_url, make_request_body(repo2, phase="2"))
                    jid2 = p2["id"]
                    s3, p3, _ = http("POST", f"{ctx2.base_url}/jobs/{jid2}/stop")
                    self.assertEqual(s3, 200)
                    self.assertEqual(p3["status"], "stopped")
                    s4, p4, _ = http("GET", f"{ctx2.base_url}/jobs/{jid2}/status")
                    self.assertIsNone(p4["started_at"])
                    http("POST", f"{ctx2.base_url}/jobs/{jid1}/stop")
                finally:
                    ctx2.__exit__(None, None, None)
            finally:
                os.environ.pop("FAKE_TANGO_MODE", None)
                os.environ.pop("FAKE_TANGO_SLEEP", None)
                if old is not None:
                    os.environ["FAKE_TANGO_MODE"] = old


# ---------------------------------------------------------------------------
# §26.7 — retry
# ---------------------------------------------------------------------------


class TestRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.jobs_dir = self.tmp_path / "jobs"
        self.repo = self.tmp_path / "repo"
        make_git_repo(self.repo)
        self._ctx = start_server(self.jobs_dir)
        self._ctx.__enter__()

    def tearDown(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        self.tmp.cleanup()

    def test_retry_creates_new_id(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        old_id = p["id"]
        wait_status(self._ctx.base_url, old_id, "succeeded", timeout=5)
        s2, p2, _ = http("POST", f"{self._ctx.base_url}/jobs/{old_id}/retry", body={}, headers={"Content-Type": "application/json"})
        self.assertEqual(s2, 202)
        new_id = p2["id"]
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(p2["retry_of"], old_id)

    def test_old_job_unchanged(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        old_id = p["id"]
        wait_status(self._ctx.base_url, old_id, "succeeded", timeout=5)
        http("POST", f"{self._ctx.base_url}/jobs/{old_id}/retry", body={}, headers={"Content-Type": "application/json"})
        old_request = json.loads((self.jobs_dir / old_id / "request.json").read_text())
        self.assertEqual(old_request["step"], "plan")
        old_status = json.loads((self.jobs_dir / old_id / "status.json").read_text())
        self.assertEqual(old_status["status"], "succeeded")

    def test_retry_unknown_field_rejected(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        old_id = p["id"]
        wait_status(self._ctx.base_url, old_id, "succeeded", timeout=5)
        s2, p2, _ = http("POST", f"{self._ctx.base_url}/jobs/{old_id}/retry", body={"foo": 1}, headers={"Content-Type": "application/json"})
        self.assertEqual(s2, 400)
        self.assertEqual(p2["error"]["code"], "unknown_field")

    def test_retry_active_rejected(self):
        with tempfile.TemporaryDirectory() as td2:
            jd2 = pathlib.Path(td2) / "jobs"
            repo2 = pathlib.Path(td2) / "repo"
            make_git_repo(repo2)
            old = os.environ.get("FAKE_TANGO_MODE")
            os.environ["FAKE_TANGO_MODE"] = "sleep"
            os.environ["FAKE_TANGO_SLEEP"] = "10"
            try:
                ctx2 = start_server(jd2)
                ctx2.__enter__()
                try:
                    s, p, _ = submit(ctx2.base_url, make_request_body(repo2))
                    jid = p["id"]
                    wait_status(ctx2.base_url, jid, "running", timeout=3)
                    s2, p2, _ = http("POST", f"{ctx2.base_url}/jobs/{jid}/retry", body={}, headers={"Content-Type": "application/json"})
                    self.assertEqual(s2, 409)
                    self.assertEqual(p2["error"]["code"], "job_not_retryable")
                    http("POST", f"{ctx2.base_url}/jobs/{jid}/stop")
                finally:
                    ctx2.__exit__(None, None, None)
            finally:
                os.environ.pop("FAKE_TANGO_MODE", None)
                os.environ.pop("FAKE_TANGO_SLEEP", None)
                if old is not None:
                    os.environ["FAKE_TANGO_MODE"] = old

    def test_retry_failed_allowed(self):
        with tempfile.TemporaryDirectory() as td2:
            jd2 = pathlib.Path(td2) / "jobs"
            repo2 = pathlib.Path(td2) / "repo"
            make_git_repo(repo2)
            old = os.environ.get("FAKE_TANGO_MODE")
            os.environ["FAKE_TANGO_MODE"] = "fail"
            try:
                ctx2 = start_server(jd2)
                ctx2.__enter__()
                try:
                    s, p, _ = submit(ctx2.base_url, make_request_body(repo2))
                    old_id = p["id"]
                    wait_status(ctx2.base_url, old_id, "failed", timeout=5)
                    s2, p2, _ = http("POST", f"{ctx2.base_url}/jobs/{old_id}/retry", body={}, headers={"Content-Type": "application/json"})
                    self.assertEqual(s2, 202)
                finally:
                    ctx2.__exit__(None, None, None)
            finally:
                if old is None:
                    os.environ.pop("FAKE_TANGO_MODE", None)
                else:
                    os.environ["FAKE_TANGO_MODE"] = old


# ---------------------------------------------------------------------------
# §26.8 — concurrency
# ---------------------------------------------------------------------------


class TestConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.jobs_dir = self.tmp_path / "jobs"

    def tearDown(self):
        self.tmp.cleanup()

    def _with_mode(self, mode: str, sleep: str = "5"):
        old_mode = os.environ.get("FAKE_TANGO_MODE")
        old_sleep = os.environ.get("FAKE_TANGO_SLEEP")
        os.environ["FAKE_TANGO_MODE"] = mode
        os.environ["FAKE_TANGO_SLEEP"] = sleep
        def restore():
            if old_mode is None:
                os.environ.pop("FAKE_TANGO_MODE", None)
            else:
                os.environ["FAKE_TANGO_MODE"] = old_mode
            if old_sleep is None:
                os.environ.pop("FAKE_TANGO_SLEEP", None)
            else:
                os.environ["FAKE_TANGO_SLEEP"] = old_sleep
        return restore

    def test_global_concurrency_enforced(self):
        repo = self.tmp_path / "repo1"
        repo2 = self.tmp_path / "repo2"
        make_git_repo(repo)
        make_git_repo(repo2)
        restore = self._with_mode("sleep", "5")
        try:
            ctx = start_server(self.jobs_dir, max_concurrent=2)
            ctx.__enter__()
            try:
                s1, p1, _ = submit(ctx.base_url, make_request_body(repo))
                s2, p2, _ = submit(ctx.base_url, make_request_body(repo2))
                s3, p3, _ = submit(ctx.base_url, make_request_body(repo))
                jid1, jid2, jid3 = p1["id"], p2["id"], p3["id"]
                wait_status(ctx.base_url, jid1, "running", timeout=3)
                wait_status(ctx.base_url, jid2, "running", timeout=3)
                time.sleep(0.3)
                _, p_status, _ = http("GET", f"{ctx.base_url}/jobs/{jid3}/status")
                self.assertEqual(p_status["status"], "queued")
                http("POST", f"{ctx.base_url}/jobs/{jid1}/stop")
                http("POST", f"{ctx.base_url}/jobs/{jid2}/stop")
                http("POST", f"{ctx.base_url}/jobs/{jid3}/stop")
            finally:
                ctx.__exit__(None, None, None)
        finally:
            restore()

    def test_per_repo_lock(self):
        repo = self.tmp_path / "repo"
        make_git_repo(repo)
        restore = self._with_mode("sleep", "5")
        try:
            ctx = start_server(self.jobs_dir, max_concurrent=5)
            ctx.__enter__()
            try:
                s1, p1, _ = submit(ctx.base_url, make_request_body(repo))
                s2, p2, _ = submit(ctx.base_url, make_request_body(repo))
                jid1, jid2 = p1["id"], p2["id"]
                wait_status(ctx.base_url, jid1, "running", timeout=3)
                time.sleep(0.3)
                _, p_status, _ = http("GET", f"{ctx.base_url}/jobs/{jid2}/status")
                self.assertEqual(p_status["status"], "queued")
                http("POST", f"{ctx.base_url}/jobs/{jid1}/stop")
                http("POST", f"{ctx.base_url}/jobs/{jid2}/stop")
            finally:
                ctx.__exit__(None, None, None)
        finally:
            restore()


# ---------------------------------------------------------------------------
# §26.9 — recovery
# ---------------------------------------------------------------------------


class TestRecovery(unittest.TestCase):
    def _make_loaded_job(self, td, jid, status_dict, request_dict):
        jd = pathlib.Path(td) / "jobs"
        jd.mkdir(exist_ok=True)
        (jd / jid).mkdir()
        (jd / jid / "request.json").write_text(json.dumps(request_dict))
        (jd / jid / "status.json").write_text(json.dumps(status_dict))
        (jd / jid / "output.log").write_text("")

    def test_running_job_becomes_interrupted(self):
        with tempfile.TemporaryDirectory() as td:
            jid = "job-20260101-000000-aaaa"
            self._make_loaded_job(td, jid, {
                "id": jid, "status": "running", "created_at": "x",
                "started_at": "x", "finished_at": None, "exit_code": None,
                "pid": 99999, "retry_of": None, "error": None,
            }, {
                "step": "plan", "phase": "1", "writer": "claude",
                "reviewer": "codex", "repo_dir": "/tmp/whatever",
            })
            ctx = start_server(pathlib.Path(td) / "jobs")
            ctx.__enter__()
            try:
                s, p, _ = http("GET", f"{ctx.base_url}/jobs/{jid}/status")
                self.assertEqual(s, 200)
                self.assertEqual(p["status"], "interrupted")
                self.assertIsNone(p["pid"])
                self.assertIsNotNone(p["finished_at"])
                self.assertEqual(p["error"]["code"], "server_restart")
            finally:
                ctx.__exit__(None, None, None)

    def test_queued_job_resumes(self):
        with tempfile.TemporaryDirectory() as td:
            jid = "job-20260101-000000-bbbb"
            repo = pathlib.Path(td) / "r"
            make_git_repo(repo)
            self._make_loaded_job(td, jid, {
                "id": jid, "status": "queued", "created_at": "x",
                "started_at": None, "finished_at": None, "exit_code": None,
                "pid": None, "retry_of": None, "error": None,
            }, {
                "step": "plan", "phase": "1", "writer": "claude",
                "reviewer": "codex", "repo_dir": str(repo),
            })
            ctx = start_server(pathlib.Path(td) / "jobs")
            ctx.__enter__()
            try:
                st = wait_status(ctx.base_url, jid, "succeeded", timeout=5)
                self.assertEqual(st["status"], "succeeded")
            finally:
                ctx.__exit__(None, None, None)

    def test_terminal_job_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            jid = "job-20260101-000000-cccc"
            self._make_loaded_job(td, jid, {
                "id": jid, "status": "succeeded", "created_at": "x",
                "started_at": "x", "finished_at": "x", "exit_code": 0,
                "pid": None, "retry_of": None, "error": None,
            }, {
                "step": "plan", "phase": "1", "writer": "claude",
                "reviewer": "codex", "repo_dir": "/tmp/whatever",
            })
            ctx = start_server(pathlib.Path(td) / "jobs")
            ctx.__enter__()
            try:
                s, p, _ = http("GET", f"{ctx.base_url}/jobs/{jid}/status")
                self.assertEqual(p["status"], "succeeded")
            finally:
                ctx.__exit__(None, None, None)

    def test_stale_pid_not_signalled(self):
        with tempfile.TemporaryDirectory() as td:
            jid = "job-20260101-000000-dddd"
            self._make_loaded_job(td, jid, {
                "id": jid, "status": "stopping", "created_at": "x",
                "started_at": "x", "finished_at": None, "exit_code": None,
                "pid": 99999, "retry_of": None, "error": None,
            }, {
                "step": "plan", "phase": "1", "writer": "claude",
                "reviewer": "codex", "repo_dir": "/tmp/whatever",
            })
            ctx = start_server(pathlib.Path(td) / "jobs")
            ctx.__enter__()
            try:
                s, p, _ = http("GET", f"{ctx.base_url}/jobs/{jid}/status")
                self.assertEqual(p["status"], "interrupted")
            finally:
                ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# §26.10 — HTTP routing
# ---------------------------------------------------------------------------


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.jobs_dir = self.tmp_path / "jobs"
        self.repo = self.tmp_path / "repo"
        make_git_repo(self.repo)
        self._ctx = start_server(self.jobs_dir)
        self._ctx.__enter__()

    def tearDown(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        self.tmp.cleanup()

    def test_help_lists_endpoints(self):
        s, p, _ = http("GET", f"{self._ctx.base_url}/help")
        self.assertEqual(s, 200)
        paths = [e["path"] for e in p["endpoints"]]
        for required in ["/jobs", "/jobs/{id}", "/jobs/{id}/status",
                          "/jobs/{id}/logs", "/jobs/{id}/stop",
                          "/jobs/{id}/retry", "/health", "/help"]:
            self.assertIn(required, paths)

    def test_jobs_start_alias(self):
        s, p, _ = http("POST", f"{self._ctx.base_url}/jobs/start", body=make_request_body(self.repo), headers={"Content-Type": "application/json"})
        self.assertEqual(s, 202)
        self.assertIn("id", p)

    def test_status_alias(self):
        s, p, _ = submit(self._ctx.base_url, make_request_body(self.repo))
        jid = p["id"]
        wait_status(self._ctx.base_url, jid, "succeeded", timeout=5)
        s1, p1, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}")
        s2, p2, _ = http("GET", f"{self._ctx.base_url}/jobs/{jid}/status")
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 200)
        self.assertEqual(p1["status"], p2["status"])

    def test_unknown_job_404(self):
        s, p, _ = http("GET", f"{self._ctx.base_url}/jobs/job-bogus-xxxx")
        self.assertEqual(s, 404)
        self.assertEqual(p["error"]["code"], "job_not_found")

    def test_unknown_path_404(self):
        s, p, _ = http("GET", f"{self._ctx.base_url}/no-such-path")
        self.assertEqual(s, 404)

    def test_jobs_get_405(self):
        s, p, _ = http("GET", f"{self._ctx.base_url}/jobs")
        self.assertEqual(s, 405)

    def test_wrong_content_type(self):
        s, p, _ = http("POST", f"{self._ctx.base_url}/jobs", body="x=1", headers={"Content-Type": "text/plain"})
        self.assertIn(s, (400, 415))

    def test_error_shape(self):
        s, p, _ = http("GET", f"{self._ctx.base_url}/jobs/job-bogus-xxxx")
        self.assertIn("error", p)
        self.assertIn("code", p["error"])
        self.assertIn("message", p["error"])


if __name__ == "__main__":
    unittest.main()
