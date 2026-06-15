# Task: Consume upstream `deprecations.json` as the deprecation source

**Status**: Implemented — PR [#85](https://github.com/pipecat-ai/pipecat-context-hub/pull/85) (`feat/deprecation-registry`), open as of 2026-06-13.
**Date**: 2026-06-12 (planned) · 2026-06-13 (implemented)
**Branch**: `feat/deprecation-registry`
**Upstream dependency**: [pipecat-ai/pipecat#4726](https://github.com/pipecat-ai/pipecat/pull/4726) ("Improve deprecation, add deprecation registry") — **merged 2026-06-13**. Supersedes the earlier draft #4722 referenced in the original plan.
**Follows**: [`20260331-feature-version-aware-indexing.md`](20260331-feature-version-aware-indexing.md) (§ Corrections / Follow-up) and the parser fixes in PR #78 (`fix/deprecation-old-new-classification`).

> **Implementation note (2026-06-13):** This plan was drafted against the *anticipated* schema of draft PR #4722. The shipped registry (#4726, `schema_version: 1`) has a flatter shape, and the implementation deviated from the plan in one significant way: **the prose parser was removed entirely rather than retained as a fallback.** See § What shipped below.

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

PR #78 made the **prose parser** correct enough to be a reliable fallback (and
fixed the genuine false positives it could). The original plan made the parser
the *fallback*, not the primary source. **What actually shipped (PR #85) went
further: it retired the prose parser entirely** — see below.

---

## What shipped (PR #85, against upstream #4726)

### Upstream registry, as merged (`scripts/deprecations/deprecations.json`, `schema_version: 1`)

Generated by pipecat's `scripts/deprecations/generate.py` from `.. deprecated::`
directives and PEP 702 `@deprecated` decorators. The shipped record shape is
**flatter than the plan anticipated** — no nested `replacement{relation,targets}`,
no separate `owner`/`subject_kind`:

```jsonc
{
  "subject": "AICFilter.create_vad_analyzer",   // bare or Class.member; module records are dotted paths
  "module": "pipecat.audio.filters.aic_filter",
  "kind": "method",                              // class | method | property | parameter | module
  "deprecated_in": "1.4.0",
  "removed_in": "1.6.0",
  "relation": "use_existing",                    // rename | merged | move | use_existing | none (flat string, not nested)
  "replacement": "AICQuailVADAnalyzer",          // flat string, not a typed target list
  "message": "`AICFilter.create_vad_analyzer` is deprecated since 1.4.0 …",
  "location": "pipecat/audio/filters/aic_filter.py"  // file path; surfaced verbatim, format-agnostic (older registries may carry a :line suffix)
}
```

Snapshot at merge (2026-06-13): **322 records** — `parameter` 220, `class` 76,
`method` 18, `module` 6, `property` 2; `relation` is overwhelmingly
`use_existing` (316), with `move` (4) and `none` (2). All records carry concrete
`deprecated_in`/`removed_in` (no nulls in the current snapshot), so the plan's
"version stamping" open question is moot in practice.

**Location resolved:** the registry lives at `scripts/deprecations/deprecations.json`,
**not** the repo root the plan guessed. It is build-tooling output and is *not*
shipped in the PyPI package — the hub reads it from the cloned git checkout, which
is fine since the hub clones the repo during `refresh`.

### Implementation decisions (deviations from the plan)

1. **Registry-only — prose parser removed, not retained as fallback.** PR #85
   deletes `build_deprecation_map_from_{source,releases,changelog}`,
   `_classify_tokens`/`_parse_release_body`, the `changelog_notes` field, and the
   753-line release-notes test file. `deprecation_map.py` drops ~890 → ~210 lines.
   **Consequence:** for pipecat versions that predate the registry (any
   `--framework-version` tag before #4726's commit), `check_deprecation` now
   returns an **empty** map (gracefully — `FileNotFoundError` → empty, logged at
   INFO) instead of imperfect prose-derived data. This is the intended trade-off:
   eliminating false positives (the whole motivation) at the cost of zero data for
   pre-registry checkouts. The plan's §3 "merge precedence" and the prose
   *fallback* are therefore **superseded, not implemented.**

2. **`DeprecationEntry` extended (not projected down).** Added `kind` and
   `relation`; `to_dict`/`from_dict` carry them. `subject` → `old_path`,
   `replacement` → `new_path`. The richer plan fields (`owner`, typed
   `replacement.targets`) don't exist in the shipped registry, so they were not
   added.

3. **Dual keying.** Each non-module record is keyed by both its bare `subject`
   and its fully-qualified `<module>.<subject>` (alias via `setdefault`, so a true
   subject always wins over an alias). 322 records → 638 lookup keys.

4. **`check()` is forward-prefix only.** The second PR commit removed a
   reverse-prefix branch that flagged *ancestor* packages (querying
   `pipecat.services` matched the deprecated `pipecat.services.grok.llm`). Lookup
   now matches the exact symbol or symbols *nested under* a deprecated key — the
   tool's real contract ("is THIS symbol deprecated?"). This subsumes and
   generalizes the #78 owner-of-member guard: because parameters/methods are keyed
   as `Class.member`, querying the bare `Class` no longer false-positives.

5. **Output surface.** `check_deprecation` now returns `kind` and `relation`
   (added to `CheckDeprecationOutput`), so an agent gets "removed; use the existing
   `X`" semantics instead of a bare replacement string.

### Smoke test (2026-06-13, against the real merged registry — 322 records)

Ran `build_deprecation_map_from_registry` over the upstream
`deprecations.json` and asserted:

- **Current-API canaries not deprecated:** `DailyTransport`, `OpenAILLMService`,
  `pipecat.services`, `pipecat.services.openai.llm`,
  `pipecat.transports.daily.transport` → all not deprecated. ✅ (This is exactly
  the Gap-D / ancestor-package failure mode the plan wanted closed.)
- **Bare + fully-qualified resolve:** `ResampyResampler` and
  `pipecat.audio.resamplers.resampy_resampler.ResampyResampler` → both DEPRECATED
  (→ `SOXRAudioResampler`, `1.2.0`→`2.0.0`). ✅
- **Method-level + owning class:** `AICFilter.create_vad_analyzer` → DEPRECATED;
  the owning class `AICFilter` and its module → not deprecated. ✅
- **Module-kind:** `pipecat.audio.vad.aic_vad` → DEPRECATED. ✅
- **No bare-subject collisions** across the 322 records (0), so the bare-key
  last-write is not currently lossy.
- **Tests:** `test_deprecation_map.py` + `test_cli.py` → 80 passed. Full PR suite
  per the description: 1094 passed, 6 skipped.

## Improvements / follow-ups identified

Resolved in the PR #85 follow-up commit:

- **(P2) ✅ `location` surfaced.** The registry `location` (a file path; older
  registries may carry a `:line` suffix) is now threaded through
  `DeprecationEntry` → persisted map → `check_deprecation` output (see AGENTS.md
  #45), giving agents a locate-the-definition pointer. Passed through verbatim —
  the consumer never parses the format.
- **(P2) ✅ AGENTS.md refreshed.** §34–37 / §45–48 rewritten for the registry
  model: dropped the `gh`/release-note prerequisite and the resolved Gap-D
  residuals; added the removed-symbols-are-absent caveat. `smoke_check_deprecation.py`
  canaries updated to registry-accurate symbols.
- **(P3) ✅ Bare-subject collision now logged.** A `WARNING` fires when two records
  collide on the same bare `subject` (last-write-wins on the bare key; both stay
  resolvable by full path). 0 collisions today, but no longer silent.
- **(P3) ✅ Empty-map contract already pinned.** `test_missing_registry_returns_empty`
  and `test_malformed_registry_returns_empty` already assert graceful degradation
  to an empty map — no new test needed.

Resolved in **PR [#88](https://github.com/pipecat-ai/pipecat-context-hub/pull/88)** (`feat/removal-history`, 2026-06-14):

- **Removed-symbol history — now handled.** Originally deferred here (decided
  2026-06-14: "do nothing now") with a revisit trigger of "when pipecat 2.0 reaches
  pre-release/RC." That trigger was **overtaken by events**: the work was picked up
  early via a producer/consumer split rather than waiting for the 2.0 cliff.

  **The gap.** The active registry is blind to *removed* symbols by construction —
  it scans live `.. deprecated::` / `@deprecated` markers, and a removed symbol has
  none. So once a symbol is deleted from source it falls out of `deprecations.json`,
  and `check_deprecation` reported it `deprecated: false` (*unknown*), not "removed
  in X; use Y" — arguably the highest-value case (an agent using an old API it
  learned from a prior pipecat). With **319 of 322** records carrying
  `removed_in: 2.0.0`, the whole class lands at once when 2.0 ships.

  **What shipped — and how it differs from the deferred design.** This plan's
  intended fix was a **version-diff tombstone** (on refresh, carry forward entries
  that vanish from the new registry, comparing against the last-persisted 1.x map).
  PR #88 instead took the *other* route this section flagged as "trivial if
  available": an **upstream `removals.json` ledger** (producer side:
  [pipecat#4734](https://github.com/pipecat-ai/pipecat/pull/4734)). The hub now reads
  `scripts/deprecations/removals.json` (sibling of `deprecations.json`) into the map
  with `status="removed"`, and answers `check_deprecation` relative to a pipecat
  **version**:
  - `add_removals_from_registry(dep_map, path)` merges removed symbols
    (`status="removed"`, actual `removed_in`, `announced_removed_in`), keyed bare +
    fully-qualified like active deprecations.
  - `status_for(entry, version)` computes `current` / `deprecated` / `removed` via
    `packaging.version`. **Safety invariant (carried over from the #78
    false-positive lessons):** it never reports `removed` for an *active*
    deprecation — whose `removed_in` is only an *announced* version — so a removal
    must be evidenced in `removals.json`.
  - `check_deprecation` gains an optional `version` input (defaulting to the indexed
    `framework_version`); output adds `status` and `announced_removed_in`. The one-shot
    CLI exposes it as `check-deprecation <symbol> --at-version <V>`.

  **Dormant until upstream ships the file.** With no `removals.json` present the
  merge is a no-op and behavior is identical to #85 — so this no longer hinges on
  the 2.0 cliff; it is in place ahead of it. The version-diff-tombstone fallback was
  **not needed** and is not implemented.

## Original plan (superseded sections, kept for provenance)

The Approach / Open-questions sections below were written against draft #4722 and
the prose-fallback design. They are retained for history; the authoritative record
of what shipped is § What shipped above.
