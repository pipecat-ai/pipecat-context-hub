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
  instructions text was confirmed untouched through the ONNX Runtime
  migration (git-verifiable), but had no CLI counterpart and, until this
  branch, no test guarding it either — now covered by
  `tests/unit/test_server.py::TestServerInstructions` and AGENTS.md item #50
  (already committed on this branch, commit `3626db4`). Whether the text is
  still *correct* post-migration (e.g. whether `load_failed` is still
  reachable at runtime) was asserted in that session but not independently
  re-verified — Phase 3 re-checks this rather than treating it as settled.
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
  `server/main.py:177` and `server/main.py:188` literals.
- Update `_SERVER_INSTRUCTIONS` in `server/main.py` to build via an
  f-string/`.format()` referencing the two constants instead of inlining the
  URLs, preserving the surrounding prose verbatim.
- Update `tests/unit/test_server.py::TestServerInstructions` to assert
  against the imported constants rather than hardcoded literals, so the test
  fails if `_SERVER_INSTRUCTIONS` stops interpolating from the shared module.
- **Add a companion value-pinning test** (e.g. `TestSupportLinks`) asserting
  `RETRIEVAL_QUALITY_ISSUE_URL` / `BUG_REPORT_ISSUE_URL` equal their exact
  literal strings. Without this, the `TestServerInstructions` update above
  becomes tautological — source and test would both reference the same
  constant, so a typo'd URL in `support_links.py` would pass everywhere.
  This test is what actually pins the value; the instructions-text test only
  pins that interpolation happened. (Review finding, architecture +
  spec-and-testing lenses.)
- Optional, low-priority strengthening: a test asserting each constant's
  `template=` query value matches a real file in `.github/ISSUE_TEMPLATE/`
  (`retrieval-quality.yml`, `bug-report.yml` both exist there today) — this
  is copying already-shipped behavior so risk is low, but the cross-check is
  cheap if added while touching this file.

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
- In `_invoke` (`cli_query.py:~252+`), immediately after entering
  `_query_runtime` (before any later failure, e.g. a `ValidationError` that
  would exit 1 — the warning is not conditional on the rest of dispatch
  succeeding), check `runtime.reranker_status.disabled_reason ==
  "not_cached"`; if true, emit a one-line `click.echo(..., err=True)`
  **remediation-first** warning: name `refresh` as the fix before citing
  `BUG_REPORT_ISSUE_URL` (e.g. "reranking disabled (model not cached) — run
  `pipecat-context-hub refresh` to download it; if it persists after that,
  file <URL>"). `not_cached` is not always an incident: swapping
  `PIPECAT_HUB_RERANKER_MODEL` without re-running `refresh` (a documented,
  routine operator step — see CLAUDE.md's reranker section) produces this
  exact state, so the message must not send a routine case straight to the
  bug tracker. Gated to only the four semantic commands (wherever
  `needs_embeddings=True` triggers reranker construction). Do not check for
  `"load_failed"` (dead path here, per Context) or `"config_disabled"`
  (excluded by design).
- **Accepted trade-off, not a bug to fix:** this warning fires once per
  process, on every semantic-command invocation while the model stays
  uncached — an agent looping `search-api` calls sees it every time, which
  sits in tension with this module's "quiet by default" stderr contract
  (`cli_query.py:14-21`). Building cross-invocation suppression (e.g. a
  state file) is out of scope — speculative generality for a `Priority: Low`
  plan. Record this explicitly as an Architecture Decision rather than
  silently accepting it (see below).
- Extend `tests/unit/test_cli_query.py`:
  - Add report-hint assertions alongside the existing
    `test_empty_index_exits_with_refresh_hint` /
    `test_unopenable_index_exits_with_reset_hint` tests.
  - Add a **third** report-hint test for the previously-untested
    `IncompatibleIndexFormatError` path (`cli_query.py:150-157`) — raise it
    from the mocked `IndexStore`/`get_index_stats` and assert exit code 2,
    the bug-report hint, and home redaction (this path's message embeds the
    absolute `chroma_path`, per the inline comment at `cli_query.py:154-155`).
  - Add a test forcing `disabled_reason="not_cached"` via
    `probe_reranker`/`_resolve_reranker` mocking that verifies the warning
    fires for a semantic command (e.g. `search-docs`) and does **not** fire
    when the reason is `config_disabled` or `None`.
  - Add a **negative test for an unknown/future reason value** (e.g. mock
    `disabled_reason="load_failed"`) asserting no warning and no crash —
    this is what actually enforces "no `load_failed` branch", which
    otherwise is only a prose promise.
  - Add a **lookup-command negative test**: force `not_cached` and run
    `status` (or `get-doc`), asserting stderr carries no bug-report URL.
    This is the non-obvious risky case — `_query_runtime` calls
    `_resolve_reranker(construct=False)` unconditionally
    (`cli_query.py:194`), so `runtime.reranker_status.disabled_reason` is
    populated even for lookup commands that never construct a reranker; a
    naive `_invoke` implementation checking only the status field (not also
    the command/`needs_embeddings`) would warn on `status`/`get-doc` too.

### Phase 3: MCP-side symmetry audit (investigate, apply only if warranted)

**Impl files:** `src/pipecat_context_hub/server/main.py` (conditional)
**Test files:** `tests/unit/test_server.py` (conditional)
**Test command:** `uv run pytest tests/unit/test_server.py -v`
**Goal:** Determine whether the CLI's precise remediation commands reveal a real gap in the MCP degraded-hub clause, and close it only if genuine — concluding "no change needed" is an acceptable outcome of this phase.

- Compare the CLI's remediation strings against what the degraded-hub clause
  currently tells the MCP agent to do (share `get_hub_status` + startup log,
  then file a bug report): **does it need a self-service remediation step
  before "file a bug report," and if so, which command?** Do not assume the
  answer — the degraded-hub clause is reranker-scoped
  (`reranker_disabled_reason` of `not_cached`/`load_failed`), which is a
  different failure domain from the CLI's `_EXIT_INDEX_UNREADY` (corrupt/
  missing Chroma index). `refresh --force --reset-index` fixes a broken
  index; it does nothing for an uncached reranker model. If a step belongs
  here, per Phase 2's finding it is plain `pipecat-context-hub refresh`
  (downloads the model), not `--force --reset-index`.
- If warranted, add a short remediation line to the degraded-hub clause
  mirroring Phase 2's CLI wording, following the existing "steps before
  giving up" structure the retrieval-quality clause already uses
  (`server/main.py:168-174`).
- Also verify `load_failed` is still reachable in the post-ONNX-migration
  `serve` runtime path (the degraded-hub clause names it, and the plan's
  Context claims the instructions text is "correct" post-migration, which
  was asserted but not re-verified when this plan was drafted).
- Record the investigation outcome in `## Findings` regardless of whether a
  change was made — "no change needed" is a legitimate result of this
  phase, not a foregone conclusion.

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
- Extend existing CLI smoke item 5 (`PIPECAT_HUB_DATA_DIR=$(mktemp -d) ...
  status`, which already exercises an index-unready path) to also assert the
  bug-report hint appears in stderr — the "freeze it twice" rule applies to
  the index-unready hints too, not just the reranker warning, and this item
  already sets up the right state cheaply.
- Note in Phase 4's own validation: `grep -rn
  'issues/new?template=' src/` must return matches only inside
  `shared/support_links.py` — call this out explicitly as a manual check
  during this phase (or add an automated test if a natural home exists) so
  the "no literal URL elsewhere in `src/`" acceptance criterion isn't
  purely aspirational.
- Add a CHANGELOG `[Unreleased]` entry summarizing the CLI/MCP parity work
  and the shared-constants extraction.
- **Land Phase 2 and Phase 4 together** (same PR/commit range). The "freeze
  it twice" rule isn't satisfied until Phase 4's smoke item exists, so a
  boundary between them would leave the new CLI behavior temporarily
  unguarded outside the unit-test net.

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
- The `not_cached` warning is remediation-first (names `refresh` before the
  bug-report URL) because `not_cached` is not inherently an incident — it is
  the same state a routine `PIPECAT_HUB_RERANKER_MODEL` swap produces before
  the next `refresh`. Jumping straight to "file a bug" would mis-route a
  self-service fix into the tracker.
- The warning fires once per process, every time a semantic command runs
  while the model is uncached — accepted as-is rather than building
  cross-invocation suppression state. This is a real, named trade-off
  against `cli_query.py`'s "quiet by default" stderr contract
  (`cli_query.py:14-21`), not an oversight: each one-shot CLI invocation is
  a fresh process with no persisted state to suppress a repeat warning
  against, and adding one (e.g. a lock/marker file) is speculative
  generality for a `Priority: Low` plan. Revisit only if this becomes an
  observed annoyance in practice.

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

- [ ] Unit test pinning the literal URL values in `support_links.py`
      (Phase 1) — not just that `_SERVER_INSTRUCTIONS` interpolates them
- [ ] Unit tests for shared-constant usage in `_SERVER_INSTRUCTIONS` (Phase 1)
- [ ] Unit tests for CLI stderr report-hint text on all three
      `_EXIT_INDEX_UNREADY` paths, including the previously-untested
      `IncompatibleIndexFormatError` path (Phase 2)
- [ ] Unit tests for the reranker `not_cached` stderr warning, including
      negative cases (`config_disabled`, `None`, and an unknown/future
      reason value such as `load_failed`) (Phase 2)
- [ ] Unit test that a lookup command (`status`/`get-doc`) never emits the
      reranker warning even when `not_cached` is forced (Phase 2)
- [ ] AGENTS.md smoke coverage extended for the index-unready bug-report
      hints, not just the reranker warning (Phase 4)
- [ ] Full suite + `ruff check` + `mypy` pass (Phase 4 validation)

### Test Results

- [ ] All existing tests pass
- [ ] New tests added and passing
- [ ] Manual verification complete

### Edge Cases Tested

- [ ] `config_disabled` never triggers the CLI warning or MCP bug-report routing
- [ ] An unknown/future `disabled_reason` value (e.g. `load_failed`) is a
      silent no-op, not a crash and not a warning
- [ ] Lookup commands never emit the reranker warning even if the reranker
      happens to be uncached (verified by a named test, not just inspection —
      `_query_runtime` probes the reranker for lookup commands too)
- [ ] Redaction still holds on all three index-unready messages after the
      appended hint line, including the previously-untested
      `IncompatibleIndexFormatError` path
- [ ] The `not_cached` CLI warning names `refresh` before the bug-report URL
      (remediation-first, not escalation-first)

## Acceptance Criteria

- `shared/support_links.py` exists with the URL constants pinned by a
  literal-value test (not just an interpolation test); both `server/main.py`
  and `cli_query.py` import them — `grep -rn 'issues/new?template=' src/`
  returns matches only inside `shared/support_links.py`.
- All three `_EXIT_INDEX_UNREADY` CLI stderr paths (including
  `IncompatibleIndexFormatError`) include a bug-report hint and are
  individually tested.
- The four semantic CLI commands emit a remediation-first stderr warning
  when, and only when, `reranker_status.disabled_reason == "not_cached"`;
  lookup commands never emit it even when `not_cached`; an unknown/future
  reason value is a silent no-op — all three cases have a named test.
- `uv run pytest tests/ -v`, `uv run ruff check src/ tests/`, and
  `uv run mypy src/ tests/` all pass.
- AGENTS.md has smoke coverage for both the reranker warning and the
  index-unready bug-report hints; CHANGELOG `[Unreleased]` has an entry.
  Phases 2 and 4 land together.
- Phase 3's investigation is recorded in `## Findings` regardless of outcome,
  and if it proposes a remediation addition to the MCP degraded-hub clause,
  the proposed command is `refresh` (not `--force --reset-index`).
- Code reviewed and approved.

<!-- reviewed: 2026-08-03 @ ecc3fd8b194cf046117a72d7f19efa61b53db926 -->

<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

- [ ] Phase 1: Shared report-hint constants module
- [ ] Phase 2: CLI stderr report-hints
- [ ] Phase 3: MCP-side symmetry audit
- [ ] Phase 4: Documentation and smoke coverage

## Findings

- **2026-08-03 — `/review-plan` run.** Five parallel lenses (architecture,
  sequencing, spec-and-testing, assumptions, codebase-claims) audited this
  plan: 18 raw findings, 0 Critical, 9 Important, 7 Minor (2 related
  cross-category pairs at the same lines). No genuine contradictions between
  findings — all were incorporated directly into the plan body above this
  marker (Phase 1's tautological-test fix, Phase 2's remediation-first
  wording + three new negative tests, Phase 3's wrong-remediation-command
  correction, Phase 4's extended smoke coverage, plus the Context/Architecture
  Decisions softening). One codebase-claims finding (a "misidentified
  degraded-hub clause" line citation) turned out to be an artifact of my own
  prompt condensation for that lens, not a real plan defect — verified
  against the actual plan text and left unchanged. Full findings:
  `.review-plan/latest-claude.json` (plan_hash
  `817f629ef39490d1bfc99d3f2fb5d359412b370a`, the pre-fix version — the
  marker below hashes the post-fix content).

### Review Waivers

(none — every finding was either addressed above or determined to be a
false positive from lens-prompt condensation, not a waived defect)

## Issues & Solutions

(none yet)

## Final Results

(fill in on completion)
