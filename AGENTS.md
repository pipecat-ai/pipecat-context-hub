# Agents Guide

Project conventions and decisions for AI coding agents working on this codebase. Local-first MCP server providing Pipecat docs, examples, and API context.

## Stack

- **Python 3.11+** (no upper cap; CI covers 3.12 and 3.14), `uv` package manager, `hatchling` build
- **Embeddings:** `all-MiniLM-L6-v2` (ONNX Runtime, local, CPU)
- **Vector store:** ChromaDB | **Keyword index:** SQLite FTS5
- **AST parsing:** Python `ast` module + `tree-sitter` (TypeScript/TSX)
- **Transport:** stdio (MCP JSON-RPC)

## Commands

```bash
uv run pytest tests/ -v                             # full test suite
uv run ruff check src/ tests/                       # lint
uv run mypy src/ tests/                             # type check
uv run pipecat-context-hub refresh                  # incremental rebuild
uv run pipecat-context-hub refresh --force          # full re-ingest + compacts the vector graph; prefer it after a pipecat upgrade
uv run pipecat-context-hub refresh --force --reset-index  # recover unhealthy local Chroma state
uv run pipecat-context-hub refresh --framework-version v0.0.96  # index framework at a specific tag
uv run pipecat-context-hub refresh --framework-version head     # ...or at its default branch (default: latest release tag)
uv run pipecat-context-hub serve                    # start MCP server (`start` is an alias)
uv run pipecat-context-hub install --print-config   # show MCP client config; change nothing
```

Installed alongside `pipecat-ai[cli]`, every command is also reachable as
`pipecat context-hub <command>` — the same click group, bridged into the Pipecat CLI by
`plugin.py`. See "Typer bridge" below.

Use `refresh --force --reset-index` when the persisted local Chroma index is
unhealthy and needs a clean rebuild.

`refresh`'s repo-cleanup pass warns instead of deleting by default: a repo
absent from *this invocation's* config (but possibly configured elsewhere —
e.g. `config.toml` shadowed by a narrower project `.env`) has its indexed
records left in place, with a warning naming the repo and record count. Pass
`--prune`, or set `PIPECAT_HUB_PRUNE=1` (invocation-scoped only — not
readable from `config.toml`), to actually delete that data. Tainted repos
(`PIPECAT_HUB_TAINTED_REPOS`) are always cleaned up regardless of `--prune`.

`refresh` refuses to start with under 5 GB free on the data-dir volume;
`PIPECAT_HUB_MIN_FREE_GB` (invocation-scoped like `PIPECAT_HUB_PRUNE`) changes
the threshold, `0` disables it.

Every MCP tool is also a **one-shot CLI subcommand** (same handlers, JSON on
stdout, logs on stderr — see `cli_query.py`):

```bash
uv run pipecat-context-hub --version                        # package version; no index/model load
uv run pipecat-context-hub check-deprecation PipelineTask   # <1s (no model load)
uv run pipecat-context-hub check-deprecation PipelineTask --at-version 2.0.0  # lifecycle at a version
uv run pipecat-context-hub status                           # index health; <1s
uv run pipecat-context-hub search-api "WebsocketServerParams" --limit 3   # ~3s (loads models)
uv run pipecat-context-hub search-docs "TTS + STT"
uv run pipecat-context-hub get-doc --path /guides/telephony/overview
```

Exit codes (`shared/exit_codes.py`): 0 success, 1 invalid input (also
`refresh`'s low-disk refusal, and `--reset-index` when index files survive its delete), 2 index missing, empty, unreadable or incompatible
(stderr says what to run; `refresh` also exits 2 when a fatal index error, such as
a tripped HNSW write guard or a compaction swap that left no `latest`, stops it; when
its open cannot restore an interrupted compaction, finds the keyword database
corrupt or with an unusable schema, or, even with `--force`, finds it missing or empty
beside vector records, saying to run `refresh --force --reset-index`; and,
without `--force`, when the vector collection is missing or empty beside a populated
keyword index: it leaves the vector half untouched and says to run `refresh --force`),
75 a `refresh` is rewriting the index (stderr says to retry in 60-120 seconds;
`refresh` itself also exits 75 when the refresh lock is busy). A parity test (`tests/unit/test_cli_query.py`) enforces that
every MCP tool has a CLI command.

A `justfile` is also available as a task runner:

```bash
just check              # lint + format check + typecheck
just test               # run tests
just audit              # pip-audit + bandit
just sbom               # generate CycloneDX SBOM
just benchmark-stability  # opt-in refresh/serve/search stability benchmark
just dashboard-refresh  # refresh index + rebuild all dashboard data
just dashboard-build    # rebuild dashboard data without re-indexing
just dashboard-serve    # serve dashboard on localhost:8765
```

## Config Sourcing

`PIPECAT_HUB_*` settings resolve in three layers, first-writer-wins: real env
vars > cwd `.env` > `~/.config/pipecat-context-hub/config.toml` (machine-global,
optional, see `config.toml.example`) > `HubConfig` field defaults. Every entry
point — `cli.py:main()`, the dashboard scripts, `scripts/smoke_check_removals.py`
— calls `shared/env_loading.py`'s `load_env_layers()` (which runs
`load_cwd_dotenv()` then `load_global_config()`) before constructing config, so
they all resolve identically. Don't hand-replicate the two calls in a new entry
point — a source-parity test (`tests/unit/test_config.py`) fails if you do. Full precedence
details and the Windows lookup path: `docs/README.md`'s "Config precedence"
subsection under Environment Variables.

## MCP Tools — Multi-Concept Queries

When calling search tools (`search_docs`, `search_examples`, `search_api`, `get_code_snippet`), use ` + ` or ` & ` to search for multiple concepts at once:

```
search_docs("TTS + STT")
search_examples("idle timeout + function calling + Gemini")
search_api("BaseTransport + WebSocketTransport")
```

Each concept is searched independently and results are interleaved for balanced coverage. Do NOT stuff multiple concepts into a single natural-language query — that clusters results around whichever concept dominates the embedding.

## Example Search Filters

`search_examples` supports domain and language filters to reduce noise:

- `domain="backend"` — Python pipeline/bot code only
- `domain="frontend"` — JS/TS client code only
- `domain="config"` — YAML/TOML/JSON config
- `domain="infra"` — Docker/CI infrastructure
- `language="python"` — filter by programming language
- `language="typescript"` — filter by programming language
- `execution_mode="local"` / `"cloud"` — inferred from capability tags
- `foundational_class=…` — legacy filter; only pre-reorg examples carry it, so
  it silently excludes new-layout examples. Prefer `domain`/`tags`.
- Combine: `search_examples("TTS pipeline", domain="backend", language="python")`

## Versioning

The version lives in **two places** — both must be updated together on every release:

1. `pyproject.toml` → `[project].version`
2. `src/pipecat_context_hub/server/main.py` → `_SERVER_VERSION`

A test (`tests/unit/test_server.py::TestVersionConsistency`) enforces they match.
The release tag must be `v<that version>` — the Release workflow
(`.github/workflows/release.yml`) refuses to publish to PyPI on a mismatch.

## Project Layout

```
src/pipecat_context_hub/
├── cli.py                    # CLI entry point (serve + refresh)
├── cli_query.py              # One-shot query subcommands (the MCP tools as shell commands)
├── cli_install.py            # `install` — register the MCP server with a coding agent
├── plugin.py                 # Typer bridge: mounts this CLI as `pipecat context-hub`
├── shared/                   # Pydantic data contracts, interfaces, config
│   ├── types.py              # Pydantic models (MCP I/O, chunks, evidence)
│   ├── config.py             # HubConfig + env-aware computed fields
│   ├── env_loading.py        # load_env_layers (the bootstrap every entry point calls) = load_cwd_dotenv + load_global_config — three-layer precedence
│   ├── interfaces.py         # IndexWriter/Reader, ServingIndex, Retriever, Ingester; FatalIndexError (ingesters re-raise: stops refresh), IndexUnavailableError (message for the caller)
│   ├── tracking.py           # Runtime helpers (IdleTracker)
│   ├── reranker.py           # probe_reranker — shared serve/CLI reranker startup decision
│   ├── model_loading.py      # quiet_model_loading — offline-first HF env defaults (serve + CLI)
│   ├── paths.py              # redact_home / redact_home_in_text (log redaction) + same_dir / is_inside (filesystem-identity predicates shared by every deletion guard)
│   ├── index_lock.py         # refresh.lock (exclusive for refresh; readers hold it shared while opening: refresh_excluded) / serve.lock (shared) advisory flocks; serve_is_running
│   ├── exit_codes.py         # EXIT_INDEX_UNREADY (2) / EXIT_REFRESH_IN_PROGRESS (75), shared by serve, refresh and the CLI queries
│   ├── staleness.py          # staleness_info / annotate_response — index-age footer on tool responses
│   ├── support_links.py      # RETRIEVAL_QUALITY_ISSUE_URL / BUG_REPORT_ISSUE_URL — single source for MCP + CLI report-hint URLs
│   ├── markdown.py           # fence-aware heading utils (fenced_ranges, inside_fence, iter_headings, extract_section, heading_titles) — shared by docs ingest + retrieval
│   └── versioning.py         # LATEST_SENTINEL / HEAD_SENTINEL / DEFAULT_FRAMEWORK_PIN / is_latest_sentinel / is_head_sentinel / canonicalize_framework_pin / parse_release_version / exact_release_version — shared tag/version helpers for `--framework-version` (pin, the `latest`/`head` sentinels, the default pin, and provenance stamping)
├── services/
│   ├── embedding.py          # EmbeddingService
│   ├── onnx_backend.py       # ONNX Runtime inference (bi-encoder + cross-encoder); repo-id resolution, cache probe
│   ├── ingest/               # Docs crawler, GitHub ingester, Python AST, TS tree-sitter, taxonomy, version extraction, deprecation map
│   ├── index/                # ChromaDB vector, SQLite FTS5, IndexStore, reopening (open_ready_index_store: the one reader open for serve + CLI; open_index_for_export: the same, taking the refresh hold itself, for the dashboard scripts; ReopeningIndexStore: serve's drop-and-reopen handle), hnsw_validation (open-time + post-write runaway link_lists.bin guard), chroma_schema (query_chroma_sqlite, the one raw read of Chroma's schema; chromadb_format_verified), errors (FatalIndexError subclasses incl. CompactionSwapError / CompactionRestoreError; InterruptedCompactionError, IndexNotBuiltError / MissingVectorCollectionError / MissingKeywordIndexError: readers never create the index, and IndexStore refuses a missing or empty vector half beside keyword records unless `refresh --force`; LostKeywordIndexError: IndexStore refuses a keyword half with no records beside vector records on every open, exit 2 with the reset remedy; CorruptKeywordIndexError: an unreadable keyword database, exit 2 with the reset remedy)
│   └── retrieval/            # HybridRetriever, decompose, rerank, evidence
└── server/
    ├── main.py               # MCP server setup (_SERVER_VERSION here)
    ├── transport.py          # stdio transport + parent-death / grandparent-death watchdogs (idle = fallback)
    └── tools/                # Per-tool handler modules

dashboard/
├── public/                   # Served by `just dashboard-serve`
│   ├── index.html            # Stats dashboard (loads dashboard_data.json)
│   └── latent-space.html     # 3D embedding space explorer (Three.js)
└── scripts/                  # Data extraction pipeline
    ├── extract_embeddings.py # ChromaDB → UMAP 3D → embeddings_3d.json
    ├── compute_clusters.py   # K-means clustering → clusters.json
    └── extract_dashboard.py  # Index stats → dashboard_data.json
```

## Typer Bridge (`pipecat context-hub`)

The Pipecat CLI mounts plugins with `Typer.add_typer`, so a plugin must expose a
`typer.Typer`; this CLI is click. `plugin.py` registers one passthrough Typer
command per click command and hands raw argv to the click group, which does all
the parsing.

The reason parsing must stay wholly inside click: typer vendors a private copy
of click (`typer._click`) whose exception types are *not* the ones a real
`click.Command` raises. Passing argv across the boundary — rather than letting
typer dispatch real click commands — is what keeps usage errors, subcommand
`--help`, and exit codes identical through either front door.

Two things break quietly if changed:

- **`help_option_names: []`** in the passthrough context settings. Without it,
  `pipecat context-hub <cmd> --help` renders the empty stub's help instead of the real
  command's. Pinned by `test_subcommand_help_is_the_real_one`.
- **Command parity.** Registration is generated from `hub_cli.commands`, so a new
  command appears automatically; `test_every_click_command_is_bridged` fails if
  that ever stops being true.

`typer` is a peer dependency, supplied at runtime by `pipecat-ai[cli]`. It is in
the `dev` extra only so the bridge is tested rather than skipped — do **not**
add it to `[project.dependencies]`.

## Pre-Merge Live MCP Smoke Test

Run populated-index, serve, watchdog, and semantic-query smoke items on POSIX
systems only. Windows intentionally fails these operations closed because the
descriptor-relative no-reparse filesystem contract is unavailable; Windows CI
covers package and path-safety checks.

Before merging any PR that touches retrieval, tool handlers, index backends,
or types, reconnect the MCP server and run these queries against the live
local index. Unit tests mock the retrieval layer and cannot catch page
assembly, filter semantics, schema issues, or stale tool metadata that only
surface against real indexed data.

**When a live failure is found in the field, freeze it twice:** a unit
regression test (with the real input text frozen into the test) *and* the
failing query added here as a numbered item — so any future change to the
harness gets re-checked against the exact query that once broke.

The numbered checklist lives in [docs/smoke-test.md](docs/smoke-test.md).

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

The PR template's **Not run** section is part of this gate: resolve every
item listed there — run it, or accept the gap with a stated reason — before
merging. A skipped check left unresolved is the highest-signal predictor of
a bug in the next PR (see PR #116 → PR #122).

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
