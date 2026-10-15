# Index write guards (runaway HNSW `link_lists.bin`)

**Branch:** `fix/index-write-guards` | **Review Gates:** none

## Problem
On 2026-10-13 `~/.pipecat-context-hub` was 351 GB: `chroma/<segment>/link_lists.bin`
was 15 TB logical / ~325 GB allocated (header bound ~45 MB; 112,572 graph
elements vs ~45k live records). The same corruption was diagnosed on 2026-10-04
(chroma#7510) and recurred within five days of the rebuild. Its trigger is not
established; the open-time check only fires when an index is opened.

## Changes
1. `HnswWriteGuard` (`services/index/hnsw_validation.py`): re-runs the bounded
   header-derived size check on segment dirs written since the guard was
   created; `VectorIndex.upsert` calls it after each write. Old orphan
   segments are ignored so they can't block a rebuild.
2. Disk preflight: `refresh` refuses below 5 GB free (`PIPECAT_HUB_MIN_FREE_GB`).
3. `shared/index_lock.py`: exclusive `refresh.lock` for `refresh` (re-taken
   after `--reset-index`, which deletes the data dir); shared `serve.lock` for
   `serve`. A live `serve` only produces a warning: refresh-while-serving is
   the normal workflow and nothing shows it is the trigger.
4. Cherry-picked the open-time `validate_persisted_hnsw` code and tests from
   `ccb4ca2` (currently only on `feature/chatgpt-local-plugin`).

## Not done / open
- Root cause. Candidates (unverified): interrupted refresh, concurrent
  refresh/serve, `--force` upsert churn. Needs a size-watching repro.
- Check whether chromadb > 1.5.9 fixes it.
