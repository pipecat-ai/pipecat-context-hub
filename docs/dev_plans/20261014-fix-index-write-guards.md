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
2. Disk preflight: `refresh` refuses below 5 GB free (`PIPECAT_HUB_MIN_FREE_GB`;
   `0` disables; an invalid, negative or non-finite value falls back to 5 GB).
3. `shared/index_lock.py`: exclusive `refresh.lock` for `refresh`; shared `serve.lock` for
   `serve`. Lock files are siblings of the data dir (`<dirname>.refresh.lock`, named from
   the full dirname so `x` and `.x` never share one),
   not inside it, so `--reset-index`'s rmtree can't unlink a held lock. Locking
   fails open if the filesystem doesn't support flock, and (with a warning) if
   the lock file is a symlink (where `O_NOFOLLOW` is missing, i.e. Windows,
   checked by `lstat` before the open and after it, where the path must also
   name the file the descriptor opened, by `(st_dev, st_ino)`), not a single-link regular
   file, or another user's: an exclusive acquire writes to it. A live `serve` only
   produces a warning, and itself backs off while the refresh runs (item 5).
4. Cherry-picked the open-time `validate_persisted_hnsw` code and tests from
   `ccb4ca2` (currently only on `feature/chatgpt-local-plugin`), extended with a
   structural walk of `link_lists.bin` (see the spike findings).
5. No reads while a refresh rewrites the index (decided 2026-10-15). Every reader
   open (serve's first open and reopens, each CLI query) runs under a shared hold
   on `refresh.lock` (`refresh_excluded`) and goes through one function,
   `reopening.open_ready_index_store`. `serve` answers tool calls with "retry in
   60-120 seconds" while a refresh runs and starts with the index unopened if
   launched mid-refresh; CLI queries exit 75. `refresh` retries its exclusive lock
   for ~10 s so an open in progress isn't mistaken for another refresh, and if
   readers still hold it after that, says so instead of "already running". Both
   refusals exit 75 (`exit_codes.EXIT_REFRESH_IN_PROGRESS`, shared with the CLI
   queries). Readers never create the index: `create` defaults to False on
   `IndexStore`, `VectorIndex` and `FTSIndex` (only `refresh` passes True), so
   a never-built index raises `IndexNotBuiltError` ("run refresh"), and a
   missing or incomplete keyword database `MissingKeywordIndexError` ("run
   refresh --force"), instead of opening an empty one beside a populated other
   half. `IndexStore` opens the keyword half first: when it has records, the
   vector half may be created only with `rebuild=True` (`refresh --force`), and
   a missing database or `latest` (from `VectorIndex`) or an empty `latest`
   (from `IndexStore`) raises `MissingVectorCollectionError` ("run refresh
   --force") before anything is written, for readers and for a plain `refresh`
   (exit 2) alike, rather than skipping unchanged sources and leaving vector
   search empty. An open writes only an incomplete keyword schema (`refresh`
   completes it and rebuilds the FTS table from `chunks`); a reader refuses one.
   The dashboard scripts open through `reopening.open_index_for_export` (the
   same `open_ready_index_store`, but taking `refresh_excluded` itself, since
   its callers hold nothing else), so they get the same exclusion, open-time
   checks and operator messages.
6. `serve` reopens the rebuilt index (`ReopeningIndexStore`). `refresh` touches its
   lock file and steps its size (so two starts within one coarse filesystem
   timestamp tick still differ) on start. The generation is the lock file's
   `(st_ino, st_mtime_ns, st_size)` (`index_lock.refresh_generation`), so a
   recreated lock file also counts; the handle records it at open and compares it
   on every call, so even a sub-second refresh no call overlapped is noticed.
   There is only ever one open store: a stale one is closed once its last
   in-flight call ends, and only then is the next opened (calls meanwhile get a
   "retry in a few seconds" message), because chromadb shares one native System
   per path among open clients and a client opened beside the stale one serves
   the stale graph. Reopens run the startup readiness checks and reload the
   deprecation map.
7. `--force` compaction (decided 2026-10-15): `VectorIndex.compact` at the end of
   a forced refresh (staging collection + swap + orphan segment removal, the
   last only when `chroma_schema.chromadb_format_verified()`, i.e. 1.5.x) rather
   than clearing up front, because the per-repo delete-after-successful-fetch
   ordering protects records of repos whose fetch fails. Non-fatal on failure,
   except an HNSW guard failure (`FatalIndexError`), which stops the refresh like
   anywhere else, and a swap that fails leaving no `latest` collection, whichever
   rename raised (`CompactionSwapError`, a `FatalIndexError`: exit 2, nothing deleted, the next
   `refresh` restores the backup). A restore at that open that fails, judged by
   the collection listing as in `compact()` so a rename that commits and then
   raises counts as restored, is `CompactionRestoreError` (also fatal, exit 2,
   prescribing `refresh --force --reset-index` since rerunning `refresh` retries
   the same rename). "No `latest`" is decided from the collection
   listing; if even that fails, the original error stays non-fatal and nothing
   is deleted. The swap parks the old collection as `latest_precompact` before
   promoting staging; a crash between the two renames is restored when `refresh`
   next opens the index (`IndexStore(..., recover=True)`, under its exclusive
   lock). Readers never repair it: they get `InterruptedCompactionError` ("run
   refresh").
8. `shared.interfaces.FatalIndexError`: the on-disk index is unsafe to keep
   using. The index errors (`IncompatibleIndexFormatError`,
   `CorruptHnswGraphError`) subclass it; ingesters re-raise it instead of
   recording one more per-source error, so `refresh` stops (exit 2).

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
- An index read already in progress when a refresh starts finishes on the
  handle it holds (the shared `refresh.lock` hold covers the open, not the read).
  Decided: `serve` must not block a refresh for the duration of reads. Reads that
  start after the refresh begins, including a running call's next index access,
  get the retry message.
- Locking fails open when flock is unsupported or the lock file is unwritable
  (decided; 21aad5f, 59690b98).
- `refresh --force` after a lost vector collection re-ingests only the sources
  in its configuration; a retained repo absent from it (kept, not pruned) stays
  keyword-only. Documented in docs/README.md (re-add it for that run, or
  `--prune`); not fixed here.
- chromadb format coupling: `hnsw_validation` parses hnswlib's private format.
  It fails closed only on verified chromadb releases (`VERIFIED_CHROMADB_RELEASES`,
  1.5); elsewhere header/structure mismatches warn and skip, and orphan segment
  removal is skipped. Every raw read of Chroma's sqlite schema goes through
  `chroma_schema.query_chroma_sqlite` (immutable for pre-open probes, a locked
  snapshot before orphan removal); collection names come from the client API.
  `test_installed_chromadb_is_a_verified_release` fails on a chromadb bump to
  force re-verification.
- Chroma's SQLite runs in rollback-journal (`delete`) mode, verified on 1.5.9
  with a writer open, so the validator's `immutable=1` read can't miss
  WAL-only segment rows. A leftover `chroma.sqlite3-journal` (a writer died
  mid-transaction) would be ignored by `immutable=1`, so the probe then opens
  the database read-write and SQLite rolls it back first, as Chroma's own
  open would.
- Upstream fix for chroma#7510 is in flight (not in 1.5.9, the latest release).
- Windows (known limitation): `msvcrt` has no shared lock mode, so a second
  `serve` is not advertised to `refresh`, and a reader holding `refresh.lock`
  across an index open (seconds) looks like a running refresh to other readers
  after their 3x20 ms retries (CLI exit 75, or the retry message from another
  `serve`). A real fix is `LockFileEx` via ctypes, untestable without a Windows
  host.
- Header-version strictness, orphan-segment mtime handling: not exercised; left
  as is.
