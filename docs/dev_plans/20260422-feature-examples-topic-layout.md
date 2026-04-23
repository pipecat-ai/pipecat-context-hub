# Task: Adapt taxonomy builder to topic-based pipecat examples layout + add smoke tests

**Status**: Not Started
**Assigned to**: Claude
**Priority**: Medium
**Branch**: `feature/examples-topic-layout`
**Created**: 2026-04-22
**Completed**:

## Objective

Update the example taxonomy builder to handle the new topic-based layout of `pipecat-ai/pipecat/examples/` (post-`foundational/` reorg), and add offline fixture-based smoke tests plus a scheduled drift-detection job that would catch this class of upstream-drift regression in future.

## Context

**Upstream change.** `pipecat-ai/pipecat` has reorganised `examples/`. The numbered `examples/foundational/NN-name/` tree is gone; examples now live under topic directories: `audio/`, `context-summarization/`, `features/`, `function-calling/`, `getting-started/`, `mcp/`, `observability/`, `persistent-context/`, `rag/`, `realtime/`, `thinking/`, `transcription/`, `transports/`, `turn-management/`, `update-settings/`, `video-avatar/`, `video-processing/`, `vision/`, `voice/`.

**Current PCH behaviour.**

- `github_ingest.py::_discover_under_examples` (lines 383–415) already handles "category dirs" generically — each topic dir is walked one level deep, so **example code chunks still get ingested**. Discovery is intact.
- `taxonomy.py::ExampleTaxonomyBuilder.build_from_directory` (line 362) hard-checks `root / "examples" / "foundational"`. When absent, it falls back to `build_from_examples_repo(root=repo_root)`, which iterates the **repo root's** immediate children (`src/`, `tests/`, `docs/`, `examples/`, …). That's wrong for the new pipecat main layout — it produces junk taxonomy entries for non-example dirs and misses the real topic dirs.
- Result: `TaxonomyEntry` metadata for new-layout examples is missing or wrong. `capability_tags`, `key_files`, and `foundational_class` aren't attached to example chunks in `github_ingest._apply_taxonomy` (line 789). Search still returns the chunks, but with weaker filter/rank metadata.

**Critical lookup-key invariant.** `github_ingest.py:960` builds the per-dir lookup key as `str(ex_dir.relative_to(repo_path))` and indexes `taxonomy_lookup` by `entry.path` (line 516). Every `TaxonomyEntry` emitted by the new builder **must** use `path == str(ex_dir.relative_to(repo_path))` for exactly the dirs `_discover_under_examples` returns — otherwise the lookup silently misses and we re-produce the bug we're fixing.

**Legacy field.** `foundational_class` on `ExampleMetadata` and `TaxonomyEntry` is now a historical concept. Downstream readers exist (`hybrid.py:370-371, 403, 456` — filter input, result annotation). Plan: keep the field readable (backward compat with persisted indexes and existing MCP filter), but stop writing it for new-layout entries. Document as deprecated.

**Why now.** Examples reorg is a real bug that degrades search quality for every user until fixed. There's no existing test that would have caught it; a real-tree discovery + taxonomy invariant would have.

## Requirements

1. **Taxonomy builder handles topic-based `examples/<topic>/<example>/` layouts.** The builder must emit one `TaxonomyEntry` per dir returned by `_discover_under_examples`, with `path == str(ex_dir.relative_to(repo_path))`. Metadata (`capability_tags`, `key_files`, `execution_mode`) must flow through the existing `_apply_taxonomy` path.
2. **Backward compatibility with older framework-version pins.** At tags like `v0.0.96`, pipecat had `examples/foundational/` **plus** non-numbered sibling dirs (e.g. `examples/simple-chatbot/`). Both must continue to yield taxonomy entries.
3. **Offline PR-gating smoke tests.** A vendored tree fixture under `tests/fixtures/smoke/` (tree-only, no `.git`) exercises discovery + taxonomy against a realistic snapshot of `pipecat` and `pipecat-examples`. Runs on every `pytest` call, no network required, ≤ 15 s.
4. **Live drift-detection CI job.** A GitHub Action scheduled every 5 days that clones the real upstream repos at `main`, runs the same invariant helpers, opens/updates a single pinned tracking issue on failure. Must **not** gate PRs.
5. **Invariant tightness.** Assertions must catch the original regression (foundational removed, taxonomy empty / junk-filled) but tolerate adding a new topic dir or renaming one. Concretely: for each dir returned by `_discover_under_examples`, a taxonomy entry with matching `path` must exist and carry non-empty `capability_tags`.
6. **Deprecate `foundational_class` clearly.** Mark deprecated in Pydantic model descriptions, add a CHANGELOG "Deprecated" section, stop writing for non-`foundational/` entries. Field remains readable; `hybrid.py` filter and result annotation stay untouched.
7. **User upgrade path.** Existing users have stale `foundational_class` values in ChromaDB metadata pointing at paths that no longer exist upstream. Release notes must require `refresh --force` post-upgrade. Alternative: bump an ingest schema version so the ingester invalidates automatically.

## Review Focus

- **Lookup-key shape** — Seam 1 contract is load-bearing. A unit test must iterate `_discover_under_examples(fixture)` and assert every returned dir has a matching `taxonomy_lookup[rel]` entry.
- **Backward-compat at `--framework-version v0.0.96`**. The combined foundational + non-foundational-sibling scan must continue to work. Add a manual acceptance step that actually runs a refresh at that tag and diffs taxonomy output.
- **Smoke-test default marker** — PR gate must be offline-fixture based. Live-clone behaviour lives in the scheduled drift job only. Do **not** make live network a PR-blocking default.
- **Invariant tightness** — parameterise over `_discover_under_examples` output; don't enumerate topic names.
- **Downstream `foundational_class` readers** — `hybrid.py` filter (line 370) and `query_by_class` (taxonomy.py:395) must keep working for existing indexes.
- **Don't hard-code topic names** — capability-tag override map is a tiny explicit dict, but the set of topics is always discovered dynamically.

## Implementation Checklist

### Phase 1: Generalise taxonomy builder for topic-based layouts

**Impl files:** `src/pipecat_context_hub/services/ingest/taxonomy.py`
**Test files:** `tests/unit/test_taxonomy.py`
**Test command:** `uv run pytest tests/unit/test_taxonomy.py -v`

- [ ] Add `build_from_topic_dirs(examples_dir, repo, commit_sha) -> list[TaxonomyEntry]` that walks `examples/<topic>/`. Mirror `_discover_under_examples` exactly: if `<topic>` has direct code files → one entry for `<topic>`; else → one entry per sub-dir under `<topic>`. Every returned entry has `path = str(dir.relative_to(repo_root))`.
- [ ] Extract a shared helper `_scan_topic_tree(examples_dir, repo_root, repo, commit_sha)` used by both branches so foundational + topic layouts cannot drift apart.
- [ ] Rework `build_from_directory` dispatch:
  1. If `examples/foundational/` exists → call `build_from_foundational` **and** `_scan_topic_tree` over non-foundational siblings (preserves `v0.0.96` behaviour, current lines 368-386).
  2. Else if `examples/` exists with any subdirs → `_scan_topic_tree(examples_dir, …)`.
  3. Else → `build_from_examples_repo(root)` (root-level layout, `pipecat-examples`).
- [ ] Capability-tag derivation for new-layout entries: start from topic dir name, apply a module-level override map for a handful of compound tags (e.g. `function-calling` → `["function-calling", "tools"]`, `realtime` → `["realtime", "voice-ai"]`). Keep the map small and documented. Do not enumerate all topic names — unknown topics pass through unchanged.
- [ ] Do **not** set `foundational_class` for new-layout entries. `_build_entry_for_example` already returns `None` (types.py:120, taxonomy.py _build_entry_for_example) — verify, don't duplicate logic.
- [ ] Mark `foundational_class` deprecated in `ExampleMetadata` and `TaxonomyEntry` Pydantic `Field(description=...)` and class docstrings. No removal.
- [ ] Unit tests against synthetic trees:
  - (a) topic-based tree with subdir examples under multiple topics
  - (b) topic-based tree where one topic contains flat `.py` files (topic dir itself is the example)
  - (c) legacy `foundational/` tree still works unchanged
  - (d) mixed layout: `foundational/` + `simple-chatbot/` siblings (the v0.0.96 case)
  - (e) `pipecat-examples`-style root-level layout still works
  - (f) repo root with no `examples/` dir falls back correctly
  - (g) **Lookup-key parity:** iterate `_discover_under_examples(fixture)` and assert every dir has a matching `taxonomy_lookup[rel]` built from the builder output. This test is the contract for Seam 1.

### Phase 2: Stop junk entries from the root-level fallback

**Impl files:** `src/pipecat_context_hub/services/ingest/taxonomy.py`
**Test files:** `tests/unit/test_taxonomy.py`
**Test command:** `uv run pytest tests/unit/test_taxonomy.py::test_no_junk_entries_from_repo_root -v`

- [ ] Add `require_example_markers: bool = False` parameter to `build_from_examples_repo`. When true, skip well-known non-example root dirs: `src`, `tests`, `docs`, `scripts`, `dashboard`, `.github`, `.claude` (in addition to current `.*`, `__pycache__`, `node_modules`).
- [ ] `build_from_directory` passes `require_example_markers=True` only when falling back at a repo root that also contains `src/` or `pyproject.toml` (i.e., a packaged project, not an examples-only repo).
- [ ] Unit test: `build_from_directory` on a fake repo root containing `src/`, `tests/`, `docs/`, `examples/foo/bot.py` yields exactly one entry for `examples/foo`, zero entries for `src`/`tests`/`docs`.

### Phase 3: Verify `_apply_taxonomy` + retrieval downstream (mostly a no-op)

**Impl files:** `src/pipecat_context_hub/services/ingest/github_ingest.py` (comments only), `tests/unit/test_github_ingest_taxonomy.py` (new or existing)
**Test files:** same
**Test command:** `uv run pytest tests/unit/test_github_ingest_taxonomy.py -v`

- [ ] Verify that `github_ingest.py:789-790` (`if taxonomy_entry.foundational_class is not None`) already correctly omits the key for topic-layout entries. Add a regression test: build a TaxonomyEntry with `foundational_class=None`, run `_apply_taxonomy`, assert `meta` has no `"foundational_class"` key.
- [ ] Verify `hybrid.py:370-371` filter path: when `SearchExamplesInput.foundational_class` is None, no filter is added (current behaviour). Confirm tests exist or add one.
- [ ] Update stale comments in `github_ingest.py` that still reference `examples/foundational/` as the canonical pipecat layout.
- [ ] **No code change to `_apply_taxonomy` or `hybrid.py`** — confirmed via review. If a change surfaces during implementation, raise it explicitly rather than silently editing.

### Phase 4: Offline PR-gating smoke tests via vendored fixtures

**Impl files:** `tests/smoke/__init__.py`, `tests/smoke/conftest.py`, `tests/smoke/invariants.py`, `tests/smoke/test_pipecat_layout.py`, `tests/fixtures/smoke/pipecat/…`, `tests/fixtures/smoke/pipecat-examples/…`, `tests/smoke/README.md`, `pyproject.toml`
**Test files:** same
**Test command:** `uv run pytest tests/smoke -v`

- [ ] Vendor **tree-only** fixture snapshots under `tests/fixtures/smoke/<repo>/` for `pipecat-ai/pipecat` and `pipecat-ai/pipecat-examples`. Include `examples/` subtree + `pyproject.toml` + `README.md`; strip `.git/`, binaries, docs/, generated files. Commit a `FIXTURE_PINS.json` next to the fixtures recording the upstream SHA + date the snapshot was taken.
- [ ] Target fixture size ≤ 2 MB per repo. Scripts in `tests/smoke/refresh_fixtures.py` regenerate the snapshot from a live clone (used manually, not in CI PR gate).
- [ ] `tests/smoke/invariants.py` exports reusable assertion helpers (importable by both the PR-gate tests and the drift script in Phase 5):
  - `assert_discovery_yields_code_files(repo_root)`
  - `assert_every_discovered_dir_has_taxonomy_entry(repo_root, builder)`
  - `assert_no_junk_entries(repo_root, builder, forbidden={"src","tests","docs","scripts"})`
  - `assert_capability_tags_non_empty(builder)`
- [ ] PR-gate tests in `test_pipecat_layout.py` parameterise these helpers over the vendored fixtures. Invariants are expressed against `_discover_under_examples` output, not hard-coded topic lists.
- [ ] Register `smoke` pytest marker in `pyproject.toml`. **Offline by design:** no `smoke` marker filter is applied — the tests always run. Do not mark them `@pytest.mark.smoke` (that marker is reserved for the live-network drift check in Phase 5).
- [ ] Ensure total wall time ≤ 15 s (no embedding model load, no Chroma, no network).
- [ ] `tests/smoke/README.md` documents: how fixtures are generated, the refresh cadence (every release, at minimum; every drift-job failure at minimum), and who owns pin-bump triage.

### Phase 5: Drift-detection CI job (live network, scheduled only)

**Impl files:** `.github/workflows/smoke-drift.yml`, `scripts/check_pipecat_drift.py`
**Test files:** manual via `workflow_dispatch`
**Test command:** `uv run python scripts/check_pipecat_drift.py --repo pipecat-ai/pipecat --ref main`

- [ ] `scripts/check_pipecat_drift.py` — clones each repo in `tests/fixtures/smoke/FIXTURE_PINS.json` at `main` (full clone, not partial — reliable on any git), reuses the `tests/smoke/invariants.py` helpers, prints a structured report, exits non-zero on any assertion failure. Supports `--dry-run` and `--ref <sha|branch|tag>`.
- [ ] GitHub Action `.github/workflows/smoke-drift.yml`:
  - Trigger: `schedule: cron: '0 6 */5 * *'` + `workflow_dispatch`.
  - Permissions: `contents: read`, `issues: write`.
  - Steps: `actions/checkout` → `astral-sh/setup-uv` → `uv sync --extra dev` → `python scripts/check_pipecat_drift.py`.
  - **De-dupe via single tracking issue**: look for an open issue with label `upstream-drift`; if found, update its body with latest report + append a dated comment; if not found, create one labelled `upstream-drift`. Use `gh` CLI inline — no third-party action required.
  - Not a required check; separate workflow file from `ci.yml`.
- [ ] Verify workflow via `workflow_dispatch` on feature branch before merge. Confirm it opens an issue on forced failure (inject a bad assertion temporarily).

### Phase 6: Backward-compat replay check at `v0.0.96`

**Impl files:** (manual verification; capture output in PR description)
**Test files:** —
**Test command:** `uv run pipecat-context-hub refresh --force --framework-version v0.0.96` then inspect taxonomy output

- [ ] Before merge, run `refresh --force --framework-version v0.0.96` against a clean data dir. Confirm:
  - Taxonomy entries still populate with `foundational_class` for `examples/foundational/NN-name/` dirs.
  - Non-foundational siblings (e.g. `examples/simple-chatbot/`) also produce entries.
  - No junk entries for `src`/`tests`/`docs`.
- [ ] Capture the output summary in the PR description as evidence. If output differs from pre-change output at that pin, justify the diff.

### Phase 7: Downstream + dashboard audit

**Impl files:** grep results, CHANGELOG, dashboard regen
**Test files:** —
**Test command:** `just dashboard-refresh` (after a full `refresh --force`)

- [ ] `rg foundational_class src/ dashboard/ tests/` — document every reader site in the PR description. Confirmed so far: `hybrid.py:370-371, 403, 456`, `types.py:120,333,358`, `taxonomy.py:395` (`query_by_class`). None require code changes if the field continues to be readable.
- [ ] Run `just dashboard-refresh` after a real `refresh --force` against a fresh clone. Confirm `dashboard_data.json` example counts are sensible (no drop-to-zero).

### Phase 8: Docs, release notes, and upgrade guidance

**Impl files:** `CHANGELOG.md`, `CLAUDE.md`, `AGENTS.md`, `README.md`
**Test files:** —
**Test command:** `uv run ruff check src/ tests/ && uv run mypy src/ tests/`

- [ ] CHANGELOG additions under Unreleased:
  - **Fixed** — taxonomy coverage for new pipecat examples topic-based layout; no junk entries from packaged-repo fallback.
  - **Added** — offline smoke-test fixtures + drift-detection workflow.
  - **Deprecated** — `foundational_class` field on `ExampleMetadata`/`TaxonomyEntry`. Still read for existing indexes and filter input; no longer written for new-layout examples.
- [ ] Release notes must include an explicit upgrade instruction: `uv run pipecat-context-hub refresh --force` after upgrade to clear stale `foundational_class` values pointing at paths that no longer exist upstream.
- [ ] Remove/update stale `examples/foundational/` references in CLAUDE.md, AGENTS.md, module docstrings.
- [ ] `tests/smoke/README.md` documents the fixture refresh workflow and the offline-by-default design.

## Technical Specifications

### Files to Modify

- `src/pipecat_context_hub/services/ingest/taxonomy.py` — new `build_from_topic_dirs` + `_scan_topic_tree` helper, updated `build_from_directory` dispatch, `require_example_markers` flag on `build_from_examples_repo`, deprecation notes on foundational-related docstrings.
- `src/pipecat_context_hub/shared/types.py` — mark `ExampleMetadata.foundational_class` (line 120) and `TaxonomyEntry.foundational_class` (lines 333, 358) deprecated in `Field(description=...)`.
- `src/pipecat_context_hub/services/ingest/github_ingest.py` — comments/docstrings only; no behavioural change.
- `pyproject.toml` — register `smoke` pytest marker (for the live-drift-only use case in Phase 5).
- `CHANGELOG.md`, `CLAUDE.md`, `AGENTS.md`, `README.md` — doc sync.

### New Files to Create

- `tests/smoke/__init__.py`
- `tests/smoke/conftest.py` — fixture path helpers only (no git).
- `tests/smoke/invariants.py` — reusable assertion helpers (imported by Phase 4 tests and Phase 5 script).
- `tests/smoke/test_pipecat_layout.py` — parameterised assertions over vendored fixtures.
- `tests/smoke/refresh_fixtures.py` — script to regenerate vendored fixtures from a live clone (manual use).
- `tests/smoke/README.md` — fixture refresh workflow, offline-by-default rationale.
- `tests/fixtures/smoke/pipecat/…` — vendored tree-only snapshot.
- `tests/fixtures/smoke/pipecat-examples/…` — vendored tree-only snapshot.
- `tests/fixtures/smoke/FIXTURE_PINS.json` — upstream SHAs + capture date.
- `scripts/check_pipecat_drift.py` — live-network drift script.
- `.github/workflows/smoke-drift.yml` — scheduled workflow (5-day cron).

### Architecture Decisions

- **Dispatch by layout detection, not by repo slug.** Builder sniffs the tree; keeps it resilient to reorgs and forks.
- **Shared `_scan_topic_tree` helper** for topic-layout work so foundational + topic branches can't drift apart.
- **Offline vendored fixtures for PR gate, live network only for the scheduled drift job.** Reasoning: no existing tests hit the network; anonymous GitHub CI clones rate-limit at 60/hr; flakes would block unrelated PRs. Vendored fixtures are deterministic and fast (≤ 15 s). Drift job carries the "did upstream change?" responsibility, with failures filed as a tracked issue.
- **Lookup-key parity is a test, not a convention.** `_discover_under_examples` output must always be key-compatible with `_build_taxonomy_lookup` indices — enforced by unit test (g) in Phase 1.
- **Keep `foundational_class` readable.** Removing it breaks persisted indexes and the `hybrid.py` filter path. Deprecation-in-place is free.
- **Capability tags: dynamic discovery + tiny override map.** Don't enumerate topic names; unknown topics pass through as single-tag entries.
- **Single tracking issue for drift, not issue-per-run.** Avoids spam; uses `gh` CLI inline — no third-party action dep.

### Dependencies

- No new runtime deps.
- Dev: git ≥ 2.25 on CI runners (drift job only; PR gate uses no git).
- `gh` CLI available in GitHub Actions runners by default (drift job).

### Integration Seams

| Seam | Writer | Caller | Contract |
|------|--------|--------|----------|
| Taxonomy entry path format | Phase 1 `build_from_topic_dirs` / `_scan_topic_tree` | `github_ingest._apply_taxonomy` via `_build_taxonomy_lookup` | `entry.path == str(ex_dir.relative_to(repo_root))` for every `ex_dir` in `_discover_under_examples(repo_root / "examples")`. Enforced by unit test in Phase 1(g). |
| Vendored fixture tree | Phase 4 fixtures + `refresh_fixtures.py` | Phase 4 tests (PR gate) | Path rooted at `tests/fixtures/smoke/<slug>/`; contains `examples/` subtree + `pyproject.toml`; no `.git`; `FIXTURE_PINS.json` records upstream SHA. |
| Invariant helper library | Phase 4 `tests/smoke/invariants.py` | Phase 4 tests + Phase 5 `check_pipecat_drift.py` | Functions are pure (no side effects, no fixtures), take a `Path` to a repo root + a fresh `ExampleTaxonomyBuilder`, raise `AssertionError` on violation. Single source of truth. |
| Drift tracking issue | Phase 5 workflow | Human triage | One open issue labelled `upstream-drift` at a time; updated in place with latest report + dated comment per failure. |
| `foundational_class` field | Phase 1 (writer — legacy paths only) | `hybrid.py:370,403,456`, `taxonomy.py:395`, MCP filter input | Remains readable; may be `None` on new-layout entries. Downstream treats `None` as "no filter applied". |

## Testing Notes

### Test Approach

- [ ] Unit tests for taxonomy builder against synthetic trees (Phase 1, 2)
- [ ] Regression test that `_apply_taxonomy` omits `foundational_class` key when entry has `foundational_class=None` (Phase 3)
- [ ] Offline smoke tests against vendored fixture trees (Phase 4)
- [ ] Manual verification of drift workflow via `workflow_dispatch` (Phase 5)
- [ ] Manual `refresh --force --framework-version v0.0.96` to confirm backward-compat (Phase 6)
- [ ] Manual `just dashboard-refresh` sanity check (Phase 7)

### Test Results
- [ ] `uv run pytest tests/ -v` passes
- [ ] `uv run ruff check src/ tests/` clean
- [ ] `uv run mypy src/ tests/` clean
- [ ] Drift workflow runs green via `workflow_dispatch` on the feature branch; injected-failure run opens a tracking issue
- [ ] `refresh --framework-version v0.0.96` diff captured in PR description

### Edge Cases Tested
- [ ] Older framework-version pin with `examples/foundational/` still works
- [ ] Mixed layout (foundational + sibling topic dirs) at `v0.0.96`-era pin
- [ ] Repo root with `src/` + `examples/` produces no junk entries for `src/`/`tests/`/`docs/`
- [ ] Topic dir containing flat `.py` files (topic itself is the example)
- [ ] Empty `examples/` dir (zero entries, no exception)
- [ ] Unknown topic name with no override → single-tag entry from topic name as-is
- [ ] Lookup-key parity: `_discover_under_examples` output ↔ `_build_taxonomy_lookup` keys

## Issues & Solutions

_(to be filled in during implementation)_

## Acceptance Criteria

- [ ] `ExampleTaxonomyBuilder.build_from_directory` produces correct entries for the current pipecat main examples topic-based layout.
- [ ] Every dir returned by `_discover_under_examples` maps to a taxonomy entry (Phase 1 test (g) green).
- [ ] Same builder still produces correct entries for a pinned older checkout with `examples/foundational/` + sibling topic dirs.
- [ ] `_apply_taxonomy` omits `foundational_class` for topic-layout chunks; `hybrid.py` filter unchanged.
- [ ] Example chunks carry `capability_tags` after a fresh `refresh --force` (verify via `search_examples` filter).
- [ ] PR-gating smoke tests run ≤ 15 s, offline, against vendored fixtures.
- [ ] Drift workflow runs on schedule, opens/updates a single labelled tracking issue on failure, does **not** gate PRs.
- [ ] `foundational_class` marked deprecated in models + CHANGELOG Deprecated section; remains readable.
- [ ] CHANGELOG includes explicit `refresh --force` upgrade instruction.
- [ ] CLAUDE.md, README.md, AGENTS.md updated; stale `examples/foundational/` references removed from docstrings.
- [ ] `v0.0.96` replay verified manually; output captured in PR description.
- [ ] Dashboard data regenerates cleanly post-refresh.
- [ ] `/review` and `/deep-review` run clean before merge.

## Final Results

_(fill in on completion)_

<!-- reviewed: 2026-04-23 @ 962d1f8c698f8d662aaab3503749dbf3355879e3 -->
