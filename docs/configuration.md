# Configuration

Part of the [Pipecat Context Hub README](README.md). This file owns the `PIPECAT_HUB_*` variable table, the three-layer config precedence, and how to add repos to the index.

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
[Config precedence](#config-precedence-env-vars-env-and-configtoml). Reranker model choices:
[Cross-Encoder Reranking](reranking.md).

Settings resolve from real environment variables, then a project `.env`, then
`~/.config/pipecat-context-hub/config.toml` (first writer wins). Details and examples:
[Config precedence](#config-precedence-env-vars-env-and-configtoml).

## Config precedence: env vars, `.env`, and `config.toml`

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

## Adding repos

CLI usage (`pipecat init`, `pipecat cloud deploy`) is covered by the indexed `docs.pipecat.ai` pages (`/api-reference/cli/*`); the `pipecat-ai/pipecat-cli` repo is archived (the CLI now ships inside `pipecat-ai/pipecat`, which is indexed). Add more repos via `PIPECAT_HUB_EXTRA_REPOS` (full `org/repo` slugs, e.g. `pipecat-ai/pipecat-mcp-server`, `pipecat-ai/pipecat-flows-editor`). Only Python, TypeScript, and RST are parsed — Swift/Kotlin/C++ client SDKs (iOS, Android, cxx, esp32) clone but yield zero source/API chunks (only a few README/config fallback chunks), so they don't surface in `search_api` / `get_code_snippet`.
