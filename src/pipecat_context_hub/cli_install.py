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
from typing import Any, Literal

import click

# MCP server name registered with the client. Matches the command name, and the
# name the agent guides tell coding agents to look for.
_SERVER_NAME = "pipecat-context-hub"

# Clients configured by shelling out to their own CLI: name -> executable.
_CLI_CLIENTS = {"claude-code": "claude", "codex": "codex"}

# Scope for a fresh Claude registration. Claude's own default is `local`, which
# keys the entry on the directory `install` ran in — so every other project is
# left without the server, silently: the agent doesn't fail, it just answers from
# training data, which is the thing the hub exists to prevent. Nothing here is
# per-project. One index serves the machine, and the registered command is an
# absolute path to a global install. Codex has no scope concept; its single
# config is already machine-wide.
_CLAUDE_FRESH_SCOPE = "user"

# Exit code for "nothing was configured automatically; the config was printed to paste".
# Distinct from success because a caller cannot see the difference otherwise, and from
# failure because the command did everything it could for that client.
_EXIT_MANUAL_SETUP = 3

# Exit code for "repair failed AND the previous, working registration could not be
# restored either" — the client now has no usable registration at all. Distinct from a
# plain failure (exit 1, previous registration still intact) because a caller cannot
# tell those apart from the message text alone, and this one needs to be seen.
_EXIT_REGISTRATION_LOST = 4

# Clients configured by hand-editing a JSON file: name -> where it lives.
_FILE_CLIENTS = {
    "cursor": "~/.cursor/mcp.json (global) or .cursor/mcp.json (per project)",
    "vscode": ".vscode/mcp.json",
    "zed": "~/.config/zed/settings.json (under `context_servers`)",
}


@dataclass(frozen=True)
class _Registration:
    """Result of inspecting an existing client registration."""

    state: Literal["absent", "matching", "mismatched", "unknown", "error"]
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

    ``-P`` isolates module resolution from the launching process's cwd. Without it,
    ``python -m`` prepends the current working directory to ``sys.path``, so a client
    started from a directory containing its own ``pipecat_context_hub/`` package (e.g.
    a cloned, untrusted repo with a same-named top-level package) would shadow the real
    installed one and execute arbitrary code at server startup. This now matters more
    than it used to: a fresh Claude Code registration is ``user``-scoped (see
    ``_CLAUDE_FRESH_SCOPE``), so this command can be launched from *any* directory the
    user later opens the client in, not just the one ``install`` ran in.
    """
    return [sys.executable, "-P", "-m", "pipecat_context_hub", "serve"]


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
    """Read the exact Claude registration at *scope* for rollback.

    Reads Claude Code's private, undocumented config files directly because
    ``claude mcp get`` has no ``--json``/structured-output equivalent (unlike
    Codex's ``mcp get --json``): this is the only way to capture the exact
    entry needed for byte-for-byte rollback restoration. This is an accepted
    fragility -- if Claude Code's on-disk schema changes, the existing
    ``KeyError``/``TypeError`` guards in ``_inspect_registration`` degrade
    gracefully to ``_Registration("error")`` (fail closed) rather than
    crashing.
    """
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
        resolved_cwd = Path.cwd().resolve()
        for project_path, entry in projects.items():
            # One broken entry (permission-denied, a stale symlink, a removed network
            # mount) must not abort the lookup for every other entry in the same dict.
            # This is a best-effort skip, not a guarantee: an entry that hangs instead
            # of erroring (an unreachable-but-not-erroring network mount) still blocks
            # here -- accepted fragility, same tradeoff this function already makes
            # elsewhere (see docstring).
            try:
                matches = Path(project_path).resolve() == resolved_cwd
            except OSError:
                continue
            if matches:
                config = entry["mcpServers"][_SERVER_NAME]
                if not isinstance(config, dict):
                    raise TypeError("Claude local registration is not an object")
                return config
    raise KeyError(f"could not locate {_SERVER_NAME!r} in Claude's {scope!r} config")


def _config_matches_command(
    config: dict[str, Any], command: list[str], *, default_type: str | None = None
) -> bool:
    """Whether a stored client entry is a stdio server running *command*.

    *default_type* is the transport type assumed when the ``type`` key is absent.
    Pass ``None`` (Codex) to require the key be present and equal to ``"stdio"``:
    Codex's ``mcp get --json`` output is a documented, structured schema that
    always includes it, so a missing key there is an unrecognized shape, not an
    implied default. Pass ``"stdio"`` (Claude Code) because Claude's own on-disk
    config -- and this module's own registration writer -- omit ``type`` whenever
    it's ``stdio``, the only transport this command ever writes.
    """
    actual_type = config.get("type", default_type)
    return (
        actual_type == "stdio" and [config.get("command"), *(config.get("args") or [])] == command
    )


def _inspect_registration(client: str, exe: str, command: list[str]) -> _Registration:
    """Inspect the entry, distinguishing absence from an unsafe inspection failure."""
    get_args = ["mcp", "get"]
    if client == "codex":
        get_args.append("--json")
    get_args.append(_SERVER_NAME)
    completed = _run_client(exe, get_args)
    if completed is None:
        return _Registration("unknown")
    if completed.returncode != 0:
        if _not_found(completed):
            return _Registration("absent")
        detail = _first_error_line(completed) or "unknown error"
        click.echo(
            f"  cannot inspect existing registration: {detail}; leaving it unchanged.",
            err=True,
        )
        return _Registration("error")

    try:
        if client == "codex":
            config = json.loads(completed.stdout)["transport"]
            if not isinstance(config, dict):
                raise TypeError("Codex transport entry is not an object")
            matches = _config_matches_command(config, command)
            return _Registration("matching" if matches else "mismatched")

        scope_match = re.search(
            rf"^To remove this server, run: claude mcp remove {re.escape(_SERVER_NAME)} "
            rf"-s (local|user|project)\s*$",
            completed.stdout,
            re.MULTILINE,
        )
        if scope_match is None:
            raise ValueError("Claude did not report the registration scope")
        scope = scope_match.group(1)
        config = _claude_config(scope)
        matches = _config_matches_command(config, command, default_type="stdio")
        return _Registration("matching" if matches else "mismatched", config, scope)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
        click.echo(f"  cannot inspect existing registration safely: {exc}", err=True)
        return _Registration("error")


def _first_error_line(completed: subprocess.CompletedProcess[str]) -> str:
    """First non-blank line of *completed*'s stderr, falling back to stdout."""
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return detail[0] if detail else ""


def _report_failure(exe: str, completed: subprocess.CompletedProcess[str]) -> None:
    click.echo(f"  {exe} exited {completed.returncode}: {_first_error_line(completed)}", err=True)


def _restore_claude_registration(exe: str, registration: _Registration) -> bool:
    """Restore a captured Claude registration after replacement failed.

    Returns True when the rollback succeeded, False when it did not (in which
    case the previous registration is lost, not merely unchanged).
    """
    # nosec B101 - invariant, not a runtime check: the only caller path that reaches
    # here is gated on registration.state == "mismatched", which is the sole state
    # _inspect_registration populates config/scope for. AssertionError would mean a
    # caller-contract bug, not untrusted input.
    assert registration.config is not None  # nosec B101
    assert registration.scope is not None  # nosec B101
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
        return False
    click.echo("  restored the previous registration.", err=True)
    return True


def _fail_with_rollback(exe: str, registration: _Registration) -> Literal["failed", "corrupted"]:
    """Attempt to restore *registration* after a failed replacement.

    Returns ``"failed"`` when rollback succeeded (previous registration intact),
    ``"corrupted"`` when rollback also failed (nothing is registered any more).
    """
    restored = _restore_claude_registration(exe, registration)
    return "failed" if restored else "corrupted"


def _register_with_cli(client: str, command: list[str]) -> Literal["ok", "failed", "corrupted"]:
    """Register the server via *client*'s own CLI.

    A fresh Claude entry is registered at ``_CLAUDE_FRESH_SCOPE`` so it covers
    every directory; a mismatched one is repaired at the scope it already holds.

    Replaces a mismatched entry rather than skipping it. Codex supports an atomic
    overwrite. Claude does not, so its exact entry is captured before removal and
    restored if adding the replacement fails.

    Returns ``"ok"`` on success, ``"failed"`` when registration did not go through
    but any prior registration is intact (or there was none), and ``"corrupted"``
    when a Claude mismatch repair removed the previous registration and the
    rollback to restore it also failed, so nothing is registered any more.
    """
    exe = _CLI_CLIENTS[client]
    registration = _inspect_registration(client, exe, command)
    is_claude_mismatch = registration.state == "mismatched" and client == "claude-code"
    if registration.state == "error":
        return "failed"
    if registration.state == "matching":
        # No restart advice: nothing changed, so the running client is already correct.
        click.echo("  already registered; no change.")
        return "ok"

    add_options: list[str] = []
    if is_claude_mismatch:
        # nosec B101 - invariant: is_claude_mismatch implies state == "mismatched",
        # the only state _inspect_registration populates scope for.
        assert registration.scope is not None  # nosec B101
        remove_args = ["mcp", "remove", _SERVER_NAME, "-s", registration.scope]
        click.echo(f"  $ {exe} {' '.join(remove_args)}")
        removed = _run_client(exe, remove_args)
        if removed is None:
            return _fail_with_rollback(exe, registration)
        if removed.returncode != 0:
            _report_failure(exe, removed)
            return _fail_with_rollback(exe, registration)
        # Repair in place: an entry that is already somewhere was put there by
        # someone, and relocating it would override that choice.
        add_options = ["-s", registration.scope]
    elif client == "claude-code" and registration.state == "absent":
        # Only a confirmed-absent entry is "fresh" and safe to scope to every
        # directory. `unknown` means `mcp get` itself could not be run (e.g. it
        # timed out) -- an entry may already exist at some uninspected scope, so
        # applying `-s user` here could create a stray duplicate instead of
        # leaving the (possibly-existing) registration alone. Fall through to
        # Claude's own default `add` behavior instead, matching what happened
        # for `unknown` before this scope policy existed.
        add_options = ["-s", _CLAUDE_FRESH_SCOPE]

    # `codex mcp add` overwrites an existing name atomically. For an absent entry,
    # both clients take this same non-destructive path.
    args = ["mcp", "add", *add_options, _SERVER_NAME, "--", *command]
    click.echo(f"  $ {exe} {' '.join(args)}")
    completed = _run_client(exe, args)
    if completed is None:
        if is_claude_mismatch:
            return _fail_with_rollback(exe, registration)
        return "failed"
    if completed.returncode != 0:
        _report_failure(exe, completed)
        if is_claude_mismatch:
            return _fail_with_rollback(exe, registration)
        return "failed"
    click.echo(f"  registered. Restart {client} to load it.")
    return "ok"


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
    A fresh Claude Code entry is registered at Claude's `user` scope, so it
    applies in every directory rather than only this one. An entry that already
    exists is repaired at the scope it already holds. Codex has no scope
    concept; its single config is already machine-wide.

    \b
    Exit codes: 0 = a client was configured, 1 = a client CLI rejected the
    registration, 3 = nothing was configured and the config was printed to
    paste instead, 4 = a repair attempt failed and the previous registration
    could not be restored either (nothing is registered for that client any
    more). The file-configured editors (Cursor, VS Code, Zed) always take the
    3 path, as does a machine with no client CLI installed.
    """
    from pipecat_context_hub.cli import refresh

    command = _server_command()

    if print_config:
        click.echo(f"Server command: {' '.join(command)}")
        click.echo(_mcp_json(command))
        return

    selected = list(clients) or _detect_cli_clients()
    failures: list[str] = []
    corrupted: list[str] = []
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
            outcome = _register_with_cli(client, command)
            if outcome == "ok":
                configured = True
            elif outcome == "corrupted":
                click.echo(
                    f"  registration failed for {client}, and the previous "
                    "registration could not be restored either.",
                    err=True,
                )
                corrupted.append(client)
            else:
                click.echo(f"  registration failed for {client}.", err=True)
                failures.append(client)

    if no_refresh:
        click.echo("\nSkipped index build. Run `refresh` before the first query.")
    else:
        click.echo("\nBuilding the index (first run downloads models; allow several minutes)...")
        ctx.invoke(refresh)

    if corrupted or failures:
        parts = []
        if failures:
            parts.append(f"Failed to register with: {', '.join(failures)}")
        if corrupted:
            parts.append(
                f"Failed to register with (previous registration lost): {', '.join(corrupted)}"
            )
        message = "; ".join(parts)
        if corrupted:
            # A plain ClickException always exits 1, which would make this
            # indistinguishable from a failure that left the old registration intact.
            # Losing a working registration needs its own exit code.
            click.echo(f"Error: {message}", err=True)
            ctx.exit(_EXIT_REGISTRATION_LOST)
        raise click.ClickException(message)
    if not configured:
        ctx.exit(_EXIT_MANUAL_SETUP)


def register_install_command(group: click.Group) -> None:
    """Attach the install command to the main CLI group."""
    group.add_command(install_command)
