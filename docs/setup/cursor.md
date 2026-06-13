# Cursor Setup

Connect Pipecat Context Hub to [Cursor](https://cursor.com/) as an MCP server over stdio.

## Prerequisites

- Python 3.11+
- [Cursor](https://cursor.com/) installed
- [`uv`](https://docs.astral.sh/uv/) package manager

## Build the Local Index

Build the local index before serving. The first run downloads the package, models, and sources — allow a few minutes:

```bash
uvx pipecat-ai-context-hub refresh
```

This populates `~/.pipecat-context-hub/`.

## Configure

### Option A: Project-level config (recommended)

Create `.cursor/mcp.json` in your project root:

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

### Option B: Global config (all projects)

Create or edit `~/.cursor/mcp.json` (same format as above).

## Verify

1. Open your project in Cursor.
2. Open Cursor Settings > MCP to confirm `pipecat-context-hub` appears and shows a green status.
3. In the AI chat, ask a question about Pipecat — the server's tools should be invoked automatically.

You can also verify the server starts correctly from the command line:

```bash
uvx pipecat-ai-context-hub serve --help
```

## Troubleshooting

- **Server not appearing**: Ensure `.cursor/mcp.json` exists in your project root directory.
- **Command not found**: Ensure `uv` is installed and on your PATH (`uvx` ships with `uv`).
- **Empty results**: Run `uvx pipecat-ai-context-hub refresh` to populate the index.
- **Red status indicator**: Check the Cursor MCP logs for error details.
