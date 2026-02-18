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

All clients follow the same general steps:

1. **Install** the package
2. **Populate** the local index
3. **Add** the MCP server config to your client
4. **Verify** the server responds

```bash
# 1. Install
uv tool install pipecat-context-hub
# or: pip install pipecat-context-hub

# 2. Populate the local index
pipecat-context-hub refresh

# 3. Add config — see the client-specific guide

# 4. Verify
pipecat-context-hub serve --help
```

## Config Templates

Pre-built config templates are available in [`config/clients/`](../../config/clients/):

- [`claude-code.json`](../../config/clients/claude-code.json) — copy to `.mcp.json`
- [`cursor.json`](../../config/clients/cursor.json) — copy to `.cursor/mcp.json`
- [`vscode.json`](../../config/clients/vscode.json) — copy to `.vscode/mcp.json`
- [`zed.json`](../../config/clients/zed.json) — merge into `~/.config/zed/settings.json`

## How It Works

The MCP server runs as a subprocess of your IDE. When your AI assistant needs Pipecat context, it calls MCP tools exposed by the server. The server queries its local index (populated by `pipecat-context-hub refresh`) and returns relevant documentation and code snippets.

```
IDE/Agent  ←stdio→  pipecat-context-hub serve  ←→  Local index (~/.pipecat-context-hub/)
```

No network requests are made during tool calls — all data is served from the local index.
