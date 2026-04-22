"""Integration tests for `serve` process lifetime.

Verifies the hub does not leak as a zombie when its MCP client either
closes stdio cleanly OR dies without closing FDs (the orphan-reparent
case that motivated the watchdog).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _serve_cmd() -> list[str]:
    return ["uv", "run", "--directory", str(REPO_ROOT), "pipecat-context-hub", "serve"]


def _initialize_payload() -> bytes:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    return (json.dumps(msg) + "\n").encode()


def test_stdin_close_exits_cleanly() -> None:
    """Closing stdin must cause `serve` to exit within a few seconds."""
    proc = subprocess.Popen(
        _serve_cmd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(_initialize_payload())
        proc.stdin.flush()
        time.sleep(2.0)  # let initialize round-trip
        proc.stdin.close()
        rc = proc.wait(timeout=10)
        assert rc == 0, f"expected clean exit, got {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="orphan-reparent semantics differ on Windows")
def test_orphaned_serve_exits_via_watchdog(tmp_path: Path) -> None:
    """Orphan `serve` (parent dies without closing stdio) must exit via watchdog.

    Spawns a small Python wrapper that itself spawns `serve` with PIPEs,
    keeps its stdin open, then exits. The serve process is reparented to
    init/launchd. The watchdog should notice within ~3s (interval=0.5s)
    and exit cleanly.
    """
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        textwrap.dedent(
            f"""
            import os, subprocess, sys, time
            env = os.environ.copy()
            env["PIPECAT_HUB_PARENT_WATCH_INTERVAL"] = "0.5"
            proc = subprocess.Popen(
                {_serve_cmd()!r},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            print(proc.pid, flush=True)
            time.sleep(0.5)
            # Exit WITHOUT closing the pipes — orphan the child.
            os._exit(0)
            """
        )
    )

    wrapper_proc = subprocess.run(
        [sys.executable, str(wrapper)],
        capture_output=True,
        timeout=15,
        check=True,
    )
    serve_pid = int(wrapper_proc.stdout.strip())

    # Poll up to 15s for the orphan to exit.
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            os.kill(serve_pid, 0)
        except ProcessLookupError:
            return  # exited as expected
        time.sleep(0.5)

    # Cleanup if still alive — and fail.
    try:
        os.kill(serve_pid, 9)
    except ProcessLookupError:
        pass
    pytest.fail(f"serve PID {serve_pid} still alive 15s after parent died")
