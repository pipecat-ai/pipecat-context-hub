# Pre-Merge Live MCP Smoke Test

The checklist referenced from [AGENTS.md](../AGENTS.md#pre-merge-live-mcp-smoke-test): run it against the live local index before merging a PR that touches retrieval, tool handlers, index backends, or types. Item numbers are cited from tests and scripts; append new items rather than renumbering.

1. `get_hub_status()` — returns a non-empty index and a recent
   `last_refresh_at`, so smoke-test failures are not caused by a stale or empty
   local corpus
2. `get_doc(path="/api-reference/server/frames/system-frames.md")` — returns
   full multi-chunk page (not a single 500-char chunk), confidence 1.0
3. `get_doc(path="/api-reference/server/frames/system-frames.md", section="StartFrame")`
   — returns only the StartFrame section from the assembled page
4. `get_doc(doc_id=<id from a search_docs result>)` — returns non-empty content
   and is not `Not Found`
5. `get_doc(path="")` and `get_doc(doc_id="")` — both raise validation errors
6. `get_doc(doc_id="", path="/api-reference/server/frames/system-frames.md")` —
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
    React component from `pipecat-ai/pipecat-ui` or `pipecat-ai/pipecat-client-web`
    (pipecat-ui's equivalents are `audio-visualizer-radial` /
    `audio-visualizer-wave` under `packages/registry/src/components/`).
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
    `pipecat-ai/pipecat-client-web-transports` or `pipecat-ai/pipecat-ui`
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
    `search_api("PipecatClientProvider", chunk_type="function")` (at the default
    `limit` the definition is in the top 3; test 52 pins why) or
    `get_code_snippet(symbol="PipecatClientProvider")`, which returns it as
    the top hit.
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
    — no hit is `compatible`: pinned hits are `newer_required` (framework
    examples pin the indexed release, and community examples pin `>=0.0.98` /
    `>=1.0.0` — all newer than the queried 0.0.95). A repo with no pipecat pin
    (e.g. `pipecat-ai/pipecat.ai-agents`) scores `unknown`, so expect a few of
    those. The score only annotates; it does not re-rank, so the order matches
    test 33 (relevance noise on a generic query like this is accepted — see the
    Review Checklist in AGENTS.md).
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
    `module_path: pipecat.flows.types` with `is_dataclass: true` (confirms the
    in-framework flows package is indexed and its Python AST / dataclass
    extraction works). No hit should come from the legacy `pipecat_flows.*`
    module path: the standalone `pipecat-ai/pipecat-flows` repo is archived
    (flows merged into `pipecat-ai/pipecat` in 1.5.0) and was dropped from
    the default sources, so a `pipecat_flows` hit means stale records survive
    from before the drop — run `refresh --prune` to remove them.
33b. `search_api("set_node", class_name="FlowManager")` — returns
    `FlowManager.set_node_from_config` from `pipecat.flows.manager` (verifies
    `class_name` prefix filtering against a flows class)
33c. `search_examples("flow manager conversation node", domain="backend")` —
    top hits come from `repo: pipecat-ai/pipecat` under `examples/flows/`
    (e.g. `examples/flows/warm_transfer.py`), confirming the flows example
    set is indexed from the framework repo and domain-filtered

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
    `"latest"`, `indexed_framework_version` is the newest release number (e.g.
    `"1.6.0"`) and `indexed_framework_commits_ahead` is `0`. A pin other than
    `latest` reaching the index from a no-flag refresh is the regression.
38b. `get_hub_status()` after `refresh --framework-version head` —
    `framework_version` is `"head"`, `indexed_framework_version` is a release
    number and `indexed_framework_commits_ahead` is an integer. Cross-check both
    against `git describe --tags --long` in
    `~/.pipecat-context-hub/repos/pipecat-ai_pipecat`: `v1.6.0-55-g1ad34dd98`
    must yield `("1.6.0", 55)`. Confirms the index records the revision it was
    actually built from, not just an operator's pin — a `null`
    `indexed_framework_version` after a successful pipecat ingest is the regression.
    Run it on a fresh copy of the index (`PIPECAT_HUB_DATA_DIR`) with no other
    hub process open. Exit 139 (SIGSEGV in `chromadb_rust_bindings`) is the
    known, probabilistic Chroma 1.5.x family (see
    `docs/dev_plans/20261014-fix-index-write-guards.md`; upstream chroma#7510),
    seen on 2026-10-16 right after the delete-and-re-add of the pipecat records
    in a 38 → 38a → 38b sequence; a re-run on the same index and on a fresh copy
    both exited 0. A crashed refresh leaves the vector and keyword halves out of
    step; a plain `refresh` repairs it, and `refresh --force --reset-index` is
    the fallback.
39. `refresh --framework-version nonexistent-tag-xyz` — exits **1** in ~1s
    with `Error: --framework-version 'nonexistent-tag-xyz': Tag '…' not found
    in repository. Available tags (latest 5): [...]`. The pin is validated
    before the docs crawl, so nothing is indexed and no metadata is written —
    read `index_metadata` and confirm `framework_version` and
    `last_refresh_at` still hold their previous values. `--framework-version
    'bad tag!!'` exits 1 the same way with `Invalid tag format`. The tolerant
    path must stay tolerant: a *transient* clone failure (network/auth/disk)
    is still a warning with exit 0 and the other repos still indexed — the
    split is `TagNotFoundError` (invalid input) vs everything else.
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
    A direct-parent launch logs `Shutting down: parent_died original_ppid=N
    current_ppid=1`; an `uv run` launch uses the grandparent watchdog and
    logs `Shutting down: client_died ...` instead.
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
    opt-out escape hatch works. Older Windows CPU measurements once showed
    30-130s cold loads; that is historical context only. The current Windows
    contract is package/path smoke, while populated index operations
    (including serve and pre-warm) fail closed. The ONNX backend loads in
    under a second on supported POSIX hosts, so pre-warm is now an
    optimisation rather than a necessity. Run this smoke on POSIX only.)
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

49. `get_doc(path="/api-reference/server/frames/system-frames.md")` — response
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
    (`refresh` for an empty index or an interrupted compaction, `refresh --force`
    for a lost vector collection beside keyword records — plain `refresh`
    refuses that state — or `refresh --force --reset-index` for an
    unreadable/incompatible index or a missing, empty or corrupt keyword
    index), reconnect, and request `get_hub_status`
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

51. **Persisted graph corruption / first-search crash (2026-10-04).** The exact
    failed query was `search_docs("TTS + STT")`: initialize/status succeeded,
    then Chroma 1.5.9 SIGSEGVed loading the vector graph and MCP returned
    `Transport closed`. Single-concept docs/API queries failed too. On a healthy
    index, repeat the exact query and then status on the same connection. With
    an oversized persisted HNSW `link_lists.bin`, fresh `serve`, `status` and
    CLI `search-docs "TTS + STT"` must instead exit **2 before initialization**,
    with `corrupt persisted HNSW` and `refresh --force --reset-index` on stderr
    and empty stdout. The probe must not change index files or run recovery.
    Regressions: `tests/unit/test_hnsw_validation.py` freezes the real header
    and logical file size; `tests/integration/test_report_hint_e2e.py` freezes
    the query against synthetic corruption. This bounded size check does not
    prove all persisted graph data is healthy.

52. **Ranking does not depend on `limit` (2026-10-16).** Each backend is asked
    for a fixed pool (`_MIN_CANDIDATES`, 50) regardless of the caller's `limit`,
    which then only truncates. Frozen from a live failure:
    `search_api("PipecatClientProvider", chunk_type="function")` dropped the
    definition (`client-react/src/PipecatClientProvider`) at `limit` ≤ 15 and
    ranked it first at `limit` ≥ 20, because exactly `limit` candidates let files
    that merely mention the name fill the pool. Run it at `limit` 5, 10, 20 and 50:
    the definition is present at every one, and each shorter result is a prefix
    of the `limit=50` result. Repeat with `search_api("send_dtmf")`. Regression:
    `tests/unit/test_retrieval.py::TestCandidatePool`.

53. **`repos` commands against a scratch config (PR 1).** Never point this at
    your real `config.toml`. In a fresh 0700 directory run
    `export PIPECAT_HUB_CONFIG_FILE=$(mktemp -d)/config.toml` (`chmod 700` the
    directory), then:
    `uv run pipecat-context-hub repos list` (effective list, every slug tagged
    `default`, and the consulted cwd line);
    `repos add pipecat-ai/tail` prints `Added pipecat-ai/tail` plus the
    `refresh` reminder, and `repos list` now shows it tagged `catalog`;
    repeat the add for `Already in config.toml`; `repos add not-listed/repo`
    exits 1 and says to pass `--unlisted`. Then
    `PIPECAT_HUB_EXTRA_REPOS=x/y uv run pipecat-context-hub repos add pipecat-ai/whisker`
    exits 1 naming layer `env`; with `--config-only` it succeeds and
    `repos list` names the shadowing layer. Finish with
    `repos remove pipecat-ai/tail` (`Removed`) and `repos remove --group <name>`.
    Confirm no network access happens (works offline) and that the scratch
    file keeps its comments. Unit-side counterpart: `tests/unit/test_cli_repos.py`.

If any of these fail, investigate before merging — the unit test suite will
not catch the regression.

## CLI query smoke (when `cli_query.py` or tool dispatch changes)

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
   exit 2 (`EXIT_INDEX_UNREADY`, `shared/exit_codes.py`), stderr says to
   run `refresh`, and (this is an empty-index first run, the most routine
   of the three exit-2 paths) also
   carries the "if this persists after trying that, file a bug report at
   .../issues/new?template=bug-report.yml" hint. E2e counterpart (real CLI
   subprocess against a genuinely empty on-disk index, no mocks):
   `tests/integration/test_report_hint_e2e.py::
   test_cli_empty_index_delivers_bug_report_hint_on_stderr`.
5a. **Refresh lock and refresh refusals** (when `shared/index_lock.py`, `cli.py`'s
   `refresh`, or the exit codes change). `refresh` holds an exclusive
   `<dirname>.refresh.lock` (a sibling of the data dir, so `--reset-index` cannot
   delete it); readers hold it shared only while opening. While a `refresh` is
   rewriting the index, `status` / `search-*` / `get-doc` / `check-deprecation`
   exit 75 (`EXIT_REFRESH_IN_PROGRESS`, retry in 60-120 s), and a second `refresh`
   that finds the lock busy also exits 75. `refresh` refuses to start with under
   5 GB free on the data-dir volume (exit 1; `PIPECAT_HUB_MIN_FREE_GB`, `0` disables).
   `refresh --force --reset-index` exits 1 if index files survive its delete.
   Unit counterparts: `tests/unit/test_index_lock.py`, `tests/unit/test_cli.py`,
   `tests/unit/test_cli_query.py`, `tests/unit/test_index_state_matrix.py`.
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
