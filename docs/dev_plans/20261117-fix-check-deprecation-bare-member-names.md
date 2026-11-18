# Task: `check_deprecation` gives no signal for bare member names

**Status**: Not Started
**Component**: check_deprecation / deprecation map
**Assigned to**: vr000m / Claude
**Priority**: Medium
**Branch**: `fix/check-deprecation-bare-member-names`
**Created**: 2026-11-17
**Review Gates**: full

Split out of #166 (closed); no issue filed yet. Design decisions resolved on 2026-11-17 (see Requirements); revised after two `/skein:review-plan` runs (run 1: 30 raw findings; run 2: 21 raw findings, including one Critical that reopened Decision 4). All findings are addressed here.

## Objective

`check_deprecation("trigger_user_turn_stopped")` returns `{"deprecated": false, "status": "current"}`, while `BaseUserTurnStopStrategy.trigger_user_turn_stopped` is deprecated since 1.15.0 (replacement `report_user_turn_stopped`). A bare member name should tell the caller that deprecated members of that name exist, without asserting something the registry cannot know, and without disturbing the tool's primary use: a current module import path must keep returning `current`.

## Context

Verified 2026-11-17 against the local clone of pipecat `v1.16.0`.

- The registry keys dotted subjects as `Class.member` (e.g. `BaseUserTurnStopStrategy.trigger_user_turn_stopped`) and adds a `<module>.<subject>` alias. `DeprecationMap.check` (`deprecation_map.py:81`) matches an exact key, or a query nested *under* a key (prefix). A bare `member` is neither, so it returns `None`, and the handler turns `None` into `current` (`check_deprecation.py:104`, `:110`). A lookup gap, not a data gap: the qualified form already works.
- `check` deliberately refuses to match *ancestors*, and names `pipecat.services.openai.llm` as a path that must report not-deprecated (`deprecation_map.py:94-98`). Module import paths are the tool's primary use (`dispatch.py:156-159`, `server/main.py:100-104`; the NOT-DEPRECATED cases at `scripts/smoke_check_deprecation.py:49` and `docs/smoke-test.md:308`).
- **Counts (recomputed in Phase 1; sources stated):**
  - Over `deprecations.json` only: 549 records; 438 with a dotted, non-module subject (345 `parameter`, 44 `class`, 37 `method`, 5 `property`, 4 `function`, 3 `event`); 146 distinct last segments. Shared names: `model` (58 records), `params` (55), `InputParams` (44), `voice_id` (34).
  - Over the *built map* (deprecations + removals; this is what the handler sees): 444 member subjects; 150 distinct last segments (100 with one home, 50 with several); `model` has 59 homes. The built map also loads `removals.json` (9 records at v1.16.0, 7 dotted non-module, e.g. `LmntTTSService.model`), so candidates can be `removed`.
  - **These are v1.16.0-only numbers; the design must not depend on them.** Tests compare builder output with `SOURCE.md` (Phase 1), never with this prose.
- **Not every dotted subject is `Class.member`.** 71 are `Class.method.param` (e.g. `BaseClassifier.ask.images`); 4 are module-qualified functions (e.g. `pipecat.audio.utils.alaw_to_pcm`); 75 have two or more dots.
- **The first-draft invariant was false.** `InputParams` is both an exact top-level key (class, `pipecat/services/google/gemini_live/llm.py`, replacement `GeminiLiveLLMService.Settings`) and the last segment of 44 nested records. `check-deprecation InputParams` returns `deprecated: true` today, so a caller asking about a different service's `InputParams` gets the wrong replacement. `InputParams.voice_id` also prefix-matches that key (existing prefix rule; unchanged here).
- **Module import paths collide with member names.** `pipecat.services.openai.llm` is a miss (`check` returns `None`, so today `current`), and `llm` has exactly one member home, `VoicemailDetector.llm`; likewise `pipecat.transports.daily.transport` and `RTVIProcessor.transport`. Other single-home names that are also module or package names: `config`, `instructions`, `settings`, `setup`, `task`, `tracing_context`, `worker`. A hint rule keyed on the last segment alone would turn these module paths into hints (this is why Decision 4 is scoped by owner shape).
- **De-duplication by object identity does not survive serve.** `DeprecationMap.from_dict` builds a new `DeprecationEntry` per key and serve loads the map from disk (`server/main.py:343`); identity is shared only in the in-memory build. On the local map, `trigger_user_turn_stopped` has 2 keys for 1 subject and `model` has 118 keys for 59 subjects. `DeprecationEntry` is not frozen, so it is also unhashable.
- **`old_path` is not always the key that resolves to an entry.** The builder lets a same-subject record from another module shadow the bare key (`deprecation_map.py:349-362`); `add_removals_from_registry` keeps an active entry on the bare key and leaves the removal reachable by alias only (`:447-463`) and never sets `location`. Registry subjects that collide (e.g. `FlowManager.task` as both a parameter and a property) are collapsed by the builder, last record wins. The map cannot list what it dropped.
- **Kind-less maps.** `from_dict` yields `kind=None` for every entry of a map written before the registry ingestion (module records included). Earlier maps were built from source-parsed `DeprecatedModuleProxy` module-to-module mappings (`AGENTS.md` review checklist), so a kind-less dotted entry is plausibly a module path, not a member.
- The handler serialises with `model_dump_json()` and no `exclude_none` (every path in `check_deprecation.py:102-146`), so the new fields appear as `null` in every response.
- `status` is already nullable (`CheckDeprecationOutput.status`, `shared/types.py:765`). `status: null` is the existing no-verdict shape for an unloadable map (`check_deprecation.py:109`, `_unloadable_map_note`, documented at `docs/index-contract.md:66-72`). The older `deprecation_map is None` path (`:96-102`) returns `status="current"` with a note. `DeprecationMap.load` returns an empty map with `load_error` set on failure (`deprecation_map.py:195-198`).
- The audit extractor (`audit/extract.py:172`) detects a hub response by the `deprecated` key, which the hint keeps; existing callers (`scripts/smoke_check_deprecation.py`, `tests/integration/test_end_to_end.py`, `tests/integration/test_mcp_v2_compat.py`) assert only `deprecated`.

### Assumptions (named, with how each is verified)

- **A1. Agents act on the hint.** The response still says `deprecated: false`, so an agent reading only that field gets the same clean verdict as before. Not verified. Gate in Phase 4: run a cold agent (no context) on `trigger_user_turn_stopped`, `InputParams` and `model`, through both the MCP tool and the CLI. Pass rule: the agent re-queries a listed symbol, or states that the hub gave no verdict. Record the result in the PR description; if the agent treats `deprecated: false` as clean, rework the note and description wording (the invariant does not change).
- **A2. The registry shape holds for other pipecat versions.** The registry is only guaranteed for the pinned version. Tests use the vendored v1.16.0 snapshot; kind-less maps return no candidates (Architecture Decisions).

## Requirements

**Invariant.** `deprecated: true` is returned only for a symbol the registry lists (an exact key, or a symbol nested under one). A bare name that is **not** an exact key never produces `deprecated: true`, however many records match, and never produces a clean `current` verdict when deprecated members of that name exist. An exact-key hit keeps its verdict and additionally lists other same-named members (Decision 3). A dotted query that is not an exact key or a descendant of one hints only under Decision 4; a current module import path never hints.

**Decisions (grilled 2026-11-17; all accepted as recommended, none waived):**

1. **Decision (grilled): hint-only.** A bare member name never returns `deprecated: true` on its own; it returns the qualified candidates and tells the caller to query one of them. Rejected: assert `deprecated: true` for names with a single home (breaks the invariant for any class that reuses the name).
2. **Decision (grilled): `status: null` on a hint, with `candidates` present as the discriminator.** No new enum field. `status: null` + `candidates` = hint; `status: null` + `note` only = unloadable map. Document this in `CheckDeprecationOutput` and `docs/index-contract.md`. The older `deprecation_map is None` path returning `status="current"` is left as it is and recorded as out of scope (separate pre-existing inconsistency).
3. **Decision (grilled): an exact bare key that is also a member name keeps its verdict and attaches the other same-named members as `candidates`** (exact record excluded, cap 10). Applies to a *bare* exact key only (symbol equals a key with no dot), on every exact-hit branch (`deprecated`, `removed`, and `current`-at-the-evaluated-version). Prefix matches (`InputParams.voice_id`) and dotted exact keys get `candidates: null`. A bare exact key with no same-named members gets `null`, not `[]` and `0`. The verdict and its `status` are unchanged.
4. **Decision (grilled, revised after review run 2): a dotted query that matches nothing hints only when (a) the segment before the last starts with an uppercase letter (class-like owner) and (b) `members_named(last)` has exactly one entry, whatever its status at the evaluated version.** `SomeSubclass.trigger_user_turn_stopped` and `pipecat.x.y.Sub.trigger_user_turn_stopped` hint at `BaseUserTurnStopStrategy.trigger_user_turn_stopped`. `pipecat.services.openai.llm` (owner `openai`), `pipecat.transports.daily.transport` (owner `daily`) and `OpenAILLMService.model` (59 homes) stay `current`. Cost: a lower-case class name never hints; pipecat has none today. Documented as a limit and pinned by tests.

## Review Focus

- The invariant: no path returns `deprecated: true` for a bare name that is not an exact key; no path returns a clean `current` for a bare name that has deprecated members; no current module import path returns a hint.
- `members_named` correctness on a *loaded* map (save/load round trip), not only the in-memory build; shadowed subjects and removal aliases.
- Backward compatibility of the output model: two new `null` keys in every response; `status: null` now also means "hint".
- Candidate contract: per-candidate `status`, deterministic order, cap, `candidates_total`, removals included; where the version enters (the handler).
- Wording of every caller-facing description (one sentence, defined once, asserted verbatim).

## Architecture Decisions

- **`DeprecationMap.members_named(name)`**: version-free, computed from `entries` (no change to the serialized map or its schema). Returns a list of `(symbol, entry)` pairs, de-duplicated and sorted by `symbol`. Selection: entries whose `old_path` has at least one dot, whose `kind` is neither `"module"` nor `None` (a kind-less entry is excluded: it may be a module path from an older map; maps with no `kind` values therefore return no candidates), and whose last `old_path` segment equals `name`. Case-sensitive. **De-duplication** compares the serialised entry (`_entry_to_dict` equality), never object identity or the dict key (the `<module>.` alias and the bare key are two keys for one subject); an active entry and a removal that share an `old_path` are different entries and both stay. **`symbol`** is a key under which `check(symbol)` returns that entry: `old_path` when `entries[old_path]` is that entry, otherwise the `<module>.` alias key that points at it. Module-qualified function subjects are members; their symbol is `old_path`.
- **`DeprecationCandidate` model** (`shared/types.py`): `symbol`, `kind`, `status` (`status_for(entry, target_version)`: `deprecated`, `removed` or `current`), `deprecated_in`, `removed_in`, `replacement`. Removals are included.
- **The version enters in the handler**, as for the exact path (`check_deprecation.py:112`): the handler turns `members_named` pairs into `DeprecationCandidate`s with `status_for`, sorts by `(status == "current", symbol)` (deprecated and removed first), and only then caps at 10. `candidates_total` is the count before the cap.
- **`CheckDeprecationOutput`** gains `candidates: list[DeprecationCandidate] | None` and `candidates_total: int | None`, both `None` by default and serialised as `null` like every other optional field. Stated in the model docs.
- **Handler (`check_deprecation.py`), in this order:**
  1. Unloadable map: existing note wins (unchanged).
  2. `check()` hit: existing verdict path on all three branches. If the symbol is a bare exact key (no dot) and `members_named(symbol)` minus the exact record is non-empty, attach `candidates` / `candidates_total` (Decision 3).
  3. `check()` miss, bare symbol (no dot): if `members_named` is non-empty, return `deprecated: false`, `status: null`, a note and the candidates. Note: "`X` matched no symbol exactly. N symbols named `X` are in the deprecation registry (showing M); query one of the listed symbols for a verdict." When every candidate is `current` at the target version, the note says none is deprecated at that version; the response is still a hint (`status: null`), because the registry lists only deprecations.
  4. `check()` miss, dotted symbol: apply Decision 4 (class-like owner and exactly one entry). Otherwise return `current`.
  5. Everything else: `current`, as today. Input edge cases (empty string, surrounding whitespace, a leading or trailing dot, `name()`, wrong case) match nothing and return `current` with no candidates.
- **The bare-name sentence is defined once** as a constant `BARE_NAME_RULE` in `shared/types.py` (the leaf the output model already lives in; `dispatch.py`, `main.py` and `cli_query.py` import it; a `shared/` module cannot import from `server/`): "A name that matches no registry symbol exactly but names deprecated members returns no verdict and lists them; query one of the listed symbols." It appears verbatim in the tool description (`dispatch.py`), the input description (`types.py`), the server instructions (`main.py`), the CLI help (`cli_query.py`) and the docs.
- **Wording:** the note and docs say "query one of the listed symbols", never "qualify with the class" (wrong for `Class.method.param` subjects and module-qualified functions).
- The CLI shares the handler, so `check-deprecation` and the MCP tool stay at parity (`cli_query.py:404-407`, `server/main.py:353`).

## Implementation Checklist

Each phase is one commit; every commit keeps `just check` and the tests green. Types, handler and caller-facing descriptions land together (Phase 3) so no commit declares a field it never sets or changes behaviour with stale text.

### Phase 1: Fixture

**Impl files:** `tests/fixtures/deprecations/v1.16.0/deprecations.json, tests/fixtures/deprecations/v1.16.0/removals.json, tests/fixtures/deprecations/v1.16.0/SOURCE.md`
**Test files:** `tests/unit/test_deprecation_fixture.py`
**Test command:** `uv run pytest tests/unit/test_deprecation_fixture.py -q`
**Goal:** a vendored, pinned registry snapshot so the whole-registry tests run in CI; no behaviour change.

- Vendor `deprecations.json` and `removals.json` from pipecat tag `v1.16.0` (fetch with `gh api` at that ref), with `SOURCE.md` recording the tag, the fetch command and the refresh rule.
- Build the map from the fixture with the existing builders. Record the recomputed counts in `SOURCE.md`, naming the measure for each: raw registry records (records, dotted non-module subjects, distinct last segments) and built-map subjects (member subjects, distinct last segments, one-home and multi-home names, `model` homes). Write measurements to `SOURCE.md` and the `## Findings` workspace only, never into this plan above the marker.
- Test: the fixture loads and `SOURCE.md` names the tag; the counts in `SOURCE.md` equal what the builders produce (compare against `SOURCE.md`, never plan prose).

### Phase 2: `members_named`

**Impl files:** `src/pipecat_context_hub/services/ingest/deprecation_map.py`
**Test files:** `tests/unit/test_deprecation_map.py`
**Test command:** `uv run pytest tests/unit/test_deprecation_map.py -q`
**Goal:** version-free member lookup that is correct on a loaded map; no caller changes yet.

- Implement `members_named` per Architecture Decisions.
- Tests: built, saved and loaded map gives one entry for `trigger_user_turn_stopped` (two keys, one entry, symbol is the subject); `members_named("llm")` on a kind-carrying map excludes the module key `pipecat.services.grok.llm`; `task` returns the one record the map holds; module-qualified function subjects appear with `old_path` as the symbol; a map written without `kind` returns no candidates; a synthetic map with an active deprecation and a removal sharing an `old_path` returns two entries with distinct symbols (`old_path` and the alias), and `check(symbol)` returns each entry (property over all pairs in the fixture and the synthetic map); a synthetic map where one subject shadows another from a different module keeps both; result order is by `symbol`.

### Phase 3: Types, handler, descriptions

**Impl files:** `src/pipecat_context_hub/server/tools/check_deprecation.py, src/pipecat_context_hub/shared/types.py, src/pipecat_context_hub/server/dispatch.py, src/pipecat_context_hub/server/main.py, src/pipecat_context_hub/cli_query.py`
**Test files:** `tests/unit/test_check_deprecation_tool.py, tests/unit/test_cli_query.py`
**Test command:** `uv run pytest tests/unit/test_check_deprecation_tool.py tests/unit/test_cli_query.py tests/unit/test_deprecation_map.py -q`
**Validation cmd:** `uv run pipecat-context-hub check-deprecation trigger_user_turn_stopped | jq -e '.candidates | length == 1'`
**Goal:** the invariant holds on every handler path; the new fields and the one-sentence rule ship together.

- **Before starting:** `uv run pipecat-context-hub status` must report `indexed_framework_version` 1.15.0 or later (otherwise `uv run pipecat-context-hub refresh`); the Validation cmd and the live smoke read the persisted `deprecation_map.json`, and the command exits 2 without an index. The Validation cmd fails before this phase (no `candidates`) and passes after.
- Add `DeprecationCandidate`, `candidates`, `candidates_total` and `BARE_NAME_RULE`; update the input description and the output model docs (the `status: null` + `candidates` discriminator).
- Implement the handler branches in the order given in Architecture Decisions; update `dispatch.py`, `main.py` server instructions and the CLI help from `BARE_NAME_RULE`.
- Tests: see the Tests section.

### Phase 4: Docs, smoke script, changelog, cold-agent gate

**Impl files:** `docs/tools.md, docs/cli.md, docs/index-contract.md, docs/smoke-test.md, scripts/smoke_check_deprecation.py, CHANGELOG.md, plugins/pipecat-context-hub/skills/explore/SKILL.md`
**Test files:** `tests/unit/test_check_deprecation_docs.py`
**Test command:** `uv run pytest tests/unit/test_check_deprecation_docs.py tests/unit/test_cli_query.py tests/unit/test_chatgpt_plugin_setup.py -q && uv run ruff check scripts/smoke_check_deprecation.py`
**Validation cmd:** `uv run python scripts/smoke_check_deprecation.py`
**Goal:** the behaviour is documented where callers read it and checked by the live smoke; assumption A1 is verified before the PR.

- Document the bare-name rule and the `status: null` discriminator in `docs/tools.md`, `docs/cli.md` and `docs/index-contract.md`; update the plugin explore skill text, keeping the phrases `tests/unit/test_chatgpt_plugin_setup.py` pins and leaving the plugin version alone (its release stays with the plugin release process; say so in the changelog).
- Add the bare-member query and `InputParams` to `docs/smoke-test.md` as numbered items (field-found failure rule in `AGENTS.md`); add a HINT group to `scripts/smoke_check_deprecation.py` (today it asserts only `deprecated`, so a hint reads as a clean pass), and make its NOT-DEPRECATED group also assert `status == "current"` and no `candidates`.
- New `tests/unit/test_check_deprecation_docs.py`: the docs contain `BARE_NAME_RULE`; the two new numbered items exist in `docs/smoke-test.md`; the smoke script's HINT classifier, with a stubbed checker, separates hint from clean pass.
- `CHANGELOG.md` `[Unreleased]`: the hint, bare `InputParams` gaining candidates, the two new `null` keys, `status: null` now also meaning "hint".
- Gate A1: run the cold-agent check on `trigger_user_turn_stopped`, `InputParams` and `model` through MCP and CLI; record the pass rule outcome in the PR description.

## Files

- `src/pipecat_context_hub/services/ingest/deprecation_map.py` (`members_named`)
- `src/pipecat_context_hub/server/tools/check_deprecation.py` (handler)
- `src/pipecat_context_hub/shared/types.py` (`DeprecationCandidate`, output fields, `BARE_NAME_RULE`, input description at `:743-749`)
- `src/pipecat_context_hub/server/dispatch.py` (tool description, `:154-160`; **not** `server/main.py`)
- `src/pipecat_context_hub/server/main.py` (server instructions, `:100-104`)
- `src/pipecat_context_hub/cli_query.py` (`check-deprecation` help, `:744-749`)
- `tests/unit/test_deprecation_map.py`, `tests/unit/test_check_deprecation_tool.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_deprecation_fixture.py` (new), `tests/unit/test_check_deprecation_docs.py` (new), `tests/fixtures/deprecations/v1.16.0/` (new)
- `scripts/smoke_check_deprecation.py`
- `docs/tools.md`, `docs/cli.md`, `docs/index-contract.md`, `docs/smoke-test.md`, `CHANGELOG.md`
- `plugins/pipecat-context-hub/skills/explore/SKILL.md` (text only)

## Tests

- **Frozen real record:** `trigger_user_turn_stopped` returns `deprecated: false`, `status: null`, exactly one candidate `BaseUserTurnStopStrategy.trigger_user_turn_stopped` with replacement `report_user_turn_stopped`, asserted against a map that was built, saved and loaded (`candidates_total == 1`, symbol is the subject).
- **Invariant, both clauses, over the whole fixture:** for every bare name that is **not** an exact key: if it is a member last segment, `deprecated` is `false`, `status` is `None`, `candidates` is non-empty and `candidates_total == len(members_named(name))`; if it is neither a key nor a member segment, the response is `current` with `candidates: null`. Exact keys are excluded from the property and tested explicitly.
- **Module paths never hint:** frozen cases `pipecat.services.openai.llm`, `pipecat.services.anthropic.llm` and `pipecat.transports.daily.transport` return `current` with `candidates: null`. Fixture-wide property: no `pipecat.`-prefixed query that is not an exact key or a descendant of one returns a hint. The smoke NOT-DEPRECATED set stays `current`, no candidates.
- **`InputParams` (Decision 3):** exact verdict unchanged (`deprecated: true`, Gemini Live replacement) plus the other same-named members as candidates, exact record excluded, capped at 10, `candidates_total` equals the computed count; `InputParams` before its `deprecated_in` (the `current`-at-version branch) also attaches candidates; `InputParams.voice_id` (prefix match) and a dotted exact key return `candidates: null`; a bare exact key with no same-named members returns `null`, not `[]` and `0`.
- **Cap and order (handler):** `model`: exactly 10 returned, deprecated and removed before current, then by symbol; `candidates_total` equals the computed count; boundaries at 10 and 11 candidates and under the cap; the same at a version where some `model` candidates are `current`.
- **Dotted queries (Decision 4):** `SomeSubclass.trigger_user_turn_stopped` and `pipecat.x.y.Sub.trigger_user_turn_stopped` hint at the single candidate; `OpenAILLMService.model` (many homes) returns `current`; boundary cases for the count basis: a sole candidate that is `current` at the target version, a sole `removed` candidate, two homes where one is `current`, a dotted miss ending in `InputParams`, a module-qualified miss, and a lower-case owner.
- **Removals:** a removal with a unique last segment (e.g. `SarvamSTTService.set_prompt`; confirm in Phase 1) queried by bare name appears as a `removed` candidate with `removed_in`.
- **Version-aware:** candidate `status` with no version, with the indexed-version default, before `deprecated_in`, after it, and at `--at-version 2.0.0` (`removed`); when every candidate is `current`, the note says none is deprecated at that version and the response is still a hint.
- **Output shape:** the exact key set of an exact-match response, a `current` response and a hint response (the two new keys are `null` in non-hint responses); a `CheckDeprecationOutput` round trip with candidates.
- **Unloadable map:** build `DeprecationMap(entries={...}, load_error="unreadable")` directly with members present (a loaded failed map is empty, so it cannot test precedence); the existing note wins and `candidates` is absent, which distinguishes it from a hint.
- **Input edge cases:** parametrised: empty string, whitespace, leading and trailing dot, `name()`, `Model` (wrong case) all return `current`, no candidates.
- **`BARE_NAME_RULE` verbatim** on all five surfaces: the dispatch description, the `symbol` schema description, the server instructions, `check-deprecation --help` (whitespace-normalised) and the three docs.
- **CLI:** `check-deprecation trigger_user_turn_stopped` exits 0 and prints JSON with `candidates`; CLI/MCP parity test unchanged.

## Verification

- `uv run ruff check src/ tests/`, `uv run mypy src/ tests/`, `uv run pytest tests/ -q` (backgrounded).
- Offline smoke when the PR opens (types change): `uv run pytest tests/smoke/ -v`.
- Live smoke (index at framework 1.15.0 or later): `check-deprecation trigger_user_turn_stopped`, `... model`, `... InputParams`, `... SomeSubclass.trigger_user_turn_stopped`, `... pipecat.services.openai.llm` (must stay `current`), `... BaseUserTurnStopStrategy.trigger_user_turn_stopped`.
- Assumption A1 gate (Phase 4): cold-agent run recorded in the PR description.

## Risk

- A response that used to say `current` now says `status: null` with candidates for bare names that match deprecated members, and bare `InputParams` gains candidates. A consumer that keys on `status == "current"` for a bare name changes behaviour; one that checks `status != "current"` sees the hint, which is the intent. Both are in the changelog.
- Every response gains two `null` keys (`candidates`, `candidates_total`). Additive; noted in the changelog.
- Response size for common names (`model`, `InputParams`): bounded by the cap of 10.
- A dotted query on a name with several deprecated homes, or a lower-case owner, still returns `current`. Documented limits (Decision 4), not oversights.
- A module import path ending in a single-home member name is the regression this plan most needs to avoid; it is pinned by frozen tests, a fixture-wide property and the smoke NOT-DEPRECATED set.
- A1: agents may still read only `deprecated: false`. Verified before the PR; mitigation is wording, not a change to the invariant.

## Acceptance Criteria

- `check-deprecation trigger_user_turn_stopped` returns `deprecated: false`, `status: null` and one candidate with replacement `report_user_turn_stopped`.
- `check-deprecation InputParams` keeps `deprecated: true` and lists other same-named members as candidates.
- No bare name that is not an exact key returns `deprecated: true` (property test over the v1.16.0 fixture).
- `pipecat.services.openai.llm` and `pipecat.transports.daily.transport` still return `current` with no candidates; `OpenAILLMService.model` still returns `current`.
- `SomeSubclass.trigger_user_turn_stopped` returns the single-candidate hint.
- `BARE_NAME_RULE` appears verbatim on the five surfaces (asserted by tests).
- The cold-agent check (A1) is recorded in the PR description.
- Code reviewed and approved; tests, `ruff` and `mypy` passing; documentation and changelog updated.

<!-- reviewed: 2026-11-18 @ ee0506800cb0180173184a4e5d4f63b7523b7ba1 -->

<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

Per-phase completion tracked here so ticking a box during a run does not bust the review marker. `/conduct` reads this section to skip already-done phases. Format: `- [ ] Phase <label>: <title>`.

- [x] Phase 1: Fixture
- [x] Phase 2: `members_named`
- [x] Phase 3: Types, handler, descriptions
- [x] Phase 4: Docs, smoke script, changelog, cold-agent gate

## Findings

Durable notes that survive after the run: diagnostic query results, decision rationale, "checked / accepted behaviour" outcomes.

- Review run 1 (2026-11-17): 30 raw findings across architecture, sequencing, spec-and-testing, assumptions and codebase-claims; all addressed. One spec-and-testing finding (MagicMock maps) was withdrawn by its lens. The sequential Contradiction Pass was not run.
- Review run 2 (2026-11-17, plan read from file, not embedded in the prompts): 21 raw findings (1 Critical, 10 Important, 10 Minor); codebase-claims clean (26 references). The Critical (Decision 4 turning current module paths into hints) was re-grilled and accepted in its revised form. The sequential Contradiction Pass was not run.
- Run 2 counts correction: "95 unique last segments" in the first draft was wrong; recomputed in Phase 1.
- Phase 2 and 3 conduct notes (2026-11-17): `OpenAILLMService.model` is an exact registry key in v1.16.0 (verdict `deprecated: true`), so the plan's example of it staying `current` as a many-homes dotted miss is wrong; tests use a made-up owner (`OpenAILLMServiceX.model`) for that case and assert `OpenAILLMService.model` as an exact hit. The invariant is unaffected.
- Phase 3 advisory review (opus/high; no Critical/Important), for the gauntlet: (1) `check-deprecation "InputParams."` returns `deprecated: true` via the existing prefix rule, so the plan's "trailing dot returns current" holds only for non-key names; (2, resolved in ded0863) the `candidates` field and `DeprecationCandidate` docs say "null unless the name has such members" / "bare query", wrong for dotted exact keys, prefix matches, multi-home dotted misses and Decision 4 hints; (3, still open: skipped because Decision 3 fixes the verdict; the cold-agent check passed anyway) exact-hit notes (e.g. `InputParams`) do not mention the attached candidates, so a note-reading agent can take the wrong replacement (assumption A1 risk); (4, resolved in ded0863) the all-current hint note says "at the evaluated version" instead of naming the version.
