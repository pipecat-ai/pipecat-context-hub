# Pipecat Context Hub

Local-first MCP server providing fresh Pipecat docs and examples context for
Claude Code, Cursor, VS Code, and Zed.

> **Quick links:**
> [Client Setup](#client-setup) |
> [MCP Tools](#mcp-tools) |
> [Version-Aware Queries](#version-aware-queries) |
> [Environment Variables](#environment-variables) |
> [Report an Issue](https://github.com/pipecat-ai/pipecat-context-hub/issues/new/choose)

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
command below under `pipecat mcp` and can wire up your coding agent for you:

```bash
uv tool install "pipecat-ai[cli]" --with pipecat-ai-context-hub
pipecat mcp install      # register the MCP server, then build the index
```

Or run the hub on its own with [`uv`](https://docs.astral.sh/uv/). `uvx` fetches
and runs it on demand; the first invocation downloads the package and local
models (allow a few minutes).

> **Naming:** the PyPI package is `pipecat-ai-context-hub` (official pipecat
> packages are `pipecat-ai*`); the command and MCP server name are
> `pipecat-context-hub`. Both spellings of the command resolve once installed.

### Inside the Pipecat CLI

Installing this package alongside `pipecat-ai[cli]` mounts it as `pipecat mcp`.
Discovery is dynamic, so this works with a Pipecat CLI that is already
installed — no upgrade needed on that side.

```bash
pipecat mcp --help                          # same commands as below
pipecat mcp refresh
pipecat mcp check-deprecation PipelineTask
```

`pipecat mcp <command>` and `pipecat-context-hub <command>` are equivalent —
same parsing, same JSON, same exit codes. Point MCP clients at
`pipecat-context-hub serve` rather than `pipecat mcp serve`, though: the direct
console script starts the server without loading the Pipecat CLI first.
`pipecat mcp install` does this for you.

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

> **Tip:** When `gh` CLI is authenticated, `refresh` also fetches GitHub release
> notes for deprecation data. Without it, `check_deprecation` coverage will be
> limited.

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

To set a client up manually, point its MCP config at
`uvx pipecat-ai-context-hub serve`. Per-client setup guides:

| Client | Setup Guide |
|--------|-------------|
| **Claude Code** | [docs/setup/claude-code.md](setup/claude-code.md) |
| **Cursor** | [docs/setup/cursor.md](setup/cursor.md) |
| **VS Code** | [docs/setup/vscode.md](setup/vscode.md) |
| **Zed** | [docs/setup/zed.md](setup/zed.md) |

**Example** (Claude Code `.mcp.json`):

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "uvx",
      "args": ["pipecat-ai-context-hub", "serve"],
      "env": {}
    }
  }
}
```

Config templates for all clients are in [`config/clients/`](../config/clients/).

### Add CLAUDE.md Instructions (Recommended)

Add this to your project's `CLAUDE.md` (or `~/.claude/CLAUDE.md` globally) so
your coding agent prefers the MCP tools for Pipecat questions:

```markdown
## MCP Tools

When pipecat-context-hub MCP is available, always prefer its tools
(`search_docs`, `search_api`, `search_examples`, `get_example`, `get_doc`,
`get_code_snippet`, `check_deprecation`) for Pipecat framework questions.
Do not read `.venv` or source files directly.

If the MCP server isn't wired up but the package is installed, the same
tools are CLI subcommands — shell out instead of reading source, e.g.
`uv run pipecat-context-hub check-deprecation PipelineTask` or
`uv run pipecat-context-hub search-docs "TTS + STT"`.

- "How do I ...?" → `search_docs`
- "Show me an example of ..." → `search_examples`, then `get_example`
- Class constructors, method signatures, frame types → `search_api`
- Specific code span or symbol → `get_code_snippet`
- Retrieve a specific doc page → `get_doc`
- Check if an import is deprecated or removed → `check_deprecation`

**Multi-concept queries:** Use ` + ` or ` & ` as delimiters
(e.g., `search_docs("TTS + STT")`). Each concept is searched independently
and results are interleaved.

When suggesting commands for Pipecat projects, always use `uv` as the
package manager:
- Install dependencies: `uv sync` (not `pip install`)
- Run scripts: `uv run python bot.py` (not `python bot.py`)
- Add packages: `uv add <package>` (not `pip install <package>`)
```

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

If your project targets a specific pipecat version, pass `pipecat_version` to
get results scored for compatibility:

```
search_examples("TTS pipeline", pipecat_version="0.0.96", domain="backend")
search_api("DailyTransport", pipecat_version="0.0.96")
```

Results are annotated with `version_compatibility`: `"compatible"`,
`"newer_required"`, `"older_targeted"`, or `"unknown"`. Use
`version_filter="compatible_only"` to exclude results requiring a newer version.

You can also pin the framework index to a specific version:

```bash
uvx pipecat-ai-context-hub refresh --framework-version v0.0.96
# or via env var:
PIPECAT_HUB_FRAMEWORK_VERSION=v0.0.96 uvx pipecat-ai-context-hub refresh
```

## Index Metadata Contract

External tooling — editor plugins, CI checks, the Pipecat CLI — often needs one
cheap answer: *how old is this index, and which pipecat version is it for?* Every
in-process query path opens ChromaDB, so importing the hub or shelling out to
`status` is too slow to run on every invocation of another tool.

The `index_metadata` table in `<data dir>/metadata.db` is therefore a **published
read contract**. Read it directly, read-only, with the standard library:

```python
import os, sqlite3
from pathlib import Path

data_dir = Path(os.environ.get("PIPECAT_HUB_DATA_DIR") or Path.home() / ".pipecat-context-hub")
conn = sqlite3.connect(f"file:{data_dir / 'metadata.db'}?mode=ro", uri=True, timeout=0)
meta = dict(conn.execute("SELECT key, value FROM index_metadata"))
```

The database is WAL, so a reader neither blocks nor is blocked by a concurrent
`refresh` or a running `serve`, and sees only committed state. Use `mode=ro` and
`timeout=0`, and treat any exception as "unknown" — a freshness check must never
fail its caller's command.

Consumers **must** honour `PIPECAT_HUB_DATA_DIR`; assuming
`~/.pipecat-context-hub` will report "no index" at anyone who relocated theirs.

### Contracted keys

| Key | Meaning |
|-----|---------|
| `metadata_contract_version` | Version of this contract (currently `1`). Absent on indexes built before it was published |
| `last_refresh_at` | UTC ISO-8601 timestamp of the last completed refresh |
| `last_refresh_error_count` | Errors in that refresh; `last_refresh_errored_at` is present only when non-zero |
| `indexed_framework_version` | Nearest pipecat release tag the index was built from, e.g. `1.6.0` |
| `indexed_framework_commits_ahead` | Commits from that tag to the indexed revision. `0` means the index *is* that release |
| `framework_version` | The operator's explicit `--framework-version` pin. Absent unless pinned — this is *not* the version the index was built from |
| `repo:<org>/<repo>:commit_sha` | Indexed commit for each source repo |
| `content_type_counts` | JSON object of record counts by content type |

`indexed_framework_version` is a **floor, not an identity**: an unpinned refresh
tracks the default branch, so an index built 55 commits past `v1.6.0` still
reports `1.6.0` with `indexed_framework_commits_ahead: 55`. Compare versions with
that slack in mind, or every developer working from a source checkout gets a
spurious mismatch.

### Compatibility

`metadata_contract_version` is bumped only when the table's shape or a documented
key's meaning changes; adding a key is backwards compatible and does not bump it.
Treat a value **higher** than you understand as "unknown" and stay silent rather
than guessing.

The keys are written on refresh, so an index built by an older hub simply lacks
the newer ones. `last_refresh_at` and `repo:*:commit_sha` predate the contract
and are present on essentially every index in the wild, so a staleness check
works immediately; version comparison starts working after the next refresh.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPECAT_HUB_DATA_DIR` | `~/.pipecat-context-hub` | Index, clones, and metadata location. Consumers of the metadata contract must honour it |
| `PIPECAT_HUB_EXTRA_REPOS` | *(empty)* | Comma-separated repo slugs to ingest alongside defaults |
| `PIPECAT_HUB_FRAMEWORK_VERSION` | *(empty)* | Pin framework repo to a specific git tag (e.g. `v0.0.96`) |
| `PIPECAT_HUB_TAINTED_REPOS` | *(empty)* | Comma-separated repo slugs to skip entirely |
| `PIPECAT_HUB_TAINTED_REFS` | *(empty)* | Comma-separated `org/repo@ref` entries to skip |
| `PIPECAT_HUB_STALE_AFTER_DAYS` | `7` | Index age (days) after which tool responses carry an `index_staleness` field with a refresh hint. `0` disables |
| `PIPECAT_HUB_RERANKER_ENABLED` | `1` | Set to `0` to disable cross-encoder reranking |
| `PIPECAT_HUB_RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Swap reranker model. Allowed: `cross-encoder/ms-marco-MiniLM-L-6-v2` (~80 MB), `cross-encoder/ms-marco-MiniLM-L-12-v2` (~130 MB), `cross-encoder/ms-marco-TinyBERT-L-2-v2` (~17 MB) |
| `PIPECAT_HUB_IDLE_TIMEOUT_SECS` | `1800` | Idle backstop: exit `serve` if no MCP request arrives for this many seconds. **Auto-disabled** when `serve` has reliable client-death detection (direct-parent launch, or `uv run` with a resolvable grandparent) — it would otherwise reap a warm hub mid-session. Stays armed when detection is unavailable (Windows, parent-watch disabled, unresolved grandparent). Set an explicit value (incl. `0`) to override the auto-decision. |
| `PIPECAT_HUB_PARENT_WATCH_INTERVAL` | `2.0` | Hidden tuning knob (primarily for tests): poll interval (seconds) for the parent-death watchdog. Floored at `0.1s` when non-zero. Set to `0` to disable the watchdog. |
| `PIPECAT_HUB_WARMUP` | `1` | Pre-warm embedding + cross-encoder at `serve` boot so the first MCP query doesn't pay the cold-start cost (matters most on Windows CPU, where cold loads can take 30-130s). Set to `0` to skip (faster boot, slower first query). |

See [`.env.example`](../.env.example) for curated repo bundles you can copy
into your `.env`.

## MCP Client Configuration

Two ways to point an MCP client (Claude Code, Cursor, Zed, etc.) at this
hub. Both exit cleanly within ~2s when the client goes away; they differ
only in convenience.

> **Note:** an MCP server is a subprocess the client spawns over stdio — it
> cannot restart *itself*. If you want the hub back after it exits, that is
> the MCP client's job (most clients respawn on the next request). The hub's
> contribution is to (a) stay alive for the whole session and (b) exit
> cleanly so the client can respawn without errors.

**Recommended — persistent install (instant orphan cleanup):**

```bash
uv tool install pipecat-ai-context-hub
```

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "pipecat-context-hub",
      "args": ["serve"]
    }
  }
}
```

The installed `pipecat-context-hub` binary is the immediate child of the
MCP client. When the client dies or restarts, the parent-death watchdog
fires within ~2s and the hub exits cleanly, releasing the Chroma + SQLite
handles. Re-run `uv tool install --upgrade pipecat-ai-context-hub` to
update.

**Alternative — `uvx` (zero install):**

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "uvx",
      "args": ["pipecat-ai-context-hub", "serve"]
    }
  }
}
```

Convenient (nothing to install — `uvx` fetches and runs the package on
demand). `uvx` stays alive as an intermediate parent, so `getppid()` never
flips when the client dies — but `serve` detects the `uvx` launcher and
watches the **grandparent** (the real client) directly, so it still exits
within ~2s of the client going away. The idle timeout is auto-disabled in
this case (it is no longer needed for cleanup), so the hub stays warm
through quiet stretches of an active session. Set
`PIPECAT_HUB_IDLE_TIMEOUT_SECS` if you still want an idle backstop.

## Data Sources

The default index includes:

- **Pipecat documentation** — `docs.pipecat.ai` (200+ pages)
- **Pipecat framework** — `pipecat-ai/pipecat` (Python AST-indexed: classes, methods, imports, call graphs)
- **Pipecat examples** — `pipecat-ai/pipecat-examples` (project-level code examples)
- **Pipecat Flows** — `pipecat-ai/pipecat-flows` (conversation flow framework, Python AST-indexed)
- **Daily Python SDK** — `daily-co/daily-python` (`.pyi` stubs + RST type definitions)
- **TypeScript SDKs** — `pipecat-client-web`, `pipecat-client-web-transports`, `pipecat-client-react-native-transports`, `voice-ui-kit`, `pipecat-prebuilt` (tree-sitter-indexed)

CLI usage (`pipecat init`, `pipecat cloud deploy`) is covered by the indexed `docs.pipecat.ai` pages (`/api-reference/cli/*`); the `pipecat-ai/pipecat-cli` repo itself is opt-in (it adds only CLI-internal source). Add more repos via `PIPECAT_HUB_EXTRA_REPOS` (full `org/repo` slugs, e.g. `pipecat-ai/pipecat-cli`, `pipecat-ai/pipecat-mcp-server`, `pipecat-ai/pipecat-flows-editor`, `pipecat-ai/pipecat-krisp`). Only Python, TypeScript, and RST are parsed — Swift/Kotlin/C++ client SDKs (iOS, Android, cxx, esp32) clone but yield zero source/API chunks (only a few README/config fallback chunks), so they don't surface in `search_api` / `get_code_snippet`.

## Security

- Threat model: [docs/security/threat-model.md](security/threat-model.md)
- Vulnerability reporting: [SECURITY.md](../SECURITY.md)
- Upstream denylisting: `PIPECAT_HUB_TAINTED_REPOS` and `PIPECAT_HUB_TAINTED_REFS`

## Troubleshooting

- **Empty results** — run `uvx pipecat-ai-context-hub refresh` to populate the index
- **Stale results** — run `uvx pipecat-ai-context-hub refresh --force` to re-ingest from latest upstream
- **Index corruption** — run `uvx pipecat-ai-context-hub refresh --force --reset-index` to wipe and rebuild
- **`serve` exits immediately with code 2** — the index is empty or
  unopenable. Run `uvx pipecat-ai-context-hub refresh` (or
  `refresh --force --reset-index` if the error message mentions a failed
  open) and try again. This is deliberate: prior versions started anyway
  and MCP clients hung on every query.
- **Stale `serve` processes** — `serve` polls its parent PID every 2s
  and exits cleanly when the MCP client disappears (look for
  `Shutting down: parent_died original_ppid=… current_ppid=1` in the
  trace). Under `uv run` (where the immediate parent is `uv`, not the
  client), it instead watches the grandparent and logs
  `Shutting down: client_died client_pid=…`. If you still see orphans
  (older versions, an unrecognized lingering launcher, or Windows where
  the watchdog is disabled), `pkill -f "pipecat-context-hub serve"` is
  safe to run between sessions.
- **`Idle watchdog disabled: watching … for client exit` on boot** —
  this `INFO` line is expected and benign. It means `serve` has reliable
  client-death detection (direct parent, or `uv`/`uvx`/`poetry`/… with a
  resolved grandparent), so the idle timeout is switched off to keep the
  hub warm through quiet stretches of an active session. No action
  needed; set `PIPECAT_HUB_IDLE_TIMEOUT_SECS` if you want an idle
  backstop anyway.
- **Diagnosing degraded starts** — on `serve` boot, look for
  `pipecat-context-hub vX.Y.Z starting: …` (`INFO`) to confirm the running
  version and index content-type counts. If reranking is off, a
  `Reranker disabled at startup: reason=…` (`WARNING`) line names the
  cause (`config_disabled` | `not_cached`) and, for `not_cached`, the
  exact HF cache directory probed.

### Windows

- **Refresh appears to hang or returns zero code results** — a prior
  `refresh` may have left a clone half-initialised (common after an
  interrupted run or antivirus quarantine). `pipecat-context-hub` now
  detects this on the next refresh and re-clones automatically; look for
  `Recovered N corrupt clone(s): …` in the summary. As a manual remedy you
  can delete `%LOCALAPPDATA%\pipecat-context-hub\repos\`.
- **`UnicodeEncodeError` in the refresh summary** — the summary table uses
  box-drawing characters that some Windows code pages (cp1252, cp1254,
  etc.) cannot encode. The server falls back to ASCII automatically. To
  opt into the full Unicode output, set `PYTHONIOENCODING=utf-8` before
  invoking `refresh`, or use Windows Terminal (which defaults to UTF-8).

If the server returns poor or missing results, [file a retrieval quality issue](https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=retrieval-quality.yml) —
the issue template includes a diagnostic prompt your coding agent can run to
generate a structured report.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture, development workflow,
benchmarking, and project structure.
