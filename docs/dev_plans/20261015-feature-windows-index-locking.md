# Windows index locking and refresh/serve coexistence

**Branch:** _(to be created, e.g. `feature/windows-index-locking`)_ | **Review Gates:** none | **Status:** Planned (design notes only; no code)

Follow-up to `20261014-fix-index-write-guards.md`. That branch's lock design was
written and tested on POSIX (macOS). Windows runs the same code with weaker
primitives and was **never run on a real Windows host**: its lock paths were
exercised only by monkeypatching on macOS. This plan records what is known to
differ, what to build, and what must be verified on Windows before any of it is
claimed fixed.

## Why this exists
`shared/index_lock.py` takes advisory locks with `fcntl.flock` (exclusive for
`refresh`, shared for `serve`, shared for readers while they open the index).
On Windows the `ImportError` fallback uses `msvcrt.locking`, which has **no
shared mode**. Every other Windows gap below is either a consequence of that or
of Windows' file-handle semantics (open files cannot be deleted, no
`O_NOFOLLOW`).

## Known gaps (what differs today)

| # | Gap | Where | User-visible effect |
|---|---|---|---|
| W1 | No shared lock mode (`msvcrt.locking` is exclusive-only). `serve.lock` is held shared on POSIX so many `serve` can coexist and `refresh` can detect them; on Windows a second `serve` cannot take it. | `_try_lock` (index_lock.py ~86) | A second `serve` is not advertised to `refresh`, so `refresh` cannot warn about it. |
| W2 | A reader holds `refresh.lock` shared across the index open (seconds). On Windows that hold is exclusive, so a concurrent reader's probe looks like a running refresh. Mitigated only by 3 x 20 ms retries (`_READER_ATTEMPTS`/`_READER_DELAY`). | `refresh_excluded`, `refresh_in_progress` | CLI exits 75, or `serve` returns "retry in 60-120 seconds", when no refresh runs. |
| W3 | No `O_NOFOLLOW`. The symlink check is `lstat` before the open plus an `(st_dev, st_ino)` identity check after it. A link swapped in during the open is refused only afterwards, so the open can already have created an empty file at a dangling target; the start-of-refresh `utime` goes by path. | `IndexLock.acquire` (~150-190) | Accepted residual: needs symlink privilege and write access to the data dir's parent; writes no content. |
| W4 | A running `serve` (or any process with Chroma/SQLite open) holds files that Windows refuses to delete. `refresh --reset-index` therefore cannot remove them. It now exits 1 when the keyword DB or Chroma dir survives its delete (dfcab95), but does not stop or detect `serve` first, and `shutil.rmtree(..., ignore_errors=True)` swallows the per-file reason. | `cli.py` ~481 | User sees "index files survived the delete; stop serve and retry" without being told which process holds them. |
| W5 | Compaction (`refresh --force`) and orphan cleanup remove old segment dirs with `rmtree(ignore_errors=True)`. On Windows a handle held by a still-running `serve` makes this silently leave data behind. | `vector.py` ~529, ~662 | Disk is not reclaimed (the very problem compaction exists to fix) with no log line saying so. |
| W6 | Refresh generation is `(st_ino, st_mtime_ns, st_size)` of the lock file. NTFS file IDs and timestamp granularity differ from POSIX inodes; whether `st_ino` is non-zero and stable, and whether the size-stepping bump is visible across handles, is untested. | `refresh_generation` (~295) | If it did not change, `serve` would keep a stale handle after a refresh and serve the old graph. |
| W7 | Windows CI runs only a subset (`tests/unit/test_index_store.py`, `test_format_detection`, `test_metadata_types`, `test_taxonomy`, `test_github_ingest`, `test_cli`, `test_hub_status`, `test_env_loading`, `test_config`). `test_index_lock.py`, the reopening/serve tests and the integration tests are not run there; several are `skipif(win32)` outright. | `.github/workflows/ci.yml` windows-smoke | Nothing automated has ever executed the Windows lock path. |
| W8 | Lock file deleted or replaced outside the hub while held lets two refreshes overlap. Not Windows-specific (accepted on every platform) but Windows is likelier to see it: antivirus/indexers and "delete the data dir" habits. | dev plan 20261014, accepted residual | Two refreshes interleave HNSW writes (the corruption this branch set out to prevent). |
| W9 | Symlink and junction skipping in the ingest walkers (`is_plain_dir` / `is_plain_file`, branch `fix/ingest-skip-symlinked-dirs`) was never run against a real Windows link. Every symlink test is `skipif(win32)` because creating symlinks needs privileges, so `is_reparse_link` is untested for junctions (creatable unprivileged with `mklink /J`). `tests/unit/test_source_ingest.py` is not in the windows-smoke list at all. Also unverified: `_scannable_py_files(recursive=True)` in `taxonomy.py` uses `rglob`, which may descend a junction nested inside a plain example directory (only the leaf is `lstat`'d, and unlike `github_ingest` there is no `resolve()` containment check). | `shared/paths.py::is_reparse_link`, `ingest_filters.py`, `taxonomy.py` (~688, ~739), `source_ingest.py` | A repo committing a junction-like directory could have outside files read into taxonomy tags or `readme_content` on Windows, and CHANGELOG can only say junctions are skipped, not that it was tested. |

## Requirements for the follow-up

Numbered so a PR can cite them.

**R1 (W1, W2): real shared locks on Windows.** Implement `LockFileEx` /
`UnlockFileEx` via `ctypes` (or evaluate a vetted dependency such as
`portalocker`; adding a runtime dependency needs an explicit decision, and
`pyproject.toml` has none for this today). Must support `LOCKFILE_FAIL_IMMEDIATELY`
with and without `LOCKFILE_EXCLUSIVE_LOCK`, so the POSIX semantics hold
unchanged: refresh exclusive on `refresh.lock`; `serve` shared on `serve.lock`;
readers shared on `refresh.lock` while opening. Map `ERROR_LOCK_VIOLATION` (33)
and `ERROR_IO_PENDING`/`ERROR_SHARING_VIOLATION` to contention; any other error
keeps today's fail-open rule (log at debug, proceed unlocked).
Acceptance: two readers open concurrently with no spurious exit 75; `serve` is
advertised to `refresh` even with two `serve` processes; the 3 x 20 ms reader
retry can stay as a safety net but must no longer be load-bearing.

**R2 (W4): `--reset-index` must name the blocker.** Before deleting, detect a
running `serve` (the `serve.lock` probe works once R1 lands) and exit 1 with the
"stop serve, then retry" remedy *before* removing anything, instead of a partial
delete. Replace `ignore_errors=True` on this path with an `onerror` handler that
records the first `PermissionError` path, so the failure message can say which
file was held. Keep the post-delete survivor check (dfcab95) as the backstop.
Acceptance: with `serve` running on Windows, `refresh --force --reset-index`
exits 1, deletes nothing, and says to stop `serve`.

**R3 (W5): report, don't swallow, failed segment removal.** Compaction and orphan
cleanup keep their non-fatal contract but log a warning with the count and bytes
left behind and the remedy (stop `serve`, rerun `refresh --force`). Do not make
this fatal: a held handle is an expected Windows condition.

**R4 (W6): prove the generation changes.** On a Windows host, show that two
consecutive exclusive acquires produce different `refresh_generation` values,
including within one timestamp tick, and that `st_ino` is non-zero on NTFS and on
ReFS/FAT-backed locations if those are reachable. If `st_ino` is unreliable,
derive the generation from a counter stored in the lock file's contents (written
under the exclusive lock) instead of size-stepping.

**R5 (W3): decide on the symlink race.** Options: (a) keep the accepted residual
and document it (current state); (b) open with `CreateFileW` and
`FILE_FLAG_OPEN_REPARSE_POINT` via ctypes so a reparse point is never followed,
which removes the race entirely. Pick (b) only if R1's ctypes layer already
exists, since it shares the plumbing. Either way, test with a real directory
symlink or junction (needs Developer Mode or elevation in CI; junctions do not).

**R6 (W7): run it in CI.** Add `tests/unit/test_index_lock.py` and the reopening
tests to the `windows-smoke` job. Un-skip the `skipif(win32)` cases that R1
makes valid (shared-lock tests); keep skips only for tests that genuinely need
symlink privilege, and cover those with a junction-based variant. Add one
multi-process test (real `subprocess`, not monkeypatching): refresh holds the
lock, a reader gets exit 75, refresh ends, the reader succeeds.

**R7 (W8): optional hardening.** Windows can hold the lock file open with
`FILE_SHARE_READ` only (no `FILE_SHARE_DELETE`), so antivirus and "delete the
folder" cannot unlink it while held. Free once R1/R5 use `CreateFileW`; otherwise
leave as an accepted residual.

**R8 (W9): test junction handling and close the nested-junction walk.** On
`windows-latest`, add tests that create a junction with `mklink /J` (no privilege
needed, unlike symlinks) and assert: `is_plain_dir` is False for it; example
discovery, the taxonomy builder and `source_ingest` do not read through it; and a
junction nested inside a plain example dir is not walked by
`_scannable_py_files(recursive=True)`. If the last fails, replace `rglob` with a walk
that prunes `is_reparse_link` directories, or add the same `resolve()` containment
check `github_ingest` uses. Add `tests/unit/test_source_ingest.py` to the
windows-smoke list. Once green, drop the "untested" caveat from the CHANGELOG
Security entry.

## Non-goals
- Changing POSIX behaviour. Every requirement above must leave `fcntl.flock`
  semantics and the existing POSIX tests untouched.
- Making Windows reads continue during a refresh (decided on the parent branch:
  no reads while a refresh rewrites the index).
- A native Windows service or any new process model.

## Verification plan (cannot be done on macOS)
All of R1-R6 and R8 need a real Windows host or the `windows-latest` runner; none can be
claimed done from monkeypatched tests. Minimum evidence before closing:
1. The new lock tests green on `windows-latest` py3.12 and py3.14.
2. A manual two-terminal run on Windows: `serve` in one, `refresh --force` in the
   other (expect the retry message, then the reopened index), `refresh --force
   --reset-index` with `serve` running (expect exit 1, nothing deleted).
3. Two `serve` processes plus `refresh`: confirm `refresh` sees them.
4. Repeat 2-3 with the data dir on a non-default drive and under OneDrive-synced
   `%USERPROFILE%` if available (sync clients take handles on files).
5. R8: a junction pointing outside a cloned repo, at `examples/foo`, `examples/`, `src/<pkg>`
   and nested inside an example, is skipped by `refresh` and nothing from its target
   appears in `get_example` or `search_api` output.

## Open questions
- Is a `ctypes` shim acceptable maintenance-wise versus a small dependency?
  (`portalocker` already handles `LockFileEx`; the cost is one more wheel on every
  platform for a Windows-only need, which the project has avoided so far.)
- Does the Windows data dir default (`%LOCALAPPDATA%\pipecat-context-hub`, per
  CLAUDE.md) differ from the lock-sibling placement assumption
  (`<dirname>.refresh.lock` beside it)? Confirm the sibling is writable there and
  not redirected by folder virtualisation.
- Should `--reset-index` offer to terminate `serve`? Current stance: no, it only
  tells the user to stop it.

## Existing Windows notes to re-validate

Carried over from the old CLAUDE.md "Windows tips". They predate the POSIX-only platform
contract in `docs/README.md`, so treat them as unverified until Windows index support lands.

- The refresh summary uses U+2500 box-drawing characters. On non-UTF-8 consoles
  (cp1252, cp1254, cp437, etc.) the hub falls back to ASCII `-` automatically —
  no crash, no lost output. To get the box-drawing look, set
  `PYTHONIOENCODING=utf-8` before invoking `refresh` (or use Windows Terminal,
  which defaults to UTF-8).
- If `refresh` previously ran but returns zero code results, the local clone may
  be half-initialized from an interrupted run. The hub now detects this on the
  next `refresh` and re-clones; look for `Recovered N corrupt clone(s)` in the
  summary. As a manual remedy you can delete `%LOCALAPPDATA%\pipecat-context-hub\repos\`.
  Removing the corrupt clone is itself hardened against git's read-only loose
  object files (`.git/objects/…`), which Windows — unlike POSIX — refuses to
  delete without first clearing the read-only attribute; a plain `rmtree`
  used to crash the recovery instead of completing it.
- On `serve` boot the hub pre-warms the embedding model (and cross-encoder when
  enabled) so the first MCP query doesn't hang. This used to matter a great
  deal: loading through `sentence-transformers` dragged in `torch` and Windows
  CPU cold-starts could take 30-130s, long enough to exceed Claude Code's
  tool-permission window. The ONNX backend loads in well under a second, so
  pre-warm is now a minor optimisation rather than a necessity. Set
  `PIPECAT_HUB_WARMUP=0` to skip it. Look for
  `Embedding model pre-warmed in …s` and (optionally)
  `Cross-encoder pre-warmed in …s` in the startup log.

## References
- `docs/dev_plans/20261014-fix-index-write-guards.md` (lock design, accepted
  residuals, "Windows (known limitation)" note).
- `docs/README.md` lock paragraph (~545-560) and CHANGELOG entry for the lock work.
- `src/pipecat_context_hub/shared/index_lock.py`, `services/index/vector.py`
  (compaction, orphan cleanup), `cli.py` (`--reset-index` delete).
- Review-gauntlet ledger for the parent branch: Windows lock paths were tested
  only by monkeypatching on macOS (rounds 1-10).
