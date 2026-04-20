# Task: Windows Refresh Resilience — Corrupt Clone Recovery + Non-UTF-8 Console Safety

**Status**: Not Started
**Assigned to**: vr000m
**Priority**: High
**Branch**: `bug/windows-refresh-resilience`
**Created**: 2026-04-20
**Completed**: —

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
- [ ] Add `_is_valid_clone(repo_path: Path) -> bool` helper (e.g., in `github_ingest.py`) using `git rev-parse --git-dir` or GitPython's `InvalidGitRepositoryError`.
- [ ] In `clone_or_fetch`, replace the `.is_dir()` check with: "exists AND is_valid_clone → fetch branch; exists AND NOT is_valid_clone → log WARNING, `shutil.rmtree`, fall through to clone branch; doesn't exist → clone branch".
- [ ] Enforce the same `resolve().relative_to(self._repos_dir)` safety check before `rmtree`.
- [ ] Add a counter on the ingest result for repos recovered; surface it in the refresh summary.

### Phase 2 — Console encoding fallback
- [ ] Add a small helper, e.g. `_safe_hr(width: int) -> str`, that tries `'─'.encode(sys.stdout.encoding)` and returns `'─' * width` or `'-' * width` accordingly.
- [ ] Replace the U+2500 separator in `_print_refresh_summary` (and anywhere else the char appears — grep for `─`) with the helper.
- [ ] Do not change other summary characters; keep the diff minimal.

### Phase 3 — Tests
- [ ] Unit: `_is_valid_clone` — valid repo → True; only `.git/objects/pack/` → False; missing `.git` → False; non-existent path → False.
- [ ] Unit: `clone_or_fetch` against a tmp dir seeded with a half-clone — asserts directory is removed and re-cloned (monkeypatch `GitRepo.clone_from` to a fake).
- [ ] Unit: `_safe_hr` returns `─` when stdout encoding accepts it; `-` when it does not (wrap a BytesIO with `TextIOWrapper(..., encoding='cp1252')`).
- [ ] Unit: `_print_refresh_summary` does not raise when stdout is cp1252 / cp1254.
- [ ] Full suite: `uv run pytest tests/ -v` clean.

### Phase 4 — Docs + release
- [ ] CHANGELOG.md entry under `Fixed` (two sub-bullets).
- [ ] CLAUDE.md: add a short "Windows tips" paragraph recommending `PYTHONIOENCODING=utf-8` for box-drawing output.
- [ ] Bump `_SERVER_VERSION` and `pyproject.toml` version (enforced by `TestVersionConsistency`).
- [ ] PR → `/review` → `/security-review` → `/deep-review` → merge → `gh release create`.

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

- [ ] Corrupt-clone recovery: integration test seeds a tmp dir with `.git/objects/pack/` only (no `HEAD`/`config`/`refs/`), monkeypatches `GitRepo.clone_from` to assert it was called, verifies the half-clone dir is removed before re-clone.
- [ ] Console encoding: monkeypatch `sys.stdout` with a `TextIOWrapper` wrapping a `BytesIO` with `encoding='cp1254'`, call `_print_refresh_summary`, assert no exception and ASCII separator appears.
- [ ] Regression: existing unit tests for `clone_or_fetch` and `_print_refresh_summary` still pass unchanged.

### Test Results

- [ ] All existing tests pass.
- [ ] New tests added and passing.
- [ ] Manual verification on a Windows VM with `chcp 1254` (optional — the unit tests cover the same path).

### Edge Cases Tested

- [ ] Existing valid clone — fetch branch, no change in behaviour.
- [ ] Non-existent repo dir — clone branch, no change in behaviour.
- [ ] Partial `.git/` (pack-only) — recovery triggers.
- [ ] `.git/` present but refs/ empty — recovery triggers.
- [ ] stdout encoding = utf-8 — U+2500 used, behaviour unchanged.
- [ ] stdout encoding = cp1252 — ASCII fallback, no exception.
- [ ] stdout encoding = cp1254 — ASCII fallback, no exception.

## Acceptance Criteria

- [ ] A repo with only `.git/objects/pack/` is detected as corrupt on next `refresh` and re-cloned.
- [ ] The recovery emits a WARNING naming the repo and the reason.
- [ ] Refresh summary reports the number of recovered repos.
- [ ] `refresh` exits 0 on a non-UTF-8 Windows console after successful indexing.
- [ ] ASCII separator is used when stdout cannot encode U+2500; U+2500 retained on UTF-8 consoles.
- [ ] CHANGELOG.md entry added under `Fixed`.
- [ ] CLAUDE.md notes `PYTHONIOENCODING=utf-8` as the recommended opt-in for Windows box-drawing output.
- [ ] All tests pass.
- [ ] `/review`, `/security-review`, `/deep-review` clean.
- [ ] Version bumped.

## Final Results

[Fill when complete]

### Summary

### Outcomes

### Learnings

### Follow-up Work

- Consider a `refresh --doctor` mode that only validates the local state (clones, indexes, caches) and reports findings without mutating, for users investigating "empty results" symptoms.
- Consider emitting a warning from `get_hub_status` if any source-type record counts are zero despite `commit_shas` being populated — a signal that source ingestion silently failed.
- Audit other U+2500 / box-drawing chars across the codebase if any are added in future — enforce via a lint rule or the `_safe_hr` helper everywhere.
