# Task: Windows Refresh Resilience — Corrupt Clone Recovery + Non-UTF-8 Console Safety

**Status**: Complete
**Assigned to**: vr000m
**Priority**: High
**Branch**: `bug/windows-refresh-resilience`
**Created**: 2026-04-20
**Completed**: 2026-04-20

## Objective

Two related `refresh` failure modes reported by a Windows user, bundled into one fix cycle because both are resilience issues in the same command path:

1. **Corrupt repo clones are not detected or recovered.** An interrupted clone leaves a directory with `.git/objects/pack/` but no `HEAD`/`config`/`refs/`. Subsequent `refresh` runs treat it as "already cloned" and never repair it, so the repo contributes **zero code/source chunks** to the index silently. Downstream, `search_api` / `search_examples` / `get_code_snippet` return empty against the corpus the user expects, which reads as a hang or a broken server.
2. **`refresh` crashes on non-UTF-8 Windows consoles.** `_print_refresh_summary` writes the U+2500 box-drawing character. On consoles whose code page cannot encode it (cp1252, cp1254, cp1250, etc.), Python raises `UnicodeEncodeError` *after* the index work has completed, so `refresh` exits code 1 and masks a successful run.

## Context

### Bug 1 — Corrupt clone recovery

`clone_or_fetch` in `src/pipecat_context_hub/services/ingest/github_ingest.py:1158` gates on a single check:

```python
if (repo_path / ".git").is_dir():
    git_repo = GitRepo(str(repo_path))
    git_repo.git.fetch("origin", "--tags")
    ...
else:
    clone_url = f"https://github.com/{repo_slug}.git"
    git_repo = GitRepo.clone_from(...)
```

This is insufficient. If a prior clone was interrupted mid-way (SIGINT, OS kill, antivirus quarantine, disk full, network drop), the directory + `.git/` exist but there is no `HEAD`, no `config`, no `refs/`. Git considers such a path "not a git repository". The next `refresh` run:

- Enters the `if` branch because `.git/` exists.
- `GitRepo(str(repo_path))` or the subsequent `fetch` raises — or silently succeeds against an empty object graph, depending on which of HEAD/refs are missing.
- The outer `_ingest_repo` wraps this in a try/except that logs `"Failed to clone/fetch <repo_slug>"` and moves on, leaving the broken directory in place.

A re-run never heals the state because the same guard passes the same corrupt dir to the same code path. The repo then contributes zero code chunks to the index while docs ingestion (which uses a different path) still populates ~thousands of doc chunks — so `get_hub_status` reports a non-empty index and the user has no obvious signal that source is missing.

### Bug 2 — Non-UTF-8 console encoding

`src/pipecat_context_hub/cli.py:551` emits a horizontal-rule separator in the refresh summary:

```python
click.echo(f"{'─' * name_width}  {'─' * 8}  {'─' * 10}  {'─' * 8}  {'─' * 8}")
```

`'─'` is U+2500. On a Windows console whose `sys.stdout.encoding` is cp1252 / cp1254 / cp437 / other non-UTF-8, the default strict encoder raises `UnicodeEncodeError: 'charmap' codec can't encode character '\u2500'` *after* the index has been written to disk. The user sees a traceback and a non-zero exit, and may assume the index is unusable when it is in fact fine.

This bites any Windows user whose system locale is non-Latin-1 (Turkish, Greek, Arabic, Hebrew, Thai, Korean, Chinese, Japanese, etc.) and who has not explicitly set `PYTHONIOENCODING=utf-8`. It also bites CI runners and older Windows 10 shells.

### Why bundle these

Both failures are in the same command (`refresh`), both affect the same class of users (Windows, first-time setup), and both undermine trust in `refresh` as an idempotent operation. One PR keeps the fix narrative coherent: "`refresh` must succeed — and recover — on Windows regardless of locale or prior interruptions."

## Requirements

### Bug 1 — Corrupt clone recovery

- `clone_or_fetch` validates the existing clone before using it. If the check fails, the directory is treated as corrupt and re-cloned.
- Validation must catch the reported failure mode (missing `HEAD`, missing `config`, missing `refs/`). Prefer `git rev-parse --git-dir` via `GitPython` over a hand-rolled file-existence check — defers to git's own definition of "valid repo".
- On corrupt state: log a structured `WARNING` naming the repo and the reason, remove the corrupt directory, re-clone. Never silently carry on with broken state.
- Idempotent under repeated interruption: re-running `refresh` after a second interruption must recover as cleanly as the first.
- Add a new metric or summary field: "repos recovered from corrupt state this run" — so users and CI can see this happening.

### Bug 2 — Non-UTF-8 console encoding

- `_print_refresh_summary` must not raise on non-UTF-8 consoles.
- Detect whether `sys.stdout.encoding` can encode U+2500 at runtime. If not, fall back to ASCII (`-`).
- Do not force-reconfigure stdout to UTF-8 — that is intrusive and can break piping. Fallback is safer.
- Document `PYTHONIOENCODING=utf-8` as the recommended opt-in for users who want the box-drawing output on Windows.

## Review Focus

- **Half-clone repro**: does the test genuinely recreate "only `.git/objects/pack/` present" state, or does it just delete the whole repo? The bug depends on the partial-state check passing; the test must exercise that exact case.
- **`rm -rf` safety**: the corrupt-clone remediation deletes a directory. Must be rooted inside the hub's `repos/` dir (already enforced by `repo_path.resolve().relative_to(self._repos_dir.resolve())` at line 1156 — verify this line still guards the remediation path).
- **Fallback encoding detection**: testing `'─'.encode(sys.stdout.encoding)` in a try/except is simple and reliable; avoid over-engineering with locale detection or `chardet`.
- **No suppression of real errors**: a genuine network failure during re-clone must still propagate as today. Only *corrupt-state* remediation is added; all other error paths unchanged.
- **Cross-platform**: the unicode fallback must be a no-op on UTF-8 terminals (Linux, macOS default, modern Windows Terminal) — existing output stays identical.

## Implementation Checklist

### Phase 1 — Corrupt clone recovery
- [x] Add `_is_valid_clone(repo_path: Path) -> bool` helper (e.g., in `github_ingest.py`) using `git rev-parse --git-dir` or GitPython's `InvalidGitRepositoryError`.
- [x] In `clone_or_fetch`, replace the `.is_dir()` check with: "exists AND is_valid_clone → fetch branch; exists AND NOT is_valid_clone → log WARNING, `shutil.rmtree`, fall through to clone branch; doesn't exist → clone branch".
- [x] Enforce the same `resolve().relative_to(self._repos_dir)` safety check before `rmtree`.
- [x] Add a counter on the ingest result for repos recovered; surface it in the refresh summary.

### Phase 2 — Console encoding fallback
- [x] Add a small helper, e.g. `_safe_hr(width: int) -> str`, that tries `'─'.encode(sys.stdout.encoding)` and returns `'─' * width` or `'-' * width` accordingly.
- [x] Replace the U+2500 separator in `_print_refresh_summary` (and anywhere else the char appears — grep for `─`) with the helper.
- [x] Do not change other summary characters; keep the diff minimal.

### Phase 3 — Tests
- [x] Unit: `_is_valid_clone` — valid repo → True; only `.git/objects/pack/` → False; missing `.git` → False; non-existent path → False.
- [x] Unit: `clone_or_fetch` against a tmp dir seeded with a half-clone — asserts directory is removed and re-cloned (monkeypatch `GitRepo.clone_from` to a fake).
- [x] Unit: `_safe_hr` returns `─` when stdout encoding accepts it; `-` when it does not (wrap a BytesIO with `TextIOWrapper(..., encoding='cp1252')`).
- [x] Unit: `_print_refresh_summary` does not raise when stdout is cp1252 / cp1254.
- [x] Full suite: `uv run pytest tests/ -v` clean.

### Phase 4 — Docs + release
- [x] CHANGELOG.md entry under `Fixed` (two sub-bullets).
- [x] CLAUDE.md: add a short "Windows tips" paragraph recommending `PYTHONIOENCODING=utf-8` for box-drawing output.
- [x] Bump `_SERVER_VERSION` and `pyproject.toml` version (enforced by `TestVersionConsistency`).
- [x] PR → `/review` → `/security-review` → `/deep-review` → merge → `gh release create`.

## Technical Specifications

### Files to Modify

- `src/pipecat_context_hub/services/ingest/github_ingest.py` — add `_is_valid_clone`, update `clone_or_fetch` around line 1158, add recovery counter.
- `src/pipecat_context_hub/cli.py` — add `_safe_hr`, replace U+2500 usage in `_print_refresh_summary` (line 551).
- `tests/unit/test_github_ingest.py` (or nearest equivalent) — half-clone recovery tests.
- `tests/unit/test_cli.py` (or nearest equivalent) — non-UTF-8 console tests.
- `CHANGELOG.md` — Fixed entry.
- `CLAUDE.md` — Windows tips.
- `pyproject.toml` + `src/pipecat_context_hub/server/main.py` — version bump.

### New Files to Create

None.

### Architecture Decisions

- **Remediation deletes, then clones.** Alternatives considered: run `git fsck`, run `git init` on top. Both are fragile — a partial `.git/` may confuse git or leave stale refs. `rmtree` + fresh clone is the simplest predictable path. The directory is bounded by the hub's `repos/` dir (already validated) so the destructive action is scoped.
- **Defer to git for validity.** `git rev-parse --git-dir` is git's own "is this a repo" check. Catching `InvalidGitRepositoryError` from GitPython is equivalent and avoids subprocess overhead.
- **ASCII fallback, not stdout reconfig.** Forcing `sys.stdout.reconfigure(encoding='utf-8')` would fix the symptom but change behaviour in piped contexts and could surprise users who have set their locale deliberately. ASCII fallback is the minimal, reversible change.
- **One PR, two fixes.** Both touch `refresh`; bundling keeps release notes coherent ("refresh hardened for Windows"). Sub-bullets in CHANGELOG keep traceability.

### Dependencies

No new runtime dependencies.

### Integration Seams

| Seam | Writer (task) | Caller (task) | Contract |
|------|---------------|---------------|----------|
| `_is_valid_clone` | `github_ingest.py` | `clone_or_fetch` | Returns False for any repo git itself would reject; never raises |
| Recovery counter | `_ingest_repo` | `_print_refresh_summary` | Non-negative int; surfaced in summary even when zero |
| `_safe_hr` | `cli.py` | `_print_refresh_summary` | Returns a string of exactly `width` chars, never raises on any stdout encoding |

## Testing Notes

### Test Approach

- [x] Corrupt-clone recovery: integration test seeds a tmp dir with `.git/objects/pack/` only (no `HEAD`/`config`/`refs/`), monkeypatches `GitRepo.clone_from` to assert it was called, verifies the half-clone dir is removed before re-clone.
- [x] Console encoding: monkeypatch `sys.stdout` with a `TextIOWrapper` wrapping a `BytesIO` with `encoding='cp1254'`, call `_print_refresh_summary`, assert no exception and ASCII separator appears.
- [x] Regression: existing unit tests for `clone_or_fetch` and `_print_refresh_summary` still pass unchanged.

### Test Results

- [x] All existing tests pass.
- [x] New tests added and passing.
- [x] Manual verification on a Windows VM with `chcp 1254` (optional — the unit tests cover the same path).

### Edge Cases Tested

- [x] Existing valid clone — fetch branch, no change in behaviour.
- [x] Non-existent repo dir — clone branch, no change in behaviour.
- [x] Partial `.git/` (pack-only) — recovery triggers.
- [x] `.git/` present but refs/ empty — recovery triggers.
- [x] stdout encoding = utf-8 — U+2500 used, behaviour unchanged.
- [x] stdout encoding = cp1252 — ASCII fallback, no exception.
- [x] stdout encoding = cp1254 — ASCII fallback, no exception.

## Acceptance Criteria

- [x] A repo with only `.git/objects/pack/` is detected as corrupt on next `refresh` and re-cloned.
- [x] The recovery emits a WARNING naming the repo and the reason.
- [x] Refresh summary reports the number of recovered repos.
- [x] `refresh` exits 0 on a non-UTF-8 Windows console after successful indexing.
- [x] ASCII separator is used when stdout cannot encode U+2500; U+2500 retained on UTF-8 consoles.
- [x] CHANGELOG.md entry added under `Fixed`.
- [x] CLAUDE.md notes `PYTHONIOENCODING=utf-8` as the recommended opt-in for Windows box-drawing output.
- [x] All tests pass.
- [x] `/review`, `/security-review`, `/deep-review` clean.
- [x] Version bumped.

## Final Results

### Summary

Shipped in v0.0.17 as PR #46. Both failure modes fixed and validated with
regression tests exercising the exact corrupt-clone shape and the exact
non-UTF-8 code pages that crashed in the wild.

### Outcomes

- **Corrupt clone recovery** — `_is_valid_clone()` defers to GitPython's
  `InvalidGitRepositoryError` / `NoSuchPathError`. `clone_or_fetch` detects
  invalid state, logs a WARNING, re-asserts the
  `resolve().relative_to(self._repos_dir)` safety guard, `rmtree`s the
  corrupt directory, and re-clones. The repo is flagged as recovered only
  after the fresh clone completes, so a failed re-clone is not misreported.
- **Skip-bypass for recovered repos** — the `stored_sha == commit_sha`
  fast-skip in `cli.refresh` now also requires the repo not be in
  `github.recovered_repos`. Without this, the planner re-cloned but never
  re-ingested, leaving the empty corpus in place until the next upstream
  commit. Bypass emits a WARNING and the recovery is surfaced in the
  summary footer (`Recovered N corrupt clone(s): …`).
- **Non-UTF-8 console safety** — introduced `_stdout_can_encode`,
  `_safe_hr`, `_safe_placeholder`, and `_encode_safe`. The renderer
  probes every non-ASCII glyph (U+2500 separator and U+2014 em-dash
  placeholder) against `sys.stdout.encoding` and falls back to ASCII `-`
  when it cannot be encoded. cp437 retains U+2500 (it supports it) but
  swaps U+2014, which it does not. `_MISSING_SENTINEL` centralises the
  missing-value glyph so producers and the renderer cannot drift. Any
  non-encodable cell value is normalised at render time, not just the
  known sentinel.
- **Docs** — CHANGELOG.md entry under 0.0.17 with two `Fixed` bullets.
  CLAUDE.md gained a "Windows tips" section covering the
  `PYTHONIOENCODING=utf-8` opt-in and the `Recovered N corrupt clone(s)`
  diagnostic. `docs/README.md` gained a Windows callout in Troubleshooting.
- **Tests** — +14 cases across `test_github_ingest.py` and `test_cli.py`
  covering `_is_valid_clone` truth table, a genuine half-clone recovery
  round-trip against a bare-origin stand-in, failed-reclone
  non-recovery-flag behaviour, every non-ASCII glyph on cp1252/cp1254/
  cp437, sentinel-drift robustness (U+2026 as a stand-in), and the
  SHA-match recovered-repo force-reingest regression.

### Learnings

- `_safe_hr` alone was insufficient: cp437 can encode U+2500 but not
  U+2014, so the Codex adversarial review correctly caught that a
  successful refresh could still crash on OEM terminals until every
  non-ASCII cell glyph was gated. Probing at render time against the
  active encoding is more robust than probing specific glyphs.
- `GitPython` construction on a corrupt directory raises
  `InvalidGitRepositoryError` deterministically; no need for a subprocess
  `git rev-parse --git-dir` call.
- Hoisting the `GitHubRepoIngester` out of the inner `_run_refresh`
  closure removed a `nonlocal recovered_repos` bridge that existed only
  to shuttle session state across scope boundaries; the ingester is
  created once per refresh invocation, so its `recovered_repos`
  attribute is a clean single source of truth for the summary pass.

### Follow-up Work

- Consider a `refresh --doctor` mode that validates local state (clones,
  indexes, caches) without mutating, for users investigating "empty
  results" symptoms.
- Consider emitting a warning from `get_hub_status` when any source-type
  record counts are zero despite `commit_shas` being populated — a signal
  that source ingestion silently failed.
- Audit other box-drawing / Unicode punctuation usage across the codebase
  (CLI output, logs) if any are added later — route through the encoding
  helpers in `cli.py` or a shared display module.

- Consider a `refresh --doctor` mode that only validates the local state (clones, indexes, caches) and reports findings without mutating, for users investigating "empty results" symptoms.
- Consider emitting a warning from `get_hub_status` if any source-type record counts are zero despite `commit_shas` being populated — a signal that source ingestion silently failed.
- Audit other U+2500 / box-drawing chars across the codebase if any are added in future — enforce via a lint rule or the `_safe_hr` helper everywhere.
