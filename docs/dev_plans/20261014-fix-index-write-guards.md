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
   fails open if the filesystem doesn't support flock. A live `serve` only produces a warning, and itself backs off while the
   refresh runs (see below).
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
- Readers during a refresh (decided 2026-10-15): `serve` answers "retry in
  60-120 seconds", defers opening the index if started mid-refresh, and reopens
  after (`ReopeningIndexStore`; `refresh` touches its lock file on start and `serve`
  compares that generation on every call, so even a sub-second refresh no call
  overlapped is noticed); CLI queries exit 75.
  Review round 1 (2026-10-15): a probe-then-open left a window for a refresh to
  start between the two, so every reader open (serve's first open and reopens,
  CLI queries) now runs under a shared hold on `refresh.lock`
  (`refresh_excluded`), and `refresh` retries its exclusive lock for ~10 s so a
  reader's hold is not mistaken for another refresh. The generation is recorded
  per handle at open time (inside the hold), so an in-flight call never reopens
  mid-refresh and a refresh right after an open is still noticed. A replaced
  handle is closed only after its last in-flight call. Reopens run the startup
  readiness checks and reload the deprecation map. The gate lives on
  `ReopeningIndexStore` (typed against `shared.interfaces.ServingIndex`).
  Smoke-tested end to end (serve started mid-refresh, tool call mid-refresh,
  recovery after; reopen after a refresh no call overlapped).
- `--force` (decided 2026-10-15): compaction at the end of a forced refresh
  (`VectorIndex.compact`, staging collection + swap + orphan segment removal)
  rather than clearing up front, because the per-repo delete-after-successful-
  fetch ordering protects records of repos whose fetch fails. The swap parks
  the old collection as `latest_precompact` before promoting staging, and
  `VectorIndex` restores it on open if a crash hit between the two renames.
- chromadb format coupling: `hnsw_validation` parses hnswlib's private format.
  It fails closed only on verified chromadb releases (`VERIFIED_CHROMADB_RELEASES`,
  1.5); elsewhere header/structure mismatches warn and skip, and orphan segment
  removal is skipped. `test_installed_chromadb_is_a_verified_release` fails on a
  chromadb bump to force re-verification.
- Chroma's SQLite runs in rollback-journal (`delete`) mode, verified on 1.5.9
  with a writer open, so the validator's `immutable=1` read can't miss
  WAL-only segment rows.
- Upstream fix for chroma#7510 is in flight (not in 1.5.9, the latest release).
- Windows: `msvcrt` has no shared lock mode, so a second `serve` is not
  advertised to `refresh`; probes retry 3x so two readers don't read each
  other as a running refresh (a reader holding the lock across an index open
  widens that window; the other reader then gets the retry message). A real fix is `LockFileEx` via ctypes, untestable
  without a Windows host.
- Header-version strictness, orphan-segment mtime handling: not exercised; left
  as is.
