# Pipecat Context Hub Architecture Plan

## Header
- **Status:** Not Started
- **Type:** design
- **Assignee:** TBD
- **Priority:** High
- **Working Branch:** N/A (repository not initialized in this directory)
- **Created:** 2026-02-18
- **Target Completion:** 2026-03-06
- **Objective:** Design and implement a local-first MCP platform that provides fresh Pipecat docs/examples context for Claude Code, Cursor, VS Code, and Zed.

## Release Scope
### v0 MVP (target: 2026-03-06)
- Retrieval-first local MCP server over `stdio`.
- Core tools only: `search_docs`, `get_doc`, `search_examples`, `get_example`, `get_code_snippet`.
- Local index refresh workflow (`refresh` command), no hosted ops stack.
- Sources: Pipecat docs + `pipecat/examples` (including `examples/foundational`) + `pipecat-examples`.

### v1 Follow-up (post-MVP)
- Higher-order tools: `compose_solution`, `propose_architecture`.
- More advanced reranking and guardrail inference.
- Optional scheduled auto-refresh and richer local observability.

## Context
Pipecat developers need grounded context for coding and ideation based on rapidly changing docs and examples. A static prompt-only approach drifts quickly and does not provide verifiable citations or reproducible outputs.  

The proposed solution is a Pipecat Context Hub with:
- MCP server capabilities for tool-based retrieval and planning support.
- LLM/Codex-led orchestration that consumes MCP outputs and composes solutions.
- Retrieval-first behavior: maximize source-grounded context quality, minimize unnecessary generation.
- A continuously refreshed knowledge base for docs and example code.
- Client compatibility across local IDE/agent MCP clients.
- A single local operating mode optimized for development and hackathons.

## Requirements
1. Provide MCP tools for document and example retrieval with source citations.
2. Support local MCP transport (`stdio`) with simple local setup.
3. Maintain freshness via scheduled + event-driven ingestion.
4. Prioritize freshness using a single `latest` index in v0.
5. Include cross-client onboarding for Claude Code, Cursor, VS Code, and Zed.
6. Optimize v0 for the primary use case: "find the right docs/examples/snippets to build a Pipecat bot."
7. Keep architecture modular so retrieval quality and storage backends can evolve independently.
8. Model foundational-example classes as first-class retrieval metadata from `pipecat/examples/foundational`.
9. Return composability guidance and dependency closure metadata in tool outputs.
10. Prioritize retrieval accuracy over synthesis; generate glue code only for explicit gaps.
11. Return explicit `known` and `unknown` items so Codex can trigger follow-up retrieval when needed.

## Implementation Checklist

### Phase 1: Foundations (v0)
- [ ] Define service boundaries: ingestion, indexing, retrieval, MCP runtime.
- [ ] Define canonical metadata schema (`source_url`, `repo`, `path`, `commit_sha`, `indexed_at`, `chunk_id`).
- [ ] Define chunking and embedding policies for docs vs code.
- [ ] Finalize initial MCP tool contract.
- [ ] Define composability response schema (interfaces, dependencies, glue points, assumptions).

### Phase 2: Ingestion and Indexing (v0)
- [ ] Implement docs crawler for `docs.pipecat.ai`.
- [ ] Implement GitHub ingest for `pipecat-ai/pipecat` and `pipecat-ai/pipecat-examples`.
- [ ] Implement local refresh workflow (`refresh` command) for rebuilding `latest` index.
- [ ] Build v0 single-index strategy: `latest`.
- [ ] Build fully automated taxonomy manifests:
  - [ ] `examples/foundational` class -> example -> capability mapping.
  - [ ] `pipecat-examples` capability mapping with no manual curation in v0.
- [ ] Add optional DeepWiki ingestion as a secondary source (explicit URL allowlist).

### Phase 3: Retrieval and Quality (v0)
- [ ] Implement hybrid retrieval (vector + keyword + metadata filters).
- [ ] Implement reranking tuned for code intent and architecture intent.
- [ ] Add mandatory citation payload and confidence metadata.
- [ ] Add known/unknown evidence reporting in retrieval responses.
- [ ] Implement heuristic `next_retrieval_queries` generation for unresolved gaps (no server-side LLM required).
- [ ] Add trace logging for retrieval decisions.
- [ ] Add capability tags and symbol maps for examples (e.g., `rtvi`, `screen-share`, `wake-word`, `gemini-video`, `tts`).
- [ ] Add evidence packs that enable Codex to infer execution mode (`one-shot`, `event-triggered`, `long-running monitor`).

### Phase 4: MCP Server and Client Compatibility (v0)
- [ ] Implement MCP tools:
  - [ ] `search_docs`
  - [ ] `get_doc`
  - [ ] `search_examples`
  - [ ] `get_example`
  - [ ] `get_code_snippet`
- [ ] Implement local runtime (`stdio`) for hackathons.
- [ ] Build client setup guides/templates for Claude Code, Cursor, VS Code, and Zed.

### Phase 5: Validation and Release (v0)
- [ ] Validate local retrieval-first user journeys for coding and ideation.
- [ ] Run load and latency tests on top retrieval paths.
- [ ] Publish local setup + refresh runbook.
- [ ] Cut v0 local release.

### Phase 6: Composition Layer (v1)
- [ ] Implement `compose_solution` and `propose_architecture`.
- [ ] Add advanced guardrail inference and verification policies.
- [ ] Add optional scheduled auto-refresh and expanded observability.

## Technical Specifications

### Target Architecture
1. **Ingestion service**
- Pulls docs and repository content.
- Normalizes into chunked records with deterministic IDs.
- Emits index update jobs.

2. **Knowledge store**
- Vector index + keyword index + metadata store.
- Single namespace for `latest` in v0.

3. **Retrieval service**
- Query understanding and filter planning.
- Hybrid retrieval + rerank.
- Source attribution and citation shaping.

4. **MCP core**
- Tool definitions and request orchestration.
- Transport adapters:
  - `stdio` adapter for local clients.

5. **Reasoning and orchestration layer (LLM/Codex)**
- Uses MCP evidence to infer intent, choose execution mode, and compose final design.
- Consumes structured context packs (citations, snippets, dependencies, guardrails).
- Produces user-facing plans and implementation drafts with assumptions clearly labeled.

### Source Targets (v0)
- `https://docs.pipecat.ai/` (primary docs source).
- `https://github.com/pipecat-ai/pipecat/tree/main/examples` (including `examples/foundational`).
- `https://github.com/pipecat-ai/pipecat-examples` (project-level examples).
- `https://deepwiki.com/pipecat-ai/pipecat/2-getting-started` and same-repo DeepWiki paths (optional secondary source, disabled by default).

### v0 Technology Defaults
- **Language:** Python 3.11+ (align with Pipecat ecosystem and existing examples).
- **Packaging:** `pyproject.toml` + `src/` layout.
- **Storage:** local SQLite database for metadata and FTS, with local vector index sidecar.
- **Embeddings:** local embedding model by default (no required API key in v0).
- **Reranking:** lightweight local reranking (fusion + heuristics) in v0; heavier model-based reranking deferred.
- **Runtime:** local `stdio` MCP server only in v0.
- **Ops:** local `refresh` command + logs; no webhook server or dashboard requirement in v0.

### Known / Unknown (v0 decisions)
- **Known:** Retrieval-first workflow and core tool set are in scope for March 6.
- **Known:** Fully automated taxonomy extraction from `pipecat/examples/foundational` and `pipecat-examples`.
- **Known:** `latest` is the only index in v0.
- **Unknown:** Final vector backend implementation details (to be selected during Phase 1 benchmark).
- **Unknown:** Whether DeepWiki adds enough recall value for v0 to remain enabled by default.

### MCP Tool Contracts (v0)
1. `search_docs`
- **Purpose:** Return ranked doc chunks for conceptual/API queries.
- **Input:** `query`, optional `area`, optional `limit`.
- **Output:** Ranked hits with `doc_id`, `title`, `section`, snippet text, and citation metadata.

2. `get_doc`
- **Purpose:** Fetch a canonical doc page/section by identifier.
- **Input:** `doc_id`, optional `section`.
- **Output:** Full normalized markdown for that page/section plus source URL and indexed timestamp.

3. `search_examples`
- **Purpose:** Find relevant examples by task, modality, stack, or component.
- **Input:** `query`, optional `repo`, optional `language`, optional `tags`, optional `foundational_class`, optional `execution_mode`, optional `limit`.
- **Output:** Ranked example records with summary, foundational class, capability tags, key files, and commit metadata.

4. `get_example`
- **Purpose:** Retrieve an example package or a specific file from it for full-context understanding.
- **Input:** `example_id`, optional `path`, optional `include_readme`.
- **Output:** Example metadata and file content (full file or selected file), with repo/commit citation and detected symbols.

5. `get_code_snippet`
- **Purpose:** Return targeted code spans for reuse, not full files.
- **Input:** one of `symbol` | `intent` | `path + line range`, optional `framework`, optional `example_ids`, optional `max_lines`.
- **v0 behavior:** `intent` and `path + line range` are required capabilities; `symbol` lookup is best-effort and may fall back to intent/path retrieval.
- **Output:** Minimal snippet(s) with start/end lines, dependency notes, required companion snippets, and interface expectations.

### MCP Tool Contracts (v1 deferred)
1. `compose_solution`
- **Purpose:** Build a capability graph by combining snippets from multiple examples and filling missing glue logic.
- **Input:** `goal`, optional `required_capabilities`, optional `constraints`, optional `target_stack`, optional `execution_mode`.
- **Output:** Composition plan with:
  - capability-to-snippet mapping
  - composability guidelines (how to connect components safely)
  - integration contracts between components
  - runtime loop design (startup, trigger handling, shutdown)
  - identified gaps requiring synthesized code only when no grounded implementation is found (for example circular frame buffer)
  - inferred guardrails and verification checks when supported by source evidence.

2. `propose_architecture`
- **Purpose:** Produce a composable implementation plan grounded in docs + examples.
- **Input:** `goal`, optional `constraints`, optional `target_stack`.
- **Output:** Architecture proposal with:
  - recommended components
  - ordered build steps
  - mapped citations
  - chosen execution mode and trigger strategy
  - snippet references (from `get_code_snippet`) and composition references (from `compose_solution`)
  - clearly labeled assumptions and generated-glue modules.

### Evidence Reporting Contract (all retrieval/composition tools)
- `known`: source-grounded facts with citation pointers.
- `unknown`: unresolved questions or missing implementation details.
- `confidence`: retrieval confidence score plus short rationale.
- `next_retrieval_queries`: deterministic heuristic suggestions from MCP in v0 (client LLM may append additional suggestions).

### Ops Plane
- **v0:** local configuration, `refresh` command, and local logs.
- **v1:** optional scheduler/webhook receiver and expanded observability.

### Intent-Mode Design
1. **One-shot**
- User asks for a bounded task (for example "explain this video clip").
- System assembles immediate pipeline and returns output once.

2. **Event-triggered**
- User asks for action when condition occurs (for example wake word, keyword, or threshold event).
- System runs lightweight monitor loop and executes task on trigger.

3. **Long-running monitor**
- User asks for continuous observation with periodic or rule-based actions.
- System defines lifecycle controls, backpressure policy, and checkpoint strategy.

### Reference Composition Flow (RTVI + Screen Share + Wake Word + Gemini Video)
This section is illustrative. The same composition and gap-handling pattern applies to other use cases and may require no synthesized glue code.

1. User intent query:
- "When wake word is spoken, analyze the last 30s of screen-share video with Gemini and speak summary via Pipecat TTS."

2. Intent inference:
- Codex classifies this as `event-triggered` with `wake-word` trigger using user intent + retrieved evidence.

3. Capability discovery:
- `search_examples` for `rtvi frontend screen share`.
- `search_examples` for `wake word` triggers.
- `search_examples` for `Gemini video` usage.
- `search_examples` for `Pipecat TTS output`.

4. Source retrieval:
- `get_example` for top matches to understand full pipeline setup.
- `get_code_snippet` for reusable fragments:
  - screen capture hooks
  - wake-word event handling
  - model invocation adapters
  - TTS response emission.

5. Composition:
- Codex composes a capability graph and integration sequence from retrieved evidence (v0).
- In v1, `compose_solution` can automate this step:
  - capture frames continuously
  - write into circular buffer sized for 30s at configured FPS
  - on wake-word event, freeze/export last window
  - call Gemini video understanding API
  - route returned text to Pipecat TTS stream.

6. Gap handling:
- If no example includes circular frame buffering, generate synthesized module spec:
  - bounded ring buffer API
  - memory/backpressure constraints
  - frame-to-video serialization strategy for Gemini input requirements.
- If grounded examples already cover the required behavior, no synthesized module is added.

7. Delivery:
- v0: Codex returns the architecture plan using retrieval evidence + known/unknown reporting.
- v1: `propose_architecture` can automate this output:
  - end-to-end architecture
  - ordered implementation plan
  - cited source mapping
  - explicit list of synthesized (non-example) components.

### Initial Files and Modules (proposed)
- `/server/mcp/` for MCP tool handlers and transport adapters.
- `/services/ingest/` for docs/repo ingestion jobs.
- `/services/retrieval/` for ranking and citation logic.
- `/services/index/` for indexing pipelines and snapshot management.
- `/config/clients/` for client setup templates.
- `/ops/` for local refresh scripts and diagnostics (scheduler/webhook support in v1).
- `/pyproject.toml` and `/src/pipecat_context_hub/` for Python package/project layout.

## Testing Notes
- Contract tests for MCP tools (input/output and error behavior).
- Ingestion tests for idempotency and incremental update correctness.
- Retrieval tests measuring relevance, citation completeness, and stale-hit rate.
- Integration tests per client type (Claude Code, Cursor, VS Code, Zed) using `stdio`.
- Performance tests for p50/p95 latency and concurrent query handling.
- Foundational taxonomy tests ensuring class filters affect example ranking and recall.
- Known/unknown contract tests verifying unresolved gaps are explicitly surfaced with follow-up retrieval suggestions.
- v1 tests: composition stitching, gap synthesis, and guardrail inference.

## Issues & Solutions
- **Issue:** Client feature parity differs by MCP client.
  - **Solution:** Keep critical workflows in MCP tools; treat prompts/resources as optional enhancements.
- **Issue:** Docs/examples drift quickly.
  - **Solution:** Use local `refresh` for v0 and add optional scheduler/webhook updates in v1.
- **Issue:** Reproducibility across local environments.
  - **Solution:** Return commit-level citations and pinned source metadata from `latest` for replayability.
- **Issue:** Over-generation can reduce trust in context-curation workflows.
  - **Solution:** Use retrieval-first responses and allow synthesis only for explicit, labeled gaps.

## Acceptance Criteria
- [ ] Architecture document finalized with service boundaries and data contracts.
- [ ] MCP tool contract finalized and reviewed.
- [ ] Freshness strategy implemented with measurable SLOs.
- [ ] Local `stdio` runtime operational in at least one IDE client.
- [ ] Core v0 tools operational: `search_docs`, `get_doc`, `search_examples`, `get_example`, `get_code_snippet`.
- [ ] End-to-end retrieval query returns cited docs/examples with source metadata.
- [ ] Local setup documented and tested across at least two MCP clients.
- [ ] Foundational example class metadata is queryable and affects retrieval outcomes.
- [ ] Outputs include dependency closure, composability guidance, known/unknown reporting, and guardrails when evidence supports inference.
- [ ] v1 scope explicitly deferred: `compose_solution` and `propose_architecture`.

## Final Results
Pending implementation.
