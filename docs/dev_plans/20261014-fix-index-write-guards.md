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
3. `shared/index_lock.py`: exclusive `refresh.lock` for `refresh`; shared `serve.lock` for
   `serve`. Lock files are siblings of the data dir (`.<dirname>.refresh.lock`),
   not inside it, so `--reset-index`'s rmtree can't unlink a held lock. Locking
   fails open if the filesystem doesn't support flock. A live `serve` only produces a warning: refresh-while-serving is
   the normal workflow and nothing shows it is the trigger.
4. Cherry-picked the open-time `validate_persisted_hnsw` code and tests from
   `ccb4ca2` (currently only on `feature/chatgpt-local-plugin`).

## Spike findings (2026-10-15, throwaway dirs, 1 GiB per-process file cap)
Scripts were scratch-only; method and results are recorded here.

| Scenario | Result |
|---|---|
| Sequential refreshes, repeated `--force` churn (delete + re-upsert x4) | Healthy. Graph element count grows ~20k per forced refresh (dead elements are never compacted): 20k -> 80k with 20k live. Explains the 112k-vs-45k graph seen on 2026-10-13; not itself the runaway. |
| Lone writer SIGKILLed mid-refresh, resumed 2x | Healthy. |
| 2-3 racing writers (same ids, or disjoint ids), then one ordinary refresh | **Reproduced the runaway** (`link_lists.bin` hit the 1 GiB cap) or a SIGSEGV (-11, the 2026-10-04 symptom) on the *next* load. Probabilistic: ~4 of 5 runs. |
| Reader opened mid-refresh, then SIGKILLed | Runaway on the next refresh in 1 of 2 runs. |
| Reader opened mid-refresh, clean close | One SIGSEGV on the next refresh in ~4 runs. |
| `serve` already open before the refresh, clean close; or `serve` opened after the refresh | Healthy. |
| Open-time validator run 7,141 times against a live writer | 0 false positives (the concurrent-read worry did not reproduce). |

Key point: after the damaging event the persisted graph has the **healthy size
and header** (so the size bound passes) and the runaway only appears on the
next load + persist. A structural walk of `link_lists.bin` (length-prefixed
records of `maxM*4+4`-byte level blocks) flags it beforehand. The walk is now
part of `check_hnsw_segment_dir` (open-time and post-write). It ran in 6 ms on
the real 45k-record index with no false positive. Consistent with upstream
chroma#7510: unvalidated `linkListSize` on load.

Implications: the `refresh.lock` is justified (concurrent writers are the one
reliably reproduced trigger). The post-write size check alone cannot stop a
fill: one native persist can write the whole file before Python regains
control, so the pre-load walk is the real defence.

## Not done / open
- Readers that open the index while a refresh is writing are a probable (not
  certain) trigger. `serve`/CLI queries only warn today; decide whether they
  should refuse or retry while `refresh.lock` is held.
- `--force` never compacts the graph; consider recreating the collection
  instead of delete + re-upsert so dead elements don't accumulate.
- Upstream fix for chroma#7510 is in flight (not in 1.5.9, the latest release).
- Header-version strictness, Windows shared locks, orphan-segment mtime
  handling: not exercised; left as is.
