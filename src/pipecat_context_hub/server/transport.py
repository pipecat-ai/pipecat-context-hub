"""stdio transport adapter for the MCP server."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import subprocess  # nosec B404 - used only for a fixed-arg, timeout-guarded `ps` probe
import sys
import threading
from typing import Callable

from mcp import stdio_server
from mcp.server.lowlevel import Server

from pipecat_context_hub.shared.tracking import IdleTracker

logger = logging.getLogger(__name__)

# Idle-watchdog poll cap. The actual poll interval is min(this, max(timeout/4, 1.0))
# so very short timeouts (used in tests) still poll frequently enough.
_IDLE_POLL_INTERVAL_SECS = 30.0

# Hard-exit timer budget — how long to wait after graceful_done.set()
# would normally fire before giving up and calling os._exit(0). Covers
# the worst-case asyncio-task-unwind + Chroma close on Linux.
_HARD_EXIT_TIMEOUT_SECS = 2.5

# Per-callback budget inside the hard-exit path. If on_watchdog_shutdown
# hangs here (e.g. Chroma close wedged), we abandon it rather than
# defeating the watchdog — on-disk state is crash-consistent.
_SHUTDOWN_CB_TIMEOUT_SECS = 1.0

# Budget for running atexit handlers before a hard os._exit(0). Lets
# loky/multiprocessing release their resource-tracker semaphores (so the
# spurious "leaked semaphore" warning does not print) without letting a
# blocking handler defeat the watchdog's "client gone, die" guarantee.
_ATEXIT_CLEANUP_TIMEOUT_SECS = 1.0

# Direct-parent process names that are *intermediate launchers* rather
# than the real MCP client. When the hub is started as
# `uv run pipecat-context-hub serve`, `uv` stays alive as the hub's
# parent, so os.getppid() never flips when the real client (the
# grandparent) dies — the parent-death watchdog cannot fire. For these,
# we watch the grandparent PID directly instead. Compared by basename.
_INTERMEDIATE_LAUNCHERS = frozenset({"uv", "uvx"})


def _inspect_process(pid: int) -> tuple[int | None, str | None]:
    """Best-effort ``(ppid, command-basename)`` for ``pid`` via ``ps``.

    One fixed-argument, timeout-guarded ``ps`` call. Returns
    ``(None, None)`` on any failure (ps missing, non-zero exit, parse
    error) so callers degrade to the idle-timeout fallback rather than
    crash. Not used on Windows (callers gate on ``sys.platform``).
    """
    try:
        out = subprocess.run(  # nosec B603 B607 - fixed args, no shell, trusted PATH
            ["ps", "-p", str(pid), "-o", "ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception:
        return (None, None)
    line = out.stdout.strip()
    if not line:
        return (None, None)
    parts = line.split(None, 1)
    ppid: int | None = None
    comm: str | None = None
    try:
        ppid = int(parts[0])
    except (ValueError, IndexError):
        ppid = None
    if len(parts) > 1 and parts[1].strip():
        comm = os.path.basename(parts[1].strip())
    return (ppid, comm)


def resolve_watch_plan(parent_pid: int) -> tuple[int | None, bool]:
    """Decide what process death proves the client is gone.

    Returns ``(client_watch_pid, death_detection_reliable)``:

    - **Direct launch** (parent is the real client): ``(None, True)`` —
      the existing parent-death watchdog (``os.getppid()`` flips to 1)
      covers orphan cleanup; no extra PID to watch.
    - **Intermediate launcher** (parent is ``uv``/``uvx``) with a
      resolvable grandparent > 1: ``(grandparent_pid, True)`` — watch the
      grandparent (the real client) for death, since ``getppid()`` will
      not flip while ``uv`` lingers.
    - **Intermediate launcher with an unresolvable grandparent**
      (``ps`` failed, or grandparent is already PID 1):
      ``(None, False)`` — we cannot reliably detect client death, so the
      caller keeps the idle-timeout fallback armed.
    """
    grandparent, comm = _inspect_process(parent_pid)
    if comm in _INTERMEDIATE_LAUNCHERS:
        if grandparent is not None and grandparent > 1:
            return (grandparent, True)
        return (None, False)
    # Direct parent is the client (or an unknown launcher we treat as the
    # client); parent-death detection applies.
    return (None, True)


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is still a live process. Best-effort, no subprocess.

    ``os.kill(pid, 0)`` sends no signal but performs the existence +
    permission check: ``ProcessLookupError`` means the process is gone;
    ``PermissionError`` means it exists but is owned by another user
    (still alive). Any other ``OSError`` is treated as alive to avoid a
    false-positive shutdown.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _run_atexit_bounded(timeout: float) -> None:
    """Run registered ``atexit`` handlers in a bounded daemon thread.

    Called just before a hard ``os._exit(0)``. ``os._exit`` skips
    ``atexit``, which leaves loky/multiprocessing resource-tracker
    semaphores unreleased and prints a spurious "leaked semaphore"
    warning (the resource_tracker child reaps them anyway). Running the
    handlers first avoids the warning. We run them off-thread with a
    timeout so a handler that blocks (e.g. one that tries to join the
    stuck stdin reader on Linux) cannot defeat the watchdog — after the
    budget we proceed to ``os._exit`` regardless.
    """
    done = threading.Event()

    def _run() -> None:
        try:
            atexit._run_exitfuncs()
        except Exception:  # nosec B110 - best-effort cleanup before hard exit
            pass  # nosec B110
        finally:
            done.set()

    threading.Thread(target=_run, name="hub-atexit-cleanup", daemon=True).start()
    done.wait(timeout)


async def _watch_parent(original_ppid: int, interval: float, client_pid: int | None = None) -> str:
    """Poll for client-process death; return a reason string when detected.

    Two detection modes run together:

    - **Parent death** (always): when the parent exits, posix reparents
      the child to PID 1 (init/launchd), so getppid() flips. Windows
      lacks the reparent semantics — getppid() may return stale PIDs —
      so the caller skips spawning this watchdog there.
    - **Grandparent death** (when ``client_pid`` is set): under an
      intermediate launcher like ``uv run`` the chain is
      ``client → uv → hub``. ``uv`` lingers after the client dies, so
      getppid() never flips. We watch the real client (the grandparent)
      directly via ``_pid_alive`` so the hub still exits when the client
      goes away. See ``resolve_watch_plan``.
    """
    while True:
        await asyncio.sleep(interval)
        current = os.getppid()
        if current != original_ppid:
            return f"parent_died original_ppid={original_ppid} current_ppid={current}"
        if client_pid is not None and not _pid_alive(client_pid):
            return f"client_died client_pid={client_pid} intermediate_ppid={original_ppid}"


async def _watch_idle(tracker: IdleTracker, timeout: float, interval: float) -> str:
    """Return a reason string when the tracker has been idle for ``timeout`` seconds."""
    while True:
        await asyncio.sleep(interval)
        idle = tracker.seconds_since_last()
        if idle >= timeout:
            return f"idle_timeout idle_seconds={idle:.0f} timeout_seconds={timeout:.0f}"


async def run_stdio(
    server: Server,
    original_ppid: int | None = None,
    idle_tracker: IdleTracker | None = None,
    parent_watch_interval_secs: float = 0.0,
    idle_timeout_secs: float = 0.0,
    client_watch_pid: int | None = None,
    on_watchdog_shutdown: Callable[[], None] | None = None,
    exit_on_watchdog_shutdown: bool = False,
) -> str | None:
    """Run the MCP server over stdio transport.

    Spawns a parent-death watchdog and (optionally) an idle-timeout
    watchdog alongside the MCP loop. If the client disappears without
    closing stdin (e.g. crashed editor that orphans its FDs, or a
    long-lived editor that stops using a hub it spawned), one of the
    watchdogs notices and triggers shutdown so the hub does not
    accumulate as a zombie holding the index.

    ``original_ppid`` is the PPID snapshot to compare against. The
    caller should capture it at process entry (before any slow startup
    work), because startup can take several seconds and the client may
    die during that window — if we snapshotted here, we'd lock in the
    already-reparented PID and the watchdog would never fire.

    ``client_watch_pid`` is the grandparent (real client) PID to watch
    for death when the hub runs under an intermediate launcher such as
    ``uv run`` (where ``getppid()`` never flips). ``None`` for direct
    launches. Resolved by the caller via ``resolve_watch_plan``.

    ``parent_watch_interval_secs`` and ``idle_timeout_secs`` are
    resolved by the caller (typically from ``ServerConfig`` env-aware
    properties). A value of 0 disables the corresponding watchdog.

    ``exit_on_watchdog_shutdown`` decides what happens when a watchdog
    fires. True (CLI mode): close stdin, arm a hard-exit timer, and
    call ``os._exit(0)`` after graceful unwind. False (in-process
    mode): no stdin close, no timer, no ``os._exit`` — tasks are
    cancelled, the shutdown callback runs once after cancellation, and
    ``run_stdio`` returns ``shutdown_reason`` so the caller can drive
    its own teardown. See ``serve_stdio`` for the full rationale.
    """
    logger.info("Starting MCP server on stdio transport")

    enable_watchdog = sys.platform != "win32" and parent_watch_interval_secs > 0
    if original_ppid is None:
        original_ppid = os.getppid() if enable_watchdog else 0

    enable_idle_watch = idle_tracker is not None and idle_timeout_secs > 0

    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        server_task = asyncio.create_task(
            server.run(read_stream, write_stream, init_options),
            name="mcp-server-run",
        )
        tasks: list[asyncio.Task[object]] = [server_task]
        watchdog_task: asyncio.Task[str] | None = None
        idle_task: asyncio.Task[str] | None = None
        if enable_watchdog:
            watchdog_task = asyncio.create_task(
                _watch_parent(original_ppid, parent_watch_interval_secs, client_watch_pid),
                name="parent-death-watchdog",
            )
            tasks.append(watchdog_task)
        if enable_idle_watch and idle_tracker is not None:
            poll = min(_IDLE_POLL_INTERVAL_SECS, max(idle_timeout_secs / 4.0, 1.0))
            idle_task = asyncio.create_task(
                _watch_idle(idle_tracker, idle_timeout_secs, poll),
                name="idle-watchdog",
            )
            tasks.append(idle_task)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        shutdown_reason: str | None = None
        if watchdog_task is not None and watchdog_task in done:
            shutdown_reason = watchdog_task.result()
        elif idle_task is not None and idle_task in done:
            shutdown_reason = idle_task.result()

        graceful_done: threading.Event | None = None
        # Single-shot guard for the on_watchdog_shutdown callback. Lives
        # outside the `exit_on_watchdog_shutdown` branch because the
        # in-process path below also invokes the callback on the
        # graceful unwind, and we want identical one-shot semantics in
        # both modes.
        shutdown_cb_lock = threading.Lock()
        shutdown_cb_started = [False]

        def _invoke_shutdown_cb_once(context: str) -> None:
            if on_watchdog_shutdown is None:
                return
            with shutdown_cb_lock:
                if shutdown_cb_started[0]:
                    return
                shutdown_cb_started[0] = True
            try:
                on_watchdog_shutdown()
            except Exception:
                logger.exception("on_watchdog_shutdown raised during %s", context)

        # Single-shot guard for the pre-exit atexit cleanup. Both the
        # graceful main thread and the hard-exit timer thread reach an
        # `os._exit(0)`; in the narrow window where graceful close
        # finishes right as the 2.5s timer fires, both could otherwise
        # run `atexit._run_exitfuncs()` concurrently (double-invoking
        # handlers on the unguarded CPython registry). Run it at most
        # once — whichever thread gets there first.
        atexit_lock = threading.Lock()
        atexit_started = [False]

        def _run_atexit_once() -> None:
            with atexit_lock:
                if atexit_started[0]:
                    return
                atexit_started[0] = True
            _run_atexit_bounded(_ATEXIT_CLEANUP_TIMEOUT_SECS)

        if shutdown_reason is not None and exit_on_watchdog_shutdown:
            logger.info("Shutting down: %s", shutdown_reason)
            # Arm a watchdog-of-the-watchdog: a plain OS thread that
            # will hard-exit the process if graceful teardown doesn't
            # complete within a few seconds. This is defensive against
            # two Linux-specific failure modes we've observed in CI:
            #
            # 1. mcp's stdio_server reads stdin via
            #    `anyio.to_thread.run_sync(readline, cancellable=False)`.
            #    On Linux, closing fd 0 does not wake the worker
            #    thread's blocked ``read(0)`` — the kernel keeps the
            #    file object alive via the thread's reference — so the
            #    asyncio task never observes its own cancellation.
            # 2. Nothing in asyncio (``wait_for``, ``wait(timeout=…)``,
            #    ``gather``) guarantees return when the inner task is
            #    stuck in an uninterruptible blocked syscall off-loop;
            #    the event-loop timer fires but the subsequent unwind
            #    path still has to await the un-cancellable task.
            #
            # An OS thread bypasses both: ``time.sleep`` + ``os._exit``
            # is scheduled by the kernel, not the event loop. The
            # watchdog's whole purpose is "client is gone, die", so
            # blocking forever on graceful unwind defeats it.
            # Event signals that graceful teardown finished, disarming
            # the hard-exit thread. Without this, a successful graceful
            # unwind (macOS, or any in-process caller) would still get
            # `os._exit` called 2.5 s later and kill the host — e.g.
            # the pytest worker when `run_stdio` is driven directly.
            #
            # Arming the timer (and closing stdin, below) is gated on
            # `exit_on_watchdog_shutdown` so in-process callers
            # (tests, library embedders) are never exposed to an
            # `os._exit` from a daemon thread or a closed host stdin.
            # The single-shot guard for `on_watchdog_shutdown` is
            # defined above so the in-process `else` path can reuse it.
            graceful_done = threading.Event()

            def _hard_exit_on_hang() -> None:
                # `Event.wait(timeout)` returns True if set before the
                # timeout, False on timeout. Only hard-exit on timeout.
                if graceful_done.wait(_HARD_EXIT_TIMEOUT_SECS):
                    return
                # Write directly to stderr (bypassing the logging
                # framework, which can buffer / deadlock with handlers
                # held by the stuck main thread) so operators still see
                # the shutdown reason.
                try:
                    sys.stderr.write(
                        "pipecat-context-hub: client gone; fast-exiting after "
                        f"{_HARD_EXIT_TIMEOUT_SECS:g}s graceful-unwind budget. "
                        "This is expected (the watchdog forces exit so the hub "
                        "does not linger when its client is gone); exit code 0, "
                        "on-disk state is intact.\n"
                    )
                    sys.stderr.flush()
                except Exception:  # nosec B110 - best-effort diagnostic before hard exit
                    pass  # nosec B110
                # Give `on_watchdog_shutdown` a short, bounded window.
                # The single-shot guard short-circuits if the graceful
                # path already started the callback (and is hung in
                # it); in that case we skip straight to `os._exit(0)`
                # rather than starting a second concurrent
                # `IndexStore.close()`. If it hangs fresh here, abandon
                # it after 1 s — on-disk state is crash-consistent
                # (SQLite WAL + Chroma recovery on next open).
                cb_done = threading.Event()

                def _run_cb() -> None:
                    try:
                        _invoke_shutdown_cb_once("hard-exit timer")
                    finally:
                        cb_done.set()

                threading.Thread(
                    target=_run_cb,
                    name="hub-hard-exit-cleanup",
                    daemon=True,
                ).start()
                cb_done.wait(_SHUTDOWN_CB_TIMEOUT_SECS)
                # Let loky/multiprocessing release resource-tracker
                # semaphores so the spurious "leaked semaphore" warning
                # does not print. Bounded so a blocking handler cannot
                # re-defeat the watchdog; single-shot so it cannot race
                # the graceful path's own call.
                _run_atexit_once()
                os._exit(0)

            threading.Thread(
                target=_hard_exit_on_hang,
                name="hub-hard-exit-timer",
                daemon=True,
            ).start()

            # Still attempt graceful unwind — on macOS/BSD (and
            # client-clean-close paths on Linux) this completes well
            # within the 2.5 s window and the timer thread is
            # harmless.
            try:
                os.close(sys.stdin.fileno())
            except (OSError, ValueError):
                pass
        elif shutdown_reason is not None:
            # In-process safe mode (`exit_on_watchdog_shutdown=False`).
            # The caller owns `sys.stdin` and the host process — do NOT
            # close stdin, do NOT arm the hard-exit timer. Cancel the
            # pending tasks, invoke the shutdown callback once so
            # critical resources still release, and return
            # `shutdown_reason` so the caller can drive its own
            # teardown. On Linux the graceful unwind may hang inside
            # `stdio_server.__aexit__` for the same reasons the timer
            # exists; that is the in-process caller's problem to handle
            # (e.g. test setups mock `stdio_server` to avoid it).
            logger.info("Shutting down: %s", shutdown_reason)
            # Callback runs AFTER cancellation (below) to mirror the
            # exit branch's ordering and avoid racing a still-pending
            # tool call that is mid-read against the IndexStore.

        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        if graceful_done is None and shutdown_reason is not None:
            # In-process safe-mode cleanup: tasks are now cancelled, so
            # it is safe to release index handles.
            _invoke_shutdown_cb_once("graceful unwind (in-process)")

        if graceful_done is not None:
            # Exit path only (`exit_on_watchdog_shutdown=True`). The
            # in-process path above already called
            # `_invoke_shutdown_cb_once` on the graceful unwind and
            # returns normally so the caller can tear down.
            #
            # Release index handles while the hard-exit timer is still
            # armed — Chroma's close can hang on Linux (internal threads
            # we cannot interrupt), and the caller's outer `finally` runs
            # after `run_stdio` returns, outside the timer's scope. If
            # this hangs, the 2.5 s timer fires; the single-shot guard
            # ensures the timer does NOT start a second concurrent
            # close, it goes straight to `os._exit(0)`. If this
            # completes, we disarm the timer below.
            _invoke_shutdown_cb_once("graceful unwind")
            # Graceful path completed — disarm the hard-exit timer so
            # it does not fire after `run_stdio` returns.
            graceful_done.set()

            # Exit before `stdio_server.__aexit__` runs. On Linux, the
            # anyio worker thread doing the cancellable=False
            # `readline` is parked in uninterruptible read(0); both
            # stdio_server's teardown and CPython's interpreter
            # shutdown wait for that thread and hang forever. The
            # watchdog's job is "client is gone, die" — skip both by
            # exiting directly. We do this from the main thread (not
            # the daemon timer) so the call is guaranteed to execute
            # even under GIL-holding C code.
            #
            # Run atexit handlers first (bounded) so loky/multiprocessing
            # release their resource-tracker semaphores — `os._exit`
            # otherwise skips atexit and prints a spurious "leaked
            # semaphore" warning. Bounded so a blocking handler cannot
            # reintroduce the very hang we exit to avoid; single-shot so
            # it cannot race the hard-exit timer's own call.
            _run_atexit_once()
            os._exit(0)

        # Surface server-task exceptions (e.g. unexpected protocol error)
        # while still letting the index_store finally-block run.
        if server_task in done:
            exc = server_task.exception()
            if exc is not None:
                raise exc

        return shutdown_reason


def serve_stdio(
    server: Server,
    original_ppid: int | None = None,
    idle_tracker: IdleTracker | None = None,
    parent_watch_interval_secs: float = 0.0,
    idle_timeout_secs: float = 0.0,
    client_watch_pid: int | None = None,
    on_watchdog_shutdown: Callable[[], None] | None = None,
    exit_on_watchdog_shutdown: bool = False,
) -> str | None:
    """Blocking entry point that runs the stdio server.

    ``original_ppid`` should be captured by the caller at process entry
    (before any index/service construction) so that a parent-death that
    happens during startup is still detected by the watchdog.
    ``client_watch_pid`` is the grandparent (real client) PID to watch
    when launched under an intermediate launcher like ``uv run``; the
    caller resolves it via ``resolve_watch_plan``.
    ``idle_tracker`` is the request-touch tracker used by the idle
    watchdog; the caller passes the same instance to ``create_server``.
    The two timeouts come from ``ServerConfig`` env-aware computed
    properties; 0 disables the corresponding watchdog.
    ``on_watchdog_shutdown`` is invoked once when a watchdog-triggered
    shutdown begins — either inline on the graceful unwind (while the
    hard-exit timer is armed) or from the timer thread if the graceful
    path hangs. A single-shot guard ensures at most one invocation, so
    a hanging close on the graceful path does not spawn a second
    concurrent close when the timer fires. Pass the index-store close
    here so critical resources are released whether the unwind is
    graceful or hard.

    ``exit_on_watchdog_shutdown`` must be True for the CLI entry point
    and False for any in-process caller (tests, library embedding).
    This is a policy choice, not a test shim: when True, ``run_stdio``
    closes ``sys.stdin`` (to unblock mcp's stdin reader on Linux),
    arms a 2.5 s daemon hard-exit timer, and calls ``os._exit(0)``
    itself after graceful unwind — otherwise, on Linux,
    ``mcp.stdio_server.__aexit__`` waits on the anyio worker thread
    parked in an uninterruptible ``read(0)`` and control never returns.
    When False, every host-affecting action is suppressed: no stdin
    close, no hard-exit timer, no ``os._exit``. The shutdown callback
    still runs (single-shot) so index handles are released, tasks are
    cancelled, and ``run_stdio`` returns ``shutdown_reason`` to the
    caller. In-process callers MUST arrange for the graceful unwind
    actually to complete on their platform (e.g. by mocking
    ``stdio_server``); the safe-mode flag does not rescue them from a
    real Linux ``read(0)`` hang.
    """
    return asyncio.run(
        run_stdio(
            server,
            original_ppid=original_ppid,
            idle_tracker=idle_tracker,
            parent_watch_interval_secs=parent_watch_interval_secs,
            idle_timeout_secs=idle_timeout_secs,
            client_watch_pid=client_watch_pid,
            on_watchdog_shutdown=on_watchdog_shutdown,
            exit_on_watchdog_shutdown=exit_on_watchdog_shutdown,
        )
    )
