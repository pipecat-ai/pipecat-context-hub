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
import subprocess
from typing import Any

import click

# MCP server name registered with the client. Matches the command name, and the
# name the agent guides tell coding agents to look for.
_SERVER_NAME = "pipecat-context-hub"

# Clients configured by shelling out to their own CLI: name -> executable.
_CLI_CLIENTS = {"claude-code": "claude", "codex": "codex"}

# Clients configured by hand-editing a JSON file: name -> where it lives.
_FILE_CLIENTS = {
    "cursor": "~/.cursor/mcp.json (global) or .cursor/mcp.json (per project)",
    "vscode": ".vscode/mcp.json",
    "zed": "~/.config/zed/settings.json (under `context_servers`)",
}


def _server_command() -> list[str]:
    """The command a client should run to start the MCP server.

    Prefers the installed console script, which starts the server directly.
    Registering ``pipecat mcp serve`` instead would make every server start
    import typer and the Pipecat CLI's plugin machinery for no benefit.
    Falls back to ``uvx``, which needs no prior install.
    """
    if shutil.which(_SERVER_NAME):
        return [_SERVER_NAME, "serve"]
    return ["uvx", "pipecat-ai-context-hub", "serve"]


def _mcp_json(command: list[str]) -> str:
    """Render the standard MCP client config block for *command*."""
    block: dict[str, Any] = {
        "mcpServers": {_SERVER_NAME: {"command": command[0], "args": command[1:], "env": {}}}
    }
    return json.dumps(block, indent=2)


def _detect_cli_clients() -> list[str]:
    """Names of CLI-configurable clients present on PATH."""
    return [name for name, exe in _CLI_CLIENTS.items() if shutil.which(exe)]


def _register_with_cli(client: str, command: list[str]) -> bool:
    """Register the server via *client*'s own CLI. True when it succeeded."""
    exe = _CLI_CLIENTS[client]
    argv = [exe, "mcp", "add", _SERVER_NAME, "--", *command]
    click.echo(f"  $ {' '.join(argv)}")
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        click.echo(f"  failed to run {exe}: {exc}", err=True)
        return False
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        click.echo(
            f"  {exe} exited {completed.returncode}: {detail[0] if detail else ''}", err=True
        )
        return False
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
    """
    from pipecat_context_hub.cli import refresh

    command = _server_command()

    if print_config:
        click.echo(f"Server command: {' '.join(command)}")
        click.echo(_mcp_json(command))
        return

    selected = list(clients) or _detect_cli_clients()
    if not selected:
        click.echo(
            "No client CLI detected (looked for: "
            f"{', '.join(sorted(_CLI_CLIENTS.values()))}).\n"
            "Add this to your client's MCP config:\n"
        )
        click.echo(_mcp_json(command))
        for name, location in sorted(_FILE_CLIENTS.items()):
            click.echo(f"  {name}: {location}")
    else:
        for client in selected:
            if client in _FILE_CLIENTS:
                click.echo(f"{client}: add this to {_FILE_CLIENTS[client]}")
                click.echo(_mcp_json(command))
                continue
            click.echo(f"Registering '{_SERVER_NAME}' with {client}:")
            if _register_with_cli(client, command):
                click.echo(f"  registered. Restart {client} to load it.")

    if no_refresh:
        click.echo("\nSkipped index build. Run `refresh` before the first query.")
        return

    click.echo("\nBuilding the index (first run downloads models; allow several minutes)...")
    ctx.invoke(refresh)


def register_install_command(group: click.Group) -> None:
    """Attach the install command to the main CLI group."""
    group.add_command(install_command)
