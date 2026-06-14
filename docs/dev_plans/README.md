# Development Plans

## Current Tasks
| Date | Type | Name | Status | Assignee | Branch | Plan |
|---|---|---|---|---|---|---|
| 2026-06-12 | feat | Consume upstream `deprecations.json` as primary deprecation source (parser becomes fallback) | Planned (blocked on pipecat#4722) | vr000m | _(future)_ `feature/deprecations-json-consumer` | `20260612-feature-deprecations-json-consumer.md` |

## Completed Tasks
| Date | Type | Name | Status | Assignee | Branch | Plan |
|---|---|---|---|---|---|---|
| 2026-06-13 | fix | `get_doc()` sections always empty — derive from page headings (fence-aware, no re-index) | Complete (PR #83) | vr000m | `fix/get-doc-sections-from-headings` | _(no plan file)_ |
| 2026-06-12 | chore | Release 0.2.0 — first PyPI publish (`pipecat-ai-context-hub` under the `pipecat` org) | Complete (v0.2.0) | vr000m | `release/0.2.0` | `20260612-chore-release-0-2-0.md` |
| 2026-06-12 | feat | PyPI trusted-publishing release workflow + dist rename to `pipecat-ai-context-hub` | Complete (v0.2.0) | vr000m | `feat/pypi-release-workflow` | _(no plan file)_ |
| 2026-06-12 | docs | Agent-oriented CLI `--help` (exit codes, filter enum values, chaining hints) | Complete (v0.2.0) | markbackman | `docs/agent-oriented-cli-help` | _(no plan file)_ |
| 2026-06-12 | feat | Staleness footer on tool responses (both front doors) | Complete (v0.2.0) | markbackman | `feat/staleness-footer` | _(no plan file)_ |
| 2026-06-12 | feat | One-shot CLI query subcommands (every MCP tool as a shell command) | Complete (v0.2.0) | markbackman | `feat/cli-query-subcommands` | `20260612-feature-cli-query-subcommands.md` |
| 2026-06-06 | feat | Add RN transports to default ingest; keep pipecat-cli opt-in; pip-audit fix + justfile↔CI ignore parity | Complete (v0.2.0) | vr000m | `feat/ingest-rn-transports-and-cli` | _(no plan file)_ |
| 2026-05-30 | chore | chromadb 1.x migration + Python 3.14 prep (format break) | Complete (v0.1.0) | vr000m | `chore/chromadb-1x-python-314` | `20260529-chore-chromadb-1x-python-314.md` |
| 2026-05-30 | fix | Bound `serve` lifetime to real client under `uv run` (grandparent-death watchdog) | Complete (v0.1.0) | vr000m | `fix/serve-uv-run-grandparent-watchdog` | `20260530-fix-serve-uv-run-grandparent-watchdog.md` |
| 2026-04-21 | bug | Bound `serve` process lifetime to its client (parent-death watchdog) | Complete (v0.0.18) | vr000m | `feature/serve-orphan-watchdog` | `20260421-bug-serve-orphan-watchdog.md` |
| 2026-04-20 | feature | Reranker Startup Telemetry (banner, HF cache-path diagnostics, degraded-hub guideline) | Complete (v0.0.17) | vr000m | `feature/reranker-startup-telemetry` | _(no plan file)_ |
| 2026-04-20 | feature | Fail-fast on empty or unopenable index in `serve` | Complete (v0.0.17) | vr000m | `feature/fail-fast-empty-index` | _(no plan file)_ |
| 2026-04-20 | feature | Configurable Cross-Encoder Reranker Model Selection | Complete (v0.0.17) | vr000m | `feature/reranker-model-selection` | `20260420-feature-reranker-model-selection.md` |
| 2026-04-20 | bug | Windows Refresh Resilience (corrupt clone recovery + non-UTF-8 console) | Complete (v0.0.17) | vr000m | `bug/windows-refresh-resilience` | `20260420-bug-windows-refresh-resilience.md` |
| 2026-03-31 | feature | Version-Aware Indexing Phase 1a+1b+2 | Complete (v0.0.15) | vr000m | `feature/version-aware-indexing` | `20260331-feature-version-aware-indexing.md` |
| 2026-03-31 | feature | Phase 2: Tree-sitter TypeScript Extraction | Complete (v0.0.13) | vr000m | `feature/tree-sitter-ts-phase2` | `20260330-feature-tree-sitter-ts-phase2.md` |
| 2026-03-30 | feature | Phase 1: Multi-Language API Extraction | Complete (v0.0.12) | vr000m | `feature/multi-language-parsing-plan` | `20260218-design-pipecat-context-hub.md` |
| 2026-03-25 | chore | MCP Server Audit & Hardening | Complete | vr000m | `chore/mcp-server-audit` | `20260325-chore-mcp-server-audit-hardening.md` |
| 2026-03-24 | bug | Chroma Index Recovery & Benchmark Hardening | Complete | vr000m | `feature/daily-python-indexing` | `20260324-bug-chroma-index-recovery.md` |
| 2026-03-24 | feature | Language & Domain Filtering for search_examples | Complete | vr000m | `feature/language-domain-filtering` | `20260218-design-pipecat-context-hub.md` |
| 2026-03-24 | feature | Advanced Reranking & Retrieval Quality | Complete | vr000m | `feature/advanced-reranking` | `20260218-design-pipecat-context-hub.md` |
| 2026-03-23 | feature | Per-method import extraction for dependency_notes | Complete | vr000m | `feature/per-method-imports` | `20260218-design-pipecat-context-hub.md` |
| 2026-03-22 | feature | Snippet Enrichment (dependency_notes, companion_snippets, interface_expectations) | Complete | vr000m | `feature/snippet-enrichment` | `20260218-design-pipecat-context-hub.md` |
| 2026-02-28 | feature | Multi-Concept Query Decomposition | Complete (v0.0.5) | vr000m | `feature/multi-concept-search` | `20260218-design-pipecat-context-hub.md` |
| 2026-02-18 | design | Pipecat Context Hub | Complete (v0.0.4) | vr000m | `main` | `20260218-design-pipecat-context-hub.md` |
