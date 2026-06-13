# Task: Consume upstream `deprecations.json` as the primary deprecation source

**Status**: Planned — **blocked on** [pipecat-ai/pipecat#4722](https://github.com/pipecat-ai/pipecat/pull/4722) (machine-readable deprecation registry) merging and a pipecat build shipping `deprecations.json`.
**Date**: 2026-06-12
**Branch**: _(future)_ `feature/deprecations-json-consumer`
**Follows**: [`20260331-feature-version-aware-indexing.md`](20260331-feature-version-aware-indexing.md) (§ Corrections / Follow-up) and the parser fixes in PR #78 (`fix/deprecation-old-new-classification`).

## Motivation

`check_deprecation` derives its data by parsing pipecat **release-note prose**
(`_parse_release_body` / `_classify_tokens` in `services/ingest/deprecation_map.py`).
That is a natural-language classification problem the heuristics handle well now
but never perfectly — the residual **"replacement-kept" (Gap D)** false positives
(`OpenAILLMService`, `LLMContext`, `WebsocketTTSService`, `LocalSmartTurnAnalyzerV3`,
the 0.0.105 `TTSService` bullet) are documented as a known gap in AGENTS.md #48
precisely because removing them needs semantic phrasing detection, not regex.

PR #4722 makes pipecat emit a structured `deprecations.json` (subject /
`subject_kind` / `owner` / typed `replacement{relation,targets}` / versions),
seeded by the 331-record backfill. If the hub consumes that file directly, the
prose parser — and every failure mode that comes with it, Gap D included — stops
being on the critical path for any release authored against the new skill.

## Approach

**Prefer `deprecations.json` when available; fall back to the prose parser.** A
flag day is not required — the two coexist during pipecat's transition.

1. **Fetch.** During `refresh`, after cloning/locating the framework repo at the
   indexed version, read `deprecations.json` from the repo root (it ships in the
   tree, same as `CHANGELOG.md`). No `gh release view` round-trips needed for the
   covered range.
2. **Map → `DeprecationMap`.** Convert each record to a `DeprecationEntry`. The
   registry is richer than today's entry, so either extend `DeprecationEntry`
   (preferred — carry `subject_kind`, `owner`, `replacement.relation`) or project
   down to the current shape. `owner` records (members) must **not** key the
   owning class — mirrors the `check()` reverse-prefix guard already added in #78.
3. **Merge precedence.** `deprecations.json` entries win over prose-parsed ones
   for any overlapping symbol; prose parsing still covers releases **after** the
   registry's `_meta.provenance.covered_through_release` until upstream catches up
   (the registry is hand-maintained per-PR going forward, so the gap should be
   small or zero).
4. **Surface the richer fields** in the `check_deprecation` response —
   `replacement.relation` (`rename` vs `merged_into` vs `use_existing` vs `none`)
   and `migration` — so an agent gets *"removed; use the existing `X`"* instead of
   today's ambiguous bare-string replacement.

## Files to modify (anticipated)

- `src/pipecat_context_hub/services/ingest/deprecation_map.py` — new
  `build_deprecation_map_from_registry(path)`; `DeprecationEntry` field additions;
  merge/precedence helper.
- `src/pipecat_context_hub/cli.py` (~L764–801) — call the registry builder first,
  then the prose builders as fallback/supplement.
- `src/pipecat_context_hub/server/tools/check_deprecation.py` — expose
  `subject_kind`, `replacement.relation`, `migration` in the response.
- `shared/types.py` — extend the `check_deprecation` output contract.
- Tests: registry-builder unit tests; precedence/fallback tests; a smoke that the
  Gap-D residuals resolve once the registry is the source.

## Open questions / decisions

- **Schema in `DeprecationEntry`:** extend (carry `owner`/`relation`/`kind`) vs
  project down. Extending is preferred but touches the persisted map format and
  `to_dict`/`from_dict` round-trip — version the on-disk map if so.
- **Version stamping:** registry records added per-PR may have null
  `deprecated_in`/`removed_in` until release; decide whether the hub backfills
  these from the release the file shipped in, or tolerates null.
- **Discovery:** root `deprecations.json` is the proposed location; confirm once
  #4722 lands (maintainers may relocate it).
- **Certification:** run the source-tree existence oracle over the seed before
  trusting it as authoritative (flagged in #4722); the hub could even run it at
  refresh as a health check.

## Relationship to PR #78

PR #78 makes the **prose parser** correct enough to be a reliable fallback (and
fixes the genuine false positives it can). This plan makes the parser the
*fallback*, not the primary source — closing the residual Gap D at the source
rather than via ever-more heuristics. The two are sequential: #78 ships now; this
starts once #4722 merges.
