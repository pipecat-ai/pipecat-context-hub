# Agents Guide

Project conventions and decisions for AI coding agents working on this codebase.

## Pre-Merge Live MCP Smoke Test

Before merging any PR that touches retrieval, tool handlers, index backends,
or types, reconnect the MCP server and run these queries against the live
local index. Unit tests mock the retrieval layer and cannot catch page
assembly, filter semantics, schema issues, or stale tool metadata that only
surface against real indexed data.

**When a live failure is found in the field, freeze it twice:** a unit
regression test (with the real input text frozen into the test) *and* the
failing query added here as a numbered item — so any future change to the
harness gets re-checked against the exact query that once broke.

1. `get_hub_status()` — returns a non-empty index and a recent
   `last_refresh_at`, so smoke-test failures are not caused by a stale or empty
   local corpus
2. `get_doc(path="/api-reference/server/frames/system-frames")` — returns
   full multi-chunk page (not a single 500-char chunk), confidence 1.0
3. `get_doc(path="/api-reference/server/frames/system-frames", section="StartFrame")`
   — returns only the StartFrame section from the assembled page
4. `get_doc(doc_id=<id from a search_docs result>)` — returns non-empty content
   and is not `Not Found`
5. `get_doc(path="")` and `get_doc(doc_id="")` — both raise validation errors
6. `get_doc(doc_id="", path="/api-reference/server/frames/system-frames")` —
   falls back to the path lookup and returns the assembled page
7. `search_api("send_dtmf", class_name="DailyTransport")` — returns
   `DailyTransportClient.send_dtmf` (prefix match)
8. `search_examples("TTS pipeline", domain="backend")` — returns hits with
   backend-style example paths, not unrelated frontend/client files
9. `search_docs("TTS + STT")` — multi-concept returns hits for both concepts
10. `list_tools()` — `get_doc` mentions path lookup, and `get_code_snippet` /
    `search_api` describe `class_name` as a prefix match and list
    `type_definition` in chunk_type
11. `search_api("DialoutSendDtmfSettings", chunk_type="type_definition")` —
    returns the Daily SDK dict schema with field keys
12. `search_api("send_dtmf settings")` — returns method signatures.
    Note: `DialoutSendDtmfSettings` type_definition does not yet surface
    in mixed queries via embedding similarity alone. Use
    `chunk_type="type_definition"` for direct lookup.
13. `get_code_snippet(symbol="CallClient.send_dtmf")` — returns method
    signature with `related_type_defs: ["DialoutSendDtmfSettings"]` linking
    to the dict schema
14. `search_api("PipecatClient")` — returns TS hits from
    `pipecat-ai/pipecat-client-web` (not only Python module overviews)
15. `search_api("WebSocketTransport")` — returns TS class extending
    `Transport` from `pipecat-ai/pipecat-client-web-transports`
16. `search_api("RTVIEvent")` — returns TS type/enum from
    `pipecat-ai/pipecat-client-web`
17. `search_api("VoiceVisualizer React component typescript")` — returns TS
    React component from `pipecat-ai/voice-ui-kit` or `pipecat-ai/pipecat-client-web`.
    Also try bare `search_api("VoiceVisualizer")` — currently requires the
    qualifier to rank above Python hits, but should improve as retrieval
    quality improves (cross-encoder, corpus weighting). If the bare query
    starts passing, that's a positive signal.
18. `search_api("PipecatClientOptions")` — a bare query may rank
    `PipecatClientOptions`-referencing method/const chunks (e.g.
    `MediaManager.setClientOptions`, `Transport.initialize`) ahead of the
    interface declaration itself — expected per the bare-query note below,
    not a blocker. Use `chunk_type="class_overview"` (test 26) for the
    reliable check that the TS interface from `pipecat-ai/pipecat-client-web`
    is indexed. (Note: `search_api` hits don't carry a `language` field —
    that's only on `get_code_snippet`/`get_example` results.)
19. `search_api("SmallWebRTCTransport")` — returns TS hits from
    `pipecat-ai/pipecat-client-web-transports` or `pipecat-ai/voice-ui-kit`
20. `search_docs("pipecat-client-ios")` — returns at least one hit from an
    iOS SDK repo (README fallback for zero-code-chunk repos)
21. `search_api("PipecatClientProvider")` — a bare query may rank other
    files that merely *import* `PipecatClientProvider` (e.g.
    `PipecatAppBase`, Ladle story `Provider` consts) ahead of the actual
    definition — same bare-query ranking behavior as test 18, extended to
    `chunk_type="function"` (const/arrow-component exports), not previously
    called out below. For the reliable check that the const export from
    `pipecat-ai/pipecat-client-web` is indexed with its full arrow-function
    body (not truncated at the parameter list), use
    `search_api("PipecatClientProvider", chunk_type="function")` or
    `get_code_snippet(symbol="PipecatClientProvider")` — both return it as
    the top/only hit.
22. `search_api("SmallWebRTCTransport", class_name="SmallWebRTCTransport")` —
    returns TS class from `pipecat-ai/pipecat-client-web-transports` (verifies
    nested-package TS detection for `pipecat-prebuilt`, formerly
    `small-webrtc-prebuilt` — renamed in PR #67)
23. `search_api("connect", class_name="PipecatClient")` — returns TS method
    chunk with `method_signature` from `pipecat-ai/pipecat-client-web`
    (Phase 2 tree-sitter method extraction)
24. `search_api("initialize", class_name="Transport")` — returns TS abstract
    method from `pipecat-ai/pipecat-client-web` (verifies abstract method
    extraction and no _MIN_METHOD_LINES filtering)
25. `search_api("WebSocketTransport", chunk_type="class_overview")` — returns
    the TS class declaration (not just method chunks). Verifies class-level
    chunks still rank when method extraction adds many per-class hits.
26. `search_api("PipecatClientOptions", chunk_type="class_overview")` —
    returns the TS interface declaration from `pipecat-ai/pipecat-client-web`.
    Same ranking-stability check as test 25.
27. `get_code_snippet(symbol="PipecatClient.connect")` — returns the TS
    method snippet with full `method_signature` (end-to-end symbol lookup
    for TS method chunks, not just search_api ranking)
28. `search_api("connected", class_name="PipecatClient")` — returns the TS
    getter chunk from `pipecat-ai/pipecat-client-web` (verifies getter
    extraction — a separate code path from regular methods)
29. `search_api("constructor", class_name="PipecatClient")` — returns the
    constructor chunk with full signature `(options: PipecatClientOptions)`

Note on bare TS symbol queries (e.g., `search_api("WebSocketTransport")`
without `chunk_type` or `class_name` filters): after Phase 2 method
extraction, method/getter chunks may rank ahead of the class declaration.
This is expected — don't treat "class is not top result" as a hard blocker.
Use `chunk_type="class_overview"` (tests 25-26) when class-level ranking
matters. The same behavior extends to const/arrow-function exports
(`chunk_type="function"`, test 21): files that merely *reference* or
*import* a symbol can outrank its actual definition. When a bare query's
top hit looks wrong, don't conclude the symbol is unindexed — retry with
the matching `chunk_type` filter or `get_code_snippet(symbol=...)` before
treating it as a regression (frozen from a live false-positive during the
2026-08-06 report-hint parity smoke run, where tests 18/21 were initially
misdiagnosed as ranking misses before the filtered/symbol-lookup form
confirmed both were indexed correctly).

30. `search_examples("TTS pipeline", pipecat_version="0.0.95", domain="backend")`
    — all hits have `version_compatibility: "newer_required"` (framework
    examples now pin pipecat `1.3.0`, and community examples pin `>=0.0.98` /
    `>=1.0.0` — all newer than the queried 0.0.95)
31. `search_examples("TTS pipeline", pipecat_version="0.0.110", domain="backend")`
    — mixed by pin: framework `pipecat`/`pipecat-examples` hits pin `1.3.0` so
    they score `newer_required` at 0.0.110; sub-1.0 community pins (e.g.
    finchvox `>=0.0.98`) score `compatible`. (Pre-PR-#67 this returned all
    `compatible` when the framework still pinned 0.0.x.)
32. `search_examples("TTS pipeline", pipecat_version="0.0.110",
    version_filter="compatible_only", domain="backend")` — no
    `newer_required` hits pass through the filter
33. `search_examples("TTS pipeline")` (no version) — all hits have
    `version_compatibility: null`

New-source coverage (repos added in PR #67 — guards against a future
re-index silently dropping them):

33a. `search_api("FlowsFunctionSchema")` — top hit is from
    `module_path: pipecat_flows.types` with `is_dataclass: true` (confirms the
    `pipecat-ai/pipecat-flows` source is indexed and its Python AST /
    dataclass extraction works)
33b. `search_api("set_node", class_name="FlowManager")` — returns
    `FlowManager.set_node_from_config` from `pipecat_flows.manager` (verifies
    `class_name` prefix filtering against a pipecat-flows class)
33c. `search_examples("flow manager conversation node", domain="backend")` —
    top hits come from `repo: pipecat-ai/pipecat-flows` (e.g.
    `examples/warm_transfer.py`, with a 1.x `pipecat_version_pin` such as
    `"<2,>=1.3.0"`),
    confirming the flows example set is indexed and domain-filtered

New-source coverage (repo added in PR #74 — guards against a future re-index
silently dropping it):

33d. `search_api("RNDailyTransport")` — top hits are from
    `class_name: RNDailyTransport` in
    `pipecat-ai/pipecat-client-react-native-transports`
    (`transports/daily/src/transport.ts`), confirming the React Native
    transports repo is indexed and tree-sitter TS extraction produced real
    source chunks. `search_api("RNSmallWebRTCTransport")` likewise returns the
    `RNSmallWebRTCTransport` class. (`pipecat-cli` is intentionally NOT a
    default — its CLI usage is covered by `docs.pipecat.ai`; do not add a
    smoke assertion expecting `pipecat-ai/pipecat-cli` source chunks.)
**Deprecation canaries are registry-backed (PR #85).** Items 34–37 and 45–48
read pipecat's machine-readable registry
(`scripts/deprecations/deprecations.json`), not release-note prose — so **no `gh`
auth is required** and the old release-note prerequisite is gone. One consequence
of the registry model: it only carries symbols that **still exist** in the indexed
pipecat source with an active `.. deprecated::` / `@deprecated` marker. A symbol
that has already been *removed* is absent from `deprecations.json` — but as of PR
#88 the hub also merges pipecat's sibling `removals.json` ledger (when present),
so a removed symbol reports `status: "removed"` with its `removed_in` and a
migration note rather than `deprecated: false`. `check_deprecation` also accepts an
optional `version` (defaulting to the indexed framework version) and returns a
`status` of `current` / `deprecated` / `removed` evaluated at that version. Until
upstream ships a populated `removals.json`, the merge is a no-op and removed
symbols still read `deprecated: false` (i.e. *unknown*). Exact symbols below track
the indexed pipecat version; re-verify against the current registry if they drift.

34. `check_deprecation("pipecat.services.grok.llm")` — `deprecated: true`,
    `kind: "module"`, `relation: "move"`,
    `replacement: "pipecat.services.xai.llm"`, `deprecated_in: "0.0.108"`.
    `check_deprecation("pipecat.services.grok.llm.GrokLLMService")` — also `true`
    (forward-prefix: a symbol nested under the deprecated module).
35. `check_deprecation("ResampyResampler")` — `deprecated: true`, `kind: "class"`,
    `relation: "use_existing"`, `replacement: "SOXRAudioResampler"`. The
    fully-qualified
    `pipecat.audio.resamplers.resampy_resampler.ResampyResampler` resolves
    identically (dual keying: bare subject + `<module>.<subject>` alias).
36. `check_deprecation("DailyTransport")` — `deprecated: false` (current API).
37. `check_deprecation("PipelineTask")` — `deprecated: true`,
    `replacement: "PipelineWorker"`, `deprecated_in: "1.3.0"`,
    `removed_in: "2.0.0"`. `check_deprecation("PipelineRunner")` — `true`,
    `replacement: "WorkerRunner"`.
38. `get_hub_status()` after `refresh --framework-version v0.0.96` — response
    includes `framework_version: "v0.0.96"` (confirms pinned version persisted
    and surfaced)
38a. `get_hub_status()` after a plain `refresh` (no pin) — `framework_version` is
    `null` but `indexed_framework_version` is a release number (e.g. `"1.6.0"`) and
    `indexed_framework_commits_ahead` is an integer. Cross-check both against
    `git describe --tags --long` in `~/.pipecat-context-hub/repos/pipecat-ai_pipecat`:
    `v1.6.0-55-g1ad34dd98` must yield `("1.6.0", 55)`. Confirms the index records
    the revision it was actually built from, not just an operator's pin — a `null`
    `indexed_framework_version` after a successful pipecat ingest is the regression.
39. `refresh --framework-version nonexistent-tag-xyz` — fails with a clear
    `ValueError` mentioning "not found" and listing available tags (confirms
    tag validation rejects invalid input)
40. `uv run pipecat-context-hub serve` — startup `INFO` log line
    `pipecat-context-hub vX.Y.Z starting: data_dir=<path> total=N counts_by_type={code=N,doc=N,source=N}`
    appears with non-zero `total` (confirms version banner, index-populated
    state, and content-type counts are observable from the MCP trace)
41. `PIPECAT_HUB_RERANKER_ENABLED=0 uv run pipecat-context-hub serve` — startup
    `WARNING` log line `Reranker disabled at startup: reason=config_disabled
    configured_model=…` appears.

    For `reason=not_cached`, point HF at an empty cache rather than naming a
    bogus model: `HF_HOME=$(mktemp -d) uv run pipecat-context-hub serve`. The
    warning must report `reason=not_cached` and name the probed path in the
    remediation hint (`checked HF cache: /…/hub`), and `get_hub_status` must
    report `reranker_disabled_reason: "not_cached"` with `reranker_model: null`.
    An unknown `PIPECAT_HUB_RERANKER_MODEL` does **not** reach this path — the
    allowlist in `RerankerConfig.effective_model` intercepts it first and falls
    back to the default with its own warning, so the reranker ends up enabled.
    (The cache probe looks for `onnx/model.onnx`; a cache holding only
    `config.json` — the shape left by the pre-ONNX backend — must read as
    not-cached. Pinned by `tests/unit/test_onnx_backend.py`.)
42. **Orphan-watchdog smoke test** — spawn `serve` from a shell, note the
    PID, then `kill -9` the shell (or close the terminal). The orphaned
    `serve` process must exit on its own within ~5s. Confirm with
    `pgrep -fl pipecat-context-hub` — no stale entry should remain.
    A successful trigger logs `Shutting down: parent_died original_ppid=N
    current_ppid=1` at INFO before exit.
43. **Idle-timeout backstop smoke test** — exercises the idle watchdog
    in its explicit-override mode (since the default idle timeout is now
    auto-disabled under `uv run`, where the grandparent watchdog covers
    client death). Launch with
    `PIPECAT_HUB_IDLE_TIMEOUT_SECS=10 uv run pipecat-context-hub serve`
    and leave the stdio transport unused for >10s. The process must
    exit on its own and log `Shutting down: idle_timeout idle_seconds=N
    timeout_seconds=10` at INFO. Confirms an explicitly-configured idle
    backstop still fires.
43b. **Grandparent-death watchdog smoke test (`uv run` headline path)** —
    confirms the hub exits when its real client dies even though `uv`
    lingers. Without setting `PIPECAT_HUB_IDLE_TIMEOUT_SECS` (so smart-idle
    disables the idle backstop), launch `uv run pipecat-context-hub serve`
    from a parent process you then kill, keeping the hub's stdin open (so
    EOF can't be the cause). The hub must exit within ~`PARENT_WATCH_INTERVAL`
    and log `Shutting down: client_died ...` at INFO — not `idle_timeout`.
    Automated equivalent:
    `uv run pytest tests/integration/test_serve_lifetime.py::test_uv_run_client_death_exits_via_grandparent_watchdog`.
44. **Model pre-warm smoke test** — `uv run pipecat-context-hub serve`
    logs `Embedding model pre-warmed in <N>s` at INFO (and
    `Cross-encoder pre-warmed in <N>s` when the reranker is enabled
    and cached). Then re-run with `PIPECAT_HUB_WARMUP=0 uv run
    pipecat-context-hub serve` — the pre-warm lines must be absent
    and replaced by `Model pre-warm skipped: PIPECAT_HUB_WARMUP=0`.
    Confirms the first-query cold-start fix is active on boot and the
    opt-out escape hatch works (matters on Windows CPU where cold
    loads once took 30-130s and could exceed Claude Code's
    tool-permission window; the ONNX backend loads in under a second,
    so pre-warm is now an optimisation rather than a necessity).
45. **`location` surfaced (PR #85).** A `deprecated: true` response carries a
    `location` field as `path/to/file.py` (relative to the pipecat repo
    root) pointing at the file holding the deprecation marker — e.g. `ResampyResampler` →
    `pipecat/audio/resamplers/resampy_resampler.py`. Lets an agent locate the
    definition. The consumer is format-agnostic: registries that still carry a
    `:line` suffix (older pipecat) are passed through unchanged. `null` for older
    pipecat versions whose registry predates the field, or when not deprecated.
46. **Forward-prefix only — ancestors are never flagged.**
    `check_deprecation(symbol="pipecat.services")` and
    `check_deprecation(symbol="pipecat")` — both `false`, even though the
    descendant module `pipecat.services.grok.llm` is deprecated. Reporting a
    current ancestor package as deprecated (with some descendant's replacement)
    is the worst failure mode this tool has; the `check()` reverse-prefix branch
    was removed in PR #85 precisely to prevent it.
47. **Owner-of-member must stay `false`.** A class whose *member* (method,
    parameter, or nested class) is deprecated must not itself be flagged. Because
    the registry keys members as `Class.member`, a bare `Class` query does not
    forward-prefix-match. All `false`: `check_deprecation(symbol="GladiaSTTService")`,
    `check_deprecation(symbol="OpenAILLMService")` (each owns only deprecated
    params), and the current module
    `check_deprecation(symbol="pipecat.services.openai.llm")`.
48. **Current-API false-positive canaries.** `check_deprecation` must return
    `deprecated: false` for stable, current classes:
    `check_deprecation(symbol="Pipeline")`,
    `check_deprecation(symbol="CartesiaTTSService")`, and
    `check_deprecation(symbol="SileroVADAnalyzer")` — all `false`. A false
    positive here (current API flagged deprecated) is the worst failure mode this
    tool has; these version-independent classes are a regression canary.

    **Runnable smoke:** `uv run python scripts/smoke_check_deprecation.py`
    exercises the CURRENT (expect `false`) and DEPRECATED (expect `true`) canary
    sets against the **live local index** (requires a prior `refresh`; not part of
    the pytest gate). Exit 0 = all canaries pass. (The pre-registry "Gap D /
    replacement-kept" residuals are resolved by the registry — there is no longer
    a `--known-gaps` mode.)

    **Removal-history smoke:** `uv run python scripts/smoke_check_removals.py`
    covers the version-aware lifecycle (PR #88): it builds a map from the real
    `deprecations.json` and merges a *synthetic* `removals.json` (upstream's is
    still empty/dormant) to assert the REMOVED lifecycle, the safety invariant (an
    active deprecation never reports `removed` past its announced version), and the
    bare-key clobber guard. It does not mutate the persisted map.

49. `get_doc(path="/api-reference/server/frames/system-frames")` — response
    `sections` field is a **non-empty list** (regression canary for the always-empty
    sections bug fixed in PR #83). Each entry in `sections` must round-trip:
    passing `section=<title>` should narrow the page to that section's content
    without returning `null` or the full page.

50. **MCP `initialize` instructions carry the self-report guidance.** Reconnect
    an MCP client to `serve` and inspect the `initialize` response's
    `instructions` field: it must still tell the connecting agent to suggest
    filing at `.../issues/new?template=retrieval-quality.yml` on persistent
    `low_confidence`/zero-hit results, and at
    `.../issues/new?template=bug-report.yml` when `get_hub_status` reports
    `reranker_disabled_reason` of `not_cached` or `load_failed` (explicitly
    **not** for `config_disabled`, a supported operator choice). For
    `not_cached` specifically, the instructions tell the agent to suggest
    `pipecat-context-hub refresh` (self-service — downloads the model)
    *before* the bug-report URL, mirroring the CLI's remediation-first
    wording — and, since this `serve` process resolved its reranker state
    once at startup and does not re-probe the model cache while running,
    the instructions also tell the agent that the underlying server process
    must actually be restarted after `refresh` completes before re-checking
    `get_hub_status` — a client-side reconnect that reuses the same running
    process will not help (Codex adversarial review round 6: a bare
    "restart or reconnect" pairing was ambiguous, since some MCP hosts keep
    the process alive across a logical reconnect); re-checking on the same
    connection, or on a reconnect that didn't restart the process, still
    reports `not_cached`. For `load_failed`, the initialized client shares the full
    `get_hub_status` response and startup logs before suggesting a bug report.
    A non-zero boot exit happens before MCP initialization, so the instructions
    instead tell the agent to follow the remediation in startup stderr first
    (`refresh` for an empty index or `refresh --force --reset-index` for an
    unreadable/incompatible index), reconnect, and request `get_hub_status`
    only after initialization succeeds. This is
    advisory text for the connecting agent, not a code path triggered by an
    exception — it only reaches clients that speak MCP (`serve`). The
    one-shot CLI (`cli_query.py`, `cli.py`) has no agent-in-the-loop to hand
    advisory text to, so it gets the equivalent guidance directly on stderr
    instead, sourced from the same `shared/support_links.py` constants —
    see CLI query smoke items 5 and 9 below. Unit-side counterpart:
    `tests/unit/test_server.py::TestServerInstructions`. E2e counterpart
    (real `serve` subprocess, real stdio `initialize` round-trip, no
    mocks): `tests/integration/test_report_hint_e2e.py::
    test_mcp_initialize_delivers_report_hint_instructions` — guards against
    a regression where the source-level wiring (`instructions=...` passed
    to `create_server`) stays intact but delivery on the wire breaks (e.g.
    an MCP SDK kwarg rename).

If any of these fail, investigate before merging — the unit test suite will
not catch the regression.

### CLI query smoke (when `cli_query.py` or tool dispatch changes)

The query subcommands share the MCP tool handlers, so the numbered checks
above cover retrieval; these confirm the CLI front door itself against the
live local index:

1. `uv run pipecat-context-hub check-deprecation PipelineTask` — valid JSON
   on stdout in under ~1s (no embedding-model load on lookup commands)
2. `uv run pipecat-context-hub status` — `total_records` non-zero,
   `last_refresh_at` recent, reranker fields populated
3. `uv run pipecat-context-hub search-api "WebsocketServerParams" --limit 3`
   — hits include `pipecat.transports.websocket.server` (models load; ~3s)
4. `uv run pipecat-context-hub get-doc` (no flags) — exit 1, one-line
   validation message on stderr, empty stdout
5. `PIPECAT_HUB_DATA_DIR=$(mktemp -d) uv run pipecat-context-hub status` —
   exit 2, stderr says to run `refresh`, and (this is an empty-index first
   run, the most routine of the three `_EXIT_INDEX_UNREADY` paths) also
   carries the "if this persists after trying that, file a bug report at
   .../issues/new?template=bug-report.yml" hint. E2e counterpart (real CLI
   subprocess against a genuinely empty on-disk index, no mocks):
   `tests/integration/test_report_hint_e2e.py::
   test_cli_empty_index_delivers_bug_report_hint_on_stderr`.
6a. **Pipecat CLI bridge** (when `plugin.py`, the command set, or `pyproject.toml`
   entry points change). Unit tests mount the bridge in-process; this confirms the
   real entry-point discovery path, which they cannot:
   ```bash
   uv tool install --reinstall "pipecat-ai[cli]" --with /path/to/pipecat-context-hub
   ```
   Note `--reinstall`: plain `--force` reuses a cached build of a local path and
   will silently test stale code. Then check parity with the direct CLI —
   `pipecat context-hub --help` lists every command, `pipecat context-hub refresh --help` shows
   `--force` (not an empty stub), `pipecat context-hub refresh --bogus` exits 2,
   `pipecat context-hub get-doc` exits 1, `pipecat context-hub status` exits 0 with **pure JSON on
   stdout** (agents pipe it), and `pipecat context-hub install --print-config` prints the
   config without changing anything.
7. `PIPECAT_HUB_STALE_AFTER_DAYS=1 uv run pipecat-context-hub search-docs "TTS"`
   — response JSON carries `index_staleness` with `age_days >= 1` and a `hint`
   (assuming the index is ≥1 day old); rerun without the env override on a fresh
   index and confirm `index_staleness` is **absent**. The footer must never
   appear on `status` / `get_hub_status` regardless of the threshold.
8. **Multi-concept CLI query with the reranker enabled** — run
   `uv run pipecat-context-hub search-docs "TTS + STT"` several times (say 5) with
   the reranker at its default (enabled). Every run must exit 0 with JSON on
   stdout. Frozen from a live failure: on the pre-ONNX backend this failed 12/12
   (10 SIGSEGV/SIGBUS, 2 hangs) because multi-concept fans out concurrent
   per-concept searches and the one-shot CLI has no pre-warm, so several threads
   raced to lazily construct the torch cross-encoder. `serve` pre-warms at boot
   and was unaffected, so only the CLI front door exposed it — run this from the
   CLI, not through MCP. Unit-side counterpart:
   `tests/integration/test_concurrent_model_load.py`.
9. **Reranker `not_cached` stderr warning** — run
   `PIPECAT_HUB_RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-12-v2 uv run
   pipecat-context-hub search-docs "TTS"` (an allowlisted model that is
   typically not yet in the local HF cache; do **not** point `HF_HOME` at an
   empty directory instead — `services/embedding.py` resolves the embedding
   model from the same cache and `quiet_model_loading()` sets
   `HF_HUB_OFFLINE=1`, so an empty `HF_HOME` fails the command outright
   rather than reproducing this state). Confirm stdout is still valid JSON
   with no report-hint URL in it, and stderr carries a remediation-first
   warning naming `refresh` before
   `.../issues/new?template=bug-report.yml`. Unit-side counterpart:
   the `not_cached` parametrized tests in `tests/unit/test_cli_query.py`.
10. **Retrieval-quality stderr hint is command-aware** — run
    `uv run pipecat-context-hub get-code-snippet --symbol NonexistentSymbolXYZ123`
    (a symbol unlikely to resolve cleanly). Stdout stays valid JSON (an empty
    `snippets` list, or a non-empty one the handler itself flagged
    `low_confidence`) with no report-hint URL in it; stderr names
    `--symbol/--intent/--path` (never `--limit`, a flag `get-code-snippet`
    doesn't have) before
    `.../issues/new?template=retrieval-quality.yml`. Contrast with
    `uv run pipecat-context-hub search-docs "asdkjhaskjdhaksjdh nonsense query xyz123"`,
    whose hint does name `--limit`, since that command has one. Regression
    canary for a fix (`9b8508d`) where the hint's wording was a single fixed
    phrase naming a flag not every semantic command carries. Unit-side
    counterpart: `_SEMANTIC_META`'s AST enrollment guard
    (`test_semantic_meta_matches_needs_embeddings_call_sites`) and
    `TestRetrievalQualityHint` in `tests/unit/test_cli_query.py`.

## Upstream Drift Check

Offline smoke tests in `tests/smoke/` exercise the taxonomy builder against
vendored snapshots of the upstream pipecat and pipecat-examples repos. They
run on every `pytest` invocation and have no `smoke` marker — no opt-in
needed. The Seam 1 contract (every dir returned by
`_discover_under_examples` has a matching `taxonomy_lookup[rel]` entry) is
what these tests enforce.

- **PR gate (offline, always runs):** `uv run pytest tests/smoke/ -v`
- **Scaffold unit coverage:** `uv run pytest tests/unit/test_smoke_scaffold.py -v`
  (root-layout refresh, SHA-vs-named-ref clone argv, symlink rejection).
- **Scheduled drift check (live network):** `.github/workflows/smoke-drift.yml`
  runs the invariants against a fresh clone of each upstream repo every 5
  days (`cron: '0 6 */5 * *'`). The same checks are runnable locally:

  ```bash
  # Branch or tag ref (default --ref main)
  uv run python scripts/check_pipecat_drift.py --repo pipecat-ai/pipecat --ref main
  # Pinned commit SHA (branch/tag refs use --branch; SHAs use init+fetch+checkout)
  uv run python scripts/check_pipecat_drift.py --repo pipecat-ai/pipecat --ref ef7fa07b
  ```
- **Refresh vendored fixtures:** when upstream changes layout, run
  `uv run python tests/smoke/refresh_fixtures.py` (optionally with
  `--ref <sha|branch|tag>` to pin the snapshot) to regenerate the
  snapshots in `tests/fixtures/smoke/`. The refresher is layout-aware:
  it copies `examples/` for topic-layout repos and every top-level
  example dir for root-layout repos (pipecat-examples). See
  `tests/smoke/README.md` for the refresh cadence and pin-bump triage.

## Pre-Merge Quality Gate

Run the full CI gate locally before merging any PR. Do not rely on tests
alone — mypy and ruff catch issues that only surface in CI.

```bash
uv run ruff check src/ tests/
uv run mypy src/ tests/
uv run pytest tests/ -q
```

## Review Checklist

Findings that have been reviewed and deliberately accepted. Do not re-flag these
in future reviews unless the underlying circumstances change.

- **[Architecture] won't-fix**: CodeSnippet enrichment fields (`dependency_notes`, `companion_snippets`, `interface_expectations`) use different names than ApiHit's raw fields (`imports`, `calls`, `yields`, `base_classes`). This is intentional — ApiHit is a raw API surface for exploration, CodeSnippet is an agent-facing enriched view with qualified names and human-readable formatting. Revisit if a third tool type needs the same data. (2026-03-22)

- **[Architecture] won't-fix**: `get_code_snippet` enrichment logic (line_sliced detection, module_overview guard, metadata mapping) is inline in the method rather than extracted into helpers. The method is ~50 lines with clear comments. Extract helpers if enrichment gains more suppression conditions or new enrichment fields. (2026-03-22)

- **[Security] won't-fix**: Chunk metadata values (class_name, calls, yields, etc.) flow unsanitized into MCP JSON-RPC responses. The AST ingester constrains these to valid Python identifiers; the TS tree-sitter parser extracts names from cloned GitHub repo source (not user input). No executable sink exists. Add input validation if user-supplied metadata or external API sources are introduced. (2026-03-22, updated 2026-03-30)

- **[Architecture] won't-fix**: `ApiHit.imports` has mixed precision by chunk type — per-method for method/function chunks, module-level pipecat imports for class_overview, full imports (including stdlib) for module_overview. This is a deliberate layering: `source_ingest._build_chunks` populates each chunk type differently, and `hybrid.py` passes the field through unchanged. The `ApiHit.imports` description documents the per-chunk-type semantics. Revisit only if a consumer needs uniform precision across chunk types. (2026-03-23)

- **[Logic] won't-fix**: Confidence scores are optimistic on weak `search_examples` results — noisy keyword matches from large repos (e.g., gradient-bang frontend files) score high via RRF + dual-hit bonus, driving confidence to ~0.95 even when results are semantically irrelevant. This is a retrieval quality issue, not a confidence calibration bug. The cross-encoder (Phase 1, disabled by default) directly addresses this by scoring query-result *pairs* for semantic relevance. Without cross-encoder, confidence reflects score distribution, not true relevance. Follow-up: example corpus weighting / repo scoring to reduce noise from non-pipeline code. (2026-03-24)

- **[Security] resolved**: `pygments` CVE-2026-4539 resolved by upgrading to 2.20.0 via PR #34 (2026-03-31). `--ignore-vuln` entry removed from CI and justfile.

- **[Security] resolved**: `lxml` GHSA-vfmq-68hx-4jfw / CVE-2026-41066 (XXE via default `iterparse()` / `ETCompatXMLParser()` config) resolved by pinning `lxml>=6.1.0` in the dev group and bumping `cyclonedx-bom` from `>=4.1,<5.0` to `>=7.3,<8.0` (cyclonedx-bom 4.x transitively pinned `lxml<6`). Landed via PR #50 (2026-04-22).

- **[Security] resolved**: `pip` CVE-2026-3219 resolved by bumping the pinned pip version to `26.1.1` via PR #57 (2026-05-07).

- **[Security] resolved**: `gitpython` path-traversal advisory in reference APIs (arbitrary file write/delete outside the repository) resolved by raising the floor to `>=3.1.49` (lock resolves to `3.1.50`) via PR #59 (2026-05-08). Closes Dependabot alert #12.

- **[Security] resolved**: `python-multipart` DoS advisory (unbounded multipart part headers) resolved by bumping the transitive lock entry to `0.0.27` via Dependabot PR #61 (2026-05-08). Closes Dependabot alert #13. No top-level pin required — `mcp` already constrains `python-multipart>=0.0.9`, and the resolver picks the latest compatible version on re-lock.

- **[Security] resolved**: `urllib3` high-severity advisories GHSA-mf9v-mfxr-j63j (decompression-bomb safeguard bypass in the streaming API) and GHSA-qccp-gfcp-xxvc (sensitive-header forwarding on cross-origin `ProxyManager` redirects) resolved by bumping the transitive lock entry to `2.7.0` via Dependabot PR #62 (2026-05-13). Closes Dependabot alerts #15 and #16. No top-level pin required — `urllib3` is reached only via `chromadb` → `kubernetes` / `posthog` → `requests`, and the resolver picks the latest compatible version on re-lock. No exploitable path from hub code: outbound HTTP uses `httpx`, and the codebase never calls `urllib3` / `ProxyManager` APIs directly.

- **[Security] resolved**: `idna` medium-severity advisory CVE-2026-45409 / GHSA-65pc-fj4g-8rjx (specially crafted inputs to `idna.encode()` bypass the CVE-2024-3651 mitigation, enabling quadratic-time processing) resolved by bumping the transitive lock entry to `3.15` via Dependabot PR #64 (2026-05-24). Closes Dependabot alert #17. No top-level pin required — `idna` is reached only via `anyio` / `httpx` / `requests`, and the resolver picks the latest compatible version on re-lock. No exploitable path from hub code: the codebase never calls `idna` directly, and hostnames resolved via `httpx` / `requests` are fixed, trusted endpoints (GitHub, docs sources), not attacker-controlled input.

- **[Security] resolved**: `starlette` medium-severity advisory PYSEC-2026-161 / GHSA-86qp-5c8j-p5mr (missing Host-header validation poisons `request.url.path`, bypassing path-based security checks) resolved by flooring the transitive entry to `starlette>=1.0.1` (lock resolves to `1.1.0`) via PR #64 (2026-05-24). Unlike the other transitive bumps this one **requires a `[tool.uv] constraint-dependencies` entry**, not a lock-only bump: a plain re-lock regresses to the vulnerable `0.52.1` because the prior `fastapi 0.129.0` capped starlette below `1.0`. The constraint forces the resolver to also lift `fastapi` to `0.136.3`. `starlette` is reached only via `mcp` / `fastapi` / `sse-starlette`; no exploitable path from hub code — the hub speaks MCP over stdio and never serves HTTP via Starlette.

- **[Security] resolved**: Batch advisory bumps (PR #93) — `cryptography` 46.0.7→49.0.0 (GHSA-537c-gmf6-5ccf), `python-multipart` 0.0.27→0.0.32 (CVE-2026-53538 / CVE-2026-53539 / CVE-2026-53540), `msgpack` 1.1.2→1.2.1 (GHSA-6v7p-g79w-8964), `pydantic-settings` 2.13.0→2.14.2 (GHSA-4xgf-cpjx-pc3j); `starlette` 1.1.0→1.3.1 rides along (no CVE). **`cryptography` is a direct dependency**, so its published floor was raised `>=46.0.7`→`>=48.0.1` (the GHSA fix floor) — a lock-only bump would leave `pyproject.toml` metadata admitting the vulnerable `46.0.7` for consumers who install from PyPI rather than this lockfile. The other four are **transitive with open upper bounds**: nothing in the tree caps them below the fix (verified — a re-lock pulls latest freely), so they follow the established no-pin pattern (same as the `pip`/`pyjwt` and `urllib3`/`idna`/`python-multipart#61` bumps) — a fresh `uvx`/`pip` resolve picks the latest compatible (patched) version. Deliberately **not** pinned in `[project.dependencies]` or `[tool.uv] constraint-dependencies`: unlike the `starlette`/`fastapi` case above, no intermediate caps them, so re-lock cannot regress. A Codex adversarial review flagged the lockfile-vs-published-metadata gap; accepted for the open-upper-bound transitives, fixed for the direct `cryptography` dep. (2026-06-19)

- **[Security] resolved**: `torch` advisory PYSEC-2026-139 / CVE-2026-4538 — resolved by removing `torch` from the dependency tree entirely. Embedding and cross-encoder inference moved to ONNX Runtime against the same model weights (`services/onnx_backend.py`), so `sentence-transformers` — the only path that reached `torch` — is gone. The `--ignore-vuln PYSEC-2026-139` entry was removed from the PR-gating `pip-audit` (ci.yml) and the `audit-deps` justfile recipe. Verified: `pip-audit` reports no findings with only the chromadb ignore. (resolved 2026-08-02)

- **[Security] resolved**: `torch` advisory CVE-2025-3000 / GHSA-rrmf-rvhw-rf47 / PYSEC-2025-194 — resolved the same way as PYSEC-2026-139: `torch` left the tree when embedding and reranking moved to ONNX Runtime. The `--ignore-vuln CVE-2025-3000` entry was removed from ci.yml and the justfile `audit-deps` recipe (parity enforced by `tests/unit/test_audit_sync.py`, which now sees a single-entry ignore set). (resolved 2026-08-02)

- **[Security] accepted**: `services/onnx_backend.py::_download` carries a `# nosec B615` on its unpinned fallback. Every model the hub ships with is pinned to an immutable commit SHA in `_PINNED_REVISIONS` (supply-chain safety, and it stops an upstream re-export of `onnx/model.onnx` silently shifting the embedding space out from under an existing index). The fallback only fires for a caller-supplied `EmbeddingConfig.model_name`, for which no pin can exist; that value comes from local config, not untrusted input, and the previous sentence-transformers backend resolved *every* model unpinned. bandit will re-flag this on each run — the pin table plus the `test_every_shipped_model_is_pinned` guard is the mitigation. (2026-08-03)

- **[Architecture] won't-fix**: `chromadb` hard-depends on `kubernetes` (~41 MB), `grpc` and `opentelemetry` for its client-server mode, none of which the hub can reach — it runs the embedded `PersistentClient` only. Verified unreachable by uninstalling `kubernetes` and confirming `PersistentClient` still opens, queries, and persists. Not removable from our side: they are unconditional dependencies of `chromadb`, not an extra, so `pyproject.toml` cannot express the exclusion and a post-install prune would only work for container builds, not `pip`/`uvx` users. Worth an upstream issue; do not re-flag as install-size debt we can fix here. (2026-08-03)

- **[Packaging] accepted**: `requires-python` is deliberately unbounded above (`>=3.11`), matching `pipecat-ai`. A cap is viral for anyone installing this alongside `pipecat-ai[cli]`: once a new Python gains wheels, a capped hub would be the sole reason that combination fails to resolve, until a hub release shipped. The accepted cost is that an unsupported Python produces resolver backtracking rather than a clean "requires-python" rejection — on 3.15 today, `pipecat-ai[cli]` already silently resolves to `pipecat-ai==0.0.101` for exactly this reason, driven by `onnxruntime~=1.24.3` in pipecat's own core dependencies. Do not re-add a cap in response to a missing-wheel report. (2026-08-03)

- **[Security] won't-fix**: `chromadb` CVE-2026-45829 / GHSA-f4j7-r4q5-qw2c — pre-authentication code injection in chromadb's HTTP **server** mode (arbitrary code execution via the `/api/v2/.../collections` endpoint with a malicious model repo and `trust_remote_code=true`). Affects 1.0.0–1.5.9 with no fixed release yet (1.5.9 is the latest 1.x). Unreachable here: the hub runs the embedded `PersistentClient` only (no server, no HTTP endpoint, no listener) and never uses chromadb's embedding functions / `trust_remote_code`. Ignored via `--ignore-vuln CVE-2026-45829` in the PR-gating `pip-audit` (ci.yml) and the `audit-deps` justfile recipe (parity enforced by `tests/unit/test_audit_sync.py`). The unfiltered biweekly `security-audit.yml` job keeps surfacing it; remove the ignore once a patched chromadb release ships. (2026-06-06)

- **[Security] resolved**: `transformers` CVE-2026-1839 — resolved by `sentence-transformers` lifting its `transformers<5.0` pin; the tree now resolves `transformers 5.5.0` (>=5.0 carries the fix). The advisory no longer surfaces under `pip-audit` (verified with zero ignores), so the `--ignore-vuln CVE-2026-1839` entry was removed from the justfile `audit-deps` recipe; CI no longer references it either. The `audit-deps` recipe and the ci.yml "Dependency Audit" step now carry reciprocal KEEP-IN-SYNC notes. (resolved 2026-06-06)

- **[Architecture] won't-fix**: Removing `pipecat_context_hub.services.ingest.ts_source_parser` is intentional. The module is treated as internal implementation detail, not supported public API, and no external consumers are expected to import it directly. Revisit only if ingestion parser modules become documented extension points. (2026-03-30)

- **[Security] won't-fix**: TypeScript import metadata currently stores raw `import_statement` text from indexed repos. This matches the existing model where source-derived metadata is returned verbatim and no executable sink exists. Revisit if user-supplied repos or prompt-sensitive metadata consumers are introduced. (2026-03-30)

- **[Architecture] won't-fix**: The TypeScript parser-to-chunk contract is intentionally direct for Phase 2: `ts_tree_sitter_parser.py` emits the declaration/member fields that `source_ingest._build_ts_chunks` needs, mirroring the current Python `_build_chunks` pattern. Extract a normalization layer only if later language phases need a shared intermediate representation. (2026-03-30)

- **[Security] won't-fix**: `DeprecationEntry.note` stores raw release-note prose from `pipecat-ai/pipecat` and returns it verbatim via `check_deprecation`. The source is the trusted upstream framework repo (not user input), and MCP JSON-RPC has no executable sink. Revisit if user-supplied repos are introduced as deprecation sources. (2026-04-07)

- **[Architecture] won't-fix**: `_fetch_release_notes()` shells out to `gh` directly from `deprecation_map.py` rather than going through an adapter in the orchestration layer. The function already handles missing CLI, auth failures, and timeouts gracefully with warning-level logging. Extract to a dedicated adapter only if other modules need GitHub release data. (2026-04-07)

- **[Logic] won't-fix**: Multi-item replacement paths from release notes are collapsed into a single comma-joined string assigned to all deprecated paths in the same bullet. This is informational metadata — users see all possible replacements rather than a potentially incorrect positional guess. Improve to positional pairing only if release notes adopt a consistent 1:1 format. (2026-04-07)

- **[Logic] won't-fix**: `DeprecationMap.check()` reverse-prefix matching (`pipecat.services` matches `pipecat.services.grok`) returns the first matching entry, which may be arbitrary when multiple children exist. This is documented behavior for broad queries. Callers should use specific module paths for precise results. (2026-04-07)

- **[Architecture] won't-fix**: Release-note entries do not override an existing `new_path` from source-derived mappings. Source-parsed `DeprecatedModuleProxy` mappings are module-to-module precise, while release notes may list multiple replacement paths. Keeping source-derived `new_path` as authoritative preserves precision. Revisit if source parsing is fully removed. (2026-04-07)
