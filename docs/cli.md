# Command-Line Guide

Every MCP tool is also a one-shot CLI command. Queries use the same handlers as
MCP, print JSON to stdout and send logs and warnings to stderr.

## Query commands

| MCP tool | CLI command |
|---|---|
| `search_docs` | `search-docs` |
| `get_doc` | `get-doc` |
| `search_examples` | `search-examples` |
| `get_example` | `get-example` |
| `search_api` | `search-api` |
| `get_code_snippet` | `get-code-snippet` |
| `check_deprecation` | `check-deprecation` |
| `get_hub_status` | `status` |

```bash
uvx pipecat-ai-context-hub status
uvx pipecat-ai-context-hub search-api "DailyTransport" --limit 3
uvx pipecat-ai-context-hub search-docs "TTS + STT"
uvx pipecat-ai-context-hub get-doc --path /guides/learn/transports
uvx pipecat-ai-context-hub check-deprecation PipelineTask --at-version 2.0.0
```

Use `<command> --help` for its options. See [MCP tools](tools.md) for filters,
multi-concept queries and response fields, and [version-aware queries](version-aware-queries.md)
for compatibility scoring. `status` and `check-deprecation` do not load models;
semantic queries do.

`refresh` reads Pipecat's machine-readable `scripts/deprecations/deprecations.json`
(and, when present, `scripts/deprecations/removals.json`) from the indexed
framework checkout. Deprecation checks do not require GitHub CLI authentication.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Invalid input |
| `2` | Index missing, empty, unreadable or incompatible |
| `75` | A refresh is rewriting the index; retry in 60–120 seconds |

These are query exit codes. See [troubleshooting](troubleshooting.md) for refresh
failures, index recovery and lock contention.

## Inside the Pipecat CLI

Installing this package alongside `pipecat-ai[cli]` mounts it as `pipecat context-hub`,
with `pipecat ch` as a shorter alias (hidden from `pipecat --help`, but equivalent).
Discovery is dynamic, so this works with a Pipecat CLI that is already
installed — no upgrade needed on that side.

```bash
pipecat context-hub --help                          # same Context Hub commands
pipecat context-hub refresh
pipecat context-hub check-deprecation PipelineTask
```

`pipecat context-hub <command>` and `pipecat-context-hub <command>` are equivalent —
same parsing, same JSON, same exit codes. Point MCP clients at this package
directly rather than at `pipecat context-hub serve`, though, so starting the server does
not load the Pipecat CLI first. `pipecat context-hub install` works out the right command
for your setup — note that a `--with` co-install exposes only the Pipecat CLI's
own scripts, so `pipecat-context-hub` is importable but not on `PATH`.
