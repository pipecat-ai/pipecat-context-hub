# Pipecat Context Hub

Local-first MCP server providing Pipecat docs, examples, and API context.

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
uv run pipecat-context-hub refresh --force          # full re-ingest
uv run pipecat-context-hub refresh --force --reset-index  # recover unhealthy local Chroma state
uv run pipecat-context-hub refresh --framework-version v0.0.96  # index framework at a specific tag
uv run pipecat-context-hub serve                    # start MCP server (`start` is an alias)
uv run pipecat-context-hub install --print-config   # show MCP client config; change nothing
```

Installed alongside `pipecat-ai[cli]`, every command is also reachable as
`pipecat context-hub <command>` — the same click group, bridged into the Pipecat CLI by
`plugin.py`. See "Typer bridge" below.

Use `refresh --force --reset-index` when the persisted local Chroma index is
unhealthy and needs a clean rebuild.

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

Exit codes: 0 success, 1 invalid input, 2 index missing/empty (stderr says to
run `refresh`). A parity test (`tests/unit/test_cli_query.py`) enforces that
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
— calls `shared/env_loading.py`'s `load_cwd_dotenv()` then `load_global_config()`
before constructing config, so they all resolve identically. Full precedence
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
│   ├── interfaces.py         # IndexWriter/Reader, Retriever, Ingester
│   ├── tracking.py           # Runtime helpers (IdleTracker)
│   ├── reranker.py           # probe_reranker — shared serve/CLI reranker startup decision
│   ├── model_loading.py      # quiet_model_loading — offline-first HF env defaults (serve + CLI)
│   ├── paths.py              # redact_home / redact_home_in_text — home-path redaction for logs
│   ├── staleness.py          # staleness_info / annotate_response — index-age footer on tool responses
│   ├── support_links.py      # RETRIEVAL_QUALITY_ISSUE_URL / BUG_REPORT_ISSUE_URL — single source for MCP + CLI report-hint URLs
│   └── markdown.py           # fence-aware heading utils (fenced_ranges, inside_fence, iter_headings, extract_section, heading_titles) — shared by docs ingest + retrieval
├── services/
│   ├── embedding.py          # EmbeddingService
│   ├── onnx_backend.py       # ONNX Runtime inference (bi-encoder + cross-encoder); repo-id resolution, cache probe
│   ├── ingest/               # Docs crawler, GitHub ingester, Python AST, TS tree-sitter, taxonomy, version extraction, deprecation map
│   ├── index/                # ChromaDB vector, SQLite FTS5, IndexStore
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

## Release Notes Template

GitHub releases must follow this format for consistency. Pull content from
`CHANGELOG.md` — the release note is a reader-friendly version, not a copy-paste.

```markdown
## What's New

[1-2 sentence summary of the release theme — what capability does this add or what problem does it solve?]

### Added
- **Feature name** — description

### Changed (if applicable)
- ...

### Fixed (if applicable)
- ...

---

**Upgrade:** `uv sync --extra dev --group dev` then `uv run pipecat-context-hub refresh --force`
**Full changelog:** https://github.com/pipecat-ai/pipecat-context-hub/compare/vPREVIOUS...vCURRENT
```

Rules:
- **Title:** version tag only (e.g., `v0.0.17`). No descriptive suffixes.
- **Sections:** use Keep a Changelog categories (`Added`, `Changed`, `Fixed`, `Security`, `Removed`). Only include sections that apply.
- **Upgrade line:** always present. Use `uv sync` (not `pip install`).
- **Full changelog link:** always present (except v0.0.1). Use GitHub compare URL.
- Do NOT add `Test Coverage`, `Index Impact`, or `Example Queries` sections — these belong in PR descriptions, not releases.

## Cross-Encoder Reranking

Cross-encoder reranking is **enabled by default**. It scores query-result pairs
for semantic relevance after RRF merge, significantly improving result quality
(especially for `search_examples` and multi-concept queries).

- **First run:** `uv run pipecat-context-hub refresh` downloads the model (size
  depends on selection — see table below)
- **Disable:** `PIPECAT_HUB_RERANKER_ENABLED=0` env var
- **Swap models:** `PIPECAT_HUB_RERANKER_MODEL=<name>` env var (unknown values
  log a warning and fall back to the default — server always boots)
- **Latency:** ~50-100ms per query on CPU (MiniLM-L-6-v2)
- **Offline:** gracefully disabled if model not cached (falls back to RRF-only)
- **Startup warning:** when reranker is disabled at boot, `serve` logs
  `Reranker disabled at startup: reason=<config_disabled|not_cached> configured_model=…`
  plus a remediation hint. For `not_cached`, the hint names the exact HF cache
  directory probed (respects `HF_HOME` / `HUGGINGFACE_HUB_CACHE`).
- **Startup banner:** on `serve` boot, one `INFO` line reports version,
  data directory (home-redacted to `~/…`), total record count, and
  `counts_by_type={code=N,doc=N,source=N}` — use this to confirm the upgraded
  binary is running and the index is populated.
- **Verify active model:** call `get_hub_status` — `reranker_enabled` reports
  live runtime state (not just configured intent), `reranker_model` is the
  active model name, `reranker_configured_model` is what the operator asked
  for, and `reranker_disabled_reason` explains why reranking is off
  (`config_disabled` | `not_cached` | `load_failed`) when `reranker_enabled` is false

| Model | ONNX download | Notes |
|-------|------|-------|
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | ~91 MB | Default — balanced quality/speed |
| `cross-encoder/ms-marco-MiniLM-L-12-v2` | ~134 MB | Higher quality, slower |
| `cross-encoder/ms-marco-TinyBERT-L-2-v2` | ~18 MB | Fastest download, lower quality — use on slow/throttled networks |

After swapping models, run `uv run pipecat-context-hub refresh` once to
pre-download the new model before the first MCP query.

## Windows tips

- The refresh summary uses U+2500 box-drawing characters. On non-UTF-8 consoles
  (cp1252, cp1254, cp437, etc.) the hub falls back to ASCII `-` automatically —
  no crash, no lost output. To get the box-drawing look, set
  `PYTHONIOENCODING=utf-8` before invoking `refresh` (or use Windows Terminal,
  which defaults to UTF-8).
- If `refresh` previously ran but returns zero code results, the local clone may
  be half-initialized from an interrupted run. The hub now detects this on the
  next `refresh` and re-clones; look for `Recovered N corrupt clone(s)` in the
  summary. As a manual remedy you can delete `%LOCALAPPDATA%\pipecat-context-hub\repos\`.
- On `serve` boot the hub pre-warms the embedding model (and cross-encoder when
  enabled) so the first MCP query doesn't hang. This used to matter a great
  deal: loading through `sentence-transformers` dragged in `torch` and Windows
  CPU cold-starts could take 30-130s, long enough to exceed Claude Code's
  tool-permission window. The ONNX backend loads in well under a second, so
  pre-warm is now a minor optimisation rather than a necessity. Set
  `PIPECAT_HUB_WARMUP=0` to skip it. Look for
  `Embedding model pre-warmed in …s` and (optionally)
  `Cross-encoder pre-warmed in …s` in the startup log.
