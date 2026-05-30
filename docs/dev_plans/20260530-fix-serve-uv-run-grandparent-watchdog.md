# Task: Bound `serve` Lifetime to the Real Client Under `uv run` (Grandparent-Death Watchdog)

**Status**: Complete (unreleased) — all phases implemented, full suite green (1046 passed, 3 skipped, incl. 4 serve-lifetime integration tests), lint/format/mypy + bandit clean. Headline `uv run` client-death path proven by `test_uv_run_client_death_exits_via_grandparent_watchdog`.
**Date**: 2026-05-30
**Branch**: `fix/serve-uv-run-grandparent-watchdog`
**Follows**: [`20260421-bug-serve-orphan-watchdog.md`](20260421-bug-serve-orphan-watchdog.md) — closes its documented **"Known Gap: `uv run` wrapper"**.

## Motivation

A developer running the hub via `uv run pipecat-context-hub serve` reported:

```
Shutting down: idle_timeout idle_seconds=1800 timeout_seconds=1800
pipecat-context-hub: graceful shutdown timed out, hard-exiting (stdin reader stuck in uninterruptible read(0))
resource_tracker: There appear to be 1 leaked semaphore objects to clean up ... {'/loky-85895-...'}
```

Investigation outcome (no data loss, no real leak):

1. **Not a crash.** The hub idle-shut-down after exactly 1800s of no MCP requests — the **idle watchdog** firing as designed. It exists *only* as a workaround for the `uv run` gap: under `uv run` the chain is `client → uv → hub`, `uv` lingers after the client dies, so `os.getppid()` never flips and the parent-death watchdog cannot fire. The idle timer was the blunt fallback.
2. **The "timed out" line** is our own 2.5s hard-exit backstop (`transport._hard_exit_on_hang`). On the developer's Python 3.13 + loaded reranker the graceful unwind exceeded 2.5s, so the backstop fired. Exit code 0; on-disk state crash-consistent. The wording reads like an error.
3. **The leaked-semaphore line** is a side effect of `os._exit(0)` skipping `atexit`. The cross-encoder pulls in `torch`/`sklearn`→`loky`, which registers a resource-tracker semaphore cleaned up only at a normal exit. Reproduced locally: `os._exit(0)` → warning; `sys.exit(0)` → clean; `atexit._run_exitfuncs()` before `os._exit(0)` → clean. The resource_tracker child reaps it regardless — benign.

The desired experience: the hub should not self-terminate mid-session, and should exit cleanly/quietly when the client truly goes away.

## Approach (decided with maintainer)

**Grandparent-death detection** + smart idle gating + clean/quiet exit.

- When the hub's **direct parent is an intermediate launcher** (`uv`/`uvx`/`pipx`/`poetry`/`pdm`/`hatch`/`rye`/`pipenv` — see `_INTERMEDIATE_LAUNCHERS`), watch the **grandparent** (the real client) for death instead of relying on `getppid()` flipping. When the client dies, the launcher reparents to PID 1, the client PID disappears, and `os.kill(client_pid, 0)` raises `ProcessLookupError` → shut down.
- When the **direct parent is the client** (direct launch), the existing parent-death watchdog already covers orphan cleanup — unchanged.
- **Idle watchdog becomes a fallback only.** When reliable client-death detection is active (direct parent, or intermediate parent with a resolved grandparent), the default idle timeout is disabled — it would only kill a warm server during a quiet stretch of an active session. The operator can always force it back via `PIPECAT_HUB_IDLE_TIMEOUT_SECS`. It stays ON when detection is *not* reliable: Windows (watchdog disabled), parent-watch disabled, or an intermediate parent whose grandparent can't be resolved.
- **Clean exit:** run `atexit` handlers in a bounded daemon thread before each `os._exit(0)` so loky/multiprocessing release resources (no semaphore warning) without risking an unbounded hang.
- **Quiet message:** reword the hard-exit stderr line so a normal client-gone fast-exit doesn't read as a crash.

A stdio MCP server **cannot restart itself** (it's a subprocess the client spawns over stdin/stdout). "Auto-restart" is the MCP client's job; our contribution is (a) not dying mid-session and (b) exiting cleanly so the client respawns without drama. Documented explicitly.

## Files to modify

- `src/pipecat_context_hub/server/transport.py`
  - `_inspect_process(pid)` → `(ppid, comm_basename)` via one best-effort `ps -p PID -o ppid=,comm=` (timeout-guarded). Returns `(None, None)` on any failure.
  - `_INTERMEDIATE_LAUNCHERS = frozenset({"uv", "uvx", "pipx", "poetry", "pdm", "hatch", "rye", "pipenv"})` (import-time `raise ValueError` if any entry > 15 chars — survives `python -O`, unlike `assert`; Linux `ps -o comm=` COMM truncation).
  - `resolve_watch_plan(parent_pid)` → `WatchPlan(client_watch_pid: int | None, detection_reliable: bool)` (a `NamedTuple`, so it still unpacks positionally).
  - `_pid_alive(pid)` via `os.kill(pid, 0)` (ProcessLookupError → dead; PermissionError/other → alive).
  - `_watch_parent(original_ppid, interval, client_pid=None)` — also fire on `not _pid_alive(client_pid)` → `"client_died ..."`.
  - `_run_atexit_bounded(timeout)` helper; call before both `os._exit(0)` sites.
  - Reword the hard-exit stderr message.
  - Thread `client_watch_pid` through `run_stdio` / `serve_stdio`.
- `src/pipecat_context_hub/cli.py` (`serve`)
  - `_resolve_watch_and_idle_plan(config, original_ppid, logger)` helper encapsulates the watch-plan + idle-gating policy and returns `(client_watch_pid, idle_timeout_secs)`. Called at process entry, **before** the slow index/model startup (gated on non-win32 + parent_watch > 0) so a client that dies during cold-start is captured while its PID is still live.
  - Disable the default idle timeout when detection is reliable and the operator did not set it explicitly; `logger.info` the decision. Pass `client_watch_pid` to `serve_stdio`.
- `src/pipecat_context_hub/shared/config.py`
  - `idle_timeout_explicitly_set` property (env var present **or** field ≠ default) so smart gating never overrides an operator's explicit value.
- `tests/unit/test_transport.py` — new tests (below).
- `CHANGELOG.md` — `### Fixed` + `### Changed` entries.
- `docs/dev_plans/20260421-bug-serve-orphan-watchdog.md` — mark the "Known Gap: `uv run`" as resolved, link here.
- `CLAUDE.md` — note idle-watchdog-as-fallback + client-restart responsibility, if warranted.

## Tests

- `resolve_watch_plan`: parent=uv with grandparent → `(gp, True)`; parent=direct → `(None, True)`; parent=uv with unresolved/`1` grandparent → `(None, False)`; `ps` failure → `(None, False)` for intermediate (mock `_inspect_process`).
- `_pid_alive`: live pid (`os.getpid()`) → True; reaped pid → False (mock `os.kill`).
- `_watch_parent`: fires `client_died` when `_pid_alive(client_pid)` flips False while `getppid()` stays stable (the `uv run` case the old watchdog missed); still fires `parent_died` on ppid flip; does not fire while both stable.
- `run_stdio` wiring: with `client_watch_pid` set and `os.kill` raising `ProcessLookupError`, the server task is cancelled and unwind completes (mirror existing `TestRunStdioWatchdogWiring`).
- Idle gating (config): `idle_timeout_explicitly_set` true when env set / field non-default; false at default.
- `_run_atexit_bounded`: returns within the budget even if a registered handler blocks (register a slow handler, assert bounded return).
- Existing transport/idle tests must stay green (regression).

## Out of scope

- Restarting the hub from within itself (architecturally impossible for stdio MCP; client's job).
- Deeper-than-one-level intermediate chains (`uv → something → hub`); resolve only the immediate grandparent. If unresolved → idle fallback stays on.
- Changing the 2.5s hard-exit budget or the `read(0)` unwind mechanics (Phase 6 of the prior plan stands).

## Recognized intermediate launchers

`_INTERMEDIATE_LAUNCHERS = {uv, uvx, pipx, poetry, pdm, hatch, rye, pipenv}`. Listing a launcher is strictly safe (deep-review finding): the name matches only when that process is the hub's *direct* parent (i.e. it lingered); a launcher that `exec`s into the target is never the parent, so its entry can't false-match. Entries must be ≤15 chars (Linux `ps -o comm=` COMM truncation).

## Known minor gaps

- **PID reuse (TOCTOU).** If the client dies and its exact PID is reused by a new process within one poll interval (default 2s), `os.kill(client_pid, 0)` succeeds and we miss the death → hub lingers until its own parent (`uv`) exits or the next launch. Low probability; documented. Idle fallback does not cover this case by design (it's disabled when grandparent watch is active).
- **Unrecognized lingering launcher.** A launcher not in `_INTERMEDIATE_LAUNCHERS` that lingers as the hub's parent falls through to `(None, True)` in `resolve_watch_plan` — idle is auto-disabled and parent-death won't fire, so the hub is reaped only by stdin EOF. Mitigated by covering the common Python launchers above; add new ones as they're reported.
