---
name: build
description: Build and verify a local Pipecat app after an explicit build request, using native local execution, installed CLI capabilities and grounded Context Hub APIs.
---

A build request authorises local project work within the chosen workspace.
An idea/concept discussion alone authorises only exploration. Skills do not
grant execution permissions. This workflow requires native local shell/file
execution and the installed Pipecat CLI; it does not use a shell MCP adapter.
If either is absent, report build unavailable and retain working exploration.
Cloud account access is not required for local build. Deployment is a separate
workflow; hand off to deploy for read-only preparation and a concrete approval
payload. Do not upload secrets, deploy or start remote agent sessions here.

## Gate and resolve choices

1. Confirm this chat has native local execution using a harmless shell command.
   Inspect `pipecat --version` and `pipecat init --help`. If unavailable, explain
   the missing capability without installing/upgrading tools automatically.
   Use `pipecat init --list-options` only if advertised; parse its JSON and
   retain the actual service identifiers and transport combinations. Installed
   help/options take precedence over newer docs. Never assume `--eval`, inferred
   bot type, config keys or flags that this version does not advertise.
2. Resolve material user choices before generation: selected project directory
   and name, browser versus telephony, transport, cascade versus realtime,
   providers, client requirements, intended behaviour and target framework
   version. Ask focused questions for missing choices; do not invent providers,
   credentials or use a qualification fixture as user defaults. Map choices to
   actual CLI identifiers. Explain unsupported combinations rather than
   silently substituting them. Clarify optional recording/video/observability
   choices when relevant. For a local build, disable Cloud file generation when
   supported; explain if this CLI requires those files anyway.
3. Inspect the selected output directory and the actual project-name child the
   CLI will create, including hidden entries, without reading existing `.env`
   files. Refuse scaffolding into any nonempty destination, existing app or
   symlink to one. Do not delete files to make it empty or force overwrites.
   For an existing project, adapt its current structure and dependency lock
   within the requested scope; do not re-scaffold. A new app needs an explicitly
   selected absent/empty directory. Recheck immediately before generation.

## Dry run, scaffold and ground customization

Build one explicit argument list from supported options. On CLI 1.3.0,
noninteractive mode uses `--name` or `--config`; `--bot-type` is required in
observed probes, and `--client-framework none` means no client. These are dated
compatibility facts, not permanent defaults. If using JSON `--config`, discover
its schema through installed documentation or a valid dry run; do not assume
`--list-options` is a config schema. Inspect resolved JSON, not just exit status.

Run the supported `--dry-run` with the same selections and output path as the
planned scaffold. Verify resolved name, bot type, transports, mode, providers,
client and optional features; ensure no files were created. Resolve errors or
unexpected defaults before proceeding. If this version lacks a safe dry run,
report that gate unavailable instead of silently generating. Then recheck the
empty destination and scaffold once. A failed/partial scaffold becomes an
existing directory: inspect and fix within it, never blindly re-run generation.

Use generated layout, entrypoint, environment template, dependency constraints
and lock rather than rewriting them from a newer example. If no version pin or
lock is generated, say so, install using the generated constraints and retain
the resolved lock/version. Do not silently upgrade pins to match indexed APIs.
Read required key **names** from generated placeholders and source; never print
secret values or read existing `.env` files. Credentials are supplied by the user
through their local workflow, not invented or copied into reports.

Before Pipecat API customization, use only the packaged connection
`pipecat-context-hub-chatgpt-plugin`: call `get_hub_status`, inspect current tool
schemas, retrieve definitions/examples, and cite returned source URLs. Separate
multiple concepts with ` + ` or ` & `. Search/detail filters are tool-specific,
as described in explore. Verify definitions rather than reference-only hits.
Check relevant deprecations. Pass the app's version where supported, but explain
that compatibility annotations do not select a different indexed snapshot.
Unknown compatibility or a version mismatch needs local validation; strict
unavailable-version requirements remain unresolved. Never refresh/reset/repair
or change the index. Preserve generated architecture and make only the requested
behaviour changes justified by retrieved APIs. Record before/after Hub provenance.

## Verify locally and report honestly

Install the generated project's local dependencies with its package manager,
respecting existing pins/lock. Inspect its documented entrypoint/help before
launching. Use bounded, project-appropriate syntax/import, startup and behaviour
checks. Isolate checks from existing credentials (`.env` loading disabled and a
clean environment); placeholder strings satisfy only local construction when
appropriate, never authenticate a provider. Avoid provider API calls, microphone
sessions or browser conversations unless separately requested and authorised.
Stop local processes after checks and record startup errors and exit codes.

Use upstream eval support only if discovered in this installed CLI; otherwise
name the explicit local verification method. Credential-free context/handler
checks or doubles can verify a narrow application behaviour, but cannot prove
STT/LLM/TTS service integration. If startup cannot run without credentials or
network, report the blocker and the checks that did run. A local HTTP health
response proves server startup, not an end-to-end conversation. Imports, mocks,
resolved configuration and generated files are distinct evidence.

Return project path, selected choices, CLI/framework versions, generated and
resolved pins, files customized with sources, exact check outcomes and remaining
credential/provider/client validation. Build unavailable, partially verified,
locally verified and conversation-verified are different results. Missing optional
shell/CLI does not disable exploration. Do not claim a live conversation from
imports or mocks, or claim revised plugin activation from a fresh rendered copy.
