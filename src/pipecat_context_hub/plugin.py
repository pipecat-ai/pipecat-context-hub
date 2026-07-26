"""Typer bridge that mounts this CLI into the Pipecat CLI as ``pipecat mcp``.

The Pipecat CLI discovers sub-CLIs through the ``pipecat_cli.extensions``
entry-point group and mounts each with ``Typer.add_typer``, so a plugin must
expose a :class:`typer.Typer`. This CLI is click-based.

Rather than restate every command and option in Typer — which would drift on
each release — each command here is a thin passthrough that hands raw argv to
the click group, which parses and dispatches it. Click therefore only ever sees
its own classes: typer vendors a private copy of click (``typer._click``) whose
exception types are *not* the ones a real ``click.Command`` raises, so keeping
parsing wholly inside click is what makes usage errors, ``--help`` on a
subcommand, and exit codes behave identically through either front door.

``typer`` is a peer dependency, not a runtime one: this module is imported only
by the Pipecat CLI, which supplies typer through its own ``cli`` extra.
"""

from __future__ import annotations

from collections.abc import Callable

import typer

from pipecat_context_hub.cli import main as hub_cli

# Let the click group own every flag, including --help. Without an empty
# help_option_names, typer answers `pipecat mcp <cmd> --help` with the
# passthrough stub's help instead of the real command's.
_PASSTHROUGH: dict[str, object] = {
    "ignore_unknown_options": True,
    "allow_extra_args": True,
    "help_option_names": [],
}

_DEFAULT_LOG_LEVEL = "INFO"

app = typer.Typer(
    add_completion=False,
    help="Pipecat Context Hub: local docs, examples, and API index for coding agents.",
)


def _dispatch(argv: list[str], log_level: str) -> None:
    """Run the click group over *argv*, translating its exit into typer's."""
    try:
        hub_cli.main(
            args=["--log-level", log_level, *argv],
            prog_name="pipecat mcp",
            standalone_mode=True,
        )
    except SystemExit as exc:
        code = exc.code
        raise typer.Exit(code if isinstance(code, int) else (0 if code is None else 1)) from None
    raise typer.Exit(0)


def _group_log_level(ctx: typer.Context) -> str:
    """Read --log-level off the bridge group's context (this app's callback)."""
    parent = ctx.parent
    obj = parent.obj if parent is not None else None
    if isinstance(obj, dict):
        level = obj.get("log_level")
        if isinstance(level, str):
            return level
    return _DEFAULT_LOG_LEVEL


def _passthrough(command_name: str) -> Callable[[typer.Context], None]:
    """Build a Typer command forwarding all argv to *command_name*."""

    def _run(ctx: typer.Context) -> None:
        _dispatch([command_name, *ctx.args], _group_log_level(ctx))

    return _run


# Mirror the click group's command set. Registration is generated, so a command
# added to the CLI shows up here without touching this module.
for _name, _command in sorted(hub_cli.commands.items()):
    app.command(
        _name,
        short_help=_command.get_short_help_str(),
        context_settings=_PASSTHROUGH,
    )(_passthrough(_name))


@app.callback(invoke_without_command=True)
def _callback(
    ctx: typer.Context,
    log_level: str = typer.Option(_DEFAULT_LOG_LEVEL, "--log-level", help="Logging level."),
) -> None:
    """Stash --log-level for the passthroughs; serve when given no subcommand."""
    # Set on this context rather than ensure_object, so the bridge never mutates
    # a dict the host CLI may have installed further up the chain.
    ctx.obj = {"log_level": log_level}
    if ctx.invoked_subcommand is None:
        # Matches the standalone CLI, where a bare invocation starts the server.
        _dispatch(["serve"], log_level)


__all__ = ["app"]
