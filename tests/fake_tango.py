#!/usr/bin/env python3
"""Fake Tango entry script for tests.

Behavior is controlled by env vars (set by the test harness via argv):
  FAKE_TANGO_MODE = ok | fail | sleep | spawn | ignore_term | write_bytes:N | write_invalid_utf8
  FAKE_TANGO_SLEEP = seconds (for sleep mode)
  FAKE_TANGO_CHILD = "1" (for spawn mode, fork a long-sleeping child)
  FAKE_TANGO_BYTES = N (for write_bytes mode, emit N bytes then exit 0)
  FAKE_TANGO_NO_TERMINAL = "1" (for ignore_term mode, ignore SIGTERM)
  FAKE_TANGO_EXIT_AFTER = seconds (for sleep mode, override sleep duration)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def _read_int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def main() -> int:
    mode = os.environ.get("FAKE_TANGO_MODE", "ok")
    if mode == "ok":
        print("[fake-tango] hello")
        return 0
    if mode == "fail":
        print("[fake-tango] simulating failure", file=sys.stderr)
        return 1
    if mode == "sleep":
        secs = _read_int("FAKE_TANGO_SLEEP", 2)
        for i in range(secs):
            print(f"[fake-tango] tick {i+1}/{secs}", flush=True)
            time.sleep(1)
        return 0
    if mode == "spawn":
        # Spawn a long-sleeping child and wait briefly so the parent is
        # "running" and the child outlives the parent in a predictable way.
        secs = _read_int("FAKE_TANGO_SLEEP", 30)
        try:
            subprocess.Popen(
                [sys.executable, __file__],
                env={**os.environ, "FAKE_TANGO_MODE": "sleep", "FAKE_TANGO_SLEEP": str(secs)},
                stdin=subprocess.DEVNULL,
                stdout=open(os.devnull, "wb"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            pass
        time.sleep(2)
        return 0
    if mode == "ignore_term":
        secs = _read_int("FAKE_TANGO_SLEEP", 60)
        if os.environ.get("FAKE_TANGO_NO_TERMINAL") == "1":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for i in range(secs):
            print(f"[fake-tango] ignored {i+1}", flush=True)
            time.sleep(1)
        return 0
    if mode.startswith("write_bytes:"):
        n = int(mode.split(":", 1)[1])
        sys.stdout.buffer.write(b"x" * n)
        sys.stdout.flush()
        return 0
    if mode == "write_invalid_utf8":
        sys.stdout.buffer.write(b"\xff\xfe\xfd good \xff")
        sys.stdout.flush()
        return 0
    print(f"[fake-tango] unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
