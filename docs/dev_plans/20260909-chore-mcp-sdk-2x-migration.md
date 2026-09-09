# Task: Migrate the `mcp` Python SDK from the 1.x line to 2.x

**Status**: Not Started
**Component**: server (mcp sdk)
**Assigned to**: Claude
**Priority**: High (not urgent-blocking — see Timeline)
**Branch**: `chore/mcp-sdk-2x-migration`
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
steps aren't actually separable, which is why Testing strategy below folds
the bump into Phase 0 itself rather than deferring it to the end.

**Version note (added during `/review-plan`, 2026-09-09):** PyPI's actual
latest is now `mcp` 2.2.0, not 2.1.1 — `uv lock` under the `<3.0` bound
will resolve 2.2.0. The mapping in this plan was spot-checked against
2.2.0 too and holds unchanged; target 2.2.0, not 2.1.1, throughout Phase 0.

## Timeline

No forced conflict exists yet: PyPI's latest `pipecat-ai` is still 1.8.1,
and it still requires `mcp<2.0,>=1.11.0` — same cap we have. The "Pipecat
moving to `<3`" trigger is Pipecat's *upcoming* 1.9.0, not something live
today. So this is "do it properly before we're caught flat-footed," not
"drop everything." Budget: land before 1.9.0 ships (~1 week out) so issue
#127 can be closed with a real fix rather than reopened as urgent once
users start hitting the resolver-drift risk described above.

**Decided during `/review-plan` (2026-09-09):** release ahead of
`pipecat-ai` 1.9.0 is acceptable — don't gate the PyPI publish on it. See
Compatibility policy's release-timing note for the accepted trade-off.

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

## Architecture & Call Flow

Three components change independently here, plus the external MCP client:
`cli.py` (`cli.py:815`, `serve_stdio(...)` entry point) → `transport.py`
(stdio loop, fd/watchdog threads, hard-exit timer) → `main.py`
(`create_server()`'s handlers). fd/stream ownership during `serve`:

```
cli.py: serve_stdio()
  -> transport.py: run_stdio()
       -> mcp.server.stdio.stdio_server()   # owns fd 0/1 today; Phase 0
                                             # decides explicit streams vs.
                                             # the default diversion
       -> Server.run(read_stream, write_stream, ...)
            -> on_list_tools / on_call_tool / on_ping   # main.py handlers
       -> watchdog threads (parent-death, idle) -> hard-exit timer (2.5s)
```

Request/error path for a `tools/call` (see What changes in 2.x, Risks):

```
client -> tools/call -> on_call_tool(ctx, params)
  unknown tool / any other raised exception -> code=0, message=str(exc)
  pydantic.ValidationError (bad arguments)   -> explicit catch in
                                                 on_call_tool (new, Phase 1)
                                                 -> CallToolResult(isError=True, ...)
  success                                    -> CallToolResult(...)
```

Treat this as Phase 0's spike artefact — update it once Phase 0 settles the
explicit-streams question and the exact shape of the `on_call_tool` error
boundary.

## What changes in 2.x (confirmed by reading the installed 2.1.1 source directly, re-verified against the actual-current 2.2.0 during `/review-plan` — not the docs site, see caveat below)

`mcp.server.lowlevel.Server`'s registration model was rewritten, not
versioned:

| Old (1.x) | New (2.2.0, re-confirmed from source during `/review-plan`) |
|---|---|
| `@server.list_tools()` decorator | `Server(..., on_list_tools=handler)` constructor kwarg |
| `@server.call_tool()` decorator | `Server(..., on_call_tool=handler)` constructor kwarg |
| No hook — ping handled internally | `Server(..., on_ping=handler)` constructor kwarg — first-class, same seam as the two above. Only `"initialize"` is protected against override; ping is not (the migration guide's "cannot be overridden" claim is wrong — see caveat below) |
| Handler signature `() -> list[Tool]` / `(name, args) -> list[TextContent]` | Handler signature `(ctx, params) -> ListToolsResult` / `(ctx, params) -> CallToolResult` — no more automatic return-value wrapping |
| `server.request_handlers` (public dict, keyed by request **type**) | No public dict. `server.get_request_handler(method: str)` / `server.add_request_handler(method: str, params_type, handler)`, keyed by **method string** (`"ping"`, `"tools/list"`, `"tools/call"`) — **not needed for ping any more** now that `on_ping` exists; still the right tool for any handler that has no constructor kwarg of its own |
| Unknown-tool `raise ValueError(...)` inside `call_tool` returns `CallToolResult(isError=True)` to the client as a *successful* result | **Verified empirically against a real 2.2.0 in-memory round trip (2026-09-09):** propagates as `MCPError(code=0, message=str(exception))` — the raw exception text reaches the client, wire-coded `code=0`. `mcp/shared/jsonrpc_dispatcher.py` marks this provisional: `# TODO: code=0 pins existing-server compat; JSON-RPC says INTERNAL_ERROR. Revisit`. This is the **legacy/stdio dispatch path's** behavior — the "modern HTTP entry" mentioned in that same file uses `INTERNAL_ERROR` instead, but that path doesn't apply here (this server is stdio-only). A generic handler exception (plain `RuntimeError`, tested directly) gets the *same* `code=0` + full-message treatment — the exception text is **not** lost for ordinary exceptions. |
| `types.Tool(name=..., inputSchema=...)` construction | Likely unchanged — `mcp_types`'s base model keeps `populate_by_name=True` with camelCase aliasing, so construction by the old kwarg name should still validate. Confirmed-from-source for the *mechanism*; not independently unit-tested |
| `stdio_server()` reads/writes fd 0/1 directly | `stdio_server()` now **diverts** fd 0/1 by default (stdin → `/dev/null`, stdout → dup of stderr) and reads/writes via a private duplicated fd ≥ 3 for the session's duration, restoring both on exit. **It also accepts explicit `stdin`/`stdout` `anyio.AsyncFile` arguments that skip the claim/diversion entirely** (`stdio_server(stdin=..., stdout=...)` — its docstring says "Explicit streams skip the claim," confirmed in the 2.2.0 source) — see Risks and Phase 0 step 3. |
| *(new)* pydantic `ValidationError` raised inside a handler | **Verified empirically:** classified specially, regardless of dispatch era — becomes `MCPError(code=INVALID_PARAMS, message="Invalid request parameters", data="")`. The generic message and empty `data` mean the actual field-level detail (which field, what value) is **dropped** — this is the one real client-visible regression from 1.x's `CallToolResult(isError=True, content=[TextContent(text=str(validation_error))])`, which included the full pydantic error text. See Risks. |

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
  the dict no longer exists, but more importantly it's no longer needed:
  2.2.0 exposes ping as a first-class constructor kwarg
  (`Server(..., on_ping=handler)`), the same seam as `on_list_tools`/
  `on_call_tool`. Register the idle-tracker-touching wrapper there
  directly; drop the `get_request_handler`/`add_request_handler` detour
  entirely — it was only ever a workaround for 1.x having no ping hook.
  The wrapper's own signature must still change from
  `(request: types.PingRequest) -> types.ServerResult` to
  `(ctx, params: types.RequestParams | None) -> types.EmptyResult`.
- `list_tools`/`call_tool` registration (`main.py:311,340`) — move to
  constructor kwargs, adapt handler signatures and return types per the
  table above.
- `on_call_tool`'s closure needs an explicit `except pydantic.ValidationError`
  branch that returns `CallToolResult(isError=True, content=[TextContent(type="text", text=str(e))])`
  — verified (see table above) that the SDK's own classification of
  `ValidationError` otherwise loses the field-level detail. No other
  exception class needs special handling: a bare `raise` (unknown tool, or
  any other handler exception) already reaches the client with its full
  message intact via the legacy dispatch's `code=0` path.
- Every test in `test_server.py` and the one in `test_staleness.py` that
  reads `server.request_handlers[...]` (6 sites total) — must move to
  `get_request_handler(method_string)`, and however we end up invoking the
  handler in tests (see **Unknown**, below) will also need to change
  because the callable's own signature changed from `(request)` to
  `(ctx, params)`.

### Likely
- The unknown-tool-name error path (`main.py:377`, currently untested) —
  **verified** (see table above): becomes `MCPError(code=0, message=str(ValueError(...)))`,
  not a successful `isError=True` result. Add a test pinning that exact
  shape (code + message), not a vaguer "raises something" assertion — the
  SDK marks `code=0` provisional, so a future `mcp` release could
  legitimately change it to `INTERNAL_ERROR`; a precise pin makes that
  break loud instead of silent.
- Every handler exception that is a `pydantic.ValidationError` on bad
  arguments — **verified**: becomes `MCPError(code=INVALID_PARAMS,
  message="Invalid request parameters", data="")`, losing the field-level
  detail 1.x's `isError=True` result used to carry. This needs the
  explicit catch-and-rewrap in `on_call_tool` described above; every other
  exception class does not (see table).
- `transport.py`'s `os.close(sys.stdin.fileno())` unblock trick
  (`transport.py:479-482`) is justified by a comment describing 1.x's
  direct-fd stdin reading. Under 2.x's fd-diversion, `sys.stdin.fileno()`
  after `stdio_server()` starts refers to the `/dev/null` diversion, not
  the private duplicated fd the reader thread actually blocks on — closing
  it is plausibly a no-op against the real read. **Recommended fix, not
  just a spike question**: pass explicit `stdin`/`stdout` streams to
  `stdio_server()` instead (confirmed available in 2.2.0 — "Explicit
  streams skip the claim" per its own docstring). That sidesteps the
  diversion mechanism entirely, matches `transport.py`'s existing pattern
  of owning fd lifecycle directly, and removes the need to reverse-engineer
  which fd the reader thread is really blocked on. If Phase 0 finds a
  reason explicit streams don't work here, fall back to the
  diversion-aware investigation this bullet originally described. Either
  way the process exits (the 2.5s hard-exit timer is unconditional) — what's
  actually at stake is whether the *graceful* path and its rationale
  comment describe reality, and `test_orphaned_serve_exits_via_watchdog`
  alone can't tell the two apart (see Acceptance Criteria).

### Possible / worth a quick check, not expected to bite
- `types.Tool(name=..., inputSchema=...)` construction may need no change
  at all (see table above) — verify with a single assertion in Phase 0
  rather than assuming.
- `mcp.types` moved to a standalone `mcp-types` PyPI distribution
  (version-locked to `mcp`) — new transitive dependency, should show up
  cleanly in `uv lock`; no code impact expected. The lock-diff checklist
  should expect `mcp-types`, `httpx2`, `httpcore2`, `truststore` (and
  `pywin32` on Windows) as the actually-new packages — `opentelemetry-api`/
  `sdk`/`exporter-otlp-proto-grpc` are already in `uv.lock` today via
  `chromadb`, so they add no new weight even though `mcp` now also depends
  on them directly.
- `opentelemetry-api` becomes a hard dependency of `mcp` 2.x (`Server`
  unconditionally instantiates `OpenTelemetryMiddleware` as default
  middleware, via `mcp.server._otel`). No new dependency weight (see
  above), and don't preemptively clear the default middleware to suppress
  it — that would mean depending on `Server.middleware`, which the
  installed source marks provisional, to pre-empt a risk that's already
  "possible," not "likely." Instead, add the existing runtime-egress test
  for this class of risk as an explicit Phase 4 gate (see Testing
  strategy) and only touch middleware if that gate actually fails.
- Bumping the lock introduces new/changed transitive packages with no
  automatic pre-push CVE check today. Run `just audit` as part of Phase
  0's bump step (see Testing strategy) — the ignore-list parity test
  (`tests/unit/test_audit_sync.py`) and the `starlette>=1.0.1` constraint
  in `pyproject.toml` both need to keep holding under the new resolve,
  not be discovered broken by CI after the fact.

### Unknown — a design decision, not a fact to look up
- How to invoke the new `(ctx, params)`-shaped handlers directly from unit
  tests. `ServerRequestContext` (`mcp/server/context.py`) is a plain
  dataclass — constructing a minimal one sufficient for `ctx` is a ~5-line
  fixture, not a mock of `mcp.*` internals, so it's compliant with the
  Acceptance Criteria's "no mocking of `mcp.*` internals" bullet either
  way. Two paths: (a) that small `ServerRequestContext` fixture, keeping
  today's "call the handler function directly" test style, or (b) drive
  tests through `server.run()` end-to-end against an in-memory `anyio`
  stream pair instead, which is more faithful but a bigger rewrite of
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

**Release-timing note (decided during `/review-plan`, 2026-09-09):** this
package is a peer plugin of `pipecat-ai[cli]` (`pyproject.toml:71-80`'s
`pipecat_cli.extensions` entry point, dynamically discovered — no
coordinated Pipecat release needed for the plugin mechanism itself), and
the only published `pipecat-ai` (1.8.1) pins `mcp<2.0` — Option A's
`mcp>=2.0,<3.0` bound is co-uninstallable with it via a plain resolver
conflict, until `pipecat-ai>=1.9.0` is published. **Decision: release ahead
of `pipecat-ai` 1.9.0 is acceptable** — don't gate the PyPI publish on it.
This accepts a temporary window where installing both packages together
fails to resolve for anyone not yet on `pipecat-ai` 1.9.0; merging and
running Phases 0-4 are unaffected either way.

## Files to Modify

- `src/pipecat_context_hub/server/main.py` — `create_server()`: `Server`
  construction (now including `on_ping=`), ping-wrapping trick removal,
  `list_tools`/`call_tool` registration and handler bodies, plus the new
  explicit `pydantic.ValidationError` catch in `on_call_tool` (see Risks).
- `src/pipecat_context_hub/server/transport.py` — verify `stdio_server()`/
  `server.run()`/`create_initialization_options()` call shapes (likely
  unchanged per the table above); adopt explicit `stdin`/`stdout` streams
  for `stdio_server()` per the Phase 0 spike (see Risks), rewriting the
  `os.close(sys.stdin.fileno())` comment blocks to describe whichever
  mechanism Phase 0 actually lands on.
- `src/pipecat_context_hub/cli_query.py` (`cli_query.py:403`) — hand-mirrors
  `create_server()`'s call_tool dispatch table; re-verify it still matches
  after `main.py`'s dispatch is rewritten, so the two don't silently drift.
- `tests/unit/test_server.py` — `TestToolRegistration`,
  `TestToolDispatch` classes (6+ assertions reading the old
  `request_handlers` dict), plus a docstring claim ("Unknown tool name
  raises ValueError") that currently has no backing test — add one pinning
  the verified `code=0` shape (see table above).
- `tests/unit/test_staleness.py` — one site at `test_staleness.py:161-170`
  using the same pattern plus `result.root` accessor (also gone in 2.x —
  "union types no longer RootModel" per the migration guide).
- `tests/integration/test_serve_lifetime.py` — no code changes expected
  (it drives the process externally via pipes), but it's the
  primary regression gate for the fd-diversion **startup-crash** risk (see
  Acceptance Criteria for why it's scoped to that, not the graceful-vs-
  hard-exit question too).
- `.github/workflows/ci.yml` — Windows smoke job's explicit test file list
  (`ci.yml:112-124`) excludes `test_server.py` and all of
  `tests/integration/`; add `test_server.py` (and the 2.x-safe parts of
  `test_transport.py`) so this migration's platform-sensitive fd handling
  is actually exercised on Windows, not just assumed safe.
- `pyproject.toml` — `"mcp>=1.0,<2.0"` → `"mcp>=2.0,<3.0"` (Option A). Do
  this as the **first** commit on this branch, not deferred to Phase 4 —
  see Testing strategy.
- `uv.lock` — regenerate in that same first commit; review the diff for
  `mcp-types`/`httpx2`/`httpcore2`/`truststore` (+ `pywin32` on Windows)
  additions, and confirm `starlette>=1.0.1` (`pyproject.toml:87-92`) still
  holds under the new resolve.
- `CHANGELOG.md` — `### Changed` entry once merged.

## Testing strategy

### Phase 0: Bump the dependency now, then spike the two open questions
1. Bump `pyproject.toml` to `mcp>=2.0,<3.0` and regenerate `uv.lock` as the
   **first commit** on this branch — not deferred to Phase 4. Every
   subsequent phase's local dev and CI runs use `uv sync --frozen`
   (`ci.yml:39,92,142`, `smoke-drift.yml:38`, `security-audit.yml:57`), so
   until this lands, Phase 1-3's own exit criteria ("mypy clean under 2.x
   signatures," "tests green under real mcp 2.x") can't actually run
   against 2.x at all — they'd silently keep testing against 1.x. `main.py`/
   `transport.py` aren't migrated yet at this point, so `uv run pytest`/
   `mypy` are expected to fail until Phase 1-3 land on top of this commit;
   that's normal WIP-red on a feature branch, not a merge blocker — don't
   merge until Phase 3 is green. Immediately after the bump, run
   `just audit` and confirm the pip-audit ignore-list
   (`tests/unit/test_audit_sync.py`) and the `starlette>=1.0.1` constraint
   still hold; update both in this same commit if not. Target whatever
   `uv lock` actually resolves under `<3.0` (2.2.0 as of 2026-09-09, not
   2.1.1 — re-verified during `/review-plan` that this plan's mapping
   table holds unchanged at 2.2.0). There's no separate scratch venv to
   prepare or keep fresh any more — the branch's own bumped lock is the
   spike harness for the rest of this phase.
2. Hand-write a throwaway `create_server()`-equivalent using
   `on_list_tools`/`on_call_tool`/`on_ping` kwargs (ping is a first-class
   constructor kwarg now, not a `request_handlers` patch — see the mapping
   table). Confirm it boots and answers a raw `initialize` → `tools/list`
   → `tools/call` JSON-RPC round trip over real stdio (mirror what
   `test_orphaned_serve_exits_via_watchdog` does at the subprocess level).
3. Wire that spike into a throwaway copy of `transport.py`'s `run_stdio`
   using **explicit `stdin`/`stdout` streams passed to `stdio_server()`**
   (confirmed available in 2.2.0 — skips the fd-claim/diversion mechanism
   entirely) as the primary approach, rather than relying on the
   diversion's default fd-swap. Exercise the orphan-watchdog shutdown path
   and confirm graceful unwind completes rather than falling through to
   the 2.5s hard-exit timer. If explicit streams turn out not to work here
   for some reason, fall back to investigating the default diversion path
   instead (checking what `sys.stdin.fileno()` actually refers to after
   `stdio_server()` starts). Write the answer — and which approach was
   adopted — back into this plan's Findings section before Phase 2.
4. Decide the unit-test invocation strategy (a minimal `ServerRequestContext`
   fixture vs. in-memory `server.run()`) by trying both against one
   existing test (`test_list_tools_touches_idle_tracker` is a good
   candidate — it's simple and exercises both the handler call and a side
   effect). Either is compliant with the "no mocking of `mcp.*` internals"
   Acceptance Criteria bullet — `ServerRequestContext` is a plain
   dataclass, constructing one isn't mocking anything.
5. Compatibility policy (Option A vs. B) is assumed settled (Option A) —
   only re-open with the user before Phase 1 if this phase's spike
   surfaces something that changes the recommendation.

### Phase 1: `main.py` migration
- Rewrite `create_server()` per the mapping table, including the `on_ping`
  registration and the explicit `pydantic.ValidationError` catch in
  `on_call_tool` (see Risks).
- Add the unknown-tool-name test pinning the verified shape:
  `MCPError(code=0, message=...)` with the handler's exact message text
  (not a vaguer "raises something" assertion — `code=0` is provisional
  upstream, so a precise pin makes a future SDK change loud instead of
  silent).
- Add the invalid-arguments test pinning the `on_call_tool` catch's output
  (client-visible remediation text preserved, not the SDK's generic
  "Invalid request parameters").
- Promote the Phase 0 raw-stdio round trip (`initialize` → `tools/list` →
  `tools/call`) into a permanent test asserting response shape, rather
  than leaving it a manual/throwaway spike — today's only wire-level
  coverage is a manual Phase 4 smoke step.
- `uv run mypy src/` clean under the new handler signatures (the
  `(ctx, params)` shape will need real type annotations, not
  `# type: ignore` — the old decorators carried `# type: ignore[no-untyped-call, untyped-decorator]` comments that should no longer be needed once we're calling documented, typed constructor kwargs). Scoped to `src/`
  only here — `tests/` still references the old `request_handlers`/`.root`
  attributes until Phase 3 migrates them, so a full `mypy src/ tests/` gate
  belongs in Phase 3, not here.

### Phase 2: `transport.py` migration
- Apply the Phase 0 spike's resolution for `stdio_server()` stream
  handling and the stdin-unblock trick.
- Re-run `test_orphaned_serve_exits_via_watchdog` and the rest of
  `test_serve_lifetime.py` until green under real mcp 2.x (not mocked).

### Phase 3: Test suite migration
- `test_server.py`, `test_staleness.py` per Phase 0's chosen invocation
  strategy.
- `uv run mypy src/ tests/` clean (the full gate deferred from Phase 1).
- Full local suite: `uv run pytest tests/` — must match or exceed today's
  pass count (1804 passed, 7 skipped as of `da72d0f` on `main`); any new
  skips must be justified, not silent.
- `tests/smoke/` and `tests/integration/` (this repo's smoke + e2e suites)
  green, same as the PR #129 merge gate this session already established
  as the standard bar.

### Phase 4: Live verification + release
- Live `serve` smoke test: run the actual CLI (`uv run pipecat-context-hub serve`)
  against a real client (or the raw JSON-RPC round trip used in Phase 0)
  and confirm tool listing + at least one real tool call + `get_hub_status`
  all work end-to-end, not just under test mocks.
- Full CI matrix (`Quality` ×3, `Windows smoke` ×2, `CodeQL`, `Security`,
  `Analyze` ×2) green — this migration touches process lifecycle and
  platform-sensitive fd handling, so the Windows smoke legs matter more
  than usual here (see Files to Modify for the ci.yml test-list fix
  needed for that leg to actually exercise this code at all); don't treat
  a green macOS/Linux run alone as sufficient.
- Confirm the `OpenTelemetryMiddleware` runtime-egress test (see Risks)
  passes under the real bumped dependency, not just in isolation.
- Release ahead of `pipecat-ai` 1.9.0 is fine (see Compatibility policy) —
  no extra release-timing gate here.

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

- [ ] `pyproject.toml` requires `mcp>=2.0,<3.0`, landed as Phase 0's first
      commit (not deferred to the end).
- [ ] `create_server()` and `run_stdio()` work under real mcp 2.x with no
      mocking of `mcp.*` internals beyond what today's tests already mock.
- [ ] Unknown-tool-name calls return `MCPError(code=0, message=...)` with
      the handler's exact message — pinned by a test, not left to
      whatever the SDK happens to do.
- [ ] Invalid tool arguments (`pydantic.ValidationError`) return
      client-visible remediation text via the explicit `on_call_tool`
      catch — pinned by a test — not the SDK's generic, detail-free
      "Invalid request parameters."
- [ ] Full local test suite passes with count ≥ today's baseline (1804
      passed / 7 skipped); `tests/smoke/` and `tests/integration/` both
      green.
- [ ] `test_orphaned_serve_exits_via_watchdog` passes for real (not
      skipped, not weakened) — this is the test that caught the original
      startup crash and is the regression gate for **that** failure mode.
      It does not, by itself, distinguish a graceful shutdown from the
      2.5s hard-exit fallback; if Phase 0/2 lands a stream-level
      graceful-unwind assertion too, name that test here as well.
- [ ] Live `serve` smoke test (real process, real stdio round trip)
      confirmed working, not just unit-tested.
- [ ] All 9 CI checks green (Quality ×3, Windows smoke ×2, CodeQL,
      Security, Analyze ×2), with the Windows smoke legs actually
      exercising `test_server.py` (see Files to Modify).
- [ ] `uv.lock` diff reviewed for new transitive deps (`mcp-types`,
      `httpx2`, `httpcore2`, `truststore`, `pywin32` on Windows) — no
      unexpected surprises. (`opentelemetry-*` is already in the lock via
      `chromadb`, not new.)
- [ ] `pip-audit` (`just audit`) clean or explicitly triaged against the
      bumped lock; `starlette>=1.0.1` constraint confirmed still holding.
- [ ] `CHANGELOG.md` entry added.
- [ ] Issue #127 closed by the merged PR, referencing this plan.
- [ ] PyPI release does not need to wait on `pipecat-ai` 1.9.0 (decided
      during `/review-plan` — see Compatibility policy's release-timing
      note); no extra release gate to satisfy here.

## Progress

Not started — this plan captures pre-implementation research only. Phase 0
(spike) is the next action.

## Findings

**2026-09-09, during `/review-plan`** — pre-verified two of Phase 0's
open questions empirically, against a real throwaway `mcp==2.2.0` install
(not the project's own `.venv`; no repo files touched by the spike
itself):

- **Unknown-tool / generic handler exceptions**: a raw in-memory
  `lowlevel.Server` + `ClientSession` round trip confirms an unknown tool
  name and a plain `RuntimeError` both surface identically as
  `MCPError(code=0, message=str(exception))` — the SDK's legacy/stdio
  dispatch path's catch-all, marked provisional in its own source
  (`jsonrpc_dispatcher.py`: `# TODO: code=0 pins existing-server compat;
  JSON-RPC says INTERNAL_ERROR`). The exception's message text is **not**
  lost for these cases.
- **pydantic `ValidationError`**: the same round trip, with a handler that
  raises `ValidationError` on bad arguments, returns
  `MCPError(code=INVALID_PARAMS, message="Invalid request parameters",
  data="")` — a generic message, empty `data`, field-level detail
  dropped. This is the one real client-visible regression from 1.x, and
  the one case that needs an explicit catch in `on_call_tool` (see Risks,
  Files to Modify).
- **`stdio_server()` explicit streams**: confirmed in the 2.2.0 source
  (`mcp/server/stdio.py`) that `stdio_server(stdin=..., stdout=...)`
  accepts explicit `anyio.AsyncFile` streams and "skip[s] the claim" —
  i.e., the fd-diversion mechanism is opt-in, not mandatory. Recommended
  as the primary approach for Phase 0 step 3 instead of reverse-engineering
  the default diversion's effect on the stdin-unblock trick.
- **PyPI version**: `mcp` 2.2.0 is current (not 2.1.1, which is what this
  plan's original research was run against) — the mapping table above was
  spot-checked against 2.2.0 too and holds unchanged.

Still open, as originally scoped — genuinely needs the real
`transport.py`/watchdog code running for real, not just an in-memory
round trip:

- Whether the stdin-unblock trick's graceful-unwind path actually
  completes under whichever stream-handling approach Phase 0 step 3
  adopts, versus always falling through to the 2.5s hard-exit timer.
- Final choice of unit-test invocation strategy (Phase 0 step 4).
