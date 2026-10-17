# Pipecat Context Hub

Local-first MCP server providing fresh Pipecat docs and examples context for
Claude Code, Cursor, VS Code, and Zed.

> **Quick links:**
> [Client Setup](#client-setup) |
> [MCP Tools](#mcp-tools) |
> [Version-Aware Queries](version-aware-queries.md) |
> [Troubleshooting](troubleshooting.md) |
> [Environment Variables](#environment-variables) |
> [Report an Issue](https://github.com/pipecat-ai/pipecat-context-hub/issues/new/choose)

> **Platform support:** macOS and Linux. On Windows the package installs and the
> CLI help runs, but `refresh`, `serve` and semantic queries currently fail closed
> (see the [Platform contract](#platform-contract)). Windows support is planned:
> [dev plan](dev_plans/20261015-feature-windows-index-locking.md).

## What It Does

When your AI coding assistant needs Pipecat context, it calls MCP tools exposed
by this server. The server queries a local index (ChromaDB + SQLite FTS5) and
returns relevant documentation, code examples, and API source — all with source
citations.

```
IDE/Agent  ←stdio→  pipecat-context-hub serve  ←→  Local index (~/.pipecat-context-hub/)
```

## Install

The quickest path is to co-install with the Pipecat CLI, which then hosts every
command below under `pipecat context-hub` and can wire up your coding agent for you:

```bash
uv tool install "pipecat-ai[cli]" --with pipecat-ai-context-hub
pipecat context-hub install      # register the MCP server, then build the index
```

Or run the hub on its own with [`uv`](https://docs.astral.sh/uv/) — install it
first if you don't already have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS/Linux
# or: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows
```

Once `uv` is on `PATH`, `uvx` fetches and runs the hub on demand; the first
invocation downloads the package and local models (allow a few minutes).

> **Naming:** the PyPI package is `pipecat-ai-context-hub` (official pipecat
> packages are `pipecat-ai*`); the command and MCP server name are
> `pipecat-context-hub`. Both spellings of the command resolve once installed.

The optional [skills-only Pipecat plugin](../plugins/pipecat-context-hub/README.md)
provides Setup, grounded Explore, local Build and approved Cloud Deploy workflows.
It uses `pipecat context-hub` or standalone `pipecat-context-hub` through native
execution, with no bundled MCP connection. Plugin installation supplies skills
and assets; ask "Set up Pipecat Context Hub" to check prerequisites and perform
only authorised missing-runtime and initial-index setup. Full-plugin setup also
covers required Pipecat/Cloud CLIs and Cloud account signup/login. Healthy
installations/indexes are reused, and existing standalone MCP clients are unchanged.
Clean local/cloud onboarding and autonomous activation of the CLI-first release
remain unqualified. The earlier stdio experiment's local build and approved READY
Cloud evidence remain dated in the [evaluation report](evaluations/pipecat-context-hub-plugin.md); they do not
prove fresh-user, provider/session or ChatGPT Work support for this release.

### Platform contract

The persisted Chroma/SQLite index and semantic retrieval path currently require
POSIX descriptor-relative, no-reparse filesystem operations. On Windows the
package and path/ingest helpers remain installable, but `refresh`, `serve` and
semantic queries fail closed with an index-safety error rather than using an
unsafe pathname-only fallback. Do not treat the Windows CI package smoke as
index support; use a POSIX host for a populated local index.

### Inside the Pipecat CLI

Installing this package alongside `pipecat-ai[cli]` mounts it as `pipecat context-hub`,
with `pipecat ch` as a shorter alias (hidden from `pipecat --help`, but equivalent).
Discovery is dynamic, so this works with a Pipecat CLI that is already
installed — no upgrade needed on that side.

```bash
pipecat context-hub --help                          # same commands as below
pipecat context-hub refresh
pipecat context-hub check-deprecation PipelineTask
```

`pipecat context-hub <command>` and `pipecat-context-hub <command>` are equivalent —
same parsing, same JSON, same exit codes. Point MCP clients at this package
directly rather than at `pipecat context-hub serve`, though, so starting the server does
not load the Pipecat CLI first. `pipecat context-hub install` works out the right command
for your setup — note that a `--with` co-install exposes only the Pipecat CLI's
own scripts, so `pipecat-context-hub` is importable but not on `PATH`.

## Populate the Local Index

Before the server can answer queries, build the local index:

```bash
# First-time setup (downloads docs, clones repos, computes embeddings)
uvx pipecat-ai-context-hub refresh

# Force full re-ingest (ignores cached state)
uvx pipecat-ai-context-hub refresh --force

# Recover from an unhealthy local index
uvx pipecat-ai-context-hub refresh --force --reset-index
```

> **Tip:** `refresh` reads Pipecat's machine-readable
> `scripts/deprecations/deprecations.json` (and, when present,
> `scripts/deprecations/removals.json`) from the indexed framework checkout.
> `check_deprecation` therefore does not require GitHub CLI authentication.

## Start the Server

Run `refresh` at least once first (see above). `serve` exits with code `2`
if the index is empty or cannot be opened — it will not start against an
unusable index, since MCP clients would otherwise hang on zero-hit queries.

```bash
uvx pipecat-ai-context-hub serve
```

## Client Setup

`install` does this for you where the client ships a CLI:

```bash
pipecat-context-hub install                     # every detected client, then refresh
pipecat-context-hub install --client codex      # just one
pipecat-context-hub install --print-config      # show the config, change nothing
```

Claude Code and Codex are registered through their own CLI, so they own the edit
to their config file. Cursor, VS Code, and Zed are configured by hand, so
`install` prints the exact JSON and where it goes rather than writing to a file
it does not own. MCP servers are read at session start — restart your agent
afterwards.

Claude Code registrations default to `user` scope; Codex has no scope concept. Scope
rules, a manual config example and the CLAUDE.md snippet that makes your agent prefer
these tools are in the [client setup guides](setup/README.md). To set a client up
manually, point its MCP config at `uvx pipecat-ai-context-hub serve`; for a persistent
install and how `serve` exits when the client goes away, see
[server lifecycle](setup/server-lifecycle.md). Per-client guides:

| Client | Setup Guide |
|--------|-------------|
| **Claude Code** | [docs/setup/claude-code.md](setup/claude-code.md) |
| **Cursor** | [docs/setup/cursor.md](setup/cursor.md) |
| **VS Code** | [docs/setup/vscode.md](setup/vscode.md) |
| **Zed** | [docs/setup/zed.md](setup/zed.md) |

## MCP Tools

| Tool | Use when... |
|------|-------------|
| `search_docs` | "How do I ...?" — conceptual questions, guides, configuration |
| `get_doc` | Retrieve a specific doc page by ID or path (e.g. `/guides/learn/transports`) |
| `search_examples` | "Show me an example of ..." — find working code by task or component |
| `get_example` | Retrieve full source files for a specific example |
| `search_api` | Class definitions, method signatures, frame types, inheritance |
| `get_code_snippet` | Get targeted code by symbol name, intent, or file path + line range |
| `check_deprecation` | Verify whether a pipecat import path is deprecated or removed (supports `--at-version` for lifecycle at a specific version) |
| `get_hub_status` | Index health, reranker runtime state, record counts, framework version, commit SHAs |

All search results include an **EvidenceReport** with confidence scores,
source-grounded facts, unresolved questions, and suggested follow-up queries.

### Filters

`search_examples` supports filters to narrow results:

- `domain` — `"backend"` (Python), `"frontend"` (JS/TS), `"config"`, `"infra"`
- `language` — `"python"`, `"typescript"`
- `repo` — filter by GitHub repo slug
- `tags` — filter by capability tags

`search_api` supports filters for framework internals:

- `module` — module path prefix (e.g. `"pipecat.services"`)
- `class_name` — class name prefix (e.g. `"DailyTransport"`)
- `chunk_type` — `"method"`, `"function"`, `"class_overview"`, `"module_overview"`, `"type_definition"`
- `yields` — methods that yield a specific frame type
- `calls` — methods that call a specific method

### Multi-Concept Queries

Use ` + ` or ` & ` to search for multiple concepts at once:

```
search_docs("TTS + STT")
search_examples("idle timeout + function calling + Gemini")
search_api("BaseTransport + WebSocketTransport")
```

Each concept is searched independently and results are interleaved for balanced
coverage.

## Version-Aware Queries

Pass `pipecat_version` to `search_examples`, `search_api` or `get_code_snippet` to get
results scored for compatibility with your project's pipecat version; results carry
`version_compatibility`. Details, filters and examples:
[Version-Aware Queries](version-aware-queries.md).

## Index Metadata Contract

External tooling can read how old the index is and which pipecat version it covers
straight from the `index_metadata` table in `<data dir>/metadata.db`, a versioned,
read-only contract. Keys, compatibility rules and a copy-paste example:
[Index Metadata Contract](index-contract.md).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPECAT_HUB_DATA_DIR` | `~/.pipecat-context-hub` | Index, clones, and metadata location. Consumers of the metadata contract must honour it |
| `PIPECAT_HUB_EXTRA_REPOS` | *(empty)* | Comma-separated repo slugs to ingest alongside defaults |
| `PIPECAT_HUB_FRAMEWORK_VERSION` | `latest` | Pin the framework repo to a specific git tag (e.g. `v0.0.96`), `latest` for its newest release tag, or `head` (or `main`) for its default branch |
| `PIPECAT_HUB_TAINTED_REPOS` | *(empty)* | Comma-separated repo slugs to skip entirely |
| `PIPECAT_HUB_TAINTED_REFS` | *(empty)* | Comma-separated `org/repo@ref` entries to skip |
| `PIPECAT_HUB_STALE_AFTER_DAYS` | `7` | Index age (days) after which tool responses carry an `index_staleness` field with a refresh hint. `0` disables |
| `PIPECAT_HUB_RERANKER_ENABLED` | `1` | Set to `0` to disable cross-encoder reranking |
| `PIPECAT_HUB_RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Swap reranker model. Allowed: `cross-encoder/ms-marco-MiniLM-L-6-v2` (~91 MB), `cross-encoder/ms-marco-MiniLM-L-12-v2` (~134 MB), `cross-encoder/ms-marco-TinyBERT-L-2-v2` (~18 MB) |
| `PIPECAT_HUB_IDLE_TIMEOUT_SECS` | `1800` | Idle backstop: exit `serve` if no MCP request arrives for this many seconds. **Auto-disabled** when `serve` has reliable client-death detection (direct-parent launch, or `uv run` with a resolvable grandparent) — it would otherwise reap a warm hub mid-session. Stays armed when detection is unavailable (Windows, parent-watch disabled, unresolved grandparent). Set an explicit value (incl. `0`) to override the auto-decision. |
| `PIPECAT_HUB_PARENT_WATCH_INTERVAL` | `2.0` | Hidden tuning knob (primarily for tests): poll interval (seconds) for the parent-death watchdog. Floored at `0.1s` when non-zero. Set to `0` to disable the watchdog. |
| `PIPECAT_HUB_WARMUP` | `1` | Pre-warm embedding + cross-encoder at `serve` boot so the first MCP query doesn't pay the cold-start cost on supported POSIX hosts. Set to `0` to skip (faster boot, slower first query). |

See [`.env.example`](../.env.example) for curated repo bundles (by topic and by
vendor: NVIDIA, Google, AWS, other providers) you can copy into your `.env`, and [`config.toml.example`](../config.toml.example) for the
machine-global equivalent described in
[Config precedence](configuration.md). Reranker model choices:
[Cross-Encoder Reranking](reranking.md).

Settings resolve from real environment variables, then a project `.env`, then
`~/.config/pipecat-context-hub/config.toml` (first writer wins). Details and examples:
[Config precedence](configuration.md#config-precedence-env-vars-env-and-configtoml).

## Data Sources

The default index includes:

- **Pipecat documentation** — `docs.pipecat.ai` (200+ pages)
- **Pipecat framework** — `pipecat-ai/pipecat` (Python AST-indexed: classes, methods, imports, call graphs; includes Pipecat Flows as `pipecat.flows` plus its `examples/flows/`)
- **Pipecat examples** — `pipecat-ai/pipecat-examples` (project-level code examples)
- **Daily Python SDK** — `daily-co/daily-python` (`.pyi` stubs + RST type definitions)
- **TypeScript SDKs** — `pipecat-client-web`, `pipecat-client-web-transports`, `pipecat-client-react-native-transports`, `pipecat-ui`, `pipecat-prebuilt` (tree-sitter-indexed)

CLI usage (`pipecat init`, `pipecat cloud deploy`) is covered by the indexed `docs.pipecat.ai` pages (`/api-reference/cli/*`); the `pipecat-ai/pipecat-cli` repo is archived (the CLI now ships inside `pipecat-ai/pipecat`, which is indexed). Add more repos via `PIPECAT_HUB_EXTRA_REPOS` (full `org/repo` slugs, e.g. `pipecat-ai/pipecat-mcp-server`, `pipecat-ai/pipecat-flows-editor`). Only Python, TypeScript, and RST are parsed — Swift/Kotlin/C++ client SDKs (iOS, Android, cxx, esp32) clone but yield zero source/API chunks (only a few README/config fallback chunks), so they don't surface in `search_api` / `get_code_snippet`.

## Security

- Threat model: [docs/security/threat-model.md](security/threat-model.md)
- Vulnerability reporting: [SECURITY.md](../SECURITY.md)
- Upstream denylisting: `PIPECAT_HUB_TAINTED_REPOS` and `PIPECAT_HUB_TAINTED_REFS`

## Troubleshooting

- **Empty results** — run `uvx pipecat-ai-context-hub refresh` to populate the index
- **Stale results** — run `uvx pipecat-ai-context-hub refresh --force` to re-ingest from latest upstream
- **Anything else** (index corruption and `--reset-index`, `--prune`, low disk, a busy
  refresh lock, `serve` exits, Windows, config debugging): see
  [Troubleshooting](troubleshooting.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture, development workflow,
benchmarking, and project structure.
