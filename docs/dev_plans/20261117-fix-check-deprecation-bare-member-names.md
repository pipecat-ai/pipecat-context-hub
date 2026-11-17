# `check_deprecation` gives no signal for bare member names

## Status

Planned. Two design decisions open (see "Decisions"). Branch `fix/check-deprecation-bare-member-names`. Split out of #166 (closed); no issue filed yet.

## Goal

`check_deprecation("trigger_user_turn_stopped")` currently returns `{"deprecated": false, "status": "current"}`, while `BaseUserTurnStopStrategy.trigger_user_turn_stopped` is deprecated since 1.15.0 (replacement `report_user_turn_stopped`). A bare member name should tell the caller a deprecated member of that name exists, without asserting something the registry cannot know.

## What we know (verified 2026-11-17, `deprecations.json` at v1.16.0)

- The registry keys dotted subjects as `Class.member` (e.g. `BaseUserTurnStopStrategy.trigger_user_turn_stopped`) and adds a `<module>.<subject>` alias. `DeprecationMap.check` matches an exact key, or a query nested *under* a key (prefix). A bare `member` is neither, so it returns `None`, and the handler turns `None` into `current` (`check_deprecation.py:104`). So this is a lookup gap, not a data gap. The qualified form already works.
- This is **by design for ancestors**: `DeprecationMap.check` deliberately refuses to match upward ("reporting a current package/module as deprecated ... is the worst failure mode for this tool"). Bare-member lookup is the same hazard in a different direction.
- Of 549 records, 438 have a dotted, non-module subject: 345 `parameter`, 44 `class` (nested, e.g. `X.InputParams`), 37 `method`, 5 `property`, 4 `function`, 3 `event`.
- Bare member names are mostly ambiguous: only 95 are unique. Shared names include `model` (58 records), `params` (55), `InputParams` (44), `voice_id` (34), `aggregate_sentences` (11), `language` (10).
- A unique name is still not proof. `trigger_user_turn_stopped` is unique in the registry, but a subclass or an unrelated class can define a current member with the same name; the registry only lists what is deprecated.

## Invariant

`deprecated: true` is only ever returned for a symbol the registry actually lists (exact key or a descendant of one). A bare member name never produces `deprecated: true`, however many records match. A bare name that matches deprecated members must not produce a clean "current" verdict.

## Decisions (need an answer before Phase 1)

1. **Hint-only (recommended) vs assert-when-unique.** Hint-only returns the matching qualified symbols and tells the caller to qualify. Assert-when-unique would return `deprecated: true` for the 95 unique names; it fixes the reported case with one call but breaks the invariant above for any class that reuses the name.
2. **Status on a hint.** `status: null` (recommended: the existing "no verdict" shape used when the map cannot be loaded, so the caller can tell "checked, nothing found" from "matched, not decidable") vs keeping `current` with extra fields, which keeps old consumers working but keeps the misleading verdict.

## Design (assuming the recommendations)

- `DeprecationMap.members_named(name)`: entries whose key's last segment equals `name`, from dotted non-module keys only, de-duplicated by entry identity (the `<module>.` alias and the bare key point at the same entry). Computed from `entries`, so no change to the serialized map or its schema.
- Handler: when `check()` is `None`, the symbol contains no `.`, and `members_named` is non-empty (and the map loaded), return `deprecated: false`, `status: null`, a note ("no exact match; N deprecated members are named `X`; qualify with the class for a verdict") and a new `candidates` list, capped (10) with `candidates_total`. Each candidate carries the qualified symbol, `kind`, `deprecated_in`, `removed_in`, `replacement`. Candidates are evaluated at the target version like the exact path (`status_for`), and ones not yet deprecated at that version are listed as such rather than dropped silently.
- `CheckDeprecationOutput` gains `candidates` / `candidates_total` (default `None`; absent from existing responses). The CLI shares the handler, so `check-deprecation` and the MCP tool stay at parity.
- Tool/CLI docs and the `check_deprecation` input description say what a bare name does.

## Files

`src/pipecat_context_hub/services/ingest/deprecation_map.py`, `src/pipecat_context_hub/server/tools/check_deprecation.py`, `src/pipecat_context_hub/shared/types.py`, `tests/unit/test_deprecation_map.py`, `tests/unit/test_check_deprecation_tool.py`, `docs/smoke-test.md`, `CHANGELOG.md`, the tool description wherever it is registered.

## Tests

- Real record frozen in: `trigger_user_turn_stopped` returns `deprecated: false`, `status: null`, one candidate `BaseUserTurnStopStrategy.trigger_user_turn_stopped` with replacement `report_user_turn_stopped`.
- Ambiguous name (`model`): candidates capped at 10, `candidates_total` is the full count.
- The invariant, as a property over the whole v1.16.0 registry: for every bare member name, `deprecated` is `false`.
- Qualified and fully-qualified queries unchanged; ancestors still `None`.
- Unloadable map: the existing "map missing / unreadable" note wins over a hint.
- Undotted queries that match nothing, and dotted queries that match nothing, still return `current`.
- Version-aware: candidate status at `--at-version` before and after `deprecated_in`.
- CLI/MCP parity test still passes.

## Verification

- `uv run ruff check src/ tests/`, `uv run mypy src/ tests/`, `uv run pytest tests/ -q` (backgrounded).
- Live smoke: `uv run pipecat-context-hub check-deprecation trigger_user_turn_stopped`, `... model`, `... BaseUserTurnStopStrategy.trigger_user_turn_stopped`; add the bare-member query to `docs/smoke-test.md` as a numbered item (field-found failure rule in `AGENTS.md`).
- Types change, so run the offline smoke (`uv run pytest tests/smoke/ -v`) when the PR opens.

## Risk

- A new optional output field and a `status: null` response for inputs that used to say `current`. A consumer that treats `status != "current"` as "not clean" will now see the hint, which is the intent; one that keys on `status == "current"` for the bare case changes behaviour. Both are noted in the changelog.
- Response size for common names (`model`): bounded by the cap.
