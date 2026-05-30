"""Unit tests for the stdio transport's parent-death + idle watchdogs.

Env-var resolution is tested in tests/unit/test_config.py since the
ServerConfig computed properties own that logic post-refactor.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, cast
from unittest.mock import patch

import pytest

from pipecat_context_hub.server import transport
from pipecat_context_hub.shared.tracking import IdleTracker


class TestWatchParent:
    @pytest.mark.asyncio
    async def test_returns_when_ppid_changes(self) -> None:
        """Simulate parent death by mocking getppid to return a different PID."""
        original = 12345
        with patch.object(os, "getppid", return_value=99999):
            result = await asyncio.wait_for(
                transport._watch_parent(original, interval=0.01),
                timeout=1.0,
            )
        assert "parent_died" in result
        assert "original_ppid=12345" in result
        assert "current_ppid=99999" in result

    @pytest.mark.asyncio
    async def test_polls_while_ppid_stable(self) -> None:
        """Watchdog must not return as long as PPID is stable; cancellable."""
        original = os.getppid()
        task = asyncio.create_task(transport._watch_parent(original, interval=0.01))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_fires_on_grandparent_death_under_uv_run(self) -> None:
        """The `uv run` case the old watchdog missed: getppid() stays
        stable (uv lingers) but the watched grandparent (real client)
        dies. _watch_parent must fire `client_died`.
        """
        original = os.getppid()  # stable — uv stays alive
        with patch.object(os, "kill", side_effect=ProcessLookupError):
            result = await asyncio.wait_for(
                transport._watch_parent(original, interval=0.01, client_pid=4242),
                timeout=1.0,
            )
        assert "client_died" in result
        assert "client_pid=4242" in result

    @pytest.mark.asyncio
    async def test_does_not_fire_while_grandparent_alive(self) -> None:
        """With a live grandparent and a stable ppid, the watchdog stays
        quiet — it must not reap a hub during an active session.
        """
        original = os.getppid()
        # os.kill(pid, 0) succeeds → grandparent alive.
        task = asyncio.create_task(
            transport._watch_parent(original, interval=0.01, client_pid=os.getpid())
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestResolveWatchPlan:
    """`resolve_watch_plan` decides what process death proves the client
    is gone. Mocks `_inspect_process` so no real `ps` call is needed.
    """

    def test_direct_parent_is_client(self) -> None:
        # Parent is a normal client (not an intermediate launcher).
        with patch.object(transport, "_inspect_process", return_value=(500, "node")):
            client_pid, reliable = transport.resolve_watch_plan(1234)
        assert client_pid is None
        assert reliable is True

    def test_uv_parent_resolves_grandparent(self) -> None:
        # Parent is `uv`; grandparent (the real client) is watched.
        with patch.object(transport, "_inspect_process", return_value=(777, "uv")):
            client_pid, reliable = transport.resolve_watch_plan(1234)
        assert client_pid == 777
        assert reliable is True

    def test_uvx_parent_resolves_grandparent(self) -> None:
        with patch.object(transport, "_inspect_process", return_value=(888, "uvx")):
            client_pid, reliable = transport.resolve_watch_plan(1234)
        assert client_pid == 888
        assert reliable is True

    def test_uv_parent_unresolved_grandparent_is_unreliable(self) -> None:
        # `ps` failed to give us the grandparent → cannot watch it.
        with patch.object(transport, "_inspect_process", return_value=(None, "uv")):
            client_pid, reliable = transport.resolve_watch_plan(1234)
        assert client_pid is None
        assert reliable is False

    def test_uv_parent_grandparent_is_init_is_unreliable(self) -> None:
        # Grandparent already reparented to PID 1 → nothing meaningful to watch.
        with patch.object(transport, "_inspect_process", return_value=(1, "uv")):
            client_pid, reliable = transport.resolve_watch_plan(1234)
        assert client_pid is None
        assert reliable is False

    def test_ps_failure_treated_as_direct(self) -> None:
        # Unknown parent (ps unavailable) → treat as direct; parent-death covers it.
        with patch.object(transport, "_inspect_process", return_value=(None, None)):
            client_pid, reliable = transport.resolve_watch_plan(1234)
        assert client_pid is None
        assert reliable is True


class TestPidAlive:
    def test_live_process(self) -> None:
        assert transport._pid_alive(os.getpid()) is True

    def test_dead_process(self) -> None:
        with patch.object(os, "kill", side_effect=ProcessLookupError):
            assert transport._pid_alive(424242) is False

    def test_permission_error_means_alive(self) -> None:
        # Process exists but is owned by another user.
        with patch.object(os, "kill", side_effect=PermissionError):
            assert transport._pid_alive(1) is True


class TestRunAtexitBounded:
    def test_returns_within_budget_even_if_handler_blocks(self) -> None:
        """A blocking atexit handler must not let `_run_atexit_bounded`
        exceed its budget — otherwise it could re-defeat the watchdog.

        We stub `atexit._run_exitfuncs` with a blocking fake rather than
        registering a real handler: the real function would fire every
        atexit handler registered in the pytest process (closing fds,
        tearing down logging/multiprocessing) and corrupt the run.
        """
        import atexit
        import time

        def _blocking_run_exitfuncs() -> None:
            time.sleep(5.0)

        with patch.object(atexit, "_run_exitfuncs", _blocking_run_exitfuncs):
            start = time.monotonic()
            transport._run_atexit_bounded(0.2)
            elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"bounded atexit took too long: {elapsed:.2f}s"

    def test_runs_registered_handlers(self) -> None:
        """The bounded wrapper actually invokes atexit handling (stubbed)."""
        import atexit

        calls: list[str] = []

        def _fake_run_exitfuncs() -> None:
            calls.append("ran")

        with patch.object(atexit, "_run_exitfuncs", _fake_run_exitfuncs):
            transport._run_atexit_bounded(1.0)
        assert calls == ["ran"]


class TestIdleTracker:
    def test_starts_at_zero_seconds_idle(self) -> None:
        t = IdleTracker()
        assert t.seconds_since_last() < 0.5  # essentially zero

    def test_touch_resets_clock(self) -> None:
        import time as _time

        t = IdleTracker()
        _time.sleep(0.05)
        assert t.seconds_since_last() >= 0.05
        t.touch()
        assert t.seconds_since_last() < 0.05

    def test_begin_marks_tracker_active_regardless_of_clock(self) -> None:
        """In-flight calls must keep seconds_since_last at 0 — otherwise a
        slow handler (e.g. cold EmbeddingService load) would be reaped
        by the idle watchdog mid-response.
        """
        import time as _time

        t = IdleTracker()
        t.begin()
        # Force the tracker to "look" stale; with an active call, the
        # consumer must still see 0.
        t._last = _time.monotonic() - 100.0
        assert t.seconds_since_last() == 0.0
        t.end()
        # After end(), the clock is fresh again (end() touches).
        assert t.seconds_since_last() < 0.05

    def test_nested_begin_requires_matching_ends(self) -> None:
        t = IdleTracker()
        t.begin()
        t.begin()
        t._last = 0.0  # simulate stale clock
        assert t.seconds_since_last() == 0.0
        t.end()
        # One call still active.
        assert t.seconds_since_last() == 0.0
        t.end()
        # All calls finished — end() touched the clock, so we're fresh.
        assert t.seconds_since_last() < 0.05

    def test_end_without_begin_is_safe(self) -> None:
        """Defensive: stray end() must not underflow or raise."""
        t = IdleTracker()
        t.end()
        assert t._active == 0


class TestWatchIdle:
    @pytest.mark.asyncio
    async def test_returns_when_timeout_exceeded(self) -> None:
        t = IdleTracker()
        # Force tracker to "look" stale by reaching in directly — avoids
        # sleeping the test for the full timeout window.
        import time as _time

        t._last = _time.monotonic() - 100.0
        result = await asyncio.wait_for(
            transport._watch_idle(t, timeout=10.0, interval=0.01),
            timeout=1.0,
        )
        assert "idle_timeout" in result
        assert "timeout_seconds=10" in result

    @pytest.mark.asyncio
    async def test_does_not_return_while_active(self) -> None:
        t = IdleTracker()
        task = asyncio.create_task(transport._watch_idle(t, timeout=10.0, interval=0.01))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_in_flight_call_suppresses_idle_fire(self) -> None:
        """With `begin()` active and the clock forced stale, the idle
        watchdog must NOT fire — this is the P2 regression guard.
        """
        import time as _time

        t = IdleTracker()
        t.begin()
        t._last = _time.monotonic() - 100.0  # would normally fire
        task = asyncio.create_task(transport._watch_idle(t, timeout=10.0, interval=0.01))
        await asyncio.sleep(0.1)
        assert not task.done(), "idle watchdog fired during an in-flight call"
        # Ending the call resets the clock, so the watchdog remains quiet.
        t.end()
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.skipif(sys.platform == "win32", reason="watchdog disabled on win32")
class TestRunStdioWatchdogWiring:
    """Verify run_stdio exits when its parent disappears, by stubbing the
    stdio_server context and the server.run coroutine to a long-sleep.

    The watchdog should fire and cancel the long-sleep before the test
    timeout. This exercises the wiring without touching real subprocesses.
    """

    @pytest.mark.asyncio
    async def test_watchdog_cancels_server_task(self) -> None:
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[None, None]]:
            yield (None, None)

        with patch.object(transport, "stdio_server", fake_stdio_server):

            class FakeServer:
                def create_initialization_options(self) -> object:
                    return object()

                async def run(self, *_args: object, **_kwargs: object) -> None:
                    await asyncio.sleep(60)

            # Flip getppid to a different value after the first poll fires.
            ppid_calls = {"n": 0}
            real_ppid = os.getppid()

            def flipping_ppid() -> int:
                ppid_calls["n"] += 1
                return real_ppid if ppid_calls["n"] <= 1 else 1

            with patch.object(os, "getppid", side_effect=flipping_ppid):
                await asyncio.wait_for(
                    transport.run_stdio(
                        cast(Any, FakeServer()),
                        original_ppid=real_ppid,
                        parent_watch_interval_secs=0.02,
                    ),
                    timeout=5.0,
                )

    @pytest.mark.asyncio
    async def test_grandparent_death_cancels_server_task(self) -> None:
        """End-to-end `uv run` wiring: ppid stays stable (uv lingers) but
        the watched grandparent dies; run_stdio must cancel the server
        task and unwind. This is the path the idle-timeout workaround
        used to cover.
        """
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[None, None]]:
            yield (None, None)

        with patch.object(transport, "stdio_server", fake_stdio_server):

            class FakeServer:
                def create_initialization_options(self) -> object:
                    return object()

                async def run(self, *_args: object, **_kwargs: object) -> None:
                    await asyncio.sleep(60)

            real_ppid = os.getppid()
            # ppid never flips (uv stays alive); the grandparent is dead.
            with (
                patch.object(os, "getppid", return_value=real_ppid),
                patch.object(os, "kill", side_effect=ProcessLookupError),
            ):
                result = await asyncio.wait_for(
                    transport.run_stdio(
                        cast(Any, FakeServer()),
                        original_ppid=real_ppid,
                        parent_watch_interval_secs=0.02,
                        client_watch_pid=4242,
                    ),
                    timeout=5.0,
                )
        assert result is not None and result.startswith("client_died")

    @pytest.mark.asyncio
    async def test_graceful_shutdown_disarms_hard_exit_timer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a clean watchdog-triggered unwind, `os._exit` must not
        fire from the backstop thread — otherwise the test runner (or
        any in-process host) would be killed 2.5s later.
        """
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[None, None]]:
            yield (None, None)

        exit_calls: list[int] = []
        monkeypatch.setattr(os, "_exit", exit_calls.append)
        # Don't actually close pytest's stdin FD (this test runs
        # in-process and would otherwise fight the runner).
        monkeypatch.setattr(os, "close", lambda _fd: None)

        with patch.object(transport, "stdio_server", fake_stdio_server):

            class FakeServer:
                def create_initialization_options(self) -> object:
                    return object()

                async def run(self, *_args: object, **_kwargs: object) -> None:
                    await asyncio.sleep(60)

            ppid_calls = {"n": 0}
            real_ppid = os.getppid()

            def flipping_ppid() -> int:
                ppid_calls["n"] += 1
                return real_ppid if ppid_calls["n"] <= 1 else 1

            with patch.object(os, "getppid", side_effect=flipping_ppid):
                await asyncio.wait_for(
                    transport.run_stdio(
                        cast(Any, FakeServer()),
                        original_ppid=real_ppid,
                        parent_watch_interval_secs=0.02,
                    ),
                    timeout=5.0,
                )
        # Wait past the 2.5s backstop window; graceful_done.set() should
        # have disarmed the timer so os._exit stays uncalled.
        await asyncio.sleep(3.0)
        assert exit_calls == [], f"hard-exit timer fired after graceful shutdown: {exit_calls}"

    @pytest.mark.asyncio
    async def test_safe_mode_does_not_close_stdin_or_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`exit_on_watchdog_shutdown=False` must not close stdin, must
        not arm the hard-exit timer, and must not call `os._exit`.

        In-process callers own `sys.stdin` and the host process — the
        safe-mode flag is their guarantee that `run_stdio` has no
        host-side effects beyond cancelling its own asyncio tasks and
        invoking the shutdown callback. This is the P3 regression guard.
        """
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[None, None]]:
            yield (None, None)

        exit_calls: list[int] = []
        close_calls: list[int] = []
        monkeypatch.setattr(os, "_exit", exit_calls.append)
        monkeypatch.setattr(os, "close", lambda fd: close_calls.append(fd))

        shutdown_cb_calls: list[str] = []

        def on_shutdown() -> None:
            shutdown_cb_calls.append("called")

        with patch.object(transport, "stdio_server", fake_stdio_server):

            class FakeServer:
                def create_initialization_options(self) -> object:
                    return object()

                async def run(self, *_args: object, **_kwargs: object) -> None:
                    await asyncio.sleep(60)

            ppid_calls = {"n": 0}
            real_ppid = os.getppid()

            def flipping_ppid() -> int:
                ppid_calls["n"] += 1
                return real_ppid if ppid_calls["n"] <= 1 else 1

            with patch.object(os, "getppid", side_effect=flipping_ppid):
                result = await asyncio.wait_for(
                    transport.run_stdio(
                        cast(Any, FakeServer()),
                        original_ppid=real_ppid,
                        parent_watch_interval_secs=0.02,
                        on_watchdog_shutdown=on_shutdown,
                        exit_on_watchdog_shutdown=False,
                    ),
                    timeout=5.0,
                )

        # Wait past the hard-exit window just in case a stray timer
        # survived refactoring.
        await asyncio.sleep(3.0)

        assert result is not None, "run_stdio should surface shutdown_reason in safe mode"
        assert result.startswith("parent_died"), f"unexpected reason: {result}"
        assert exit_calls == [], f"os._exit should not fire in safe mode: {exit_calls}"
        assert close_calls == [], f"os.close(stdin) should not fire in safe mode: {close_calls}"
        assert shutdown_cb_calls == ["called"], (
            f"shutdown callback must still run once in safe mode: {shutdown_cb_calls}"
        )
