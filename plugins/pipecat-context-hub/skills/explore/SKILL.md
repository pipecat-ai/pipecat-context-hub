---
name: explore
description: Explore a voice AI agent idea or Pipecat concept, including STT, TTS and conversational pipelines, using sourced documentation, API definitions and examples.
---

Use the installed PCH query CLI through native command execution. Prefer
`pipecat context-hub` when installed help exposes its PCH commands; otherwise
use standalone `pipecat-context-hub`. This skills-only plugin does not require
an MCP connection. Native execution, a compatible runtime and a populated
Context Hub index are required for grounded retrieval. If they are absent,
report retrieval unavailable and offer [Context Hub setup](../setup/SKILL.md).
Cloud credentials are not required for exploration.

Exploration is read-only. An idea or concept question authorises retrieval and
discussion, not dependency installation, indexing, scaffolding, local file writes,
execution of examples, builds, secret access/uploads or deployment.
After an explicit build request, hand off to the build skill to check native
execution, installed CLI and material project choices. After a deployment
request, hand off to deploy for read-only preparation and concrete approval
before secret writes, image builds or Cloud mutations. Skills do not grant
executable permissions.

## Readiness and preferences

1. Run CLI `status` before retrieval. Record the selected executable/prefix,
   record count, refresh date, `framework_version` (operator pin),
   `indexed_framework_version` (observed release) and
   `indexed_framework_commits_ahead` when present. A `latest` pin is not proof
   of today's latest release. Retain relevant commit citations.
2. Check exit codes and parse stdout as JSON, keeping stderr diagnostics separate.
   Missing execution/runtime/index means retrieval unavailable; offer Setup and
   wait for an explicit request before installation or initial indexing. Index
   failures, invalid input, low confidence, empty hits and unavailable versions
   are not reasons to select another index or silently switch installations.
   Explain startup remediation without running it. An empty index cannot ground
   an answer. A stale index can supply explicitly dated evidence with a coverage
   limitation. Unknown provenance is not a freshness pass. Never refresh/reset/
   repair during exploration. Describe reranker limitations from status;
   `config_disabled` is an operator choice, not a bug.
3. Extract the user's idea/concept, Pipecat version and source preference.
   Inspect installed CLI help before mapping preferences:
   installed command versions may differ from this source package. Use only supported
   arguments.
   If a preference cannot be enforced, say so before offering clearly labelled
   broader evidence. A strict source/version requirement remains unresolved;
   ask whether the user wants a broader alternative, while retrieving any
   independent material that already meets their request.

| Handler operation | Supported version preference | Supported source/scope preference |
|---|---|---|
| `search_docs` | None | `area` docs-path prefix; no `repo` filter |
| `get_doc` | None | `doc_id` from a hit or `path`, optionally `section`; no repo filter |
| `search_examples` | `pipecat_version`; optional `version_filter="compatible_only"` requires it | `repo`, `language`, `domain`, `tags`, `execution_mode`; `foundational_class` is legacy and not populated for topic-layout entries |
| `get_example` | None | `example_id` from a hit; `include_readme`; inherit and verify the selected hit's provenance |
| `search_api` | `pipecat_version`; optional `version_filter="compatible_only"` requires it | `module`/`class_name` prefixes, `chunk_type`, `is_dataclass`, `yields`, `calls`; no repo/language filter |
| `get_code_snippet` | `pipecat_version` annotation; no `version_filter` | Exactly one lookup mode: `symbol`, `intent`, or `path` + `line_start`; intent may also have path/range scope. `module`/`class_name` prefixes apply to symbol mode only. `content_type` selects `source` or `code`; no repo filter |
| `check_deprecation` | `version` for lifecycle evaluation; omitted means indexed framework version | `symbol`; no source filter |
| `get_hub_status` | None | No retrieval filters |

Compatibility annotations do not switch indexed framework versions or validate
an unindexed release. If a requested snapshot (older or newer) differs from the
indexed provenance, disclose that the requested snapshot is unavailable here;
retrieved APIs remain evidence from the indexed snapshot. For supported tools,
pass the stated target version to get annotations. `compatible_only` excludes
`newer_required` results; it can retain `unknown` results. Null compatibility
without a target version means unevaluated; `unknown` with a target means
insufficient evidence. Neither is proof of compatibility. Inspect pins and
annotations on individual hits. `check_deprecation(version=...)` evaluates
the indexed lifecycle registry, not source from that version; an announced
removal date alone is not proof that removal occurred. A negative lifecycle
lookup is not proof of complete coverage or compatibility.

## CLI retrieval

Discover an already-installed
`pipecat` or `pipecat-context-hub` executable through native execution tools or
installed tool-manager metadata. For Pipecat CLI, verify
`pipecat context-hub --help` exposes the PCH query commands; finding `pipecat`
alone is insufficient. Its `ch` alias may also exist, but prefer the full name.
If that subcommand is absent, check the standalone executable. Do not guess
machine paths, use a shell MCP adapter, or install/download tools automatically.
If native execution or PCH is absent, explain the missing prerequisite.

Inspect the resolved CLI's help and each needed subcommand's `--help`. Prefix
the commands below with the verified `pipecat context-hub` or standalone
`pipecat-context-hub`; for example, `pipecat context-hub status` and
`pipecat context-hub search-docs "TTS + STT"`. Use only these read-only query
commands, with safe argument quoting:

| Handler operation | PCH CLI command |
|---|---|
| `get_hub_status` | `status` |
| `search_docs` | `search-docs QUERY` |
| `get_doc` | `get-doc --doc-id ID` or `get-doc --path PATH` |
| `search_examples` | `search-examples QUERY` |
| `get_example` | `get-example EXAMPLE_ID` |
| `search_api` | `search-api QUERY` |
| `get_code_snippet` | `get-code-snippet --symbol SYMBOL`, `--intent QUERY`, or `--path PATH --line-start N` |
| `check_deprecation` | `check-deprecation SYMBOL` |

The query commands return the same handler JSON on stdout; keep stderr
diagnostics separate and check exit codes. Inspect stderr on a nonzero exit to
distinguish invalid input from an unready index; neither is successful retrieval.
Use the selected Hub data directory and installed command consistently. Record
status again after retrieval and require unchanged index/version provenance.
Do not silently change the index, corpus or installation.

Apply the same preferences, detail lookups, citations and evidence rules below.
Installed help controls flag mapping: `version_filter="compatible_only"` uses
`--compatible-only` with `--pipecat-version`; lifecycle `version` uses
`--at-version`; example `tags` use repeated `--tag`; `include_readme=false` uses
`--no-readme`. The handler-operation table above names internal fields; map them
to verified CLI flags. Never run `refresh`,
`install`, reset/repair, package-manager commands or Cloud commands as recovery.

## Idea workflow

Translate the proposed behaviour into concepts to investigate: for example,
transport, STT, turn handling, LLM and TTS for a conversational agent. State
assumptions briefly. Ask a focused question when a choice materially changes
the recommendation (such as telephone versus browser access, or whether the
agent listens versus only speaks); continue independent concept retrieval
while waiting. Do not invent providers, credentials, latency targets or a
workspace, and do not block useful retrieval on optional build/deploy choices.

Use `search_docs` for concepts and `search_examples` for relevant patterns,
separating multiple concepts with ` + ` or ` & ` (for example `TTS + STT`).
Check that hits cover each requested concept. Read the relevant page/section
with `get_doc` and the relevant example with `get_example`. Use `search_api`
and `get_code_snippet` where a concrete API assertion needs definition evidence.
Check deprecations for Pipecat imports before recommending them.

Return an explanation of how the retrieved pieces could fit the idea, with
citations and unresolved decisions. Label the proposed composition as inference
unless a retrieved example demonstrates it. A TTS-only example does not prove
a complete STT conversation; fetched code is not an executed/validated app.

## Concept workflow

Determine whether the user needs a definition, behaviour, constructor/type,
method signature, or usage example. Start with `search_docs` for explanations
or `search_api` for definitions, then read detail via `get_doc` or
`get_code_snippet`. Add `search_examples`/`get_example` when usage helps.
For exact symbols use matching `chunk_type` (including `type_definition`)
and prefix filters when available, then verify the returned symbol and body.
Read `related_type_defs` with `search_api(query=name,
chunk_type="type_definition")` when needed.

A nonempty search, even with high confidence, may contain only references or
imports of the requested symbol. It is not definition evidence. Verify the
name, path and contents; use a symbol/detail lookup or narrower filter before
claiming success. `get-doc --section` can fall back to the full page: check
that the returned text is actually the requested section. Empty content or
`Not Found` is missing evidence even when `low_confidence` is false.

## Evidence and bounded recovery

Cite returned source URLs beside claims; retain repo/path and commit or line
anchors for code. If only a path is available, identify it as a retrieved
repository path, without fabricating a URL. Distinguish source-supported facts,
your inference and unknowns. Explain what an example demonstrates and which
parts are untested. Scope conclusions to the actual indexed version and source.

On zero-hit, low-confidence or irrelevant results, disclose the gap and try a
small number of targeted follow-ups (such as a direct page, symbol or matching
chunk-type lookup). Do not repeat indefinitely or silently relax strict user
preferences. Missing results do not prove an API does not exist. If the gap
persists, state what remains unknown, offer a focused clarification and follow
the CLI's stderr report/remediation guidance without filing or mutating anything
automatically. End with the grounded answer, citations, material limitations
and the next decision needed; no implementation action follows an idea prompt.
