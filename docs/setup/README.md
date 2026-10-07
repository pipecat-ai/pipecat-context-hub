# Client Setup Guides

Pipecat Context Hub is a local-first MCP server that provides fresh Pipecat documentation and code examples to your AI-powered IDE. It communicates over **stdio** — your client spawns the server process and talks to it via stdin/stdout.

## Supported Clients

| Client | Config File | Guide |
|--------|-------------|-------|
| [Claude Code](claude-code.md) | `.mcp.json` (project root) | [Setup guide](claude-code.md) |
| [Cursor](cursor.md) | `.cursor/mcp.json` | [Setup guide](cursor.md) |
| [VS Code](vscode.md) | `.vscode/mcp.json` | [Setup guide](vscode.md) |
| [Zed](zed.md) | `~/.config/zed/settings.json` | [Setup guide](zed.md) |

## Quick Start

Install [`uv`](https://docs.astral.sh/uv/), then follow three steps (the same for every client):

1. **Build** the local index
2. **Add** the MCP server config to your client (see the client-specific guide)
3. **Verify** the server responds

```bash
# 1. Build the local index — first run downloads the package, models, and
#    sources (a few minutes)
uvx pipecat-ai-context-hub refresh

# 2. Add config — see the client-specific guide

# 3. Verify
uvx pipecat-ai-context-hub serve --help
```

> **Naming:** the PyPI package is `pipecat-ai-context-hub` (official pipecat packages are `pipecat-ai*`); the command and MCP server name are `pipecat-context-hub`. Both spellings of the command resolve once installed.

The optional [skills-only Pipecat plugin](../../plugins/pipecat-context-hub/README.md)
provides Setup, grounded Explore, local Build and approved Cloud Deploy workflows.
It uses `pipecat context-hub` or standalone `pipecat-context-hub` through native
execution, with no bundled MCP connection. Plugin installation supplies skills
and assets; ask "Set up Pipecat Context Hub" to check prerequisites and perform
only authorised missing-runtime and initial-index setup. Full-plugin setup also
covers required Pipecat/Cloud CLIs and Cloud account signup/login. Healthy
installations/indexes are reused, and existing standalone MCP clients are unchanged.
Clean local/cloud onboarding and autonomous activation of the CLI-first release
remain unqualified. The earlier stdio experiment's local build and approved READY
Cloud evidence remain dated in the [evaluation report](../evaluations/pipecat-context-hub-plugin.md); they do not
prove fresh-user, provider/session or ChatGPT Work support for this release.

## How It Works

The MCP server runs as a subprocess of your IDE. When your AI assistant needs Pipecat context, it calls MCP tools exposed by the server. The server queries its local index (populated by `uvx pipecat-ai-context-hub refresh`) and returns relevant documentation and code snippets.

```
IDE/Agent  ←stdio→  pipecat-context-hub serve  ←→  Local index (~/.pipecat-context-hub/)
```

No network requests are made during tool calls — all data is served from the local index.
