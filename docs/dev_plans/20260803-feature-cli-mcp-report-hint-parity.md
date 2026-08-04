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
  re-verified — Phase 4 re-checks this rather than treating it as settled.
- **Key constraint found during exploration:** `probe_reranker()`
  (`shared/reranker.py:26-49`) never *returns* `"load_failed"` to a caller —
  that string is only ever assembled by `serve`'s own `_reranker_status()`
  closure (`cli.py:389-395`). The one-shot CLI's probe (`_resolve_reranker`)
  can only surface `"config_disabled"` or `"not_cached"` via
  `runtime.reranker_status.disabled_reason`. This is narrower than "the CLI
  cannot hit this failure mode at all": when a semantic command actually
  constructs a live cross-encoder (`construct=True`), `CrossEncoderReranker`
  can still fail to load the ONNX weights at that point —
  `_load_model()` (`services/retrieval/cross_encoder.py:74-85`) swallows the
  exception, logs a `WARNING`, and flips `_available = False`, which is the
  same underlying condition `serve` labels `load_failed`. `reranker_status`
  is captured *before* dispatch, so it still reports `disabled_reason=None`
  in that case. This plan does not attempt to surface that runtime failure
  as a report-hint — see the corresponding Architecture Decision below — but
  the plan must not claim the failure mode is structurally impossible in the
  CLI process, only that `probe_reranker()`'s return value can't carry it.

## Requirements

- Both issue-template URLs live in exactly one place; MCP instructions and
  CLI messages both import from it — no second hardcoded copy anywhere in
  `src/`.
- CLI gains a bug-report hint on all three `_EXIT_INDEX_UNREADY` stderr paths
  (`IncompatibleIndexFormatError`, failed-to-open, empty-index —
  `cli_query.py:150-184`; exploration found three call sites sharing this
  exit code, not the two originally assumed).
- CLI gains a degraded-reranker stderr warning gated on
  `disabled_reason == "not_cached"` only (`probe_reranker()` never returns
  `"load_failed"` to the CLI, and `"config_disabled"` is a deliberate
  operator choice), emitted only when `needs_embeddings=True` (the same flag
  that already gates reranker construction at each of the four semantic
  commands' call sites) — expressed as that literal condition, not a
  hardcoded command-name list, so a future semantic command inherits the
  warning automatically.
- `cli.py`'s `serve`-startup `not_cached` remediation message
  (`cli.py:352-371`) gets the same `BUG_REPORT_ISSUE_URL` treatment as the
  CLI-query and MCP-instructions copies, so this plan's own "one source of
  truth" requirement doesn't leave a third, untouched emitter — see Phase 2.
- No change to the stdout JSON contract — every new string goes to stderr;
  covered by a dedicated test (Phase 2), not just implied by the requirement.
- MCP instructions gain the CLI's remediation specificity only if Phase 4's
  investigation finds a genuine gap — this may conclude "no change needed."
- Every new behavior gets this repo's "freeze it twice" treatment: a unit
  test plus an AGENTS.md numbered smoke item.
- A CHANGELOG entry under the currently-empty `[Unreleased]` section.

## Review Focus

- The reranker-warning gating must reproduce `not_cached`-only,
  `config_disabled`-excluded logic without also branching on `load_failed`,
  which `probe_reranker` never *returns* to the CLI — check this isn't
  silently presented as "the CLI structurally cannot hit this failure mode"
  when the underlying cross-encoder load failure actually can occur there
  (see Context and the corresponding Architecture Decision); the narrower,
  accurate claim is only that `disabled_reason` can't carry it.
- No redaction regression: every new/modified stderr string — this now
  includes the new reranker `not_cached` warning line, not just the three
  existing `_EXIT_INDEX_UNREADY` paths — must stay inside the existing
  `redact_home_in_text(...)` wrapping, or be independently verified to carry
  no path.
- Wording-drift risk: confirm the MCP-side prose, the CLI-query stderr line,
  and the `cli.py` startup log line all reference the *same* imported
  constant, not independently retyped literals.
- Consistency risk: the plan's "don't mis-route a routine state to the bug
  tracker" reasoning is applied to the `not_cached` reranker warning; check
  whether the same reasoning should apply to the empty-index
  `_EXIT_INDEX_UNREADY` path (also a routine first-run state) — see the
  Architecture Decision addressing this asymmetry.

## Implementation Checklist

Every phase that produces a commit runs the full quality gate before landing
— `uv run pytest tests/ -q && uv run ruff check src/ tests/ && uv run mypy
src/ tests/` — not just its own per-phase test command. Phase 1 introduces a
new module imported by `server/main.py`; a stale import elsewhere should not
be able to hide until the last phase.

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
- **Add a required automated single-source-of-truth test** (e.g.
  `TestSupportLinks::test_no_stray_issue_template_literals`) walking
  `src/**/*.py` and asserting the substring `"issues/new?template="`
  appears only inside `shared/support_links.py`. This finishes landing only
  once Phase 2 (and, if adopted, the `cli.py` update) also import from the
  shared module, so the test should be added now (it will trivially pass) and
  re-run as a regression guard once Phase 2 removes the last hardcoded copy —
  do not leave this as a manual `grep` note in Phase 4; the repo already has
  the equivalent pattern in `TestToolCommandParity` /
  `TestVersionConsistency`. (Known, accepted exception: `docs/README.md:444`
  carries a doc-only literal copy that this test does not and should not
  cover — markdown can't import Python constants; see the Phase 4 note.)

### Phase 2: CLI stderr report-hints

**Impl files:** `src/pipecat_context_hub/cli_query.py`, `src/pipecat_context_hub/cli.py`
**Test files:** `tests/unit/test_cli_query.py`
**Test command:** `uv run pytest tests/unit/test_cli_query.py -v`
**Goal:** The three `_EXIT_INDEX_UNREADY` stderr paths, the reranker degraded-state case, and `cli.py`'s `serve`-startup `not_cached` log line all give the operator the same "where to report this" nudge the MCP instructions already give a connecting agent, without touching the stdout JSON contract or any exit code.

- Import `BUG_REPORT_ISSUE_URL` from `shared/support_links.py` into
  `cli_query.py` and `cli.py`.
- Append a report-hint line to all three `_EXIT_INDEX_UNREADY` messages
  (`IncompatibleIndexFormatError` handler, failed-to-open handler,
  empty-index handler — `cli_query.py:150-184`), keeping the appended text
  inside the existing `redact_home_in_text(...)` call for structural
  uniformity even though the hint itself carries no path. Each of the three
  already names its own remediation (`--force --reset-index` or `refresh`)
  before the appended text; word the appended hint as "if this persists
  after trying that, file <URL>" for all three, so the empty-index path (the
  most routine of the three — a bare first run before `refresh`) doesn't
  read as escalation-first any more than the reranker warning below does.
- Emit the reranker-degraded warning from inside `_query_runtime`
  (`cli_query.py:150-209`), immediately after `_resolve_reranker(construct=True)`
  resolves `reranker_status` — co-located with the other runtime diagnostics
  it's built from, rather than from `_invoke` after the context is entered.
  Condition: `if needs_embeddings and reranker_status.disabled_reason ==
  "not_cached"`, expressed as that literal boolean condition (not an
  enumerated command-name list — `needs_embeddings` is already threaded as a
  parameter into every command body, so a future semantic command inherits
  the warning automatically without a plan/code update). Emit a one-line
  `click.echo(..., err=True)` **remediation-first** warning inside the same
  `redact_home_in_text(...)` wrapping used above (the message itself embeds
  no path today, but wrapping it keeps the invariant structural rather than
  case-by-case): name `refresh` as the fix before citing
  `BUG_REPORT_ISSUE_URL` (e.g. "reranking disabled (model not cached) — run
  `pipecat-context-hub refresh` to download it; if it persists after that,
  file <URL>"). `not_cached` is not always an incident: swapping
  `PIPECAT_HUB_RERANKER_MODEL` to one of the allowlisted model names
  (`shared/config.py`'s allowlist) without re-running `refresh` (a
  documented, routine operator step — see CLAUDE.md's reranker section)
  produces this exact state — a swap to a *non*-allowlisted value instead
  falls back to the cached default and never reaches `not_cached` at all, so
  the "routine swap" framing only applies to allowlisted targets. Do not
  check for `"load_failed"` (never returned by `probe_reranker()` to the
  CLI, per Context) or `"config_disabled"` (excluded by design).
- Add the same `not_cached` → `BUG_REPORT_ISSUE_URL` remediation-first
  treatment to `cli.py`'s `serve`-startup log line
  (`cli.py:352-371`), so the third emitter of this exact operator guidance
  doesn't drift from the other two — this is the parity gap this plan exists
  to close, not an optional stretch goal.
- **Accepted trade-off, not a bug to fix:** the CLI-query warning fires once
  per process, on every semantic-command invocation while the model stays
  uncached — an agent looping `search-api` calls sees it every time, which
  sits in tension with this module's "quiet by default" stderr contract
  (`cli_query.py:14-21`). Building cross-invocation suppression (e.g. a
  state file) is out of scope — speculative generality for a `Priority: Low`
  plan. Record this explicitly as an Architecture Decision rather than
  silently accepting it (see below).
- **Named, accepted gap (not fixed by this plan):** a *runtime* cross-encoder
  load failure inside a one-shot semantic command (the ONNX weights are
  present but fail to load — see Context) degrades silently to RRF-only
  beyond a `WARNING` log; `reranker_status.disabled_reason` was captured
  before dispatch and won't reflect it. This plan does not add report-hint
  coverage for that path — record it as a known gap in `## Findings` rather
  than silently shipping it as if `load_failed` handling were complete.
- Extend `tests/unit/test_cli_query.py`:
  - Add report-hint assertions alongside the existing
    `test_empty_index_exits_with_refresh_hint` /
    `test_unopenable_index_exits_with_reset_hint` tests, including for each
    an assertion that `result.stdout == ""` (or is otherwise untouched) —
    pinning the "no change to the stdout JSON contract" requirement, not
    just the stderr addition.
  - Add a **third** report-hint test for the previously-untested
    `IncompatibleIndexFormatError` path (`cli_query.py:150-157`) — raise it
    from the mocked `IndexStore`/`get_index_stats` and assert exit code 2,
    the bug-report hint, and home redaction (this path's message embeds the
    absolute `chroma_path`, per the inline comment at `cli_query.py:154-155`).
  - Add a test, **parametrized over all four semantic commands**
    (`search-docs`, `search-examples`, `search-api`, `get-code-snippet`),
    forcing `disabled_reason="not_cached"` via
    `probe_reranker`/`_resolve_reranker` mocking, that verifies: the warning
    fires on stderr; `json.loads(result.stdout)` still succeeds and contains
    no `BUG_REPORT_ISSUE_URL`; and no home path leaks into the warning line.
    A single-command test would miss a miswired `needs_embeddings` flag on
    any of the other three.
  - Add a negative test verifying the warning does **not** fire when the
    reason is `config_disabled` or `None`.
  - Add a **negative test for an unknown/future reason value** (e.g. mock
    `disabled_reason="load_failed"`) asserting no warning and no crash —
    this is what actually enforces "no `load_failed` branch", which
    otherwise is only a prose promise.
  - Add a **lookup-command negative test, parametrized over all four lookup
    commands** (`check-deprecation`, `get-doc`, `get-example`, `status`):
    force `not_cached` and assert stderr carries no bug-report URL. This is
    the non-obvious risky case — `_query_runtime` calls
    `_resolve_reranker(construct=False)` unconditionally
    (`cli_query.py:194`), so `runtime.reranker_status.disabled_reason` is
    populated even for lookup commands that never construct a reranker; a
    naive implementation checking only the status field (not also
    `needs_embeddings`) would warn on all four. `status` alone is the
    weakest example (its JSON body already surfaces
    `reranker_disabled_reason`), so it isn't sufficient on its own.
  - Add a **co-fire negative test**: for each of the three
    `_EXIT_INDEX_UNREADY` exits, assert the reranker-warning's
    `BUG_REPORT_ISSUE_URL` line does not also appear in stderr — these exits
    raise before the reranker warning's emission point, and this pins that
    invariant so a later refactor can't silently merge the two paths.

### Phase 3: Documentation and smoke coverage

**Impl files:** `AGENTS.md`, `CHANGELOG.md`
**Test files:** none (docs)
**Test command:** `uv run pytest tests/ -q`
**Goal:** The new CLI behavior gets the same "freeze it twice" treatment already applied to the MCP side (AGENTS.md #50), so a future refactor can't silently drop it.

- Add a new CLI-query-smoke item (after existing item 8; note the CLI smoke
  list already has a non-sequential `6a` entry with no plain `6` — that
  numbering is pre-existing and must be left as-is, so the new item is `9`)
  documenting how to reproduce the `not_cached` reranker warning. Use
  `PIPECAT_HUB_RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-12-v2` (an
  allowlisted, typically-uncached model) as the recipe, **not**
  `HF_HOME` pointed at an empty cache — an empty `HF_HOME` also breaks the
  embedding-model load (`services/embedding.py` resolves from the same HF
  cache, and `quiet_model_loading()` sets `HF_HUB_OFFLINE=1`), so it would
  fail the command outright rather than reproduce "stdout JSON and stderr
  warning together." Confirm both the stdout JSON and the stderr warning
  appear together; reference the new unit tests as the automated
  counterpart.
- Extend existing CLI smoke item 5 (`PIPECAT_HUB_DATA_DIR=$(mktemp -d) ...
  status`, which already exercises an index-unready path) to also assert the
  bug-report hint appears in stderr — the "freeze it twice" rule applies to
  the index-unready hints too, not just the reranker warning, and this item
  already sets up the right state cheaply.
- **Update AGENTS.md item #50 and the `tests/unit/test_server.py` docstring
  it's cross-referenced from**: both currently state the self-report
  guidance "never reaches the one-shot CLI (`cli_query.py`), which has no
  agent-in-the-loop to hand it to." Phase 2 makes that statement false —
  reword item #50 to point at the new CLI counterpart (and its AGENTS.md
  smoke items) instead of denying it exists, and correct the
  `TestServerInstructions` docstring accordingly.
- Phase 1's automated single-source-of-truth test (`TestSupportLinks`)
  already enforces the "no literal URL elsewhere in `src/`" invariant; no
  further action needed here beyond confirming it passes post-Phase-2.
  `docs/README.md:444` carries a known, accepted third literal copy of the
  retrieval-quality URL outside `src/` (markdown can't import Python
  constants) — note it here as the place to update by hand if a template is
  ever renamed; it is intentionally not covered by the automated test or the
  acceptance grep.
- Add a CHANGELOG `[Unreleased]` entry summarizing the CLI/MCP parity work
  and the shared-constants extraction.
- **Land Phase 2 and Phase 3 together** (same PR/commit range). The "freeze
  it twice" rule isn't satisfied until this phase's smoke item exists, so a
  boundary between them would leave the new CLI behavior temporarily
  unguarded outside the unit-test net. Phase 4 (below) is independent of
  this pairing and must not land between Phase 2 and Phase 3.

### Phase 4: MCP-side symmetry audit (investigate, apply only if warranted; standalone commit)

**Impl files:** `src/pipecat_context_hub/server/main.py` (conditional), `AGENTS.md` (conditional), `CHANGELOG.md` (conditional)
**Test files:** `tests/unit/test_server.py` (conditional)
**Test command:** `uv run pytest tests/unit/test_server.py -v`
**Goal:** Determine whether the CLI's precise remediation commands reveal a real gap in the MCP degraded-hub clause, and close it only if genuine — concluding "no change needed" is an acceptable outcome of this phase. This phase is intentionally sequenced last, as its own standalone commit — it must not land between Phase 2 and Phase 3, which are required to land together.

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
  (`server/main.py:168-174`). If this phase changes `_SERVER_INSTRUCTIONS`
  prose, update AGENTS.md item #50 and append to the same CHANGELOG
  `[Unreleased]` entry Phase 3 added, in this same commit — both would
  otherwise describe stale instructions text.
- Also verify `load_failed` is still reachable in the post-ONNX-migration
  `serve` runtime path (the degraded-hub clause names it, and the plan's
  Context claims the instructions text is "correct" post-migration, which
  was asserted but not re-verified when this plan was drafted). **Both
  outcomes are anticipated, not just the "add a remediation line" branch:**
  - If reachable (expected — `services/retrieval/cross_encoder.py:74-85`
    flips `_available=False` on an `OnnxCrossEncoder` construction failure
    inside `serve`'s long-lived process, and `cli.py:389-395`'s
    `_reranker_status()` closure reports it as `load_failed`), this
    confirms the existing instructions text and no removal is needed.
  - If somehow found unreachable, the same commit must remove `load_failed`
    from `_SERVER_INSTRUCTIONS`, drop the corresponding assertion in
    `tests/unit/test_server.py` (the one pinning `"load_failed" in
    _SERVER_INSTRUCTIONS`), and update AGENTS.md item #50 and the CHANGELOG
    entry accordingly — this phase cannot conclude with a red suite.
- Record the investigation outcome in `## Findings` regardless of whether a
  change was made — "no change needed" is a legitimate result of this
  phase, not a foregone conclusion.

## Technical Specifications

### Files to Modify

- `src/pipecat_context_hub/server/main.py` — interpolate `_SERVER_INSTRUCTIONS`
  from shared constants; possible Phase-4 remediation addition.
- `src/pipecat_context_hub/cli_query.py` — three `_EXIT_INDEX_UNREADY`
  messages plus a new reranker-degraded stderr warning, emitted from
  `_query_runtime`.
- `src/pipecat_context_hub/cli.py` — `serve`-startup `not_cached` log line
  gains the same `BUG_REPORT_ISSUE_URL` treatment.
- `tests/unit/test_server.py` — `TestServerInstructions` updated to assert
  against constants; docstring corrected (Phase 3) to stop claiming the CLI
  never gets this guidance; conditional Phase-4 assertion changes if
  `load_failed` proves unreachable.
- `tests/unit/test_cli_query.py` — extended stderr assertions plus new
  reranker-warning tests (parametrized across all four semantic and all four
  lookup commands), stdout-contract assertions, and co-fire negative tests.
- `AGENTS.md` — new CLI smoke item; item #50 reworded; conditional Phase-4
  update if the degraded-hub clause changes.
- `CHANGELOG.md` — `[Unreleased]` entry (Phase 3); conditional Phase-4
  addendum if the degraded-hub clause changes.

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
  itself isn't the drift risk. **Named trade-off (review finding):** this
  leaves the `not_cached` → remediation-text *mapping* itself duplicated
  across `cli_query.py` and `cli.py` — a future new disable reason still
  needs its remediation wording written twice. Accepted for this
  `Priority: Low` plan rather than introducing a
  `disabled_reason_hint(reason) -> str | None` helper in
  `shared/reranker.py`; revisit if a third disable reason is ever added.
- Do not attempt to detect `load_failed` via `probe_reranker()`'s return
  value in the CLI — it structurally never returns that string to any
  caller (verified during exploration); building handling for a value that
  can't appear would be speculative generality against this project's
  "minimal impact" convention. This is narrower than claiming the *failure
  mode* can't occur in the CLI process: a runtime cross-encoder load failure
  inside a one-shot semantic command can still happen (see Context) and is
  a named, accepted gap in this plan's coverage, not something this
  decision resolves.
- The reranker warning fires when `needs_embeddings` is true — expressed as
  that literal condition, not an enumerated list of "the four semantic
  commands." Lookup commands (`check-deprecation`/`get-doc`/`get-example`/
  `status`) never set `needs_embeddings`, so warning there would be either
  always-silent or (for `status`, which already reports
  `reranker_disabled_reason` in its JSON body) redundant. Keying off the
  existing flag rather than a name list means a future semantic command
  inherits the warning without a plan or code change.
- The `not_cached` warning is remediation-first (names `refresh` before the
  bug-report URL) because `not_cached` is not inherently an incident — it is
  the state a routine `PIPECAT_HUB_RERANKER_MODEL` swap *to an allowlisted
  model name* produces before the next `refresh` (a swap to a
  non-allowlisted name instead silently falls back to the cached default
  and never reaches `not_cached`). Jumping straight to "file a bug" would
  mis-route a self-service fix into the tracker. The same reasoning now
  applies uniformly to all three `_EXIT_INDEX_UNREADY` messages too (Phase
  2): each already names its own remediation before the appended
  report-hint, phrased "if this persists after trying that, file <URL>," so
  the routine empty-index first-run case isn't treated as more
  escalation-worthy than `not_cached`.
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
| Issue-template URLs | Phase 1 (`shared/support_links.py`) | Phase 2 (`cli_query.py`, `cli.py`), Phase 4 (`server/main.py`, conditional) | All import the same constants; neither hardcodes a literal URL after Phase 1 lands (enforced by Phase 1's automated test, not just Phase 3's manual note) |
| `RerankerStatus.disabled_reason` vocabulary | `shared/reranker.py::probe_reranker` (unchanged by this plan) | Phase 2's `_query_runtime` | Phase 2 only branches on `"not_cached"`; treats `"config_disabled"`/`None` as no-op and any other/future value as no-op rather than raising |
| MCP degraded-hub clause vocabulary (`not_cached`/`load_failed`, `config_disabled` excluded) | `server/main.py::_SERVER_INSTRUCTIONS` (conditional edit) | AGENTS.md item #50, `tests/unit/test_server.py` | Phase 4's outcome (add remediation line, or confirm/remove `load_failed`) must be reflected in both the same commit — a stale AGENTS.md item #50 or a stale test assertion is itself a finding class this plan exists to prevent recurring |

No `## Architecture & Call Flow` section: the MCP server process and the CLI
process (both `cli.py`'s `serve` entry point and `cli_query.py`'s one-shot
commands) are alternate independent front doors to the same backend, not a
call chain within a single run — neither hands control to the other during
one invocation, so the "2+ independently-executing components" trigger for
that section (designed for cross-component call chains) doesn't apply here.

## Testing Notes

### Test Approach

- [ ] Unit test pinning the literal URL values in `support_links.py`
      (Phase 1) — not just that `_SERVER_INSTRUCTIONS` interpolates them
- [ ] Unit tests for shared-constant usage in `_SERVER_INSTRUCTIONS` (Phase 1)
- [ ] Automated single-source-of-truth test: `"issues/new?template="`
      appears only in `shared/support_links.py` across `src/**/*.py` (Phase 1)
- [ ] Unit tests for CLI stderr report-hint text on all three
      `_EXIT_INDEX_UNREADY` paths, including the previously-untested
      `IncompatibleIndexFormatError` path, each also asserting
      `result.stdout` is unaffected (Phase 2)
- [ ] Unit test, parametrized over all four semantic commands, for the
      reranker `not_cached` stderr warning, including negative cases
      (`config_disabled`, `None`, and an unknown/future reason value such as
      `load_failed`), each also asserting `stdout` parses as valid JSON with
      no `BUG_REPORT_ISSUE_URL` in it (Phase 2)
- [ ] Unit test, parametrized over all four lookup commands
      (`check-deprecation`/`get-doc`/`get-example`/`status`), that none ever
      emits the reranker warning even when `not_cached` is forced (Phase 2)
- [ ] Co-fire negative test: none of the three `_EXIT_INDEX_UNREADY` exits
      also emits the reranker-warning URL (Phase 2)
- [ ] `cli.py`'s `serve`-startup `not_cached` log line carries
      `BUG_REPORT_ISSUE_URL` (Phase 2)
- [ ] AGENTS.md smoke coverage extended for the index-unready bug-report
      hints, not just the reranker warning; item #50 reworded to match
      reality (Phase 3)
- [ ] Full suite + `ruff check` + `mypy` pass as the exit gate for every
      phase that lands a commit, not just Phase 3
- [ ] Phase 4's `load_failed`-reachability check lands a regression test (or,
      if unreachable, removes the corresponding assertion) rather than
      resting on a one-time manual verification

### Test Results

- [ ] All existing tests pass
- [ ] New tests added and passing
- [ ] Manual verification complete

### Edge Cases Tested

- [ ] `config_disabled` never triggers the CLI warning or MCP bug-report routing
- [ ] An unknown/future `disabled_reason` value (e.g. `load_failed`) is a
      silent no-op, not a crash and not a warning
- [ ] Lookup commands never emit the reranker warning even if the reranker
      happens to be uncached, verified across all four lookup commands by a
      named test, not just inspection — `_query_runtime` probes the
      reranker for lookup commands too
- [ ] Redaction still holds on all three index-unready messages after the
      appended hint line, including the previously-untested
      `IncompatibleIndexFormatError` path, and on the new reranker warning
      line
- [ ] The `not_cached` CLI warning names `refresh` before the bug-report URL
      (remediation-first, not escalation-first); the three index-unready
      hints follow the same ordering
- [ ] The `not_cached` warning fires for all four semantic commands, not
      just one representative command
- [ ] Neither the stdout JSON payload nor its parseability is affected by
      any new stderr text, on both the index-unready and reranker-warning
      paths
- [ ] A non-allowlisted `PIPECAT_HUB_RERANKER_MODEL` value falls back to the
      cached default and does not reach `not_cached` (documents the limit of
      the "routine swap" framing; not necessarily a new automated test if
      already covered by existing config tests)

## Acceptance Criteria

- `shared/support_links.py` exists with the URL constants pinned by a
  literal-value test (not just an interpolation test); `server/main.py`,
  `cli_query.py`, and `cli.py` all import them — enforced by an automated
  test walking `src/**/*.py` (Phase 1), not just a manual grep; the same
  grep pattern against `docs/` is expected to still find the one known,
  accepted `docs/README.md:444` copy.
- All three `_EXIT_INDEX_UNREADY` CLI stderr paths (including
  `IncompatibleIndexFormatError`) include a bug-report hint, each
  remediation-first (names the existing fix before the URL), and are
  individually tested with stdout-contract assertions alongside them.
- The four semantic CLI commands each emit a remediation-first stderr
  warning when, and only when, `reranker_status.disabled_reason ==
  "not_cached"` — verified individually, not via one representative command;
  all four lookup commands never emit it even when `not_cached`; an
  unknown/future reason value is a silent no-op; none of these co-fires with
  an `_EXIT_INDEX_UNREADY` exit — all cases have a named test.
- `cli.py`'s `serve`-startup `not_cached` log line carries the same
  `BUG_REPORT_ISSUE_URL` as the other two emitters.
- `uv run pytest tests/ -v`, `uv run ruff check src/ tests/`, and
  `uv run mypy src/ tests/` all pass — run as the exit gate for every phase
  that lands a commit, not only as a final Phase-3 check.
- AGENTS.md has smoke coverage for both the reranker warning and the
  index-unready bug-report hints, using the corrected `PIPECAT_HUB_RERANKER_MODEL`-based
  recipe (not `HF_HOME`); item #50 reflects that the CLI now has this
  guidance; CHANGELOG `[Unreleased]` has an entry. Phases 2 and 3 land
  together; Phase 4 lands as its own standalone commit, never between them.
- Phase 4's investigation is recorded in `## Findings` regardless of outcome.
  If it proposes a remediation addition to the MCP degraded-hub clause, the
  proposed command is `refresh` (not `--force --reset-index`). If
  `load_failed` proves unreachable, the same commit removes it from
  `_SERVER_INSTRUCTIONS` and the corresponding test assertion rather than
  leaving a dead-path promise; if reachable (expected), the phase lands a
  regression test pinning that reachability rather than resting on a
  one-time manual check.
- Code reviewed and approved.

<!-- reviewed: 2026-08-04 @ 3852bf4cbcecc2f8568fbf523f20010a768dc89e -->

<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

- [ ] Phase 1: Shared report-hint constants module
- [ ] Phase 2: CLI stderr report-hints
- [ ] Phase 3: Documentation and smoke coverage
- [ ] Phase 4: MCP-side symmetry audit (standalone)

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

- **2026-08-04 — `/review-plan` re-review, all findings incorporated.**
  Re-ran all five lenses fresh (previous marker was still hash-valid; this
  was a deliberate second pass, not drift recovery). 24 raw findings, 0
  Critical, 11 Important, 13 Minor (4 related cross-category pairs). The
  headline finding: the plan's Context/Architecture-Decision claim that
  `load_failed` is "observable solely by the long-lived `serve` process" was
  over-generalized — `probe_reranker()` itself never returns that string,
  but a runtime cross-encoder load failure inside a one-shot semantic
  command can still occur via `cross_encoder.py`'s swallowed exception path;
  narrowed the claim and named the residual gap as accepted-but-uncovered
  rather than silently absent. Also incorporated: `cli.py`'s `serve`-startup
  `not_cached` log line was a third, unaddressed emitter of this exact
  guidance (added to Phase 2); Phase 3 (MCP audit) was sequenced between the
  required-together Phase 2/4 pair (swapped Phase 3 and Phase 4 so the audit
  lands standalone, last); AGENTS.md item #50 and a test docstring assert
  the opposite of what Phase 2 ships (now corrected in Phase 3); the
  `load_failed`-unreachable negative branch had no plan (now spelled out in
  Phase 4); several testing gaps (stdout-contract assertion, 4-command
  parametrization instead of one representative command, redaction scope
  for the new warning line, co-fire negative test, automated
  single-source-of-truth test instead of a manual grep); the Phase-4 smoke
  recipe (`HF_HOME` at an empty cache) would have broken the embedding-model
  load rather than reproducing the target state; and the "routine model
  swap" premise was qualified to allowlisted model names only. Full
  findings: `.review-plan/latest-claude.json` (plan_hash
  `1b87186813f7e33f457dd6229b8b58f8ff9f2167`, the pre-fix version — the
  marker below hashes the post-fix content). All 24 findings were
  incorporated directly into the plan body above; none waived.

### Review Waivers

(none — every finding across both review passes was either addressed above
or determined to be a false positive, not a waived defect)

## Issues & Solutions

(none yet)

## Final Results

(fill in on completion)
