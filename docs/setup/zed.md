# Zed Setup

Connect Pipecat Context Hub to [Zed](https://zed.dev/) as an MCP server over stdio. MCP tools are available in Zed's Agent panel.

## Prerequisites

- Python 3.11+
- [Zed](https://zed.dev/) with Agent panel support
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

Zed uses a global settings file — there is no project-level MCP config.

Edit `~/.config/zed/settings.json` (open with `zed: open settings` from the command palette) and add the `context_servers` entry:

```json
{
  "context_servers": {
    "pipecat-context-hub": {
      "source": "custom",
      "command": "pipecat-context-hub",
      "args": ["serve"],
      "env": {}
    }
  }
}
```

> A ready-to-use template is available at [`config/clients/zed.json`](../../config/clients/zed.json). Merge its contents into your existing `settings.json`.

**Note:** Zed uses `"context_servers"` (not `"mcpServers"`) and requires `"source": "custom"` for manually configured servers.

## Verify

1. Open Zed and open the Agent panel.
2. The server should appear in the MCP server list. Check for any error indicators.
3. Ask the agent a question about Pipecat — the server's tools should be invoked.

You can also verify the server starts correctly from the command line:

```bash
pipecat-context-hub serve --help
```

## Troubleshooting

- **Server not appearing**: Ensure the `context_servers` key is at the top level of `settings.json` and that `"source": "custom"` is included.
- **Command not found**: Make sure `pipecat-context-hub` is on your `PATH`. If installed with `uv tool`, run `uv tool list` to confirm.
- **Empty results**: Run `pipecat-context-hub refresh` to populate the index.
- **JSON parse errors**: Zed's `settings.json` contains other settings — make sure you merge the `context_servers` block rather than replacing the entire file.
