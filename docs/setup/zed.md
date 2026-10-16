# Zed Setup

Connect Pipecat Context Hub to [Zed](https://zed.dev/) as an MCP server over stdio. MCP tools are available in Zed's Agent panel.

## Prerequisites

- Python 3.11+
- [Zed](https://zed.dev/) with Agent panel support
- [`uv`](https://docs.astral.sh/uv/) package manager

## Build the Local Index

Run the populated-index workflow on POSIX (macOS or Linux). Windows installs
can inspect help and package metadata, but refresh and semantic index
operations fail closed.

Build the local index before serving. The first run downloads the package, models, and sources — allow a few minutes:

```bash
uvx pipecat-ai-context-hub refresh
```

This populates `~/.pipecat-context-hub/`.

## Configure

Zed uses a global settings file — there is no project-level MCP config.

Edit `~/.config/zed/settings.json` (open with `zed: open settings` from the command palette):

```json
{
  "context_servers": {
    "pipecat-context-hub": {
      "source": "custom",
      "command": "uvx",
      "args": ["pipecat-ai-context-hub", "serve"],
      "env": {}
    }
  }
}
```

**Note:** Zed uses `"context_servers"` (not `"mcpServers"`) and requires `"source": "custom"` for manually configured servers.

## Verify

1. Open Zed and open the Agent panel.
2. The server should appear in the MCP server list. Check for any error indicators.
3. Ask the agent a question about Pipecat — the server's tools should be invoked.

You can also inspect the server command from the command line (this is only a
help check, not an MCP handshake):

```bash
uvx pipecat-ai-context-hub serve --help
```

## Troubleshooting

- **Server not appearing**: Ensure the `context_servers` key is at the top level of `settings.json` and that `"source": "custom"` is included.
- **Command not found**: Ensure `uv` is installed and on your PATH (`uvx` ships with `uv`).
- **Empty results**: On macOS or Linux, run `uvx pipecat-ai-context-hub refresh` to populate the index. Windows populated refresh and semantic index operations are unsupported; run the refresh on a POSIX machine instead.
- **JSON parse errors**: Zed's `settings.json` contains other settings — make sure you merge the `context_servers` block rather than replacing the entire file.
