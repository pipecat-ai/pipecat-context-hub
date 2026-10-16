# Index write guards (runaway HNSW `link_lists.bin`)

**Branch:** `fix/index-write-guards` | **Review Gates:** none | **Status:** Complete (PR #139 open; Windows follow-up: `20261015-feature-windows-index-locking.md`)

## Problem
On 2026-10-13 `~/.pipecat-context-hub` was 351 GB: `chroma/<segment>/link_lists.bin`
was 15 TB logical / ~325 GB allocated (header bound ~45 MB; 112,572 graph
elements vs ~45k live records). The same corruption was diagnosed on 2026-10-04
(chroma#7510) and recurred within five days of the rebuild. Its trigger is not
established; the open-time check only fires when an index is opened.

## Changes
1. `HnswWriteGuard` (`services/index/hnsw_validation.py`): re-runs the bounded
   header-derived size check on segment dirs written since the guard was
   created; `VectorIndex.upsert` calls it after each batch, and `compact()`
   after each staging-copy batch. Deletes do not call it. Old orphan
   segments are ignored so they can't block a rebuild.
2. Disk preflight: `refresh` refuses below 5 GB free (`PIPECAT_HUB_MIN_FREE_GB`;
   `0` disables; an invalid, negative or non-finite value falls back to 5 GB).
3. `shared/index_lock.py`: exclusive `refresh.lock` for `refresh`; shared `serve.lock` for
   `serve`. Lock files are siblings of the data dir (`<dirname>.refresh.lock`, named from
   the full dirname so `x` and `.x` never share one),
   not inside it, so `--reset-index`'s rmtree can't unlink a held lock. Locking
   fails open if the filesystem doesn't support flock, and (with a warning) if
   the lock file is a symlink (checked by `lstat` before the open on every
   platform; where `O_NOFOLLOW` is missing, i.e. Windows, also after it, where
   the path must also name the file the descriptor opened, by `(st_dev, st_ino)`),
   not a single-link regular file, or another user's: an exclusive acquire
   writes to it. A live `serve` only
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
   missing or incomplete keyword database `MissingKeywordIndexError` (the
   `--reset-index` remedy), instead of opening an empty one beside a populated
   other half. The reverse, a keyword half with no records beside vector
   records, raises `LostKeywordIndexError` (the `--reset-index` remedy, exit 2)
   on every `IndexStore` open, `rebuild` included, before any vector record is
   written: the repo commit markers live in the keyword half, so a forced
   refresh that carried the vectors forward left those of an unconfigured repo
   beyond cleanup and `--prune`. A keyword half lost beside an empty or absent
   vector half is still rebuilt by `refresh`. `IndexStore` opens the keyword half first: when it has records, the
   vector half may be created only with `rebuild=True` (`refresh --force`), and
   a missing database or `latest` (from `VectorIndex`) or an empty `latest`
   (from `IndexStore`) raises `MissingVectorCollectionError` ("run refresh
   --force") before the vector half is touched, for readers and for a plain `refresh`
   (exit 2) alike, rather than skipping unchanged sources and leaving vector
   search empty (`rebuild` without `create` is a `ValueError`). When `--force`
   does recreate it, or the open finds the vector half empty for any reason
   (`IndexStore.vector_empty`, e.g. created beside a keyword half with no rows
   but surviving markers), the refresh body (not the open helper, which writes
   no metadata) calls `_distrust_skip_markers`: it drops `docs:content_hash`
   and flags every indexed repo `repo:*:vector_empty` (which the skip
   shortcut honours, like `repo:*:cleanup_failed`, and a successful re-ingest
   or the repo's cleanup clears along with it) while keeping its
   `repo:*:commit_sha`, through which the stale-repo cleanup and the
   tainted-ref purge find repos. So a source whose fetch fails in that run is
   re-ingested by the next plain refresh instead of staying keyword-only. The
   marker keys are built and parsed only through `cli.py`'s `_DOCS_HASH_KEY` /
   `_repo_sha_key` / `_repo_slug_of_sha_key` / `_repo_reingest_flag_keys`.
   Every delete of records a skip marker vouches for is write-ahead: `refresh`
   drops `docs:content_hash`, or sets `repo:<slug>:cleanup_failed` (a
   write-ahead "delete pending" flag; the on-disk name predates that use), before
   deleting, and if that write fails it deletes nothing and reports the source
   as errored. The repo flag is cleared only by a clean re-ingest or the repo's
   full cleanup, so no abort mid-run can leave a marker vouching for deleted
   records. An open writes only an incomplete keyword schema
   (`refresh` completes it and rebuilds the FTS table from `chunks` in one
   transaction, so a failed rebuild is retried next time); a reader refuses one.
   A keyword database `FTSIndex` cannot use (any SQLite result code other than
   the transient BUSY/LOCKED/IOERR/FULL/CANTOPEN/PERM/READONLY/NOMEM/INTERRUPT/
   PROTOCOL, e.g. NOTADB, CORRUPT, or SQLITE_ERROR for an unusable schema) raises
   `CorruptKeywordIndexError` (an `IndexUnavailableError` carrying
   `RESET_INDEX_REMEDIATION`), so `refresh` exits 2 with the `--reset-index`
   remedy, as readers report it; a transient error propagates unchanged (a
   reader reports it with a retry hint, never the reset remedy, which it keeps
   only for unclassified failures). One empty-index refusal (`IndexNotBuiltError`) serves
   both a missing index and an open one with 0 records.
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
   refresh"). The open-time graph check covers the backup only when `latest`
   is missing, the one case in which the open loads it; a backup left beside
   `latest` (its drop failed) is never loaded, is dropped by the next
   compaction, and does not block an open.
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
  (decided; 21aad5f, 59690b98). Accepted residual on Windows (no `O_NOFOLLOW`):
  a symlink swapped in during the open is refused only after it, so the open may
  already have created an empty file at the link target, and the
  start-of-refresh `utime` goes by path. Needs symlink privilege and write
  access to the data dir's parent; writes no content. Accepted residual on every
  platform: the hub never unlinks or replaces a lock file, so if something
  outside it deletes or replaces one while it is held, the next process locks a
  new inode and two holders coexist. An identity check at acquire time cannot
  catch a removal after it, so none is added on POSIX.
- `refresh --reset-index` refuses (exit 1) to rebuild when the keyword database
  or Chroma directory survived the delete (Windows refuses to delete files a
  running `serve` holds open); it does not stop `serve` itself.
- The keyword schema check is by object name only (as `CREATE ... IF NOT
  EXISTS` is): a hand-edited database reusing the names with other shapes opens
  and fails at query time. The hub writes the schema in one transaction, so
  only out-of-band tampering produces that; not validated further.
- `refresh --force` after a lost vector collection re-ingests only the sources
  in its configuration; a retained repo absent from it (kept, not pruned) stays
  keyword-only. Documented in docs/README.md (re-add it for that run, or
  `--prune`); not fixed here.
- The mirror case is refused rather than repaired: a keyword half with no
  records beside vector records (`LostKeywordIndexError`) needs
  `--reset-index`. An interrupted write cannot produce it: `IndexStore.upsert`
  writes keyword rows before vectors and its deletes remove vectors first, so
  every vector keeps its keyword row at any interruption point, and a first
  `refresh` interrupted mid-write leaves keyword records ahead of the vectors,
  which `refresh --force` rebuilds. Only a lost or emptied `index.db` reaches
  it. The refused open may leave an empty `index.db`; later opens refuse it the
  same way.
- Accepted trade-off of the keyword-first write order: a `refresh` interrupted
  after some, not all, of a batch's vector writes leaves every keyword row but
  only some of the vectors. Readers accept it (both halves are non-empty), so
  vector search can miss records keyword search finds until the next `refresh`
  re-ingests them (no skip marker is written before a clean ingest). The
  opposite order made the same interruption unrecoverable without
  `--reset-index`. An interrupted first refresh already served a partial index
  before this order existed (docs ingested, code not); the new part is only the
  within-batch skew. Found by a one-off Codex review (high); not fixed here.
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
  host. Gaps, requirements and verification plan:
  `20261015-feature-windows-index-locking.md`.
- The write guard checks only HNSW files already persisted when it runs.
  Chroma persists the graph only when its sync threshold is crossed (verified
  on 1.5.9): `client.close()` and reads do not persist, so a sub-threshold
  tail stays in Chroma's log until a later write persists it, and a delete can
  be that write. A persist triggered by a delete with no later upsert batch in
  the same run is not checked by the guard; the open-time check
  (`validate_persisted_hnsw`), which runs before every native load, is what
  catches it. Not extended to deletes or to the end of refresh: it would need a
  new store method and changes to every delete call site's error handling,
  which catches `Exception` (quarantined, round 3 confirm pass).
- Header-version strictness, orphan-segment mtime handling: not exercised; left
  as is.
- A data dir given as a symlink is resolved for the lock path but used
  unresolved for index work, so retargeting the link during a run splits the
  two. Same class as a lock file replaced outside the hub (needs write access
  to the operator's own directories); resolving once would have to cover every
  opener, not just `refresh`. Not fixed.
- `serve`'s per-call gate (`unavailable_reason`) and a post-refresh reopen run
  on the event loop, as do `ReopeningIndexStore`'s sync reads. The probe is a
  non-blocking flock; the reopen is rare. Moving them to worker threads would
  move Chroma's native open/close off the loop thread; not done.
- The opt-in benchmarks (`tests/benchmarks/`) open the live index with
  `IndexStore` (read-only, `create=False`) without `refresh_excluded`.
  Developer tools; not changed.
