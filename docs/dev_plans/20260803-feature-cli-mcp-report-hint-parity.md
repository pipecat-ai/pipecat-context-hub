# Task: CLI/MCP self-report guidance parity

**Status**: Not Started
**Component**: cli, server (mcp instructions)
**Assigned to**: Claude
**Priority**: Low
**Branch**: feature/self-report-guidance-parity
**Created**: 2026-08-03
**Completed**:
**Review Gates**: quick

## Objective

Give the CLI front door (`cli_query.py`) the same "when to self-report" guidance
the MCP server already gives a connecting agent via `_SERVER_INSTRUCTIONS`,
without letting the two copies of the report-hint URLs drift apart, and check
whether the CLI's existing per-error remediation specificity should be
backported to the MCP side for symmetry.

## Context

- The MCP server sends `_SERVER_INSTRUCTIONS` (`server/main.py:125-194`) to
  every connecting agent at `initialize`, with two report-hint clauses:
  **retrieval-quality** (persistent `low_confidence`/zero-hit results) →
  `.../issues/new?template=retrieval-quality.yml`, and **degraded-hub**
  (`reranker_disabled_reason` of `not_cached`/`load_failed`, explicitly
  excluding `config_disabled`) → `.../issues/new?template=bug-report.yml`.
- The CLI (`cli_query.py`) is a second front door to the identical retrieval
  stack and tool handlers (module docstring, `cli_query.py:1-29`) but has no
  equivalent nudge — it already gives its own remediation hints on error
  (`refresh --force --reset-index` / `refresh`) but never points at either
  GitHub issue tracker.
- This gap surfaced during a PR #107/#108 follow-up review session: the MCP
  instructions text was confirmed untouched (and correct) through the ONNX
  Runtime migration, but had no CLI counterpart and, until this branch, no
  test guarding it either — now covered by
  `tests/unit/test_server.py::TestServerInstructions` and AGENTS.md item #50
  (already committed on this branch, commit `3626db4`).
- **Key constraint found during exploration:** `probe_reranker()`
  (`shared/reranker.py:26-49`) never returns `"load_failed"` — that reason is
  runtime-only, observable solely by the long-lived `serve` process. The
  one-shot CLI's own probe can only ever surface `"not_cached"`. The plan
  must not promise CLI-side handling of a `load_failed` case that structurally
  cannot occur there.

## Requirements

- Both issue-template URLs live in exactly one place; MCP instructions and
  CLI messages both import from it — no second hardcoded copy anywhere in
  `src/`.
- CLI gains a bug-report hint on all three `_EXIT_INDEX_UNREADY` stderr paths
  (`IncompatibleIndexFormatError`, failed-to-open, empty-index —
  `cli_query.py:150-184`; exploration found three call sites sharing this
  exit code, not the two originally assumed).
- CLI gains a degraded-reranker stderr warning gated on
  `disabled_reason == "not_cached"` only (never `load_failed`, which cannot
  occur here; never `config_disabled`, a deliberate operator choice), emitted
  only for the four semantic commands that actually construct a reranker
  (`search-docs`, `search-examples`, `search-api`, `get-code-snippet`).
- No change to the stdout JSON contract — every new string goes to stderr.
- MCP instructions gain the CLI's remediation specificity only if Phase 3's
  investigation finds a genuine gap — this may conclude "no change needed."
- Every new behavior gets this repo's "freeze it twice" treatment: a unit
  test plus an AGENTS.md numbered smoke item.
- A CHANGELOG entry under the currently-empty `[Unreleased]` section.

## Review Focus

- The reranker-warning gating must reproduce `not_cached`-only,
  `config_disabled`-excluded logic without also branching on `load_failed`,
  which `probe_reranker` never returns to the CLI — check this isn't silently
  presented as "handled" when it's actually dead code.
- No redaction regression: every new/modified stderr string must stay inside
  the existing `redact_home_in_text(...)` wrapping used by the three
  `_EXIT_INDEX_UNREADY` paths.
- Wording-drift risk: confirm the MCP-side prose and the CLI-side stderr line
  both reference the *same* imported constant, not independently retyped
  literals.

## Implementation Checklist

### Phase 1: Shared report-hint constants module

**Impl files:** `src/pipecat_context_hub/shared/support_links.py` (new), `src/pipecat_context_hub/server/main.py`
**Test files:** `tests/unit/test_server.py`
**Test command:** `uv run pytest tests/unit/test_server.py -v`
**Goal:** One source of truth for the two GitHub issue-template URLs, so `_SERVER_INSTRUCTIONS` and the future CLI strings structurally cannot diverge on the URL itself.

- Create `shared/support_links.py` with `RETRIEVAL_QUALITY_ISSUE_URL` and
  `BUG_REPORT_ISSUE_URL` constants, values copied verbatim from the current
  `server/main.py:178` and `server/main.py:190` literals.
- Update `_SERVER_INSTRUCTIONS` in `server/main.py` to build via an
  f-string/`.format()` referencing the two constants instead of inlining the
  URLs, preserving the surrounding prose verbatim.
- Update `tests/unit/test_server.py::TestServerInstructions` to assert
  against the imported constants rather than hardcoded literals, so the test
  fails if the source and the test's copy of the URL ever diverge
  independently.

### Phase 2: CLI stderr report-hints

**Impl files:** `src/pipecat_context_hub/cli_query.py`
**Test files:** `tests/unit/test_cli_query.py`
**Test command:** `uv run pytest tests/unit/test_cli_query.py -v`
**Goal:** The three `_EXIT_INDEX_UNREADY` stderr paths and the reranker degraded-state case give the CLI operator the same "where to report this" nudge the MCP instructions already give a connecting agent, without touching the stdout JSON contract or any exit code.

- Import `BUG_REPORT_ISSUE_URL` from `shared/support_links.py` into
  `cli_query.py`.
- Append a report-hint line to all three `_EXIT_INDEX_UNREADY` messages
  (`IncompatibleIndexFormatError` handler, failed-to-open handler,
  empty-index handler — `cli_query.py:150-184`), keeping the appended text
  inside the existing `redact_home_in_text(...)` call for structural
  uniformity even though the hint itself carries no path.
- In `_invoke` (`cli_query.py:~252+`), after entering `_query_runtime`, check
  `runtime.reranker_status.disabled_reason == "not_cached"`; if true, emit a
  one-line `click.echo(..., err=True)` warning citing
  `BUG_REPORT_ISSUE_URL`, gated to only the four semantic commands (wherever
  `needs_embeddings=True` triggers reranker construction). Do not check for
  `"load_failed"` (dead path here, per Context) or `"config_disabled"`
  (excluded by design).
- Extend `tests/unit/test_cli_query.py`: add report-hint assertions
  alongside the existing `test_empty_index_exits_with_refresh_hint` /
  `test_unopenable_index_exits_with_reset_hint` tests (or new sibling
  tests), and add a new test forcing `disabled_reason="not_cached"` via
  `probe_reranker`/`_resolve_reranker` mocking that verifies the warning
  fires for a semantic command and does **not** fire when the reason is
  `config_disabled` or `None`.

### Phase 3: MCP-side symmetry audit (investigate, apply only if warranted)

**Impl files:** `src/pipecat_context_hub/server/main.py` (conditional)
**Test files:** `tests/unit/test_server.py` (conditional)
**Test command:** `uv run pytest tests/unit/test_server.py -v`
**Goal:** Determine whether the CLI's precise remediation commands reveal a real gap in the MCP degraded-hub clause, and close it only if genuine — concluding "no change needed" is an acceptable outcome of this phase.

- Compare the CLI's three remediation strings (`refresh --force
  --reset-index`, `refresh`, and the bare `IncompatibleIndexFormatError`
  passthrough) against what the degraded-hub clause currently tells the MCP
  agent to do (share `get_hub_status` + startup log, then file a bug report)
  — it currently omits a "try `refresh --force --reset-index` first" step
  that the CLI already gives for free.
- If warranted, add a short remediation line to the degraded-hub clause
  mirroring the CLI's, following the existing "steps before giving up"
  structure the retrieval-quality clause already uses
  (`server/main.py:168-174`).
- Record the investigation outcome in `## Findings` regardless of whether a
  change was made.

### Phase 4: Documentation and smoke coverage

**Impl files:** `AGENTS.md`, `CHANGELOG.md`
**Test files:** none (docs)
**Test command:** `uv run pytest tests/ -q`
**Goal:** The new CLI behavior gets the same "freeze it twice" treatment already applied to the MCP side (AGENTS.md #50), so a future refactor can't silently drop it.

- Add a new CLI-query-smoke item (after existing item 8, i.e. item 9)
  documenting how to reproduce the `not_cached` reranker warning (e.g. point
  `PIPECAT_HUB_RERANKER_MODEL` or `HF_HOME` at an empty cache) and confirm
  both the stdout JSON and the stderr warning appear together; reference the
  new unit tests as the automated counterpart.
- Add a CHANGELOG `[Unreleased]` entry summarizing the CLI/MCP parity work
  and the shared-constants extraction.

## Technical Specifications

### Files to Modify

- `src/pipecat_context_hub/server/main.py` — interpolate `_SERVER_INSTRUCTIONS`
  from shared constants; possible Phase-3 remediation addition.
- `src/pipecat_context_hub/cli_query.py` — three `_EXIT_INDEX_UNREADY`
  messages plus a new reranker-degraded stderr warning in `_invoke`.
- `tests/unit/test_server.py` — `TestServerInstructions` updated to assert
  against constants.
- `tests/unit/test_cli_query.py` — extended stderr assertions plus new
  reranker-warning tests.
- `AGENTS.md` — new CLI smoke item.
- `CHANGELOG.md` — `[Unreleased]` entry.

### New Files to Create

- `src/pipecat_context_hub/shared/support_links.py` — the two GitHub
  issue-template URL constants; the single source of truth for both MCP and
  CLI.

### Architecture Decisions

- Extract only the URL constants, not full report-hint sentences, into the
  shared module. MCP's prose is written for an LLM reading multi-step
  instructions; CLI's is a single terse stderr line for a human or script.
  Forcing identical wording would make one or the other read badly. Sharing
  the URL is what actually prevents drift (a template rename or repo move
  breaking one copy silently while the other still works) — the wording
  itself isn't the drift risk.
- Do not attempt to have the CLI detect `load_failed`. `probe_reranker()`
  structurally never returns it outside the long-lived `serve` process
  (verified during exploration); building handling for a case that cannot
  occur would be speculative generality against this project's "minimal
  impact" convention.
- The reranker warning fires only for the four semantic commands. Lookup
  commands (`check-deprecation`/`get-doc`/`get-example`/`status`) never
  construct a reranker, so warning there would be either always-silent or
  (for `status`, which already reports `reranker_disabled_reason` in its
  JSON body) redundant.

### Dependencies

- None new — internal refactor plus additive stderr text only.

### Integration Seams

| Seam | Writer (task) | Caller (task) | Contract |
|------|---------------|----------------|----------|
| Issue-template URLs | Phase 1 (`shared/support_links.py`) | Phase 2 (`cli_query.py`), Phase 3 (`server/main.py`) | Both import the same constants; neither hardcodes a literal URL after Phase 1 lands |
| `RerankerStatus.disabled_reason` vocabulary | `shared/reranker.py::probe_reranker` (unchanged by this plan) | Phase 2's `_invoke` | Phase 2 only branches on `"not_cached"`; treats `"config_disabled"`/`None` as no-op and any other/future value as no-op rather than raising |

No `## Architecture & Call Flow` section: the MCP server process and the CLI
process are alternate independent front doors to the same backend, not a
call chain within a single run — neither hands control to the other during
one invocation, so the "2+ independently-executing components" trigger for
that section (designed for cross-component call chains) doesn't apply here.

## Testing Notes

### Test Approach

- [ ] Unit tests for shared-constant usage in `_SERVER_INSTRUCTIONS` (Phase 1)
- [ ] Unit tests for CLI stderr report-hint text on all three
      `_EXIT_INDEX_UNREADY` paths (Phase 2)
- [ ] Unit tests for the reranker `not_cached` stderr warning, including
      negative cases (`config_disabled`, `None`) (Phase 2)
- [ ] Full suite + `ruff check` + `mypy` pass (Phase 4 validation)

### Test Results

- [ ] All existing tests pass
- [ ] New tests added and passing
- [ ] Manual verification complete

### Edge Cases Tested

- [ ] `config_disabled` never triggers the CLI warning or MCP bug-report routing
- [ ] Lookup commands never emit the reranker warning even if the reranker
      happens to be uncached
- [ ] Redaction still holds on all three index-unready messages after the
      appended hint line

## Acceptance Criteria

- `shared/support_links.py` exists; both `server/main.py` and
  `cli_query.py` import the URL constants from it — no literal
  `github.com/pipecat-ai/pipecat-context-hub/issues/new?template=...` string
  exists anywhere else in `src/`.
- All three `_EXIT_INDEX_UNREADY` CLI stderr paths include a bug-report hint.
- The four semantic CLI commands emit a stderr warning when, and only when,
  `reranker_status.disabled_reason == "not_cached"`.
- `uv run pytest tests/ -v`, `uv run ruff check src/ tests/`, and
  `uv run mypy src/ tests/` all pass.
- AGENTS.md has a new CLI-side smoke item; CHANGELOG `[Unreleased]` has an
  entry.
- Code reviewed and approved.

<!-- reviewed: YYYY-MM-DD @ <hash> -->
<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

- [ ] Phase 1: Shared report-hint constants module
- [ ] Phase 2: CLI stderr report-hints
- [ ] Phase 3: MCP-side symmetry audit
- [ ] Phase 4: Documentation and smoke coverage

## Findings

- (append findings here as work proceeds)

## Issues & Solutions

(none yet)

## Final Results

(fill in on completion)
