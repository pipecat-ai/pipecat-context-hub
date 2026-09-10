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
this plan: the initial probe installed `mcp==2.1.1` and ran our test suite
against it, unmodified, producing:

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
steps aren't actually separable, which is why the implementation checklist
below folds the bump into Phase 0 itself rather than deferring it to the end.

**Version note (added during `/review-plan`, 2026-09-09):** PyPI's actual
latest was `mcp` 2.2.0, not the 2.1.1 used by the initial probe — `uv lock`
under the `<3.0` bound was expected to resolve 2.2.0. The mapping in this
plan was spot-checked against 2.2.0 too and holds unchanged; use 2.2.0 as
the locked baseline, then explicitly probe the declared range's floor and
newest available 2.x release before claiming range compatibility.

## Timeline

At plan creation, PyPI's latest `pipecat-ai` was 1.8.1 and still required
`mcp<2.0,>=1.11.0` — the same cap we had. The "Pipecat moving to `<3`"
trigger was its *upcoming* 1.9.0, not a released dependency state. The
release-time matrix below must verify the live metadata rather than relying
on that snapshot or schedule; issue #127 should close with a real fix before
users hit the resolver-drift risk described above.

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

The direct SDK surface is concentrated in `main.py`/`transport.py` (tool
handlers only touch our own `shared/types.py` Pydantic models): `Server(...)`
construction, constructor callback registration, the old
`server.request_handlers` ping workaround, `stdio_server()` + `server.run()`
and `server.create_initialization_options()`. `cli_query.py` is a second
front door over the same application handlers and is included to prevent
dispatch drift. The external `initialize`/`initialized`, `tools/list`,
`tools/call`, and `ping` exchange is part of the supported wire surface.
Small surface — this is a contained migration, not a rewrite.

## Architecture & Call Flow

Five implementation pieces change here, plus the external MCP client:
`cli.py` (`cli.py:843`, `serve_stdio(...)` entry point) → `transport.py`
(stdio loop, fd/watchdog threads, hard-exit timer) → `main.py`
(`create_server()`'s MCP adapters), with `server/dispatch.py` owning the
shared typed registry that `cli_query.py` uses for the one-shot CLI front
door. The registry owns tool names, schemas, and uniform handler mapping;
front-door-specific behavior (`get_hub_status`, `check_deprecation`, MCP
result wrapping, staleness annotation, and CLI exit handling) remains in each
adapter. fd/stream ownership during `serve`:

```
cli.py: serve_stdio()
  -> transport.py: run_stdio()
       -> TextIOWrapper -> anyio.wrap_file()
       -> mcp.server.stdio.stdio_server(stdin=..., stdout=...)
       -> Server.run(read_stream, write_stream, ...)
            -> initialize -> initialization response
                 (name/version/instructions/capabilities)
            -> tools/list -> on_list_tools
            -> tools/call -> on_call_tool
            -> ping -> on_ping
       -> watchdog threads (parent-death, idle) -> hard-exit timer (2.5s)

cli_query.py: one-shot subcommands (search-api, status, ...)
  -> server/dispatch.py: shared typed registry
  -> cli_query.py: front-door adapter
       # in-process — no stdio_server(), no watchdogs
```

The external-client handshake is part of the contract: `initialize` reaches
`Server(...)`'s name, version, instructions, and capabilities; the client
then sends `notifications/initialized` before `tools/list` and `tools/call`.
The explicit stream adapter must keep the underlying process stdio handles
owned by the caller while the async wrappers live for the `stdio_server()`
context, and must not close or reroute those handles independently during the
watchdog path. Phase 0 verifies the exact close behavior against the locked
2.x implementation.

Request/error path for a `tools/call` (see What changes in 2.x, Risks):

```
client -> tools/call -> on_call_tool(ctx, params)
  unknown tool / any other raised exception -> SDK-specific transport error
                                                 (2.2.0 currently code=0)
  pydantic.ValidationError (bad arguments)   -> explicit catch in
                                                 on_call_tool (new, Phase 1)
                                                 -> CallToolResult(isError=True, ...)
  success                                    -> CallToolResult(...)
```

The stable application contract is the exact client-visible error message and
response reachability. The transport error code is a version-specific
compatibility canary: Phase 0 records the locked `mcp` version, package
metadata/source hash, raw initialize/list/call/error frames, and stdio
lifecycle observations in the Findings section; future lockfile updates must
rerun the same probe and update the canary deliberately.

## What changes in 2.x (provisional until the Phase 0 probe, based on the locked target and re-verified against 2.2.0 during `/review-plan` — not the docs site, see caveat below)

`mcp.server.lowlevel.Server`'s registration model was rewritten, not
versioned:

| Old (1.x) | New (2.2.0, re-confirmed from source during `/review-plan`) |
|---|---|
| `@server.list_tools()` decorator | `Server(..., on_list_tools=handler)` constructor kwarg |
| `@server.call_tool()` decorator | `Server(..., on_call_tool=handler)` constructor kwarg |
| No hook — ping handled internally | `Server(..., on_ping=handler)` constructor kwarg — first-class, same seam as the two above. Only `"initialize"` is protected against override; ping is not (the migration guide's "cannot be overridden" claim is wrong — see caveat below) |
| Handler signature `() -> list[Tool]` / `(name, args) -> list[TextContent]` | Handler signature `(ctx, params) -> ListToolsResult` / `(ctx, params) -> CallToolResult` — no more automatic return-value wrapping |
| `server.request_handlers` (public dict, keyed by request **type**) | No public dict. `server.get_request_handler(method: str)` / `server.add_request_handler(method: str, params_type, handler)`, keyed by **method string** (`"ping"`, `"tools/list"`, `"tools/call"`) — **not needed for ping any more** now that `on_ping` exists; still the right tool for any handler that has no constructor kwarg of its own |
| Unknown-tool `raise ValueError(...)` inside `call_tool` returns `CallToolResult(isError=True)` to the client as a *successful* result | **Pre-verified in the review probe and revalidated by the retained Phase 0 probe:** a 2.2.0 stdio round trip currently propagates as `MCPError(code=0, message=str(exception))`, with the raw exception text preserved. `mcp/shared/jsonrpc_dispatcher.py` marks code `0` provisional, so the stable project contract is the exact message/reachability; the numeric code remains a version-specific canary. A registered generic handler exception must be probed separately from the unknown-tool path. |
| `types.Tool(name=..., inputSchema=...)` construction | Likely unchanged — `mcp_types`'s base model keeps `populate_by_name=True` with camelCase aliasing, so construction by the old kwarg name should still validate. The retained Phase 0 probe must assert this directly in the locked and floor/latest environments. |
| `stdio_server()` reads/writes fd 0/1 directly | `stdio_server()` now **diverts** fd 0/1 by default (stdin → `/dev/null`, stdout → dup of stderr) and reads/writes via a private duplicated fd ≥ 3 for the session's duration, restoring both on exit. **It also accepts explicit `stdin`/`stdout` `anyio.AsyncFile` arguments that skip the claim/diversion entirely** (`stdio_server(stdin=..., stdout=...)` — its docstring says "Explicit streams skip the claim," confirmed in the 2.2.0 source) — see Risks and Phase 0 step 3. |
| *(new)* pydantic `ValidationError` raised inside a handler | **Pre-verified in the review probe and revalidated by the retained Phase 0 probe:** the SDK classifies it as `MCPError(code=INVALID_PARAMS, message="Invalid request parameters", data="")`, dropping field-level detail. The migration catches it explicitly and returns `CallToolResult(isError=True, content=[TextContent(type="text", text=str(e))])`, preserving the application remediation text. |

The Phase 0 probe is the reproducible implementation gate for the mapping:
it records the exact installed version, package/source identity, raw frames,
and lifecycle observations before implementation relies on any row below.
The review probe supplied initial evidence; the locked, floor, and newest
2.x runs decide whether the declared support range remains honest.

**Caveat on sourcing**: the official migration guide
(`https://py.sdk.modelcontextprotocol.io/v2/migration/`) contradicts the
installed source on two points — its code samples show a
decorator-based `@server.on_list_tools()`/`@server.on_request(...)` syntax
that does not exist anywhere in the installed package (only constructor
kwargs and `add_request_handler`/`get_request_handler` exist), and it
claims the `"ping"` handler "cannot be overridden," while the source shows
only `"initialize"` is protected. **Treat the guide as directional and the
Phase 0 probe against the locked target as ground truth** — re-run it for
every supported-version or lockfile change, since the guide mismatch and
open `<3.0` range make unverified version drift unsafe.

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
  reads `server.request_handlers[...]` (7 sites total, confirmed — includes
  `test_server.py`'s two ping tests, not only `test_staleness.py`) — must
  move to `get_request_handler(method_string)`, which returns a
  `HandlerEntry`, not a bare callable (call `.handler` on it, and note it's
  keyed by method string, not by request type); and however we end up
  invoking the handler in tests (see **Unknown**, below) will also need to
  change because the callable's own signature changed from `(request)` to
  `(ctx, params)`.

### Likely
- The unknown-tool-name error path (`main.py:377`, currently untested) —
  the locked 2.2.0 probe currently observes
  `MCPError(code=0, message=str(ValueError(...)))`, not a successful
  `isError=True` result. The stable test contract is exact message text and
  client-visible error reachability; retain a version-scoped code `0` canary
  for the locked target and fail the compatibility probe if a future SDK
  changes the mapping.
- Every handler exception that is a `pydantic.ValidationError` on bad
  arguments — **verified**: becomes `MCPError(code=INVALID_PARAMS,
  message="Invalid request parameters", data="")`, losing the field-level
  detail 1.x's `isError=True` result used to carry. This needs the
  explicit catch-and-rewrap in `on_call_tool` described above. Registered
  handler exceptions of other classes intentionally retain the SDK's current
  transport-error behaviour; the permanent wire test must pin their exact
  message/reachability without treating the provisional numeric code as an
  application API.
- `transport.py`'s `os.close(sys.stdin.fileno())` unblock trick
  (`transport.py:479-482`) is justified by a comment describing 1.x's
  direct-fd stdin reading. Under 2.x's default fd-diversion,
  `sys.stdin.fileno()` after `stdio_server()` starts refers to the `/dev/null`
  diversion, not the private duplicated fd the reader thread blocks on —
  closing it is plausibly a no-op against the real read. **Recommended fix,
  not just a spike question**: build UTF-8 `TextIOWrapper` instances over
  `sys.stdin.buffer`/`sys.stdout.buffer`, wrap them with
  `anyio.wrap_file()`, and pass those async files as explicit `stdin=` and
  `stdout=` to `stdio_server()`. Keep the wrappers alive for the server
  context and leave the process stdio handles caller-owned; do not combine
  this with a `dup2(2, 1)` diversion. If Phase 0 finds a reason explicit
  streams don't work here, fall back to a diversion-aware investigation and
  document which actual descriptor is closed. Either way the process exits
  (the 2.5s hard-exit timer is unconditional) — what is at stake is whether
  the *graceful* path and its rationale comment describe reality. The
  lifetime regression must distinguish the graceful `parent_died` log from
  the hard-exit fallback, because process disappearance alone cannot do so.

### Possible / worth a quick check, not expected to bite
- `types.Tool(name=..., inputSchema=...)` construction may need no change
  at all (see table above) — verify with a single assertion in Phase 0
  rather than assuming.
- `mcp.types` moved to a standalone `mcp-types` PyPI distribution
  (version-locked to `mcp`) — new transitive dependency, should show up
  cleanly in `uv lock`; no code impact expected. The lock-diff checklist
  should expect `mcp-types`, `httpx2`, `httpcore2`, `truststore` as the
  actually-new packages (`pywin32` is already present in `uv.lock` on every
  host today via an existing dependency, not newly added by this bump, and
  not Windows-only in the lock file) — `opentelemetry-api` is the only
  piece of `opentelemetry-*` that `mcp` 2.2.0 declares directly; `sdk`/
  `exporter-otlp-proto-grpc` are already in `uv.lock` today via
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
  0's bump step (see Implementation Checklist) — the ignore-list parity test
  (`tests/unit/test_audit_sync.py`) and the `starlette>=1.0.1` constraint
  in `pyproject.toml` both need to keep holding under the new resolve,
  not be discovered broken by CI after the fact.

### Unknown — a design decision, not a fact to look up
- How to invoke the new `(ctx, params)`-shaped handlers directly from unit
  tests. `ServerRequestContext` (`mcp/server/context.py`) is a `kw_only`
  dataclass that requires a real `ServerSession` to construct — **not
  confirmed to be a trivial ~5-line fixture**; Phase 0 needs to spike
  whether a minimal `ctx` is actually cheap to build or needs a
  stand-in/mock `ServerSession`. Either way it's compliant with the
  Acceptance Criteria's "no mocking of `mcp.*` internals" bullet as long as
  any stand-in mocks `ServerSession`, not the handler-dispatch machinery
  itself. Two paths: (a) that `ServerRequestContext` fixture (however heavy
  it turns out to be), keeping today's "call the handler function
  directly" test style, or (b) drive tests through `server.run()`
  end-to-end against an in-memory `anyio` stream pair instead, which is
  more faithful but a bigger rewrite of `test_server.py`. **Decide in
  Phase 0**, informed by which one actually lets the existing assertions
  (idle-tracker touch on list/call, ping
  passthrough) survive with the least structural change.

## Compatibility policy — accepted clean cutover with a version-scoped canary

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

**Decision recorded after grilling:** accept Option A. Implement one 2.x code
path and keep the stable application contract explicit: exact error message,
response reachability, validation-detail preservation, and clean JSON-RPC
stdout. The observed `MCPError` numeric code is a canary for the locked
`mcp==2.2.0` resolve, not a promise for every future release in `<3.0`.
Every lockfile or supported-version change reruns the retained Phase 0 probe;
if the SDK maps the same application error differently, update the
version-scoped canary and compatibility notes only after confirming that the
stable application contract still holds. A support-matrix/resolver check at
release time records which `pipecat-ai` versions co-install with this bound.

**Release-timing note (decided during `/review-plan`, 2026-09-09):** this
package is a peer plugin of `pipecat-ai[cli]` (`pyproject.toml:71-80`'s
`pipecat_cli.extensions` entry point, dynamically discovered — no coordinated
Pipecat release needed for the plugin mechanism itself). The currently
published Pipecat metadata and the eventual release metadata must be checked
at sign-off rather than treated as a schedule fact: if a supported
`pipecat-ai` release still pins `mcp<2.0`, this package's `mcp>=2.0,<3.0`
bound cannot co-install with it through a plain resolver. **Decision: release
ahead of `pipecat-ai` 1.9.0 is acceptable** — don't gate the PyPI publish on
that upstream release. Record the actual compatibility matrix and resolver
result in Findings so the temporary incompatibility is visible to users and
can be removed when upstream's metadata changes.

## Files to Modify

- `src/pipecat_context_hub/server/main.py` — `create_server()`: `Server`
  construction (now including `on_ping=`), ping-wrapping trick removal,
  `list_tools`/`call_tool` registration and handler bodies, plus the new
  explicit `pydantic.ValidationError` catch in `on_call_tool` (see Risks).
- `src/pipecat_context_hub/server/transport.py` — verify `stdio_server()`/
  `server.run()`/`create_initialization_options()` call shapes; adopt the
  explicit UTF-8 `TextIOWrapper` → `anyio.wrap_file()` streams confirmed in
  Phase 0, or document the evidence for the diversion-aware fallback if the
  probe rejects them. Keep wrappers alive for the `stdio_server()` context,
  leave process stdio caller-owned, and rewrite the
  `os.close(sys.stdin.fileno())` comments to describe the actual unblock
  mechanism. Do not add a `dup2(2,1)` fallback: with explicit streams,
  stderr-only logging discipline is the wire-cleanliness contract. The
  permanent integration test covers clean stdout and a non-ASCII round trip.
- `tests/unit/test_transport.py` — the four `TestRunStdioWatchdogWiring`
  tests (`test_transport.py:354,395,436,491`) patch `transport.stdio_server`
  with a zero-arg `fake_stdio_server()`; once `run_stdio` passes
  `stdin=`/`stdout=` these fakes need to accept those kwargs (a
  `_stdio_streams()` factory seam is one way to keep this to a single
  patch point) — this file was missing from Files to Modify.
- `src/pipecat_context_hub/server/dispatch.py` (**new**) — one typed registry
  for tool names, input schemas, and shared handler lookup consumed by both
  `main.py` and `cli_query.py`; keep MCP result wrapping, staleness metadata,
  and CLI exit handling in their respective front-door adapters.
- `src/pipecat_context_hub/cli_query.py` — consume the shared dispatch
  registry instead of maintaining a hand-mirrored call table. Preserve its
  special `get_hub_status`/`check_deprecation` paths and CLI-only error,
  staleness, and exit handling.
- `tests/unit/test_server.py` — `TestToolRegistration`,
  `TestToolDispatch` classes (7 sites reading the old `request_handlers`
  dict — includes the two ping tests, not only `test_staleness.py`), plus
  update direct invocations to the selected typed-context fixture or real
  in-memory `server.run()` path. Add dispatcher-level pins for unknown
  tools (exact message plus a code `0` canary for locked 2.2.0), registered
  generic `RuntimeError` (same stable message/reachability contract), and
  `pydantic.ValidationError` (successful result with `isError=True`,
  `TextContent`, and preserved field detail). Also assert
  `get_hub_status` presence/absence in `tools/list`, and that
  `idle_tracker.end()` runs when `on_call_tool` raises.
- `tests/unit/test_staleness.py` — one site at `test_staleness.py:161-170`
  using the selected 2.x result accessor after Phase 0 confirms the model
  shape; retain the existing staleness assertions.
- `tests/unit/test_cli_query.py` — parity assertions that the CLI's exposed
  command/tool set and schema names come from the shared registry, without
  asserting the CLI's front-door-specific output handling is identical to
  MCP.
- `tests/integration/test_mcp_v2_compat.py` (**new**) — permanent real
  subprocess/stdio contract test: initialize and initialized notification,
  exact `tools/list` names and schemas, ping, representative calls for every
  shared handler, `get_hub_status` with and without a store,
  `check_deprecation`, validation/unknown/generic error paths, clean JSON-RPC
  stdout, and non-ASCII payload round trip. Keep it pipe-based and portable
  so the Windows smoke job can run it explicitly.
- `tests/integration/test_serve_lifetime.py` — modify the external watchdog
  test to capture stderr and assert the deterministic graceful
  `Shutting down: parent_died ...` marker is present, the exact hard-exit
  marker (`pipecat-context-hub: client gone; fast-exiting after`) is absent,
  and exit completes within a bound below the 2.5s hard-exit timer; retain
  the startup-crash regression assertion.
- `scripts/probe_mcp_2x.py` (**new**) — retained, reproducible Phase 0
  diagnostic that prints the locked SDK version, package metadata/source
  identity, raw initialize/list/call/ping/error frames, and stream/watchdog
  lifecycle observations. It is the required first step after every lockfile
  or supported-version change, not an unrecorded throwaway experiment.
- `.github/workflows/ci.yml` — Windows smoke job's explicit test file list
  — add `test_server.py`, the 2.x-safe unit transport coverage, and the
  portable `test_mcp_v2_compat.py` to the Windows smoke command. Verify the
  actual workflow jobs at sign-off; do not name checks that do not exist in
  this repository.
- `pyproject.toml` — `"mcp>=1.0,<2.0"` → `"mcp>=2.0,<3.0"` (Option A). Do
  this as the **first** commit on this branch, not deferred to Phase 4 —
  see the Implementation Checklist.
- `uv.lock` — regenerate in that same first commit; review the diff for
  `mcp-types`/`httpx2`/`httpcore2`/`truststore` additions or changes, and
  confirm `starlette>=1.0.1` still holds under the new resolve.
- `CHANGELOG.md` — add the migration under `### Changed` in `[Unreleased]`
  in the migration PR, before the final CI/release verification; do not defer
  it until after merge.

## Implementation Checklist

The phase blocks below are the conduct execution contract. The detailed
design rationale and acceptance criteria remain part of the same reviewed
plan; each phase declares its write scope, test scope, test command, and
validation command so autonomous conduct can route workers and failures
deterministically.

### Phase 0: Bump the dependency now, then spike the two open questions
**Goal:** Establish the frozen 2.x dependency and capture reproducible SDK,
stdio, encoding, and watchdog evidence before implementation begins.

**Impl files:** pyproject.toml, uv.lock, scripts/probe_mcp_2x.py

**Test files:** tests/unit/test_audit_sync.py

**Test command:** `just audit`

**Validation cmd:** `uv run python scripts/probe_mcp_2x.py --matrix`

1. Bump `pyproject.toml` to `mcp>=2.0,<3.0` and regenerate `uv.lock` as the
   **first commit** on this branch — not deferred to Phase 4. Every
   subsequent phase's local dev and CI runs use `uv sync --frozen`
   (`ci.yml:39,92,142`, `smoke-drift.yml:38`, `security-audit.yml:57`), so
   until this lands, Phase 1-3's exit criteria cannot actually run against
   2.x; they would silently keep testing 1.x. `main.py`/`transport.py` are
   expected to be WIP-red immediately after this dependency-only commit;
   that is a feature-branch checkpoint, not a merge state. Run `just audit`
   immediately, confirm the `tests/unit/test_audit_sync.py` ignore-list and
   `starlette>=1.0.1` constraint, and update them in the same commit if the
   new resolve requires it. Record the exact resolved `mcp` version rather
   than assuming 2.2.0; the retained probe below is the source of truth.
2. Run the retained `scripts/probe_mcp_2x.py` against the frozen environment.
   It must exercise `types.Tool(name=..., inputSchema=...)`, construct a
   real low-level `Server` with `on_list_tools`/`on_call_tool`/`on_ping`, and
   capture raw `initialize` → `notifications/initialized` → `tools/list` →
   `tools/call` → `ping` frames, including unknown-tool, registered generic
   exception, and Pydantic validation-error cases. Record the locked version,
   package metadata/source hash, exact frames, and observed error codes in
   Findings; do not make the implementation depend on an unrecorded local
   experiment. Because the declared range includes more than the lock's
   single version, also run the same probe against the supported floor
   (`mcp==2.0.*`) and the newest available 2.x release in isolated `uv run
   --with` environments. If either fails the stable application contract,
   narrow the lower/upper bound or fix the implementation before Phase 1;
   do not claim `<3.0` compatibility from a 2.2.0-only result.
3. Exercise the real `transport.py` stdio/watchdog shape in the probe using
   explicit UTF-8 `TextIOWrapper` → `anyio.wrap_file()` streams passed to
   `stdio_server(stdin=..., stdout=...)`. Verify wrapper lifetime, caller
   ownership, clean JSON-RPC stdout, non-ASCII round trip, orphan-watchdog
   graceful unwind, and the absence/presence of the deterministic shutdown
   markers. The Windows run must execute with UTF-8 mode disabled and a
   non-UTF-8 console/code-page setting (record the effective encoding), then
   round-trip representative non-ASCII text such as `é` and `東京` as raw
   UTF-8 JSON. If explicit streams fail, investigate the default diversion
   and record the actual descriptor and fallback rationale before Phase 2.
4. Select the unit seam by trying one existing idle-tracker assertion with a
   documented `ServerRequestContext` construction and one with an in-memory
   `server.run()` stream pair. Prefer the smallest seam that uses public
   2.x types; do not mock `mcp.*` dispatch internals. Use the in-memory
   transport for wire-shape assertions even if direct callback invocation is
   retained for cheap side-effect tests, and record the final choice in
   Findings.
5. Option A is already accepted. Re-open it only if the Phase 0 probe finds
   a technical incompatibility that changes the recommendation; a temporary
   Pipecat resolver conflict is an accepted release trade-off, not a reason
   to add a dual implementation.

### Phase 1: `main.py` migration
**Goal:** Replace the 1.x registration surface with typed 2.x callbacks and
make MCP and CLI dispatch consume one explicit registry while preserving
application-level error semantics.

**Impl files:** src/pipecat_context_hub/server/main.py, src/pipecat_context_hub/server/dispatch.py, src/pipecat_context_hub/cli_query.py

**Test files:** tests/unit/test_server.py, tests/unit/test_cli_query.py, tests/unit/test_staleness.py

**Test command:** `uv run pytest tests/unit/test_server.py tests/unit/test_cli_query.py tests/unit/test_staleness.py -q`

**Validation cmd:** `uv run mypy src/`

- Rewrite `create_server()` per the mapping table, including the `on_ping`
  registration and the explicit `pydantic.ValidationError` catch in
  `on_call_tool` (see Risks).
- Introduce the shared typed dispatch registry in `server/dispatch.py` and
  migrate `cli_query.py` to consume it in the same phase as `main.py`.
  Include a parity test for names, schemas, and handler lookup; keep CLI
  formatting, staleness annotation, and exit codes front-door-specific.
- Add dispatcher-level tests for unknown tools, registered generic handler
  exceptions, and invalid arguments. Pin exact application messages and
  reachability; pin `MCPError(code=0)` only as the locked 2.2.0 canary, and
  fail loudly if a future supported SDK changes that mapping. Pin the
  validation result as a successful `CallToolResult` with `isError=True`,
  `TextContent`, and preserved field-level remediation detail.
- Migrate the seven `request_handlers` assertions in `test_server.py` and
  the staleness test's accessor if they are needed to exercise the new
  registration seam; use the Phase 0 public-type fixture decision.
- `uv run mypy src/` clean under the new handler signatures (the
  `(ctx, params)` shape will need real type annotations, not
  `# type: ignore` — the old decorators carried `# type: ignore[no-untyped-call, untyped-decorator]` comments that should no longer be needed once we're calling documented, typed constructor kwargs). Scoped to `src/`
  only here because transport and remaining test migrations are Phase 2/3
  work. The full `mypy src/ tests/` gate belongs in Phase 3, not Phase 1.
- **Exit criterion:** `create_server()` and the shared registry are
  type-checkable under the frozen 2.x dependency, unit dispatcher/error and
  CLI-parity tests pass, and no test silently reads the removed 1.x public
  `request_handlers`/result-root API. The retained wire test is promoted in
  Phase 2 after `transport.py` is bootable.

### Phase 2: `transport.py` migration
**Goal:** Make real mcp 2.x stdio transport and watchdog shutdown reliable,
portable, and observable through a permanent wire-level regression test.

**Impl files:** src/pipecat_context_hub/server/transport.py

**Test files:** tests/unit/test_transport.py, tests/integration/test_mcp_v2_compat.py, tests/integration/test_serve_lifetime.py

**Test command:** `uv run pytest tests/unit/test_transport.py tests/integration/test_mcp_v2_compat.py tests/integration/test_serve_lifetime.py -q`

**Validation cmd:** `uv run ruff check src/pipecat_context_hub/server/transport.py tests/`

- Apply the Phase 0 resolution for `stdio_server()` stream handling and the
  stdin-unblock trick. The preferred implementation is explicit UTF-8
  `TextIOWrapper` streams wrapped with `anyio.wrap_file()`, kept alive for
  the `stdio_server()` context, with process stdio left caller-owned.
- Update the four patched stdio fakes in `tests/unit/test_transport.py` in
  this same phase so their signatures accept the explicit stream kwargs; do
  not defer test-double migration to the final suite phase.
- Add and run `tests/integration/test_mcp_v2_compat.py` as a permanent real
  subprocess/stdio test. Assert the initialize handshake and
  `notifications/initialized`, exact `tools/list` names and input schemas,
  ping, every shared handler at least once, both `get_hub_status` branches,
  `check_deprecation`, validation/unknown/generic error paths, clean JSON-RPC
  stdout, and a non-ASCII payload round trip. Assert the locked 2.2.0 error
  code only as a version-scoped canary; stable assertions cover message and
  reachability. Run this test on the Windows smoke leg as well as POSIX.
- Modify `test_serve_lifetime.py` to capture stderr and distinguish graceful
  `Shutting down: parent_died ...` from the exact
  `pipecat-context-hub: client gone; fast-exiting after` hard-exit fallback.
  Keep the startup-crash assertion and bound graceful completion below the
  fallback timer.
- Re-run `test_orphaned_serve_exits_via_watchdog` and the remaining lifetime
  tests against real mcp 2.x, then run the existing regression set:
  `test_end_to_end.py`, `test_report_hint_e2e.py`, and
  `test_concurrent_model_load.py`.

### Phase 3: Test suite migration
**Goal:** Finish all 2.x test and CI wiring, then prove the complete local
quality gate and test-collection invariant against the base revision.

**Impl files:** tests/unit/test_server.py, tests/unit/test_staleness.py, tests/unit/test_transport.py, .github/workflows/ci.yml

**Test files:** tests/unit/test_server.py, tests/unit/test_staleness.py, tests/unit/test_transport.py

**Test command:** `uv run pytest tests/ -q`

**Validation cmd:** `uv run ruff check src/ tests/ && uv run mypy src/ tests/`

- Finish `test_server.py`, `test_staleness.py`, and `test_transport.py`
  migration, including the four `TestRunStdioWatchdogWiring` fakes and the
  selected public 2.x callback/context seam.
- Run the full local quality gate: `uv run ruff check src/ tests/`,
  `uv run mypy src/ tests/`, and `uv run pytest tests/ -q`.
- Compare collected pytest node IDs against the base revision to prove no
  tests disappeared. The historical reference is 1804 passed / 7 skipped as
  of `da72d0f` on `main`, but counts alone are not a gate: every new skip
  needs a named node ID and written reason, and every removed/renamed test
  needs an intentional explanation.
- Run `uv run pytest tests/smoke/ -v` and the relevant integration suite,
  including `test_mcp_v2_compat.py`, `test_serve_lifetime.py`,
  `test_report_hint_e2e.py`, and `test_concurrent_model_load.py`.

### Phase 4: Live verification + release
**Goal:** Verify the final packaged dependency, version metadata, live stdio
smoke path, security checks, resolver matrix, and actual repository CI before
release.

**Impl files:** pyproject.toml, src/pipecat_context_hub/server/main.py, uv.lock, CHANGELOG.md

**Test files:** tests/unit/test_server.py, tests/integration/test_no_telemetry_egress.sh

**Test command:** `uv run pytest tests/unit/test_server.py -q`

**Validation cmd:** `just check && just test && just audit && bash tests/integration/test_no_telemetry_egress.sh`

- Live `serve` smoke test: run the actual CLI (`uv run pipecat-context-hub serve`)
  against a real client (or the raw JSON-RPC round trip used in Phase 0)
  and confirm tool listing + at least one real tool call + `get_hub_status`
  all work end-to-end, not just under test mocks.
- Before the final gate, set `[project].version` and `_SERVER_VERSION`
  together, add the `[Unreleased]` `CHANGELOG.md` entry in the migration PR,
  regenerate `uv.lock`, and review its root metadata and transitive changes.
- Run the actual repository CI jobs: Quality on Python 3.12 and 3.14, the
  aggregate Quality gate, Windows smoke on Python 3.12 and 3.14 (including
  `test_server.py`, transport unit coverage, and the portable compatibility
  test), and Security. The repository has no CodeQL or Analyze workflow;
  scheduled workflows are not substitutes for these PR checks. Verify the
  actual job names/statuses at sign-off rather than using a fixed count.
- Confirm the `OpenTelemetryMiddleware` runtime-egress test (see Risks) —
  `tests/integration/test_no_telemetry_egress.sh` — passes under the real
  bumped dependency, not just in isolation. This script is not currently
  wired into `just`/CI, so run it explicitly by name and record the output
  in this plan's Findings section; treat it as a manual release-gate check
  until it is wired into a local CI recipe, not an enforced CI gate.
- Verify the version consistency test and run a package-resolver matrix
  against the actual supported `pipecat-ai` metadata. Record whether the
  temporary `<2.0`/`>=2.0` conflict remains; it is accepted and documented,
  not silently ignored.
- Run the live MCP smoke and telemetry-egress check after the final lock and
  version changes. Publish only after those checks and the actual CI jobs
  are green; release timing does not wait for a speculative Pipecat 1.9.0
  date.

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
- [ ] The application error contract is stable: unknown and registered
      generic failures preserve their exact message and remain client-
      reachable; `MCPError(code=0)` is pinned only as the locked 2.2.0
      compatibility canary, not promised for every future `<3.0` release.
- [ ] Invalid tool arguments (`pydantic.ValidationError`) return
      client-visible remediation text via the explicit `on_call_tool`
      catch as a successful `CallToolResult(isError=True)` containing
      `TextContent` — pinned by a real dispatcher-level test, not the SDK's
      generic, detail-free "Invalid request parameters."
- [ ] The shared dispatch registry is consumed by both MCP and CLI front
      doors, with a parity test preventing tool/schema drift.
- [ ] Full local quality gate passes: `ruff check`, `mypy src/ tests/`, and
      `pytest tests/ -q`; smoke and relevant integration suites are green.
      Collected pytest node IDs are compared with base: no tests disappear,
      and every new skip has a named test and written reason. The historical
      reference is 1804 passed / 7 skipped, but raw counts are not sufficient.
- [ ] `test_orphaned_serve_exits_via_watchdog` passes for real (not
      skipped, not weakened) — this is the test that caught the original
      startup crash and is the regression gate for **that** failure mode.
- [ ] The lifetime test captures stderr and proves graceful
      `parent_died` shutdown, absence of the exact
      `pipecat-context-hub: client gone; fast-exiting after` fallback marker,
      and completion below the 2.5s hard-exit timer; it does not merely
      observe process disappearance.
- [ ] `tests/integration/test_mcp_v2_compat.py` performs a real stdio
      initialize/list/call/ping round trip and covers exact tool names and
      schemas, every shared handler, both status branches, deprecation,
      validation/unknown/generic errors, clean JSON-RPC stdout, and a
      non-ASCII payload. It runs on the Windows smoke legs.
- [ ] Live `serve` smoke test (real process, real stdio round trip)
      confirmed working, not just unit-tested.
- [ ] The actual repository CI jobs are green: Quality Python 3.12/3.14,
      aggregate Quality, Windows smoke Python 3.12/3.14, and Security.
      Windows runs include registration, transport-unit, and portable
      compatibility coverage; no nonexistent CodeQL/Analyze checks are
      required.
- [ ] `uv.lock` diff reviewed for new transitive deps (`mcp-types`,
      `httpx2`, `httpcore2`, `truststore`) and root metadata — no unexpected
      surprises; `starlette>=1.0.1` still holds.
- [ ] `pip-audit` (`just audit`) clean or explicitly triaged against the
      bumped lock and the audit ignore-list remains synchronized.
- [ ] `pyproject.toml`'s `[project].version` and `main.py::_SERVER_VERSION`
      bumped together in the release commit; `TestVersionConsistency` green.
- [ ] `CHANGELOG.md` entry added under `[Unreleased]` in the migration PR.
- [ ] The retained Phase 0 probe is rerun for every lockfile or supported-
      version change, with version/source identity, raw frames, error canary,
      and stream ownership/lifecycle evidence recorded in Findings.
- [ ] The release-time `pipecat-ai` resolver matrix is recorded, including
      any temporary `<2.0` conflict; release is not blocked on an unverified
      Pipecat 1.9.0 schedule.
- [ ] Issue #127 closed by the merged PR, referencing this plan.

<!-- reviewed: 2026-09-10 @ 5a6b0c1001a1a36808a85432dedf739778181bd9 -->

## Progress

Phase 0 complete — the dependency is locked to the 2.x line and the retained
probe passes the locked, floor, and newest in-range SDK environments.

- [x] Phase 0: Bump the dependency now, then spike the two open questions
- [x] Phase 1: `main.py` migration
- [ ] Phase 2: `transport.py` migration
- [ ] Phase 3: Test suite migration
- [ ] Phase 4: Live verification + release

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

**2026-09-10, accepted review repairs:** the plan now treats the clean
cutover as decided, separates stable application error semantics from the
locked SDK's provisional numeric code, and requires a retained probe plus
floor/latest 2.x compatibility checks. It assigns the shared MCP/CLI
dispatch registry and all affected unit migrations to the implementation
phases, promotes the raw stdio exercise to a permanent portable integration
test, and makes graceful watchdog evidence deterministic. Release edits now
precede the final gate; the CI list reflects the workflows actually present
in this repository, and the Pipecat resolver relationship is recorded as a
release-time matrix rather than an unverified schedule assumption.

**2026-09-10, Phase 0 conduct run:** `pyproject.toml` now requires
`mcp>=2.0,<3.0`, with the lock resolving `mcp==2.2.0` and matching
`mcp-types`. `scripts/probe_mcp_2x.py --matrix` passed in the locked
environment, the supported floor (`mcp==2.0.1`), and the newest available
in-range 2.x environment (`mcp==2.2.0`). The probe records package/source
identity and real `Server.run` frames for initialize, initialized, tools/list,
tools/call, ping, unknown-tool, generic-exception, and validation-error paths;
the locked error canaries were code `0` for unknown/generic exceptions and
`-32602` with the SDK's generic message for validation. Its explicit-stdio
round trip preserved UTF-8 `é 東京`, reported `c3a920e69db1e4baac`, left caller
stdio objects unchanged, and returned cleanly on EOF. `just audit` and Ruff
passed. The final unit-test invocation seam and hub watchdog evidence remain
Phase 1/2 work; the retained probe covers SDK transport lifecycle only.

**2026-09-10, Phase 1 conduct run:** `create_server()` now registers typed
2.x constructor callbacks, `ValidationError` is returned as an
`isError=True` `CallToolResult` with field detail, and MCP plus CLI consume the
same `TOOL_REGISTRY`. The targeted suite passed 125 tests and `mypy src/`
passed. Unit tests invoke the public `Server.get_request_handler()` entries
directly with `ctx=None` because the handlers do not read request context; the
real `Server.run` context and stdio path are reserved for Phase 2's portable
integration test. This is a deliberate small unit seam, not a mock of MCP
dispatch internals.
