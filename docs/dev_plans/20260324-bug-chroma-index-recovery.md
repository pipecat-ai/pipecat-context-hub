# Chroma Index Recovery & Benchmark Hardening

## Header
- **Status:** Complete
- **Type:** bug
- **Assignee:** vr000m
- **Priority:** High
- **Working Branch:** feature/daily-python-indexing
- **Created:** 2026-03-24
- **Target Completion:** 2026-03-24
- **Objective:** Recover cleanly from unhealthy persisted Chroma state, shut down Chroma background workers on exit, and make the live retrieval benchmark fail fast with a clear rebuild instruction.

## Context

The live retrieval-quality benchmark can hang for several minutes against the current local index and then emit repeated Chroma HNSW consumer errors during interruption and shutdown. Direct vector queries reproduce the same failure, which means the problem is below retrieval scoring: the persisted vector index can become unhealthy, and the current code does not provide a clean recovery path or consistently shut down Chroma background services.

## Requirements

- Add explicit Chroma shutdown to the index lifecycle so background worker threads stop cleanly.
- Provide a supported way to wipe and rebuild the local index when persisted Chroma state is unhealthy.
- Keep the recovery path small and local to index/CLI layers.
- Make the retrieval-quality benchmark detect unhealthy vector state quickly and report the rebuild command instead of hanging indefinitely.
- Preserve existing refresh behavior unless the new reset option is explicitly requested.

## Implementation Checklist

- [x] Add `close()` and full reset support to the vector index.
- [x] Add store-level reset and lifecycle plumbing for both backends.
- [x] Add a `refresh --reset-index` recovery path and guaranteed store shutdown in CLI flows.
- [x] Harden the live quality benchmark with a fast vector health probe and clearer failure output.
- [x] Add or update unit tests for reset/close behavior and the new CLI option.
- [x] Update docs for the new recovery command.

## Technical Specifications

- Files to modify:
  - `src/pipecat_context_hub/services/index/vector.py`
  - `src/pipecat_context_hub/services/index/fts.py`
  - `src/pipecat_context_hub/services/index/store.py`
  - `src/pipecat_context_hub/cli.py`
  - `tests/benchmarks/test_retrieval_quality.py`
  - `tests/unit/test_index_store.py`
  - `tests/unit/test_cli.py`
  - `docs/README.md`
- `VectorIndex.close()` should be idempotent and stop the Chroma system only when the last in-process client for that persistence path is released.
- `VectorIndex.reset()` should rebuild the on-disk Chroma directory so recovery does not depend on deleting a potentially wedged collection in place.
- `IndexStore.reset()` should clear both search backends and wipe cached metadata so a subsequent refresh cannot skip sources based on stale hashes/SHAs.
- `refresh --reset-index` should imply a forced rebuild and should document the intended recovery use case.
- The benchmark should perform a subprocess-based vector health probe so timeouts can terminate a wedged query without leaving the pytest worker stuck on a background thread.

## Testing Notes

- `uv run pytest tests/unit/test_index_store.py tests/unit/test_cli.py -q`
- `PIPECAT_HUB_ENABLE_QUALITY_BENCHMARK=1 uv run pytest tests/benchmarks/test_retrieval_quality.py -q -s`
- `uv run ruff check src/pipecat_context_hub/services/index/vector.py src/pipecat_context_hub/services/index/fts.py src/pipecat_context_hub/services/index/store.py src/pipecat_context_hub/cli.py tests/benchmarks/test_retrieval_quality.py tests/unit/test_index_store.py tests/unit/test_cli.py`
- Manual verification of the recovery command may be limited by the read-only sandbox.

## Issues & Solutions

- Chroma shutdown uses internal client state rather than a public `close()` API in 0.6.3.
  Solution: encapsulate the stop logic in `VectorIndex` so the rest of the codebase does not depend on Chroma internals.
- A simple async timeout around `vector_search()` is not sufficient because the underlying worker thread can continue running.
  Solution: move the benchmark health check into a subprocess and enforce the timeout there.

## Acceptance Criteria

- [x] `IndexStore.close()` shuts down both SQLite and Chroma-backed resources cleanly.
- [x] `pipecat-context-hub refresh --force --reset-index` performs a full local rebuild path.
- [x] The live retrieval-quality benchmark reports an unhealthy vector index quickly instead of hanging for minutes.
- [x] Tests cover the new reset/close behavior and CLI wiring.
- [x] User-facing docs mention the recovery command.

## Final Results

- Added explicit Chroma lifecycle management in `VectorIndex`, including idempotent close behavior and a guarded full reset path for recovery flows.
- Added `FTSIndex.reset()` and `IndexStore.reset()` so CLI recovery can wipe both search backends and stale metadata in one operation.
- Added `refresh --reset-index`, ensured both `refresh` and `serve` close the store on exit, and documented the rebuild command.
- Hardened the live retrieval-quality benchmark with a subprocess-based vector health probe. On the current unhealthy local index, it now fails in about 16 seconds with the rebuild command instead of hanging for several minutes.
