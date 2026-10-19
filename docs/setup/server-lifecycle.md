# MCP Client Configuration and Server Lifecycle

Part of the [client setup guides](README.md).

Two ways to point an MCP client (Claude Code, Cursor, Zed, etc.) at this
hub. Both exit cleanly within ~2s when the client goes away; they differ
only in convenience.

This is standard MCP stdio config — the `command`/`args` pair below works
with *any* MCP-compatible client, not just the ones named above. If your
client isn't in that list (including an unfamiliar coding agent you've
pointed at this repo), adapt the same `command`/`args` values to wherever
that client keeps its own MCP server config; `pipecat-context-hub install
--print-config` prints this exact JSON without writing anything, if you'd
rather generate it than copy it by hand.

> **Note:** an MCP server is a subprocess the client spawns over stdio — it
> cannot restart *itself*. If you want the hub back after it exits, that is
> the MCP client's job (most clients respawn on the next request). The hub's
> contribution is to (a) stay alive for the whole session and (b) exit
> cleanly so the client can respawn without errors.

**Recommended — persistent install (instant orphan cleanup):**

```bash
uv tool install pipecat-ai-context-hub
```

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "pipecat-context-hub",
      "args": ["serve"]
    }
  }
}
```

The installed `pipecat-context-hub` binary is the immediate child of the
MCP client. When the client dies or restarts, the parent-death watchdog
fires within ~2s and the hub exits cleanly, releasing the Chroma + SQLite
handles. Re-run `uv tool install --upgrade pipecat-ai-context-hub` to
update.

**Alternative — `uvx` (zero install):**

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "uvx",
      "args": ["pipecat-ai-context-hub", "serve"]
    }
  }
}
```

Convenient (nothing to install — `uvx` fetches and runs the package on
demand). `uvx` stays alive as an intermediate parent, so `getppid()` never
flips when the client dies — but `serve` detects the `uvx` launcher and
watches the **grandparent** (the real client) directly, so it still exits
within ~2s of the client going away. The idle timeout is auto-disabled in
this case (it is no longer needed for cleanup), so the hub stays warm
through quiet stretches of an active session. Set
`PIPECAT_HUB_IDLE_TIMEOUT_SECS` if you still want an idle backstop.
