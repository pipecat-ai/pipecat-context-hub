# Pipecat Context Hub

Give your AI coding assistant source-grounded Pipecat documentation, API definitions
and working examples.

Context Hub indexes Pipecat documentation and selected repositories locally, then
makes them searchable through MCP tools or the command line. Use it to understand
APIs, find implementation examples and check deprecated imports.

**Supported platforms:** macOS and Linux. Windows currently supports installation
and help commands; indexing and semantic retrieval require a POSIX host.

## Quick Start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install
Context Hub alongside the Pipecat CLI:

```bash
uv tool install "pipecat-ai[cli]" --with pipecat-ai-context-hub
pipecat context-hub install
```

The setup command registers detected Claude Code and Codex clients and builds the
local index. For Cursor, VS Code and Zed, it prints configuration instructions.
The first index build downloads sources and local models and may take a few
minutes. Restart your coding assistant afterwards.

See the [setup guides](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/setup/README.md)
for standalone installation, client configuration and verification.

## Use It

Ask your assistant questions such as:

- “Find a WebSocket voice-agent example.”
- “Show me the API for DailyTransport.”
- “Is this Pipecat import deprecated?”

The tools search documentation, examples and API source, returning source
citations. To search multiple concepts, use ` + `:

```text
search_examples("idle timeout + function calling")
```

You can also query directly from your terminal:

```bash
pipecat context-hub search-api "DailyTransport" --limit 3
pipecat context-hub check-deprecation PipelineTask
```

Refresh the local index when you need updated sources:

```bash
pipecat context-hub refresh
```

## Documentation

- [MCP tools, filters and search responses](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/tools.md)
- [CLI commands and exit codes](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/cli.md)
- [Configuration, indexed sources and additional repositories](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/configuration.md)
- [Version-aware queries](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/version-aware-queries.md)
- [Index metadata contract](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/index-contract.md)
- [Cross-encoder reranking](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/reranking.md)
- [Troubleshooting](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/troubleshooting.md)
- [Optional Setup, Explore, Build and Deploy skills plugin](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/plugins/pipecat-context-hub/README.md)
- [Contributing](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/CONTRIBUTING.md) · [Changelog](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/CHANGELOG.md)
- [Security policy](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/SECURITY.md) · [Threat model](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/security/threat-model.md) · [Report an issue](https://github.com/pipecat-ai/pipecat-context-hub/issues/new/choose)
