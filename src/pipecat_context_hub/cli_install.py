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
import re
import shutil
import subprocess  # nosec B404 - fixed-arg, timeout-guarded client CLI registration
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
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


@dataclass(frozen=True)
class _Registration:
    """Result of inspecting an existing client registration."""

    state: Literal["absent", "matching", "mismatched", "error"]
    config: dict[str, Any] | None = None
    scope: str | None = None


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


def _not_found(completed: subprocess.CompletedProcess[str]) -> bool:
    """Whether a failed ``mcp get`` unambiguously means the entry is absent."""
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    return "no mcp server named" in output or "no mcp server found with name" in output


def _claude_config(scope: str) -> dict[str, Any]:
    """Read the exact Claude registration at *scope* for rollback."""
    if scope == "project":
        document = json.loads((Path.cwd() / ".mcp.json").read_text())
        config = document["mcpServers"][_SERVER_NAME]
        if not isinstance(config, dict):
            raise TypeError("Claude project registration is not an object")
        return config

    document = json.loads((Path.home() / ".claude.json").read_text())
    if scope == "user":
        config = document["mcpServers"][_SERVER_NAME]
        if not isinstance(config, dict):
            raise TypeError("Claude user registration is not an object")
        return config
    if scope == "local":
        projects = document["projects"]
        for project_path in (str(Path.cwd()), str(Path.cwd().resolve())):
            if project_path in projects:
                config = projects[project_path]["mcpServers"][_SERVER_NAME]
                if not isinstance(config, dict):
                    raise TypeError("Claude local registration is not an object")
                return config
    raise KeyError(f"could not locate {_SERVER_NAME!r} in Claude's {scope!r} config")


def _inspect_registration(client: str, exe: str, command: list[str]) -> _Registration:
    """Inspect the entry, distinguishing absence from an unsafe inspection failure."""
    get_args = ["mcp", "get"]
    if client == "codex":
        get_args.append("--json")
    get_args.append(_SERVER_NAME)
    completed = _run_client(exe, get_args)
    if completed is None:
        return _Registration("error")
    if completed.returncode != 0:
        if _not_found(completed):
            return _Registration("absent")
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        click.echo(
            f"  cannot inspect existing registration: {detail[0] if detail else 'unknown error'}",
            err=True,
        )
        return _Registration("error")

    try:
        if client == "codex":
            config = json.loads(completed.stdout)["transport"]
            matches = (
                config.get("type") == "stdio"
                and [
                    config.get("command"),
                    *config.get("args", []),
                ]
                == command
            )
            return _Registration("matching" if matches else "mismatched")

        scope_match = re.search(r"mcp remove .* -s (local|user|project)", completed.stdout)
        if scope_match is None:
            raise ValueError("Claude did not report the registration scope")
        scope = scope_match.group(1)
        config = _claude_config(scope)
        matches = (
            config.get("type", "stdio") == "stdio"
            and [
                config.get("command"),
                *config.get("args", []),
            ]
            == command
        )
        return _Registration("matching" if matches else "mismatched", config, scope)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
        click.echo(f"  cannot inspect existing registration safely: {exc}", err=True)
        return _Registration("error")


def _report_failure(exe: str, completed: subprocess.CompletedProcess[str]) -> None:
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    click.echo(f"  {exe} exited {completed.returncode}: {detail[0] if detail else ''}", err=True)


def _restore_claude_registration(exe: str, registration: _Registration) -> None:
    """Restore a captured Claude registration after replacement failed."""
    assert registration.config is not None
    assert registration.scope is not None
    rollback = _run_client(
        exe,
        [
            "mcp",
            "add-json",
            "-s",
            registration.scope,
            _SERVER_NAME,
            json.dumps(registration.config, separators=(",", ":")),
        ],
    )
    if rollback is None or rollback.returncode != 0:
        click.echo("  failed to restore the previous registration.", err=True)
    else:
        click.echo("  restored the previous registration.", err=True)


def _register_with_cli(client: str, command: list[str]) -> bool:
    """Register the server via *client*'s own CLI. True when it succeeded.

    Replaces a mismatched entry rather than skipping it. Codex supports an atomic
    overwrite. Claude does not, so its exact entry is captured before removal and
    restored if adding the replacement fails.
    """
    exe = _CLI_CLIENTS[client]
    registration = _inspect_registration(client, exe, command)
    if registration.state == "error":
        return False
    if registration.state == "matching":
        # No restart advice: nothing changed, so the running client is already correct.
        click.echo("  already registered; no change.")
        return True

    add_options: list[str] = []
    if registration.state == "mismatched" and client == "claude-code":
        assert registration.scope is not None
        removed = _run_client(exe, ["mcp", "remove", _SERVER_NAME, "-s", registration.scope])
        if removed is None:
            return False
        if removed.returncode != 0:
            _report_failure(exe, removed)
            return False
        add_options = ["-s", registration.scope]

    # `codex mcp add` overwrites an existing name atomically. For an absent entry,
    # both clients take this same non-destructive path.
    args = ["mcp", "add", *add_options, _SERVER_NAME, "--", *command]
    click.echo(f"  $ {exe} {' '.join(args)}")
    completed = _run_client(exe, args)
    if completed is None:
        if registration.state == "mismatched" and client == "claude-code":
            _restore_claude_registration(exe, registration)
        return False
    if completed.returncode != 0:
        _report_failure(exe, completed)
        if registration.state == "mismatched" and client == "claude-code":
            _restore_claude_registration(exe, registration)
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
