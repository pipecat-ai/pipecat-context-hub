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
command below under `pipecat context-hub` and can wire up your coding agent for you:

```bash
uv tool install "pipecat-ai[cli]" --with pipecat-ai-context-hub
pipecat context-hub install      # register the MCP server, then build the index
```

Or run the hub on its own with [`uv`](https://docs.astral.sh/uv/). `uvx` fetches
and runs it on demand; the first invocation downloads the package and local
models (allow a few minutes).

> **Naming:** the PyPI package is `pipecat-ai-context-hub` (official pipecat
> packages are `pipecat-ai*`); the command and MCP server name are
> `pipecat-context-hub`. Both spellings of the command resolve once installed.

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

Without a pin, every repo is indexed at its default branch (`main`). Pass
`latest` to index the framework's newest release tag instead:

```bash
uvx pipecat-ai-context-hub refresh --framework-version latest
```

`latest` re-resolves on every refresh, so setting
`PIPECAT_HUB_FRAMEWORK_VERSION=latest` in your MCP client's `env` block tracks
releases as they ship — a plain incremental `refresh` picks up a new release
without `--force`. Prereleases are skipped unless the repo has nothing else.

Note that a pin applies only to the framework repo (`pipecat-ai/pipecat`).
The examples, flows, and client SDK repos always track their default branch,
and docs come from the live `docs.pipecat.ai`, which is unversioned.

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

All contract keys written at the end of a single refresh (contract version,
timestamps, upsert/error counts, `framework_version`, and the
`indexed_framework_version`/`indexed_framework_commits_ahead` pair) are written
in one transaction, so a reader never observes a partially-updated related-key
set — e.g. a new `indexed_framework_version` can never be paired with a stale
`indexed_framework_commits_ahead` from a previous refresh.

### Contracted keys

| Key | Meaning |
|-----|---------|
| `metadata_contract_version` | Version of this contract (currently `1`). Absent on indexes built before it was published |
| `last_refresh_at` | UTC ISO-8601 timestamp of the last completed refresh |
| `last_refresh_error_count` | Errors in that refresh; `last_refresh_errored_at` is present only when non-zero |
| `indexed_framework_version` | Nearest pipecat release tag the index was built from, e.g. `1.6.0` |
| `indexed_framework_commits_ahead` | Commits from that tag to the indexed revision. `0` means the index *is* that release |
| `framework_version` | The operator's explicit `--framework-version` pin, recorded verbatim — so a `latest` pin stores `latest`, not the tag it resolved to. Absent unless pinned — this is *not* the version the index was built from |
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
| `PIPECAT_HUB_FRAMEWORK_VERSION` | *(empty)* | Pin framework repo to a specific git tag (e.g. `v0.0.96`), or `latest` for its newest release tag |
| `PIPECAT_HUB_TAINTED_REPOS` | *(empty)* | Comma-separated repo slugs to skip entirely |
| `PIPECAT_HUB_TAINTED_REFS` | *(empty)* | Comma-separated `org/repo@ref` entries to skip |
| `PIPECAT_HUB_STALE_AFTER_DAYS` | `7` | Index age (days) after which tool responses carry an `index_staleness` field with a refresh hint. `0` disables |
| `PIPECAT_HUB_RERANKER_ENABLED` | `1` | Set to `0` to disable cross-encoder reranking |
| `PIPECAT_HUB_RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Swap reranker model. Allowed: `cross-encoder/ms-marco-MiniLM-L-6-v2` (~91 MB), `cross-encoder/ms-marco-MiniLM-L-12-v2` (~134 MB), `cross-encoder/ms-marco-TinyBERT-L-2-v2` (~18 MB) |
| `PIPECAT_HUB_IDLE_TIMEOUT_SECS` | `1800` | Idle backstop: exit `serve` if no MCP request arrives for this many seconds. **Auto-disabled** when `serve` has reliable client-death detection (direct-parent launch, or `uv run` with a resolvable grandparent) — it would otherwise reap a warm hub mid-session. Stays armed when detection is unavailable (Windows, parent-watch disabled, unresolved grandparent). Set an explicit value (incl. `0`) to override the auto-decision. |
| `PIPECAT_HUB_PARENT_WATCH_INTERVAL` | `2.0` | Hidden tuning knob (primarily for tests): poll interval (seconds) for the parent-death watchdog. Floored at `0.1s` when non-zero. Set to `0` to disable the watchdog. |
| `PIPECAT_HUB_WARMUP` | `1` | Pre-warm embedding + cross-encoder at `serve` boot so the first MCP query doesn't pay the cold-start cost (matters most on Windows CPU, where cold loads can take 30-130s). Set to `0` to skip (faster boot, slower first query). |

See [`.env.example`](../.env.example) for curated repo bundles you can copy
into your `.env`, and [`config.toml.example`](../config.toml.example) for the
machine-global equivalent described below.

### Config precedence: env vars, `.env`, and `config.toml`

Every entry point (the CLI, the `pipecat context-hub` Typer bridge, `serve`,
and the dashboard scripts) resolves `PIPECAT_HUB_*` settings from three
layers, first-writer-wins, in this order:

1. **Real environment variables** — highest precedence, e.g. exported from
   your shell profile or set in an MCP client's `env` block.
2. **`.env` in the current working directory** — project-local, loaded on
   every invocation from `Path.cwd()`.
3. **`~/.config/pipecat-context-hub/config.toml`** — machine-global, read
   only if present, for settings that should apply everywhere without being
   re-exported per shell or duplicated into every project's `.env`. Uses the
   same `PIPECAT_HUB_*` keys as flat TOML values (a homogeneous array of
   scalars, e.g. `PIPECAT_HUB_EXTRA_REPOS = ["org/repo-a", "org/repo-b"]`, is
   accepted and coerced to the same comma-separated form as the env-var
   form). See [`config.toml.example`](../config.toml.example).
   Keep it private — `chmod 600 ~/.config/pipecat-context-hub/config.toml`.
   Its values go straight into the process environment, so on POSIX systems a
   world-writable file, or one owned by another user, is ignored with a
   warning; a group-writable one is still loaded, but warned about.

Anything not set in any layer falls back to `HubConfig`'s field defaults
(the table above).

A project `.env` always outranks `config.toml` — that's true for every
invocation, not just a normal project-local CLI run. In particular, if an
MCP client happens to launch `serve` with its working directory set to a
project checkout that has its own `.env`, that `.env` shadows `config.toml`
for that session, the same as it would for a direct CLI invocation from that
directory. Whether a given MCP client passes a project `cwd` at all, or a
fixed/empty one, is client-specific and not something this project controls;
`serve` logs `cwd=... env_keys=...` at startup (`INFO` level) so you can
confirm which directory — and therefore which `.env`, if any — was in effect
for a given session. The path is home-redacted to `~/…`, and only key *names*
are logged, never values, so the line is safe to paste into a bug report.

Windows: the lookup path is `%USERPROFILE%\.config\pipecat-context-hub\config.toml`
in the common case, but that's a description of where `Path.home()` usually
resolves, not a separate rule — `Path.home()` reads `USERPROFILE`, falls back
to `HOMEDRIVE`+`HOMEPATH` if that's unset, and roaming or domain-managed
profiles can relocate the home directory entirely. This deliberately diverges
from this project's usual Windows convention for the index/repo cache
(`%LOCALAPPDATA%`) — `config.toml`'s location follows `Path.home() / ".config"`
on every platform, matching its POSIX/macOS path structure rather than
Windows convention, so the same relative path (`.config/pipecat-context-hub/config.toml`)
works everywhere `Path.home()` resolves.

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
- **Index corruption** — run `uvx pipecat-ai-context-hub refresh --force --reset-index` to wipe and rebuild.
  Only the data directory is removed: your machine-global
  `~/.config/pipecat-context-hub/config.toml` and any project `.env` are
  preserved, and the reset aborts with an error rather than deleting a
  `PIPECAT_HUB_DATA_DIR` that happens to contain either one — including the
  case where a project `.env` sets `PIPECAT_HUB_DATA_DIR=.`, which would
  otherwise make the working tree holding that `.env` the deletion target.
- **`--prune` / `PIPECAT_HUB_PRUNE`** — `refresh` no longer deletes
  previously-indexed data for a repo that isn't configured *for this
  invocation* by default; it only warns
  (`Repo <slug> not configured in this run; leaving N indexed record(s) in
  place — pass --prune to remove`) and leaves the records and metadata in
  place, since the repo may still be configured elsewhere (e.g. in
  `config.toml` but shadowed by a narrower project `.env`). Pass `--prune`,
  or set `PIPECAT_HUB_PRUNE=1`, to actually delete that data. This var is
  invocation-scoped: it is not read from `config.toml` (see [Config
  precedence](#config-precedence-env-vars-env-and-configtoml) above) and is
  intentionally excluded from `config.toml.example` and the Environment
  Variables table above. Tainted repos
  (`PIPECAT_HUB_TAINTED_REPOS`) are unaffected — they are always cleaned up,
  with or without `--prune`.
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

- **Where to report what** — you shouldn't usually need to look this up: both
  front doors surface it themselves. The one-shot CLI prints a remediation-first
  hint on stderr (never on stdout, so piped JSON stays clean) whenever a
  semantic command returns poor/empty results or the reranker model is
  uncached; `serve`'s MCP `initialize` response gives a connecting agent the
  same guidance. If you're filing directly: poor or missing search results go
  to [retrieval-quality.yml](https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=retrieval-quality.yml)
  (the template includes a diagnostic prompt your coding agent can run to
  generate a structured report); crashes, incorrect behavior, or anything else
  go to [bug-report.yml](https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=bug-report.yml).

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

### Can't see which config a `serve` session actually used

`serve` logs `cwd=… env_keys=…` at `INFO` on every boot (home-redacted, key
names only), which tells you which directory — and therefore which `.env`, if
any — shadowed `config.toml` for that session. If your MCP client hides the
server's stderr entirely, set `PIPECAT_HUB_DEBUG_PROBE=1` in the client's
`env` block: `serve` appends the same evidence to
`~/.cache/pipecat-context-hub/serve-debug.log` on each boot. This is a
troubleshooting fallback, not a setting — it is deliberately absent from
`config.toml.example` and the Environment Variables table, and is ignored when
set in `config.toml` (a machine-global file must never persistently enable a
disk-writing probe). Unset it once you've read the file.

### Pointing the hub at a different `config.toml`

`PIPECAT_HUB_CONFIG_FILE=/path/to/config.toml` overrides the lookup path for
the machine-global config file — useful for testing a config before installing
it, or for running one invocation against a different profile. Like
`PIPECAT_HUB_DEBUG_PROBE` above, it is invocation-scoped rather than a setting:
it is deliberately absent from `config.toml.example` and the Environment
Variables table, and is ignored when set *inside* `config.toml` (honouring it
from within the file it locates would be circular). A `~`-rooted value is
expanded; a value that isn't a usable path is ignored with a warning and **no**
config file is loaded — the hub will not quietly fall back to the default
location when you asked for a specific file. Setting it to an *empty* value
(`PIPECAT_HUB_CONFIG_FILE=`) is treated the same way: it disables the lookup
for that invocation (warning, no config loaded). Unset the variable entirely
to go back to the default location.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture, development workflow,
benchmarking, and project structure.
