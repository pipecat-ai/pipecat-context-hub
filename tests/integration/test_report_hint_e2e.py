"""End-to-end wire-level checks for the CLI/MCP self-report guidance.

`tests/unit/test_server.py::TestServerInstructions` pins that
`create_server` is *called* with `_SERVER_INSTRUCTIONS` (via
`inspect.getsource`), and `tests/unit/test_cli_query.py` pins the CLI's
stderr hints against a mocked handler response. Neither exercises a real
process: a refactor could leave the source-level wiring intact while
breaking delivery on the wire (e.g. an MCP SDK upgrade that renames the
`instructions=` kwarg, or a CLI code path that writes the hint to stdout
instead of stderr). These tests catch that class of regression by running
the real `serve` subprocess over stdio and the real CLI subprocess against
a real (deliberately empty) index — no mocks.

Kept in `tests/integration/` (not `tests/smoke/`, which is reserved for
offline fixture-tree layout invariants — see `tests/smoke/README.md`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipecat_context_hub.shared.support_links import (
    BUG_REPORT_ISSUE_URL,
    RETRIEVAL_QUALITY_ISSUE_URL,
)
from tests.integration.test_serve_lifetime import (
    _drain_stderr,
    _env_with_home,
    _initialize_payload,
    _readline_with_timeout,
    _serve_cmd,
    seeded_home,
)

__all__ = ["seeded_home"]  # re-exported fixture; referenced only by pytest


def test_mcp_initialize_delivers_report_hint_instructions(seeded_home: Path) -> None:
    """A real MCP client must receive both report-hint URLs on `initialize`.

    Sends a raw `initialize` JSON-RPC request over stdio (same helper the
    lifetime tests use) and parses `result.instructions` from the reply —
    the actual bytes an agent connecting over MCP would see, not the
    `_SERVER_INSTRUCTIONS` constant in isolation.
    """
    proc = subprocess.Popen(
        _serve_cmd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env_with_home(seeded_home),
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(_initialize_payload())
        proc.stdin.flush()
        try:
            line = _readline_with_timeout(proc.stdout, 45.0)
        except TimeoutError:
            stderr = _drain_stderr(proc)
            pytest.fail(f"serve did not respond to initialize within 45s. stderr tail:\n{stderr}")
        assert line, "serve closed stdout before responding to initialize"

        response = json.loads(line)
        instructions = response["result"]["instructions"]

        assert RETRIEVAL_QUALITY_ISSUE_URL in instructions
        assert BUG_REPORT_ISSUE_URL in instructions
        assert "not_cached" in instructions
        assert "load_failed" in instructions
        # The exclusion is the point of the requirement — config_disabled
        # must be *named* (as the thing NOT to report) rather than the
        # word simply being absent, which would also pass a substring
        # check for the wrong reason.
        assert "config_disabled" in instructions

        proc.stdin.close()
        rc = proc.wait(timeout=10)
        assert rc == 0, f"expected clean exit, got {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_cli_empty_index_delivers_bug_report_hint_on_stderr(tmp_path: Path) -> None:
    """A real CLI subprocess against a genuinely empty index must exit 2
    and carry the bug-report hint on stderr, with stdout left empty.

    Unlike the unit-side `TestIndexUnready` tests (which patch the index
    open call), this spawns the real `pipecat-context-hub status` binary
    against an on-disk empty `$HOME`, so a regression that moved the hint
    to stdout, changed the exit code, or dropped the URL text at the CLI
    entry point would be caught even if the underlying handler-level unit
    test still passed.
    """
    home = tmp_path / "empty_home"
    home.mkdir()
    result = subprocess.run(
        ["uv", "run", "pipecat-context-hub", "status"],
        capture_output=True,
        text=True,
        env=_env_with_home(home),
        timeout=30,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert BUG_REPORT_ISSUE_URL in result.stderr
    assert "refresh" in result.stderr
