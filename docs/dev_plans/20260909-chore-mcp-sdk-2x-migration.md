# Task: Migrate the `mcp` Python SDK from the 1.x line to 2.x

**Status**: Not Started
**Component**: server (mcp sdk)
**Assigned to**: Claude
**Priority**: High (not urgent-blocking — see Timeline)
**Branch**: `chore/mcp-sdk-2x-migration` (not yet created)
**Created**: 2026-09-09
**Review Gates**: full

## Why

Issue [#127](https://github.com/pipecat-ai/pipecat-context-hub/issues/127)
asks to widen `pyproject.toml`'s `"mcp>=1.0,<2.0"` to `<3.0`, to match
Pipecat's own upcoming bound (Pipecat 1.9.0, expected within the next week,
loosens its own `mcp<2,>=1.11.0` pin). The literal ask reads like a one-line
version-bound edit with real SDK-v2 support deferred to "later."

**That framing doesn't hold up.** I tested it empirically before writing
this plan: installing `mcp==2.1.1` (latest on PyPI) and running our test
suite against it, unmodified, produces:

```
AttributeError: 'Server' object has no attribute 'list_tools'
AttributeError: 'Server' object has no attribute 'request_handlers'. Did you mean: '_request_handlers'?
```

`create_server()` crashes the instant it tries to register a tool. 11 tests
fail, including a real subprocess-level integration test
(`test_orphaned_serve_exits_via_watchdog`) where `serve` fails to boot at
all ("wrapper: serve closed stdout before responding" — not a mock
assertion, the actual server process dying). Loosening the pyproject bound
today would let any fresh install whose resolver lands on 2.x ship a server
that doesn't start. There is no version of "bump the toml, support v2
later" that doesn't first require making the code work under v2 — the two
steps aren't actually separable.

## Timeline

No forced conflict exists yet: PyPI's latest `pipecat-ai` is still 1.8.1,
and it still requires `mcp<2.0,>=1.11.0` — same cap we have. The "Pipecat
moving to `<3`" trigger is Pipecat's *upcoming* 1.9.0, not something live
today. So this is "do it properly before we're caught flat-footed," not
"drop everything." Budget: land before 1.9.0 ships (~1 week out) so issue
#127 can be closed with a real fix rather than reopened as urgent once
users start hitting the resolver-drift risk described above.

## API surface we actually depend on

Grep-confirmed, `src/pipecat_context_hub/server/`:

```python
# main.py
from mcp import types
from mcp.server.lowlevel import Server
# transport.py
from mcp import stdio_server
from mcp.server.lowlevel import Server
```

Four call sites, all in `main.py`/`transport.py` (never in tool handlers —
those only touch our own `shared/types.py` Pydantic models, never `mcp.*`
directly): `Server(...)` construction, `server.request_handlers`
get/set (ping-wrapping trick), `@server.list_tools()`/`@server.call_tool()`
decorators, `stdio_server()` + `server.run()` + `server.create_initialization_options()`.
Small surface — this is a contained migration, not a rewrite.

## What changes in 2.x (confirmed by reading the installed 2.1.1 source directly — not the docs site, see caveat below)

`mcp.server.lowlevel.Server`'s registration model was rewritten, not
versioned:

| Old (1.x) | New (2.1.1, confirmed from source) |
|---|---|
| `@server.list_tools()` decorator | `Server(..., on_list_tools=handler)` constructor kwarg |
| `@server.call_tool()` decorator | `Server(..., on_call_tool=handler)` constructor kwarg |
| Handler signature `() -> list[Tool]` / `(name, args) -> list[TextContent]` | Handler signature `(ctx, params) -> ListToolsResult` / `(ctx, params) -> CallToolResult` — no more automatic return-value wrapping |
| `server.request_handlers` (public dict, keyed by request **type**) | No public dict. `server.get_request_handler(method: str)` / `server.add_request_handler(method: str, params_type, handler)`, keyed by **method string** (`"ping"`, `"tools/list"`, `"tools/call"`) |
| Unknown-tool `raise ValueError(...)` inside `call_tool` returns `CallToolResult(isError=True)` to the client as a *successful* result | Same `raise` now propagates as a **JSON-RPC error response** instead — an observable client-facing behavior change, currently untested either way (see Testing strategy) |
| `types.Tool(name=..., inputSchema=...)` construction | Likely unchanged — `mcp_types`'s base model keeps `populate_by_name=True` with camelCase aliasing, so construction by the old kwarg name should still validate. Confirmed-from-source for the *mechanism*; not independently unit-tested |
| `stdio_server()` reads/writes fd 0/1 directly | `stdio_server()` now **diverts** fd 0/1 (stdin → `/dev/null`, stdout → dup of stderr) and reads/writes via a private duplicated fd ≥ 3 for the session's duration, restoring both on exit |

Full mapping table with file:line citations and confidence markers is in
the session transcript that produced this plan (subagent research pass,
2026-09-09) — reproduced in condensed form under **Risks** below where it
changes what we build; ask me to regenerate the full 13-row table if it's
needed as a standalone reference during implementation.

**Caveat on sourcing**: the official migration guide
(`https://py.sdk.modelcontextprotocol.io/v2/migration/`) contradicts the
installed 2.1.1 source on two points — its code samples show a
decorator-based `@server.on_list_tools()`/`@server.on_request(...)` syntax
that does not exist anywhere in the installed package (only constructor
kwargs and `add_request_handler`/`get_request_handler` exist), and it
claims the `"ping"` handler "cannot be overridden," while the source shows
only `"initialize"` is protected. **Treat the guide as directional, the
installed source as ground truth** — re-verify against whatever `mcp`
version is actually being targeted at implementation time, since the guide
mismatch suggests either version drift in the docs or in our reading of
them.

## Risks (confidence-tiered, so Phase 0 spends effort where it's actually needed)

### Almost certain to require code changes
- `main.py`'s ping-wrapping trick (`server.request_handlers.get(types.PingRequest)` /
  `server.request_handlers[types.PingRequest] = ...`, `main.py:298,309`) —
  the dict no longer exists; rewrite via `get_request_handler("ping")` /
  `add_request_handler("ping", entry.params_type, wrapped_handler)`. The
  wrapper's own signature must also change from
  `(request: types.PingRequest) -> types.ServerResult` to
  `(ctx, params: types.RequestParams | None) -> types.EmptyResult`.
- `list_tools`/`call_tool` registration (`main.py:311,340`) — move to
  constructor kwargs, adapt handler signatures and return types per the
  table above.
- Every test in `test_server.py` and the one in `test_staleness.py` that
  reads `server.request_handlers[...]` (6 sites total) — must move to
  `get_request_handler(method_string)`, and however we end up invoking the
  handler in tests (see **Unknown**, below) will also need to change
  because the callable's own signature changed from `(request)` to
  `(ctx, params)`.

### Likely
- The unknown-tool-name error path (`main.py:377`, currently untested)
  changes observable behavior (`isError=True` result → JSON-RPC error). Add
  a test either way so the behavior is pinned, not just migrated blind.
- `transport.py`'s `os.close(sys.stdin.fileno())` unblock trick
  (`transport.py:479-482`) is justified by a comment describing 1.x's
  direct-fd stdin reading. Under 2.x's fd-diversion, `sys.stdin.fileno()`
  after `stdio_server()` starts refers to the `/dev/null` diversion, not
  the private duplicated fd the reader thread actually blocks on — closing
  it is plausibly a no-op against the real read, meaning graceful unwind
  would always fall through to the 2.5s hard-exit timer instead of
  completing. The process still exits either way (the timer is
  unconditional), but the *graceful* path and its Linux-specific rationale
  comment would no longer describe reality. **Not yet run-verified** — this
  is Phase 0's highest-value spike, precisely because
  `test_orphaned_serve_exits_via_watchdog` is the test that already caught
  the startup crash once.

### Possible / worth a quick check, not expected to bite
- `types.Tool(name=..., inputSchema=...)` construction may need no change
  at all (see table above) — verify with a single assertion in Phase 0
  rather than assuming.
- `mcp.types` moved to a standalone `mcp-types` PyPI distribution
  (version-locked to `mcp`) — new transitive dependency, should show up
  cleanly in `uv lock`; no code impact expected.
- `opentelemetry-api` becomes a hard dependency of `mcp` 2.x (`Server`
  unconditionally instantiates `OpenTelemetryMiddleware` as default
  middleware). Review the `uv lock` diff for size/license impact — we don't
  currently emit or care about traces, so this is a dependency-weight
  question, not a behavior one.

### Unknown — a design decision, not a fact to look up
- How to invoke the new `(ctx, params)`-shaped handlers directly from unit
  tests. `ServerRequestContext` (`mcp/server/context.py`) requires a real
  `ServerSession` plus `lifespan_context`/`protocol_version`/`method` — not
  trivially fakeable inline. Two paths: (a) build a minimal fake/mock
  `ServerSession` sufficient for `ctx`, keeping today's "call the handler
  function directly" test style, or (b) drive tests through
  `server.run()` end-to-end against an in-memory `anyio` stream pair
  instead, which is more faithful but a bigger rewrite of
  `test_server.py`. **Decide in Phase 0**, informed by which one actually
  lets the existing assertions (idle-tracker touch on list/call, ping
  passthrough) survive with the least structural change.

## Compatibility policy — decision needed before Phase 1

1.x and 2.x are not simultaneously satisfiable by one code path here (the
handler registration model is incompatible, not just renamed). Two options:

- **A — Clean cutover (recommended)**: migrate the code to the 2.x API,
  set `pyproject.toml` to `mcp>=2.0,<3.0`, drop 1.x support entirely. Single
  code path, single test suite, matches how this project has handled every
  prior major-version dependency break (chromadb 0.6→1.x was a hard cutover
  with no dual-support shim — see
  `20260529-chore-chromadb-1x-python-314.md`).
- **B — Dual support**: branch on the installed `mcp` major version at
  runtime, maintaining two implementations of `create_server()`/`run_stdio`.
  Doubles the code and CI surface for a dependency most operators will
  happily let float forward; not recommended, but flagged because it's a
  real choice with a real downside (users hard-pinned to `mcp<2.0`
  elsewhere in their environment would be locked out of future releases
  under Option A).

**This plan assumes Option A.** Flag before Phase 1 if that's wrong — it
determines whether Phase 2 writes one code path or two.

## Files to Modify

- `src/pipecat_context_hub/server/main.py` — `create_server()`: `Server`
  construction, ping-wrapping trick, `list_tools`/`call_tool` registration
  and handler bodies.
- `src/pipecat_context_hub/server/transport.py` — verify `stdio_server()`/
  `server.run()`/`create_initialization_options()` call shapes (likely
  unchanged per the table above); rework or re-verify the
  `os.close(sys.stdin.fileno())` graceful-unwind trick per the Phase 0
  spike finding.
- `tests/unit/test_server.py` — `TestToolRegistration`,
  `TestToolDispatch` classes (6+ assertions reading the old
  `request_handlers` dict).
- `tests/unit/test_staleness.py` — one site at `test_staleness.py:161-170`
  using the same pattern plus `result.root` accessor (also gone in 2.x —
  "union types no longer RootModel" per the migration guide).
- `tests/integration/test_serve_lifetime.py` — no code changes expected
  (it drives the process externally via pipes), but it's the
  primary regression gate for the fd-diversion risk — must pass, not just
  "not crash for an unrelated reason."
- `pyproject.toml` — `"mcp>=1.0,<2.0"` → `"mcp>=2.0,<3.0"` (Option A).
- `uv.lock` — regenerate; review the diff for `mcp-types`/`opentelemetry-*`
  additions.
- `CHANGELOG.md` — `### Changed` entry once merged.

## Testing strategy

### Phase 0: Spike — resolve the two open questions before touching real code
1. In a scratch venv (`uv==2.1.1` already prepared at
   `/private/tmp/.../mcp2-scratch` from this session — regenerate if stale),
   hand-write a throwaway `create_server()`-equivalent using
   `on_list_tools`/`on_call_tool` kwargs and the `add_request_handler`
   ping-wrapping pattern from the table above. Confirm it boots and answers
   a raw `initialize` → `tools/list` → `tools/call` JSON-RPC round trip
   over real stdio (mirror what `test_orphaned_serve_exits_via_watchdog`
   does at the subprocess level).
2. With that spike wired into a throwaway copy of `transport.py`'s
   `run_stdio`, actually exercise the orphan-watchdog shutdown path and
   observe whether graceful unwind completes or falls through to the 2.5s
   hard-exit timer. This resolves the highest-confidence unknown in this
   plan — write the answer back into this plan's Findings section before
   Phase 2.
3. Decide the unit-test invocation strategy (fake `ServerSession` vs.
   in-memory `server.run()`) by trying both against one existing test
   (`test_list_tools_touches_idle_tracker` is a good candidate — it's
   simple and exercises both the handler call and a side effect).
4. Resolve compatibility policy (Option A vs. B) — confirm with the user
   before Phase 1 if Phase 0 surfaces new information that changes the
   recommendation.

### Phase 1: `main.py` migration
- Rewrite `create_server()` per the mapping table.
- Add the previously-missing unknown-tool-name test (pin whatever the real
  2.x behavior turns out to be — JSON-RPC error vs. something else).
- `uv run mypy src/ tests/` clean under the new handler signatures (the
  `(ctx, params)` shape will need real type annotations, not
  `# type: ignore` — the old decorators carried `# type: ignore[no-untyped-call, untyped-decorator]` comments that should no longer be needed once we're calling documented, typed constructor kwargs).

### Phase 2: `transport.py` migration
- Apply the Phase 0 spike's resolution for the stdin-unblock trick.
- Re-run `test_orphaned_serve_exits_via_watchdog` and the rest of
  `test_serve_lifetime.py` until green under real mcp 2.x (not mocked).

### Phase 3: Test suite migration
- `test_server.py`, `test_staleness.py` per Phase 0's chosen invocation
  strategy.
- Full local suite: `uv run pytest tests/` — must match or exceed today's
  pass count (1804 passed, 7 skipped as of `da72d0f` on `main`); any new
  skips must be justified, not silent.
- `tests/smoke/` and `tests/integration/` (this repo's smoke + e2e suites)
  green, same as the PR #129 merge gate this session already established
  as the standard bar.

### Phase 4: Dependency bump + live verification
- `pyproject.toml` + `uv lock`, review the diff.
- Live `serve` smoke test: run the actual CLI (`uv run pipecat-context-hub serve`)
  against a real client (or the raw JSON-RPC round trip used in Phase 0)
  and confirm tool listing + at least one real tool call + `get_hub_status`
  all work end-to-end, not just under test mocks.
- Full CI matrix (`Quality` ×3, `Windows smoke` ×2, `CodeQL`, `Security`,
  `Analyze` ×2) green — this migration touches process lifecycle and
  platform-sensitive fd handling, so the Windows smoke legs matter more
  than usual here; don't treat a green macOS/Linux run alone as sufficient.

### What we are NOT testing (and why)
- HTTP/streamable transport, resources, prompts, elicitation, roots,
  logging-level, progress notifications — none of these are used by this
  server today (stdio-only, tools-only), and 2.x's deprecation warnings for
  `on_set_logging_level`/`on_roots_list_changed`/`on_progress` constructor
  kwargs don't apply since we never pass them.
- `mcp.server.mcpserver.MCPServer` (the renamed FastMCP) — not used today,
  not part of this migration; noted in research only as a fallback if the
  lowlevel migration proves unexpectedly painful.

## Acceptance Criteria

- [ ] `pyproject.toml` requires `mcp>=2.0,<3.0` (pending Phase 0's
      compatibility-policy confirmation).
- [ ] `create_server()` and `run_stdio()` work under real mcp 2.x with no
      mocking of `mcp.*` internals beyond what today's tests already mock.
- [ ] Full local test suite passes with count ≥ today's baseline (1804
      passed / 7 skipped); `tests/smoke/` and `tests/integration/` both
      green.
- [ ] `test_orphaned_serve_exits_via_watchdog` passes for real (not
      skipped, not weakened) — this is the test that caught the original
      crash and is the main regression gate for the fd-diversion risk.
- [ ] Live `serve` smoke test (real process, real stdio round trip)
      confirmed working, not just unit-tested.
- [ ] All 9 CI checks green (Quality ×3, Windows smoke ×2, CodeQL,
      Security, Analyze ×2).
- [ ] `uv.lock` diff reviewed for new transitive deps
      (`mcp-types`, `opentelemetry-*`) — no unexpected surprises.
- [ ] `CHANGELOG.md` entry added.
- [ ] Issue #127 closed by the merged PR, referencing this plan.

## Progress

Not started — this plan captures pre-implementation research only. Phase 0
(spike) is the next action.

## Findings

_(To be filled in during Phase 0 — the two flagged unknowns above:
fd-diversion effect on graceful unwind, and the unit-test invocation
strategy.)_
