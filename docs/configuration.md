# Configuration

Part of the [Pipecat Context Hub README](README.md). This file owns the `PIPECAT_HUB_*` variable table, the three-layer config precedence, and how to add repos to the index.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPECAT_HUB_DATA_DIR` | `~/.pipecat-context-hub` | Index, clones, and metadata location. Consumers of the metadata contract must honour it |
| `PIPECAT_HUB_EXTRA_REPOS` | *(empty)* | Comma-separated repo slugs to ingest alongside defaults (case-insensitive: a case variant of a slug already listed is ignored, the first spelling wins) |
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

## Managing repos from the CLI

`pipecat-context-hub repos` (also `pipecat context-hub repos`) edits only the
`PIPECAT_HUB_EXTRA_REPOS` key of `~/.config/pipecat-context-hub/config.toml`,
preserving comments (including those on and between array entries), other keys
and CRLF line endings; a removed entry's own trailing comment goes with it, and
a comma-separated string value is rewritten as an array. It works offline; run
`pipecat-context-hub refresh` afterwards to index (or `refresh --prune` to drop
data after a remove). Under a shadowing layer (rule 3, with `--config-only`) the
reminder says the change is not in effect there, and warns against
`refresh --prune` with that layer in force: it would delete the indexed data of
every repo `config.toml` lists that the layer hides.

```bash
pipecat-context-hub repos list                       # effective repos, tagged default / catalog / unlisted
pipecat-context-hub repos list --groups              # ...plus the catalog group names for --group
pipecat-context-hub repos add pipecat-ai/tail        # a catalog repo
pipecat-context-hub repos add --group nvidia         # every parseable, non-tainted repo of a catalog group
pipecat-context-hub repos add owner/repo --unlisted  # a well-formed repo that is not in the catalog
pipecat-context-hub repos remove pipecat-ai/tail     # also --group NAME, --unlisted
```

Rules, applied in this order to a positional slug (the first that applies decides; exit code 1 on refusal):

1. A default repo, or one in `PIPECAT_HUB_TAINTED_REPOS`, is refused.
2. A malformed slug is refused (`owner/repo`, each part starting with a letter or digit, then letters, digits, `.`, `_`, `-`: the grammar `refresh` clones); a slug outside the catalog needs `--unlisted`; a catalog slug with `--unlisted` is refused (the catalog spelling is used).
3. If `PIPECAT_HUB_EXTRA_REPOS` is set by a real environment variable or the cwd `.env`, that layer outranks `config.toml`, so the change would have no effect and is refused, naming the layer. `--config-only` edits `config.toml` anyway (it is then in the file but not in the effective list, and `repos list` names the shadowing layer).
4. Adding a slug already in `config.toml`, or removing one that is not, exits 0 (`Already in config.toml` / `Not in config.toml`). Slugs compare case-insensitively.
5. Otherwise the file is written atomically (`Added <slug>` / `Removed <slug>`).

`--group` (mutually exclusive with a slug and `--unlisted`) skips tainted and `parseable = false` members and reports them; it applies rules 3 to 5 (a group with nothing left to act on does not read `config.toml`). Its output is one line per member: `Added <slug>` or `Already in config.toml: <slug>` (`add`), `Removed <slug>` (`remove`, members not in the file are not listed), then `Nothing to add` / `Nothing to remove` when no member changed (with the shadowing layer named if one overrides the key), then `Skipped (tainted): <slug>` / `Skipped (parseable = false): <slug>` for each skipped member. It exits 0 whenever nothing is refused, including a group whose every member is skipped. An unknown group lists the valid ones. If the catalog fails to load, `add`/`remove` refuse and change nothing; `repos list` still works and tags every non-default repo `unlisted`. An untrusted, invalid or non-array `config.toml` makes `add`/`remove` exit 1 with the file untouched, and so does a filesystem error while writing it; `repos list` prints the effective list, names the ignored file and exits 0. `add` never creates a new `config.toml` the loader would then ignore or never open (for example through a symlink planted by another account, in a world-writable directory, through a `..` after a missing component, also inside a symlink's target, or through a dangling symlink whose target ends in `/`), and it re-checks where the path lands just before the atomic rename, refusing with the file untouched if a symlink moved in between. `repos list` also prints the cwd whose `.env` was consulted, since a `serve` launched from another directory reads a different one.

## Repo catalog

Curated public repos you can add beyond the defaults, via `PIPECAT_HUB_EXTRA_REPOS`. This section is generated from `src/pipecat_context_hub/data/repo_catalog.toml` (run `just catalog-regen`); a test fails if it drifts from the catalog or from `.env.example`.

<!-- BEGIN GENERATED: repo catalog (edit data/repo_catalog.toml, run `just catalog-regen`) -->
Last audited 2026-10-16. Public repos only. A repo marked *no source chunks* clones but yields nothing to search (Swift/Kotlin/C++ are not parsed).

| Group | Kind | Description | Repos |
|-------|------|-------------|-------|
| `client-sdks` | topic | Client SDKs and transports for web, mobile and native | `pipecat-ai/pipecat-client-ios` *(no source chunks)*<br>`pipecat-ai/pipecat-client-ios-daily` *(no source chunks)*<br>`pipecat-ai/pipecat-client-ios-small-webrtc` *(no source chunks)*<br>`pipecat-ai/pipecat-client-android` *(no source chunks)*<br>`pipecat-ai/pipecat-client-android-transports` *(no source chunks)*<br>`pipecat-ai/pipecat-client-cxx` *(no source chunks)*<br>`pipecat-ai/pipecat-esp32` *(no source chunks)*<br>`pipecat-ai/pipecat-client-ios-gemini-live-websocket-demo` *(no source chunks)*<br>`pipecat-ai/pipecat-client-ios-openai-realtime-webrtc-demo` *(no source chunks)* |
| `voice-ui-kit` | topic | Legacy Voice UI Kit (npm package, superseded by the default pipecat-ai/pipecat-ui) | `pipecat-ai/voice-ui-kit` |
| `flows` | topic | Conversation flows and structured dialogues | `pipecat-ai/pipecat-flows-editor` |
| `dev-tools` | topic | Cloud, CLI and developer tools | `pipecat-ai/tail`<br>`pipecat-ai/whisker`<br>`pipecat-ai/skills` |
| `audio-and-benchmarks` | topic | Audio, turn detection and benchmarks | `pipecat-ai/smart-turn`<br>`pipecat-ai/stt-benchmark`<br>`pipecat-ai/gb-benchmarks` |
| `example-apps` | topic | Example apps and demos | `pipecat-ai/peekaboo`<br>`pipecat-ai/pipecat-mcp-server`<br>`pipecat-ai/pipecat-music-player`<br>`pipecat-ai/yc-voice-agents-hackathon`<br>`pipecat-ai/pipecat-deepgram-webinar-demo` |
| `gradient-bang` | topic | Gradient Bang ecosystem | `pipecat-ai/gradient-bang`<br>`pipecat-ai/gb-context-viewer` |
| `nvidia` | vendor | NVIDIA Nemotron speech and voice agents | `NVIDIA-AI-Blueprints/nemotron-voice-agent`<br>`pipecat-ai/nemotron-january-2026`<br>`pipecat-ai/nemotron-voicechat-dgx-spark`<br>`pipecat-ai/nemo3-battleships` |
| `google` | vendor | Google Gemini Live | `pipecat-ai/gemini-live-web-starter`<br>`askjohngeorge/askjg-demo-gemini-pcc`<br>`daily-co/pcc-gemini-screen-voice-ui-kit` |
| `aws` | vendor | AWS Bedrock, SageMaker and Nova Sonic | `aws-samples/build-intelligent-ai-voice-agents-with-pipecat-and-amazon-bedrock`<br>`pipecat-ai/aws-workshops`<br>`pipecat-ai/aws-deepgram-workshop`<br>`deepgram/rxconnect-deepgram-pipecat-sagemaker-demo`<br>`chadbailey59/aws-telephony-bot`<br>`aws-solutions-library-samples/guidance-for-voice-agents-on-aws`<br>`aws-samples/sample-voice-agent-on-aws` |
| `tavus` | vendor | Tavus avatars | `aws-samples/sample-voice-ai-tavus-avatar-demo` |
| `murf` | vendor | Murf text-to-speech | `murf-ai/pipecat-murf-tts` |
| `supermemory` | vendor | Supermemory long-term memory | `supermemoryai/pipecat-memory` |
| `lip-sync-characters` | topic | Visemes and multi-character pipelines (personal projects, may be rewritten or archived without notice) | `jptaylor/pipecat-visemes`<br>`jptaylor/pipecat-one-pipeline-two-characters` |
| `community-projects` | topic | Community voice-bot projects (personal projects, may be rewritten or archived without notice) | `chadbailey59/caracal`<br>`chadbailey59/agent-voice-bot`<br>`finchvox/finchvox`<br>`aaronsu11/Dum-E` |
<!-- END GENERATED: repo catalog -->
