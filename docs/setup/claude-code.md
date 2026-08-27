# Claude Code Setup

Connect Pipecat Context Hub to [Claude Code](https://code.claude.com/) as an MCP server over stdio.

## Prerequisites

- Python 3.11+
- [Claude Code](https://code.claude.com/) installed
- [`uv`](https://docs.astral.sh/uv/) package manager

## Build the Local Index

Build the local index before serving. The first run downloads the package, models, and sources — allow a few minutes:

```bash
uvx pipecat-ai-context-hub refresh
```

This populates `~/.pipecat-context-hub/`.

> **Tip:** with the `gh` CLI authenticated, `refresh` also fetches GitHub release notes for deprecation data; without it, `check_deprecation` coverage is limited.

## Configure

### Option A: `install` (recommended)

```bash
uvx pipecat-ai-context-hub install --client claude-code
```

`uvx` runs the package without installing it persistently, so the bare
`pipecat-context-hub` command above (used elsewhere in this doc, and in
`--print-config` output) is only on `PATH` if you've separately run
`uv tool install pipecat-ai-context-hub` (or installed it into an active venv).
Without a persistent install, prefix every command in this doc with `uvx
pipecat-ai-context-hub` instead of `pipecat-context-hub`.

This shells out to `claude mcp add` for you and then builds the index. The entry
is registered at Claude's `user` scope, so it applies in every directory — one
index serves the whole machine, so there is nothing per-project to scope it to.
An entry that already exists is repaired at whatever scope it currently holds,
so a deliberate `local` or `project` registration stays where it is.

Pass `--no-refresh` to skip the index build, or `--print-config` to see the
config without changing anything.

### Option B: User-level config (all projects)

Add this block to `~/.claude.json` — the hand-edited equivalent of Option A:

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

### Option C: Project-level config (for teams)

Put the same block in `.mcp.json` at the root of your project instead. Prefer
this over Option A only when the registration should be checked into the repo so
teammates pick it up — it costs you the server in every other project.

### Option D: CLI

```bash
claude mcp add -s user pipecat-context-hub -- uvx pipecat-ai-context-hub serve
```

Swap `-s user` for `-s project` to match Option C.

## Recommended CLAUDE.md Instructions

Add these lines to your project's `CLAUDE.md` (or global `~/.claude/CLAUDE.md`) so Claude knows to use the MCP tools for Pipecat questions:

```markdown
## MCP Tools

When pipecat-context-hub MCP is available, always prefer its tools (`search_docs`, `search_api`, `search_examples`, `get_example`, `get_doc`, `get_code_snippet`, `check_deprecation`) for Pipecat framework questions. Do not read `.venv` or source files directly.

If the MCP server isn't wired up but the package is installed, the same tools are CLI subcommands — shell out instead of reading source, e.g. `uv run pipecat-context-hub check-deprecation PipelineTask` or `uv run pipecat-context-hub search-docs "TTS + STT"`.

- "How do I ...?" → `search_docs`
- "Show me an example of ..." → `search_examples`, then `get_example`
- Class constructors, method signatures, frame types → `search_api`
- Specific code span or symbol → `get_code_snippet`
- Retrieve a specific doc page → `get_doc`
- Check if an import is deprecated or removed → `check_deprecation`

**Multi-concept queries:** Use ` + ` or ` & ` as delimiters (e.g., `search_docs("TTS + STT")`). Each concept is searched independently and results are interleaved.

When suggesting commands for Pipecat projects, always use `uv` as the package manager:
- Install dependencies: `uv sync` (not `pip install`)
- Run scripts: `uv run python bot.py` (not `python bot.py`)
- Add packages: `uv add <package>` (not `pip install <package>`)
```

## Verify

1. Start Claude Code in your project directory.
2. Claude Code will detect the MCP config and prompt you to approve the server on first use.
3. Ask Claude a question about Pipecat — the server's tools should appear in the tool list.

You can also verify the server starts correctly from the command line:

```bash
# Check that the serve command is available
uvx pipecat-ai-context-hub serve --help

# Test stdin/stdout communication (sends an MCP initialize request)
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1.0"}}}' | uvx pipecat-ai-context-hub serve
```

## Troubleshooting

- **Server not detected**: `install` (Option A) registers at Claude's `user`
  scope by default, so first check `claude mcp list` or `~/.claude.json` for
  the `pipecat-context-hub` entry. Only if you deliberately used Option C
  (project-scoped, for teams) should you check `.mcp.json` at the project root
  (not inside `.claude/`) instead.
- **Command not found**: Ensure `uv` is installed and on your PATH (`uvx` ships with `uv`).
- **Empty results**: Run `uvx pipecat-ai-context-hub refresh` to populate the index.
