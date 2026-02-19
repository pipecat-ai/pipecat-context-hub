# Cursor Setup

Connect Pipecat Context Hub to [Cursor](https://cursor.com/) as an MCP server over stdio.

## Prerequisites

- Python 3.11+
- [Cursor](https://cursor.com/) installed
- `uv` (recommended) or `pip`

## Install

```bash
# Option A: uv (recommended — installs into an isolated environment)
uv tool install pipecat-context-hub

# Option B: pip
pip install pipecat-context-hub
```

## Populate the Local Index

Before the server can answer queries, populate the local index:

```bash
pipecat-context-hub refresh
```

This downloads Pipecat docs and example repos to `~/.pipecat-context-hub/`.

## Configure

### Option A: Project-level config (recommended)

Create `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "pipecat-context-hub",
      "args": ["serve"],
      "env": {}
    }
  }
}
```

> A ready-to-use template is available at [`config/clients/cursor.json`](../../config/clients/cursor.json).

### Option B: Global config (all projects)

Create or edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "pipecat-context-hub",
      "args": ["serve"],
      "env": {}
    }
  }
}
```

## Verify

1. Open your project in Cursor.
2. Open Cursor Settings > MCP to confirm `pipecat-context-hub` appears and shows a green status.
3. In the AI chat, ask a question about Pipecat — the server's tools should be invoked automatically.

You can also verify the server starts correctly from the command line:

```bash
pipecat-context-hub serve --help
```

## Troubleshooting

- **Server not appearing**: Ensure `.cursor/mcp.json` exists in your project root directory.
- **Command not found**: Make sure `pipecat-context-hub` is on your `PATH`. If installed with `uv tool`, run `uv tool list` to confirm.
- **Empty results**: Run `pipecat-context-hub refresh` to populate the index.
- **Red status indicator**: Check the Cursor MCP logs for error details. The most common cause is the command not being found on `PATH`.
