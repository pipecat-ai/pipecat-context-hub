# ChatGPT plugin 0.2.4 visibility and setup refinement

**Status**: Complete — candidate ready for consolidated review
**Component**: plugin packaging, discovery instructions, CLI workflows
**Branch**: `feature/chatgpt-local-plugin`
**Created**: 2026-10-16
**Updated**: 2026-10-16

## Context

Version 0.2.3 is published. Employee traces identified three opportunities for
one consolidated follow-up: reduce setup approval churn, explain a safe Pipecat
CLI upgrade path, and improve discovery wording for relevant voice/multimodal
agent requests. Plugin-host discovery and approval policy remain outside the
skills-only package's control, so this plan must not attempt to force promotion,
change host approval settings, or silently mutate installations.

## Requirements

- Preserve 0.2.3 as the published baseline; this work is an unpublished 0.2.4 candidate.
- Keep the package skills-only with no MCP or lifecycle hooks.
- Batch bounded read-only setup checks and present one consolidated approval for mutations.
- Detect an older Pipecat CLI and offer an exact, manager-aware upgrade path only after explicit approval; never upgrade automatically by default.
- Keep existing indexes, pins, extensions, credentials, and project locks unless the user explicitly authorises a change.
- Use neutral, accurate discovery wording. Mention multimodal/vision only where the indexed corpus supports useful grounded coverage; do not manipulate fair plugin discovery.
- Add a repeatable visibility-evaluation fixture/report covering direct, indirect, and negative prompts, approval burden, workflow selection, and CLI mismatch handling.

## Implementation Checklist

### Phase 1: Refine setup and build instructions

**Impl files:** `plugins/pipecat-context-hub/skills/setup/SKILL.md`, `plugins/pipecat-context-hub/skills/build/SKILL.md`, `plugins/pipecat-context-hub/README.md`
**Test files:** `tests/unit/test_chatgpt_plugin_setup.py`
**Test command:** `uv run pytest tests/unit/test_chatgpt_plugin_setup.py -q`
**Goal:** Make safe setup and upgrade proposals concise, explicit, and approval-efficient without granting implicit mutation authority.

- Define a bounded read-only discovery batch and a single approval boundary for installation/index mutations.
- Add version-mismatch detection and a manager-aware, approval-gated upgrade proposal with post-upgrade verification.
- Keep unsupported/unknown package managers and missing execution capabilities read-only and actionable.

### Phase 2: Update package metadata and evaluation evidence

**Impl files:** `plugins/pipecat-context-hub/plugin.json`, `plugins/pipecat-context-hub/skills/explore/SKILL.md`, `docs/evaluations/pipecat-context-hub-plugin.md`
**Test files:** `tests/unit/test_chatgpt_plugin_setup.py`
**Test command:** `uv run pytest tests/unit/test_chatgpt_plugin_setup.py -q`
**Goal:** Improve relevant discovery recall while preserving negative precision and accurately documenting what the host controls.

- Bump the candidate manifest to 0.2.4 and revise descriptions/default prompts for supported voice and multimodal intent.
- State that installation prompting and approval-mode selection are host/user policy, not plugin guarantees.
- Add a compact golden-prompt matrix and result fields for employee trace evaluation; do not claim measured improvement before running it.

### Phase 3: Validate and record the candidate

**Impl files:** `docs/dev_plans/20261016-feature-chatgpt-plugin-0-2-4.md`, `plugins/pipecat-context-hub/README.md`, `CHANGELOG.md`
**Test files:** `tests/unit/test_chatgpt_plugin_setup.py`
**Test command:** `uv run pytest tests/unit/test_chatgpt_plugin_setup.py -q`
**Validation cmd:** `python -m json.tool plugins/pipecat-context-hub/plugin.json >/dev/null && git diff --check`
**Goal:** Leave a reviewable, source-grounded 0.2.4 candidate with no claim that requires portal or host-side evidence.

- Update package documentation and changelog with the consolidated user-visible changes.
- Run packaging, schema, PII/secret, and focused contract checks.
- Keep submission and publication separate from this implementation commit.

## Technical Specifications

### Files to modify

- `plugins/pipecat-context-hub/plugin.json` — candidate version and neutral discovery metadata.
- `plugins/pipecat-context-hub/skills/setup/SKILL.md` — batched checks, one approval boundary, and explicit CLI upgrade proposal.
- `plugins/pipecat-context-hub/skills/build/SKILL.md` — route older CLI detection to the approval-gated upgrade path.
- `plugins/pipecat-context-hub/skills/explore/SKILL.md` — supported voice/multimodal discovery wording and host-boundary caveat.
- `plugins/pipecat-context-hub/README.md` — user-facing setup, upgrade, discovery, and evaluation limits.
- `docs/evaluations/pipecat-context-hub-plugin.md` — compact 0.2.4 evaluation matrix and baseline boundary.
- `CHANGELOG.md` — concise candidate entry.
- `tests/unit/test_chatgpt_plugin_setup.py` — contract assertions for metadata and safety wording.

### Boundaries

The plugin cannot detect or install itself when absent, change ChatGPT approval
settings, or guarantee model-side surfacing. OpenAI host policy and fair-play
guidance remain authoritative. The candidate only improves the information and
workflow available after the plugin is selected.

## Testing Notes

The existing packaging test suite verifies resource copying, manifest shape,
skills-only boundaries, and credential-safe setup. New assertions will freeze
the 0.2.4 metadata and the explicit no-silent-upgrade/approval contract. Employee
traces are evidence for a later submission decision, not a substitute for these
source-level checks. Focused packaging/contract tests pass (51 passed); the full
repository suite passes (1,957 passed, 7 skipped); Ruff format/check, JSON
validation and `git diff --check` pass. A read-only live Hub query confirmed
Vision and multimodal source coverage at indexed framework 1.12.0.

## Acceptance Criteria

- Candidate manifest is valid and reports version 0.2.4.
- Setup can describe one bounded read-only check followed by one approval for mutations.
- Older Pipecat CLI handling offers a precise approved upgrade path and verifies it afterward without silently changing the environment.
- Discovery wording is neutral, source-grounded, and does not claim automatic plugin installation or measured visibility improvement.
- Focused package tests, JSON validation, diff checks, and secret/PII checks pass.
- No submission, upload, merge, or publication occurs as part of this plan.

## Progress

- [x] Phase 1: Refine setup and build instructions.
- [x] Phase 2: Update package metadata and evaluation evidence.
- [x] Phase 3: Validate and record the candidate.

## Findings

- 2026-10-16 — Exploration confirmed no current multimodal/vision wording in the plugin source, while the live local index now supplies grounded coverage: `vision + multimodal` returned the Vision supported-services section, `VisionFullResponse*` frames and multimodal Gemini Live material at indexed framework 1.12.0. The skill still reports gaps rather than inferring coverage.
- 2026-10-16 — The published 0.2.3 package is the comparison baseline; host-side installation prompting, approval mode, and automatic discovery remain outside package control.

## Final Results

The unpublished 0.2.4 candidate now carries neutral voice/multimodal discovery
metadata, one bounded setup approval boundary, and an explicit manager-aware CLI
upgrade proposal that never upgrades silently. Historic 0.2.3 acceptance is
cross-linked without rewriting its chronology. Submission and publication remain
separate follow-up actions.
