# Version-Aware Indexing

**Status:** Not Started
**Priority:** Medium
**Branch:** `feature/version-aware-indexing`
**Created:** 2026-03-31
**Objective:** Track which pipecat version each repo/example targets, and
surface version compatibility information in retrieval results. Enable users
pinning a specific pipecat version to get relevant, compatible results.

## Context

The context hub indexes 70+ repos at HEAD. When a user asks "how do I use
DailyTransport?", we return the latest framework API + example code that may
have been written for an older or newer pipecat version. This creates a
mismatch:

- A user on pipecat `v0.0.95` gets examples written for `v0.0.98` that may
  use APIs that don't exist in their version
- A user on latest gets examples pinned to `v0.0.85` that use deprecated
  patterns
- There's no way to distinguish "this example uses the latest API" from
  "this example is 6 months stale"

### Current State

**What we track today:**
- `commit_sha` per chunk (which commit was indexed)
- `indexed_at` timestamp
- `repo` name
- Staleness score decay (-0.10 over 365 days)

**What we don't track:**
- Which pipecat version a repo targets
- Which APIs are deprecated in which version
- Version compatibility between framework and examples
- Whether a chunk's API still exists in the user's version

### Pipecat's Versioning Practices (research findings)

**Framework:**
- Dynamic versioning via `setuptools_scm` (git tags)
- Current release: v0.0.108
- CHANGELOG.md follows Keep a Changelog with `Added`, `Changed`,
  `Deprecated`, `Removed`, `Fixed` sections
- Breaking changes marked with warning symbols
- Migration guide at `docs.pipecat.ai/client/migration-guide`

**Deprecation mechanism:**
- `DeprecatedModuleProxy` redirects old import paths to new ones with
  `DeprecationWarning` — 40+ module-level deprecations
- Old imports keep working until explicitly removed
- Removals happen in same release as deprecation notice

**Example repo version pinning (observed patterns):**
| Pattern | Example | Count |
|---------|---------|-------|
| Exact pin `==0.0.98` | audio-recording-s3, nemotron | ~15 |
| Minimum `>=0.0.105` | pipecat-flows, pipecat-subagents | ~10 |
| Range `>=0.0.93,<1` | peekaboo | ~5 |
| No constraint | pipecat-quickstart | ~10 |

**TypeScript SDKs:**
- Use caret ranges (`^1.7.0`) allowing minor/patch updates
- Transport packages versioned separately but closely tracked

**Documentation:**
- Single docs site (always latest, no version selector)
- No versioned doc variants

## Requirements

### Phase 1: Version Extraction (metadata only)

1. **Extract pipecat version from each repo** — parse `pyproject.toml`,
   `requirements.txt`, `setup.py`, `setup.cfg`, and `package.json` for
   `pipecat-ai` / `@pipecat-ai/client-js` version constraints
2. **Store as chunk metadata** — `pipecat_version_pin` field on each chunk
   (e.g., `"==0.0.98"`, `">=0.0.105"`, `null` for no pin)
3. **Surface in retrieval results** — include version pin in `search_api`,
   `search_examples`, and `get_code_snippet` responses
4. **No filtering yet** — Phase 1 is purely additive metadata

### Phase 2: Deprecation Awareness

1. **Extract deprecation markers from framework** — parse
   `DeprecatedModuleProxy` usage, `@deprecated` decorators, and CHANGELOG
   `Deprecated`/`Removed` sections
2. **Build a deprecation map** — `{old_path: {new_path, deprecated_in,
   removed_in}}` for module-level deprecations
3. **Annotate chunks using deprecated APIs** — when an example imports
   a deprecated module, flag it in the chunk metadata
4. **Surface in results** — `deprecated_apis: ["pipecat.services.grok.llm"]`

### Phase 3: User Version Context

1. **Detect user's pipecat version** — parse the user's project
   `pyproject.toml` / `requirements.txt` at query time (or accept via
   env var / config)
2. **Version-aware scoring** — boost chunks whose `pipecat_version_pin`
   is compatible with the user's version, penalize incompatible ones
3. **Filter option** — allow excluding results targeting versions newer
   than the user's (opt-in, not default)
4. **Compatibility annotations** — add `version_compatibility: "compatible"
   | "newer_required" | "deprecated" | "unknown"` to results

### Phase 4: Historical Version Indexing (stretch)

1. **Index specific git tags** — allow indexing `pipecat-ai/pipecat` at a
   specific tag (e.g., `v0.0.95`) alongside HEAD
2. **Multi-version API surface** — users pinned to v0.0.95 get API docs
   from that version, not HEAD
3. **Diff awareness** — know which APIs were added/removed between versions

## Implementation Checklist

### Phase 1: Version Extraction

- [ ] Add `_extract_pipecat_version(repo_path: Path) -> str | None` helper
      in `github_ingest.py` — parses pyproject.toml, requirements.txt,
      setup.py, setup.cfg, package.json for pipecat version constraints
- [ ] Store `pipecat_version_pin` in chunk metadata during ingestion
- [ ] Store `pipecat_version_pin` in `TaxonomyEntry` for example chunks
- [ ] Surface in `ExampleHit`, `ApiHit`, `CodeSnippet` response models
- [ ] Unit tests for version extraction from each file format
- [ ] Verify: `search_examples("TTS pipeline")` results include version pin
- [ ] No changes to scoring or filtering — purely additive metadata

### Phase 2: Deprecation Awareness

- [ ] Parse `DeprecatedModuleProxy` usage from pipecat source — build
      `{old_module: new_module}` map
- [ ] Parse CHANGELOG.md for `### Deprecated` and `### Removed` sections
      with version numbers
- [ ] Store deprecation map as a versioned artifact (rebuild on refresh)
- [ ] Cross-reference example imports against deprecation map during ingest
- [ ] Add `deprecated_apis` metadata field to affected chunks
- [ ] Surface `deprecated_apis` in retrieval results
- [ ] Unit tests for deprecation map parsing

### Phase 3: User Version Context

- [ ] Detect user's pipecat version from project files (at query time or
      via `PIPECAT_HUB_USER_VERSION` env var)
- [ ] Add `version_compatibility` field to retrieval results
- [ ] Implement version-aware scoring adjustments
- [ ] Add opt-in version filtering to `search_examples` and `search_api`
- [ ] Unit tests for version comparison and scoring

### Phase 4: Historical Version Indexing (stretch)

- [ ] Support indexing specific git tags via config or CLI flag
- [ ] Separate index partitions per version
- [ ] Version-aware `search_api` — prefer user's version's API surface
- [ ] Performance: don't double index count unnecessarily

## Technical Specifications

### Version Extraction (Phase 1)

**Parsing priority order:**
1. `pyproject.toml` → `[project].dependencies` → find `pipecat-ai` entry
2. `requirements.txt` → line matching `pipecat-ai`
3. `setup.py` / `setup.cfg` → `install_requires` → `pipecat-ai`
4. `package.json` → `dependencies` / `peerDependencies` →
   `@pipecat-ai/client-js`

**Version constraint format stored as-is:**
- `"==0.0.98"` (exact pin)
- `">=0.0.105"` (minimum)
- `">=0.0.93,<1"` (range)
- `"^1.7.0"` (npm caret range)
- `null` (no pipecat dependency found)

### Deprecation Map (Phase 2)

```python
@dataclass
class DeprecationEntry:
    old_path: str           # e.g., "pipecat.services.grok.llm"
    new_path: str           # e.g., "pipecat.services.xai.llm"
    deprecated_in: str      # version string, e.g., "0.0.100"
    removed_in: str | None  # version if removed, else None
```

**Sources:**
1. `DeprecatedModuleProxy` usage in `__init__.py` files → old/new path
2. CHANGELOG.md `### Deprecated` → version + description
3. CHANGELOG.md `### Removed` → version + description

### Version Compatibility Scoring (Phase 3)

```
user_version = "0.0.95"
chunk_version_pin = "==0.0.98"

if chunk requires newer than user → penalty -0.15, label "newer_required"
if chunk uses deprecated APIs in user's version → no penalty, label "uses_deprecated"
if chunk is compatible → no change, label "compatible"
if no version info → no change, label "unknown"
```

### Files to Modify

| Phase | File | Changes |
|-------|------|---------|
| 1 | `github_ingest.py` | `_extract_pipecat_version()` helper |
| 1 | `types.py` | `pipecat_version_pin` on response models |
| 1 | `hybrid.py` | Surface version in results |
| 2 | New: `deprecation_map.py` | Parse deprecations from framework source |
| 2 | `source_ingest.py` | Cross-reference imports vs deprecation map |
| 3 | `hybrid.py` | Version-aware scoring |
| 3 | `config.py` | `PIPECAT_HUB_USER_VERSION` env var |

## Open Questions

1. **Should version filtering be opt-in or default?** — Filtering by default
   could reduce useful results. Annotating (no filtering) is safer as a first
   step.
2. **How to handle repos with no version pin?** — Mark as `"unknown"`,
   don't penalize. These are often actively maintained (follow latest).
3. **Should we track TS SDK versions separately?** — The JS/TS packages use
   a different versioning scheme (`^1.7.0`). Phase 1 should extract both
   Python and TS version pins.
4. **Multi-version indexing cost** — Indexing 3 versions of pipecat would
   ~3x the source chunk count (~15K → ~45K). Retrieval performance impact?
5. **How stale is "too stale"?** — A repo pinned to `v0.0.85` (released
   months ago) may have perfectly valid patterns. Only deprecated/removed
   APIs are truly problematic.

## Review Focus

- **Version extraction reliability** — pyproject.toml formats vary
  (PEP 621 vs setuptools vs poetry). Cover all common patterns.
- **Deprecation map completeness** — `DeprecatedModuleProxy` covers module
  renames but not individual API deprecations (method removed, parameter
  changed). CHANGELOG parsing is needed for the rest.
- **Scoring balance** — version penalties should not overwhelm relevance.
  A highly relevant example from v0.0.95 is still better than an irrelevant
  one from v0.0.108.
- **Phase 4 feasibility** — multi-version indexing has real storage and
  performance costs. May be better as a "version snapshot" feature than
  a permanent multi-version index.

## Acceptance Criteria

- [ ] `search_examples` results include `pipecat_version_pin` when available
- [ ] `get_code_snippet` results show version compatibility annotation
- [ ] Examples using deprecated APIs are flagged in results
- [ ] User version detection works from project pyproject.toml
- [ ] No scoring regressions — existing smoke tests still pass
- [ ] Version info doesn't clutter results for users who don't need it
