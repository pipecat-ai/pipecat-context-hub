"""The ``install`` command: register the MCP server with a coding agent.

Setting the hub up by hand is four steps across two tools — install the package,
find the client's MCP config, write the right JSON, run the first refresh — and
the config location differs per client. This collapses that to one command.

Clients that ship their own CLI (Claude Code, Codex) are configured through it,
so they own the merge into their config file; this command never edits those
files itself. For editors configured by hand, it prints the exact JSON and where
to put it rather than writing into a file it does not own.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed-arg, timeout-guarded client CLI registration
import sys
from typing import Any

import click

# MCP server name registered with the client. Matches the command name, and the
# name the agent guides tell coding agents to look for.
_SERVER_NAME = "pipecat-context-hub"

# Clients configured by shelling out to their own CLI: name -> executable.
_CLI_CLIENTS = {"claude-code": "claude", "codex": "codex"}

# Exit code for "nothing was configured automatically; the config was printed to paste".
# Distinct from success because a caller cannot see the difference otherwise, and from
# failure because the command did everything it could for that client.
_EXIT_MANUAL_SETUP = 3

# Clients configured by hand-editing a JSON file: name -> where it lives.
_FILE_CLIENTS = {
    "cursor": "~/.cursor/mcp.json (global) or .cursor/mcp.json (per project)",
    "vscode": ".vscode/mcp.json",
    "zed": "~/.config/zed/settings.json (under `context_servers`)",
}


def _server_command() -> list[str]:
    """The command a client should run to start the MCP server.

    Always names this interpreter, never the ``pipecat-context-hub`` console script.
    The config outlives the shell that wrote it and is read by a different process at
    session start — often one launched from a GUI, with no shell PATH at all — so a
    bare script name defers resolution to an environment this command cannot see. An
    absolute interpreter pins the client to the hub that was actually installed: the
    Pipecat CLI's bundled copy when invoked through ``pipecat context-hub``, the
    standalone tool when invoked directly. It also pins the version, where ``uvx``
    would resolve the latest published one at every server start.

    Never ``pipecat context-hub serve``: that would load typer and the Pipecat CLI's
    plugin machinery on every start for no benefit.
    """
    return [sys.executable, "-m", "pipecat_context_hub", "serve"]


def _mcp_json(command: list[str], client: str | None = None) -> str:
    """Render the MCP client config block for *command*, shaped for *client*.

    Each file-configured client expects a different top-level schema (see
    ``docs/setup/*.md``). *client* selects which one to render:

    - ``"vscode"`` -> ``{"servers": {name: {"type": "stdio", ...}}}``
    - ``"zed"`` -> ``{"context_servers": {name: {"source": "custom", ...}}}``
    - anything else (including ``"cursor"`` and no client detected) ->
      ``{"mcpServers": {name: {...}}}``
    """
    entry: dict[str, Any] = {"command": command[0], "args": command[1:], "env": {}}

    if client == "vscode":
        block: dict[str, Any] = {"servers": {_SERVER_NAME: {"type": "stdio", **entry}}}
    elif client == "zed":
        block = {"context_servers": {_SERVER_NAME: {"source": "custom", **entry}}}
    else:
        block = {"mcpServers": {_SERVER_NAME: entry}}

    return json.dumps(block, indent=2)


def _detect_cli_clients() -> list[str]:
    """Names of CLI-configurable clients present on PATH."""
    return [name for name, exe in _CLI_CLIENTS.items() if shutil.which(exe)]


def _run_client(exe: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a client CLI subcommand, or None when the executable could not be run."""
    try:
        return subprocess.run(  # nosec B603 - argv from _CLI_CLIENTS allowlist, no shell
            [exe, *args], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        click.echo(f"  failed to run {exe}: {exc}", err=True)
        return None


def _already_registered(exe: str, command: list[str]) -> bool:
    """Whether *exe* already records this exact server command.

    Looks for our own command in the client's output rather than parsing that output's
    shape. The question is only "is the registered entry already this one?", so a client
    that changes how it prints costs a redundant rewrite rather than a broken server.
    """
    completed = _run_client(exe, ["mcp", "get", _SERVER_NAME])
    if completed is None or completed.returncode != 0:
        # Not registered, or a client CLI with no `get`. Nothing to compare against.
        return False
    return all(part in completed.stdout for part in command)


def _register_with_cli(client: str, command: list[str]) -> bool:
    """Register the server via *client*'s own CLI. True when it succeeded.

    Replaces a mismatched entry rather than skipping it: ``mcp add`` refuses a name it
    already has and leaves whatever is there, so a registration written by an earlier
    release would otherwise survive every reinstall. Removing first is safe only because
    an entry that already matches returns before we get here.
    """
    exe = _CLI_CLIENTS[client]
    if _already_registered(exe, command):
        # No restart advice: nothing changed, so the running client is already correct.
        click.echo("  already registered; no change.")
        return True

    # Usually absent, and `mcp remove` exits non-zero for a name it does not have, so
    # the result is deliberately ignored.
    _run_client(exe, ["mcp", "remove", _SERVER_NAME])

    args = ["mcp", "add", _SERVER_NAME, "--", *command]
    click.echo(f"  $ {exe} {' '.join(args)}")
    completed = _run_client(exe, args)
    if completed is None:
        return False
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        click.echo(
            f"  {exe} exited {completed.returncode}: {detail[0] if detail else ''}", err=True
        )
        return False
    click.echo(f"  registered. Restart {client} to load it.")
    return True


@click.command("install")
@click.option(
    "--client",
    "clients",
    multiple=True,
    type=click.Choice([*_CLI_CLIENTS, *_FILE_CLIENTS]),
    help="Client to configure (repeatable). Defaults to every detected CLI client.",
)
@click.option("--no-refresh", is_flag=True, help="Skip building the index.")
@click.option(
    "--print-config",
    is_flag=True,
    help="Only print the MCP config and the command; change nothing.",
)
@click.pass_context
def install_command(
    ctx: click.Context, clients: tuple[str, ...], no_refresh: bool, print_config: bool
) -> None:
    """Register the MCP server with a coding agent and build the index.

    \b
    Configures every detected client CLI (Claude Code, Codex) unless --client
    is given, then runs the first refresh. MCP servers are read at session
    start, so restart your agent afterwards.

    \b
    Exit codes: 0 = a client was configured, 1 = a client CLI rejected the
    registration, 3 = nothing was configured and the config was printed to
    paste instead. The file-configured editors (Cursor, VS Code, Zed) always
    take the last path, as does a machine with no client CLI installed.
    """
    from pipecat_context_hub.cli import refresh

    command = _server_command()

    if print_config:
        click.echo(f"Server command: {' '.join(command)}")
        click.echo(_mcp_json(command))
        return

    selected = list(clients) or _detect_cli_clients()
    failures: list[str] = []
    configured = False
    if not selected:
        click.echo(
            "No client CLI detected (looked for: "
            f"{', '.join(sorted(_CLI_CLIENTS.values()))}).\n"
            "Add the block below to your client's MCP config (schema differs per client):\n"
        )
        for name, location in sorted(_FILE_CLIENTS.items()):
            click.echo(f"{name}: add this to {location}")
            click.echo(_mcp_json(command, name))
    else:
        for client in selected:
            if client in _FILE_CLIENTS:
                click.echo(f"{client}: add this to {_FILE_CLIENTS[client]}")
                click.echo(_mcp_json(command, client))
                continue
            click.echo(f"Registering '{_SERVER_NAME}' with {client}:")
            if _register_with_cli(client, command):
                configured = True
            else:
                click.echo(f"  registration failed for {client}.", err=True)
                failures.append(client)

    if no_refresh:
        click.echo("\nSkipped index build. Run `refresh` before the first query.")
    else:
        click.echo("\nBuilding the index (first run downloads models; allow several minutes)...")
        ctx.invoke(refresh)

    if failures:
        raise click.ClickException(f"Failed to register with: {', '.join(failures)}")
    if not configured:
        ctx.exit(_EXIT_MANUAL_SETUP)


def register_install_command(group: click.Group) -> None:
    """Attach the install command to the main CLI group."""
    group.add_command(install_command)
