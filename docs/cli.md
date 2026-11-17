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

## Audit commands (session feedback)

`audit` reviews how coding agents used Context Hub in real sessions. It reads local transcripts
(Claude Code, Codex, pi; other formats are only text-scanned), works offline, and **sends nothing
unless you run `gist --yes`**.

```bash
pipecat-context-hub audit extract -i ~/.claude/projects/<project> --out ./hub-audit --since 2026-11-18   # --anonymize: participant-N labels
# read ./hub-audit/share/report.md (what would be shared), then choose one:
pipecat-context-hub audit gist ./hub-audit --yes      # secret gist: unlisted, NOT private (refuses a bundle with nothing to share unless --allow-empty)
#   ...or send ./hub-audit/share.tar.gz yourself (email, chat), or:
pipecat-context-hub audit discard ./hub-audit --yes   # deletes only files `extract` wrote
pipecat-context-hub audit repack ./hub-audit          # after editing share/ by hand: re-validates rows, re-counts, regenerates report.md, re-checks for leaks
pipecat-context-hub audit ingest a.tar.gz b.tar.gz --out ./merged   # organizers
```

The `share/` bundle carries tool names, queries, result shapes (hit counts, confidence), flags and
latency, with opaque ids; transcript text, shell commands and paths are dropped or replaced. A shell
call is shared only as the hub command it ran, rebuilt from its parsed argv and re-quoted (inert data:
organizers rebuild a call from it by hand and never execute it); a line using a construct the parser
does not model (`$'...'`, `${...}`, `$((...))`, a pipe or here-string into a shell, ...) is still
counted but shares no command text. A Codex JS batch call shares only its literal arguments. A field
named like a credential (`api_key`, `token`...) is replaced whole, and so is a labelled value in
free text (`password: ...`, `--api-key ...`, `Authorization: Basic ...`, URL userinfo); a number of
32+ digits is replaced too. Redaction is best-effort and queries are your own words, so read
`report.md` first.

Before anything is kept or posted, `extract`, `repack` and `gist` run a leak check over the
finished files: JSON is checked decoded (every string, key and repeated key, so a `\u0073k-...`
escape cannot hide a token), and a hit refuses the whole bundle, naming the file and rule but never
the matched text. "Nothing to share" means no call rows, no session that used Context Hub and no
session that did Pipecat work without it; `gist` refuses such a bundle unless `--allow-empty`, and
leaves out a `calls.jsonl` or `sessions.jsonl` with no rows (a file holding only whitespace counts
as empty; a gist cannot hold an empty file). `gist`, `repack` and `ingest` read a share/ directory
the same way (no symlinked directory or file, size limits) and `extract`, `repack`, `gist` and
`ingest` validate a bundle with the same rules, including the shape of `manifest.json`'s `analysis`
and each call's `latency_s` and `input`, so a bundle `repack` accepts is one `ingest` accepts; after
a hand edit, run `repack` before `gist`. A refused `extract` or `repack` writes nothing: no file is
kept or rewritten until the whole bundle has passed, and no output is ever written through a
symlink. `gist` posts the bytes it checked, and refuses a `gh` found in the current directory.

`ingest` treats bundles as untrusted: tarballs are read in memory as a stream with fixed limits
(archive size, member count, member size, total bytes), only the five known filenames are accepted
once each, and rows are validated. Merged rows are namespaced by their source bundle, so one bundle
cannot shadow another's; the same bundle given twice merges once, and two bundles carrying the same
ref or session are both kept and reported in `ingest.json`. The `context-hub-session-audit` skill
(`.claude/skills/`) adds the organizer's judgment rubric.

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
