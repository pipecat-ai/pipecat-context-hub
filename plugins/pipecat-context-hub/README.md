# Local Pipecat Context Hub plugin

This optional desktop experiment packages read-only exploration with the
installed Context Hub. It does not bundle Python, Pipecat CLI or an index.
Build and Cloud workflows are pending later phases.

## Prepare a local copy

Install Context Hub separately and have a populated local index. Find its
absolute Python command with `pipecat-context-hub install --print-config`
(this prints configuration without registering a client). Run that Python:

```sh
"/absolute/installed/hub/python" -P plugins/pipecat-context-hub/scripts/prepare_local.py \
  /absolute/local-marketplace/plugins/pipecat-context-hub
```

The destination must be outside this checkout and absent or empty. The
renderer checks importability, copies only named package files and skill
entrypoints, preserves `mcp.template.json`, and creates root `mcp.json` with
the installed interpreter’s bare executable name, `PATH` containing only its
absolute interpreter directory, and `-P -m pipecat_context_hub serve`.
Interpreter symlinks retain virtual-environment identity. The portable loader
rejects an absolute `command`; the pinned `PATH` provides no ambient/system
Python fallback. Extra launch fields or environment values are refused. The packaged
MCP server is named `pipecat-context-hub-chatgpt-plugin`, distinct from the
standalone `pipecat-context-hub` registration. The renderer rejects templates
containing any other server entries. It does not
register the package or refresh the index. Never register the unresolved
template. No machine-specific launch paths belong in this source package.

## Install and verify separately

Follow [OpenAI's local marketplace instructions](https://developers.openai.com/plugins/build/plugins).
In the local marketplace root create `.agents/plugins/marketplace.json`:

```json
{
  "name": "pipecat-hub-local",
  "interface": {"displayName": "Local Pipecat Hub"},
  "plugins": [{
    "name": "pipecat-context-hub",
    "source": {"source": "local", "path": "./plugins/pipecat-context-hub"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity"
  }]
}
```

Use `codex plugin marketplace add /absolute/local-marketplace` to add that
source; inspect installed `codex plugin --help` for supported commands.
Install from the desktop Plugins Directory (or `codex plugin add
pipecat-context-hub@pipecat-hub-local` if supported), then open a new local
chat after refreshing/restarting the host as required. Confirm the plugin
and `explore` skill are discovered, invoke exploration, and observe its own
MCP initialize and `get_hub_status` calls through
`pipecat-context-hub-chatgpt-plugin`. A subprocess smoke or an existing
Hub registration cannot prove this plugin activated in the desktop.

Record retrieval, execution and Cloud prerequisites independently in
`docs/evaluations/pipecat-context-hub-plugin.md` in the
[source repository](https://github.com/pipecat-ai/pipecat-context-hub).
Evaluation reports stay in repository documentation and are not copied into
the installed plugin. Local stdio support is host-dependent.
Discussion-only hosts cannot promise local file creation. Exploration needs
neither Pipecat CLI nor Cloud credentials; later build work additionally
needs local shell and Pipecat CLI, and Cloud deployment needs its optional
CLI/account setup. Discover versions and flags from installed help.

After changing the plugin or installed interpreter, render a fresh destination
and use the host's documented marketplace refresh/reinstall flow. Disable or
remove this plugin using the host's plugin controls; remove its marketplace
only when no other plugins need it. Keep the Hub installation and index for
other clients. Never remove unrelated registrations.
