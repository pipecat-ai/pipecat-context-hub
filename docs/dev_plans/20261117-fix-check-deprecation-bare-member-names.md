# Task: `check_deprecation` gives no signal for bare member names

**Status**: Not Started
**Component**: check_deprecation / deprecation map
**Assigned to**: vr000m / Claude
**Priority**: Medium
**Branch**: `fix/check-deprecation-bare-member-names`
**Created**: 2026-11-17
**Review Gates**: full

Split out of #166 (closed); no issue filed yet. Design decisions resolved on 2026-11-17 (see Requirements); revised after a first `/skein:review-plan` run (5 lenses, 30 raw findings, all addressed here).

## Objective

`check_deprecation("trigger_user_turn_stopped")` returns `{"deprecated": false, "status": "current"}`, while `BaseUserTurnStopStrategy.trigger_user_turn_stopped` is deprecated since 1.15.0 (replacement `report_user_turn_stopped`). A bare member name should tell the caller that deprecated members of that name exist, without asserting something the registry cannot know.

## Context

Verified 2026-11-17 against the local clone of pipecat `v1.16.0`.

- The registry keys dotted subjects as `Class.member` (e.g. `BaseUserTurnStopStrategy.trigger_user_turn_stopped`) and adds a `<module>.<subject>` alias. `DeprecationMap.check` (`deprecation_map.py:81`) matches an exact key, or a query nested *under* a key (prefix). A bare `member` is neither, so it returns `None`, and the handler turns `None` into `current` (`check_deprecation.py:104`, `:110`). A lookup gap, not a data gap: the qualified form already works.
- `check` deliberately refuses to match *ancestors* ("reporting a current package/module as deprecated ... is the worst failure mode for this tool"). Bare-member lookup is the same hazard in the other direction, which is why the hint never asserts (Decision 1).
- **Counts, over `deprecations.json` only:** 549 records; 438 with a dotted, non-module subject (345 `parameter`, 44 `class`, 37 `method`, 5 `property`, 4 `function`, 3 `event`); 95 unique last segments. Shared names include `model` (58), `params` (55), `InputParams` (44), `voice_id` (34). **These are v1.16.0-only numbers; the design must not depend on them and tests compute counts from the fixture.** The built map also loads `removals.json` (9 records at v1.16.0, 7 dotted non-module, e.g. `LmntTTSService.model`), so candidates can be `removed`; counts are recomputed over the *built map* in Phase 1.
- **Not every dotted subject is `Class.member`.** 71 are `Class.method.param` (e.g. `BaseClassifier.ask.images`); 4 are module-qualified functions (e.g. `pipecat.audio.utils.alaw_to_pcm`); 75 have two or more dots. Wording and tests must not assume one owner segment.
- **The first-draft invariant was false.** `InputParams` is both an exact top-level key (class, `pipecat/services/google/gemini_live/llm.py`, replacement `GeminiLiveLLMService.Settings`) and the last segment of 44 nested records. `check-deprecation InputParams` returns `deprecated: true` today, so a caller asking about a different service's `InputParams` gets the wrong replacement. `InputParams.voice_id` also prefix-matches that key (existing prefix rule; unchanged here).
- **De-duplication by object identity does not survive serve.** `DeprecationMap.from_dict` builds a new `DeprecationEntry` per key and serve loads the map from disk (`server/main.py:343`); identity is shared only in the in-memory build. On the local map, `trigger_user_turn_stopped` has 2 keys for 1 subject and `model` has 118 keys for 59 subjects. `DeprecationEntry` is not frozen, so it is also unhashable.
- Registry subjects that collide (e.g. `FlowManager.task` as both a parameter and a property) are collapsed by the builder, last record wins, with a warning. The map cannot list both; candidates come from the map. Documented limit, pinned by a test.
- The handler serialises with `model_dump_json()` and no `exclude_none` (every path in `check_deprecation.py:102-146`), so every response already carries `null` keys, and the new fields appear as `null` in every response.
- `status` is already nullable (`CheckDeprecationOutput.status`, `shared/types.py:765`). `status: null` is the existing no-verdict shape for an unloadable map (`check_deprecation.py:109`, `_unloadable_map_note`, documented at `docs/index-contract.md:66-72`). The older `deprecation_map is None` path (`:96-102`) returns `status="current"` with a note.
- The audit extractor (`audit/extract.py:172`) detects a hub response by the `deprecated` key, which the hint keeps; existing callers (`scripts/smoke_check_deprecation.py`, `tests/integration/test_end_to_end.py`, `tests/integration/test_mcp_v2_compat.py`) assert only `deprecated`.

### Assumptions (named, with how each is verified)

- **A1. Agents act on the hint.** The response still says `deprecated: false`, so an agent reading only that field gets the same clean verdict as before. Not verified. Gate in Phase 4: run `check-deprecation trigger_user_turn_stopped` through a cold agent with no context and record whether it notices `candidates`/`note`; if not, rework the note and tool-description wording (the invariant does not change).
- **A2. The registry shape holds for other pipecat versions.** The registry is only guaranteed for the pinned version. Tests use the vendored v1.16.0 snapshot; older registries lack `kind` (see Architecture Decisions).

## Requirements

**Invariant.** `deprecated: true` is returned only for a symbol the registry lists (an exact key, or a symbol nested under one). A bare name that is **not** an exact key never produces `deprecated: true`, however many records match, and never produces a clean `current` verdict when deprecated members of that name exist. An exact-key hit keeps its verdict and additionally lists other same-named members (Decision 3).

**Decisions (grilled 2026-11-17; all accepted as recommended, none waived):**

1. **Decision (grilled): hint-only.** A bare member name never returns `deprecated: true` on its own; it returns the qualified candidates and tells the caller to query one of them. Rejected: assert `deprecated: true` for the 95 unique names (breaks the invariant for any class that reuses the name).
2. **Decision (grilled): `status: null` on a hint, with `candidates` present as the discriminator.** No new enum field. `status: null` + `candidates` = hint; `status: null` + `note` only = unloadable map. Document this in `CheckDeprecationOutput` and `docs/index-contract.md`. The older `deprecation_map is None` path returning `status="current"` is left as it is and recorded as out of scope (separate pre-existing inconsistency).
3. **Decision (grilled): an exact bare key that is also a member name keeps its verdict and attaches the other same-named members as `candidates`** (exact record excluded, cap 10). Applies to a *bare* exact key only (symbol equals a key with no dot), not to prefix matches. The verdict and its `status` are unchanged.
4. **Decision (grilled): a dotted query that matches nothing hints only when its last segment names exactly one deprecated member in the map.** `SomeSubclass.trigger_user_turn_stopped` hints at `BaseUserTurnStopStrategy.trigger_user_turn_stopped`; `OpenAILLMService.model` stays `current` (58 homes: too noisy for a legitimate qualified check). Names with several homes keep returning `current` for dotted queries; documented as a limit and pinned by a test.

## Review Focus

- The invariant: no path returns `deprecated: true` for a bare name that is not an exact key; no path returns a clean `current` for a bare name that has deprecated members.
- `members_named` correctness on a *loaded* map (save/load round trip), not only the in-memory build.
- Backward compatibility of the output model: two new `null` keys in every response; `status: null` now also means "hint".
- Candidate contract: per-candidate `status`, deterministic order, cap, `candidates_total`, removals included.
- Wording of every caller-facing description (one sentence, reused verbatim).

## Architecture Decisions

- **`DeprecationMap.members_named(name)`** (computed from `entries`; no change to the serialized map or its schema). Selects entries whose `old_path` has at least one dot and whose `kind != "module"` (kind `None`, from maps written by earlier releases, counts as a member), whose last segment equals `name`. Case-sensitive. De-duplicates on serialised identity, `(old_path, location)`, never on object identity or the dict key (the `<module>.` alias and the bare key are two keys for one subject). Module-qualified function subjects are members; their symbol is `old_path`. Returns candidates sorted by `(status == "current", symbol)`.
- **`DeprecationCandidate` model** (`shared/types.py`): `symbol` (the entry's `old_path`), `kind`, `status` (`status_for(entry, target_version)`: `deprecated`, `removed` or `current`), `deprecated_in`, `removed_in`, `replacement`. Removals are included.
- **`CheckDeprecationOutput`** gains `candidates: list[DeprecationCandidate] | None` and `candidates_total: int | None`, both `None` by default and serialised as `null` like every other optional field. Stated explicitly in the model docs.
- **Handler (`check_deprecation.py`), in this order:**
  1. Unloadable map: existing note wins (unchanged).
  2. `check()` hit: existing verdict path. If the symbol is a bare exact key (no dot) and `members_named(symbol)` is non-empty, attach `candidates` / `candidates_total` (Decision 3).
  3. `check()` miss, bare symbol (no dot): if `members_named` is non-empty, return `deprecated: false`, `status: null`, a note and the candidates (cap 10, `candidates_total` is the full count). Note: "`X` matched no symbol exactly. N symbols named `X` are in the deprecation registry (showing M); query one of them for a verdict." When every candidate is `current` at the target version, the note says none is deprecated at that version; the response is still a hint (`status: null`), because the registry lists only deprecations.
  4. `check()` miss, dotted symbol: take the last segment; if `members_named(last)` has exactly one candidate, return the hint with that candidate (Decision 4). Otherwise return `current`.
  5. Everything else: `current`, as today. Input edge cases (empty string, surrounding whitespace, a leading or trailing dot, `name()`, wrong case) match nothing and return `current` with no candidates; pinned by a parametrised test.
- **Wording:** the note and docs say "query one of the listed symbols", never "qualify with the class" (wrong for `Class.method.param` subjects and module-qualified functions).
- The CLI shares the handler, so `check-deprecation` and the MCP tool stay at parity (`cli_query.py:404-407`, `server/main.py:353`).
- **One sentence for the bare-name rule, reused verbatim wherever the tool is described:** "A name that matches no registry symbol exactly but names deprecated members returns no verdict and lists them; query one of the listed symbols." Used in the tool description (`dispatch.py`), input description (`types.py`), server instructions (`main.py`), CLI help and docs.

## Implementation Checklist

Each phase is one commit; every commit keeps `just check` and the tests green. Types, handler and caller-facing descriptions land together (Phase 3) so no commit declares a field it never sets or changes behaviour with stale text.

### Phase 1: Fixture

**Impl files:** `tests/fixtures/deprecations/v1.16.0/deprecations.json, tests/fixtures/deprecations/v1.16.0/removals.json, tests/fixtures/deprecations/v1.16.0/SOURCE.md`
**Test files:** `tests/unit/test_deprecation_fixture.py`
**Test command:** `uv run pytest tests/unit/test_deprecation_fixture.py -q`
**Goal:** a vendored, pinned registry snapshot so the whole-registry tests run in CI; no behaviour change.

- Vendor `deprecations.json` and `removals.json` from pipecat tag `v1.16.0` (fetch with `gh api` at that ref), with `SOURCE.md` recording the tag, the fetch command and the refresh rule.
- Build the map from the fixture with the existing builders and record the real counts (records, dotted non-module subjects, unique last segments, removals) in `SOURCE.md`.
- Test: the fixture loads, `SOURCE.md` names the tag, and the counts quoted in this plan match what the builders produce.

### Phase 2: `members_named`

**Impl files:** `src/pipecat_context_hub/services/ingest/deprecation_map.py`
**Test files:** `tests/unit/test_deprecation_map.py`
**Test command:** `uv run pytest tests/unit/test_deprecation_map.py -q`
**Goal:** member lookup that is correct on a loaded map; no caller changes yet.

- Implement `members_named` per Architecture Decisions.
- Tests: built, saved and loaded map gives one candidate for `trigger_user_turn_stopped`; `members_named("llm")` excludes the module key `pipecat.services.grok.llm`; `task` returns the one record the map holds; module-qualified function subjects appear with `old_path` as the symbol; a map written without `kind` still selects members; removals appear; order is `(status == "current", symbol)`; `check(candidate.symbol)` returns that candidate's entry (property over all candidates in the fixture).

### Phase 3: Types, handler, descriptions

**Impl files:** `src/pipecat_context_hub/server/tools/check_deprecation.py, src/pipecat_context_hub/shared/types.py, src/pipecat_context_hub/server/dispatch.py, src/pipecat_context_hub/server/main.py, src/pipecat_context_hub/cli_query.py`
**Test files:** `tests/unit/test_check_deprecation_tool.py, tests/unit/test_cli_query.py`
**Test command:** `uv run pytest tests/unit/test_check_deprecation_tool.py tests/unit/test_cli_query.py tests/unit/test_deprecation_map.py -q`
**Validation cmd:** `uv run pipecat-context-hub check-deprecation trigger_user_turn_stopped`
**Goal:** the invariant holds on every handler path; the new fields and the one-sentence rule ship together.

- Add `DeprecationCandidate`, `candidates` and `candidates_total`; update the input description and the output model docs (the `status: null` + `candidates` discriminator).
- Implement the handler branches in the order given in Architecture Decisions; update `dispatch.py`, `main.py` server instructions and the CLI help with the one-sentence rule.
- Tests (see Tests section): frozen real record; invariant over the whole fixture; `InputParams`; `model` cap and order; dotted queries (Decision 4); output key sets; unloadable map precedence; version-aware candidate status; input edge cases; CLI exit code and JSON; parity test unchanged.

### Phase 4: Docs, smoke script, changelog, cold-agent gate

**Impl files:** `docs/tools.md, docs/cli.md, docs/index-contract.md, docs/smoke-test.md, scripts/smoke_check_deprecation.py, CHANGELOG.md, plugins/pipecat-context-hub/skills/explore/SKILL.md`
**Test files:** `tests/smoke/`
**Test command:** `uv run pytest tests/smoke/ tests/unit/test_cli_query.py -q`
**Validation cmd:** `uv run python scripts/smoke_check_deprecation.py`
**Goal:** the behaviour is documented where callers read it and checked by the live smoke; assumption A1 is verified before the PR.

- Document the bare-name rule and the `status: null` discriminator in `docs/tools.md`, `docs/cli.md` and `docs/index-contract.md`; update the plugin explore skill text (its own version bump and release stay with the plugin release process; say so in the changelog).
- Add the bare-member query and `InputParams` to `docs/smoke-test.md` as numbered items (field-found failure rule in `AGENTS.md`); add a HINT group to `scripts/smoke_check_deprecation.py` (today it asserts only `deprecated`, so a hint reads as a clean pass).
- `CHANGELOG.md` `[Unreleased]`: the hint, bare `InputParams` gaining candidates, the two new `null` keys, `status: null` now also meaning "hint".
- Gate A1: run `check-deprecation trigger_user_turn_stopped` through a cold agent with no context; record in the PR description whether it noticed the hint; if not, rework the note and description wording.

## Files

- `src/pipecat_context_hub/services/ingest/deprecation_map.py` (`members_named`)
- `src/pipecat_context_hub/server/tools/check_deprecation.py` (handler)
- `src/pipecat_context_hub/shared/types.py` (`DeprecationCandidate`, output fields, input description at `:743-749`)
- `src/pipecat_context_hub/server/dispatch.py` (tool description, `:154-160`; **not** `server/main.py`)
- `src/pipecat_context_hub/server/main.py` (server instructions, `:100-104`)
- `src/pipecat_context_hub/cli_query.py` (`check-deprecation` help, `:744-749`)
- `tests/unit/test_deprecation_map.py`, `tests/unit/test_check_deprecation_tool.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_deprecation_fixture.py` (new), `tests/fixtures/deprecations/v1.16.0/` (new)
- `scripts/smoke_check_deprecation.py`
- `docs/tools.md`, `docs/cli.md`, `docs/index-contract.md`, `docs/smoke-test.md`, `CHANGELOG.md`
- `plugins/pipecat-context-hub/skills/explore/SKILL.md` (text only)

## Tests

- **Frozen real record:** `trigger_user_turn_stopped` returns `deprecated: false`, `status: null`, exactly one candidate `BaseUserTurnStopStrategy.trigger_user_turn_stopped` with replacement `report_user_turn_stopped`, asserted against a map that was built, saved and loaded (one candidate, `candidates_total == 1`, symbol is the subject).
- **Invariant, over the whole fixture:** for every bare name that is **not** an exact key, `deprecated` is `false`; exact keys are excluded from the property and tested explicitly.
- **`InputParams`:** exact verdict unchanged (`deprecated: true`, Gemini Live replacement) plus candidates for the other 44, exact record excluded, capped at 10, `candidates_total` equals the computed count.
- **Cap and order:** `model`: exactly 10 returned in the documented order, `candidates_total` equals the computed count; boundaries at 10 and 11 candidates, and `candidates_total` when under the cap.
- **Dotted queries (Decision 4):** `SomeSubclass.trigger_user_turn_stopped` hints at the single candidate; `OpenAILLMService.model` returns `current`, no candidates; a dotted name with several homes stays `current`.
- **Selection edge cases:** see Phase 2.
- **Removals:** a removed member queried by bare name appears as a `removed` candidate with `removed_in`.
- **Version-aware:** candidate `status` with no version, with the indexed-version default, before `deprecated_in`, after it, and at `--at-version 2.0.0` (`removed`); when every candidate is `current`, the note says none is deprecated at that version and the response is still a hint.
- **Output shape:** the exact key set of an exact-match response, a `current` response and a hint response (the two new keys are present as `null` in non-hint responses); a `CheckDeprecationOutput` round trip with candidates.
- **Unloadable map:** the existing note wins over a hint; `status: null` with candidates absent distinguishes it from a hint.
- **Input edge cases:** parametrised: empty string, whitespace, leading and trailing dot, `name()`, `Model` (wrong case) all return `current`, no candidates.
- **CLI:** `check-deprecation trigger_user_turn_stopped` exits 0 and prints JSON with `candidates`; the input description mentions bare names; CLI/MCP parity test unchanged.

## Verification

- `uv run ruff check src/ tests/`, `uv run mypy src/ tests/`, `uv run pytest tests/ -q` (backgrounded).
- Offline smoke when the PR opens (types change): `uv run pytest tests/smoke/ -v`.
- Live smoke: `check-deprecation trigger_user_turn_stopped`, `... model`, `... InputParams`, `... SomeSubclass.trigger_user_turn_stopped`, `... BaseUserTurnStopStrategy.trigger_user_turn_stopped`.
- Assumption A1 gate (Phase 4): cold-agent run recorded in the PR description.

## Risk

- A response that used to say `current` now says `status: null` with candidates for bare names that match deprecated members, and bare `InputParams` gains candidates. A consumer that keys on `status == "current"` for a bare name changes behaviour; one that checks `status != "current"` sees the hint, which is the intent. Both are in the changelog.
- Every response gains two `null` keys (`candidates`, `candidates_total`). Additive; noted in the changelog.
- Response size for common names (`model`, `InputParams`): bounded by the cap of 10.
- A dotted query on a name with several deprecated homes still returns `current`. A documented limit (Decision 4), not an oversight.
- A1: agents may still read only `deprecated: false`. Verified before the PR; mitigation is wording, not a change to the invariant.

## Acceptance Criteria

- `check-deprecation trigger_user_turn_stopped` returns `deprecated: false`, `status: null` and one candidate with replacement `report_user_turn_stopped`.
- `check-deprecation InputParams` keeps `deprecated: true` and lists other same-named members as candidates.
- No bare name that is not an exact key returns `deprecated: true` (property test over the v1.16.0 fixture).
- `OpenAILLMService.model` still returns `current`.
- The bare-name rule appears verbatim in the tool description, input description, server instructions, CLI help and docs.
- The cold-agent check (A1) is recorded in the PR description.
- Code reviewed and approved; tests, `ruff` and `mypy` passing; documentation and changelog updated.

<!-- reviewed: YYYY-MM-DD @ <hash> -->
<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

Per-phase completion tracked here so ticking a box during a run does not bust the review marker. `/conduct` reads this section to skip already-done phases. Format: `- [ ] Phase <label>: <title>`.

- [ ] Phase 1: Fixture
- [ ] Phase 2: `members_named`
- [ ] Phase 3: Types, handler, descriptions
- [ ] Phase 4: Docs, smoke script, changelog, cold-agent gate

## Findings

Durable notes that survive after the run: diagnostic query results, decision rationale, "checked / accepted behaviour" outcomes.

- First `/review-plan` run (2026-11-17): 30 raw findings across architecture, sequencing, spec-and-testing, assumptions and codebase-claims; all addressed above. One spec-and-testing finding (MagicMock maps) was withdrawn by its lens. The sequential Contradiction Pass was not run on that pass.
