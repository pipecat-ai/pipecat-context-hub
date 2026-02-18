# Pipecat Context Hub

Local-first MCP server providing fresh Pipecat docs and examples context for Claude Code, Cursor, VS Code, and Zed.

## Quick Start

```bash
uv pip install -e ".[dev]"
pipecat-context-hub refresh   # index docs + examples
```

Then add the server to your MCP client config. See `docs/setup/` for per-client guides.
