"""Tests for the Typer bridge that mounts this CLI as ``pipecat context-hub``.

The bridge exists so the Pipecat CLI can host a click-based CLI. Its failure
modes are quiet: a subcommand can go missing, or ``--help`` on a subcommand can
render the passthrough stub instead of the real command — both look fine at a
glance. These pin the parts that would silently regress.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from pipecat_context_hub.cli import main as hub_cli
from pipecat_context_hub.plugin import _SHORT_HELP_LIMIT, alias, app

runner = CliRunner()


def _mounted() -> typer.Typer:
    """A pipecat-like root app with the bridge mounted, as _build_app does."""
    root = typer.Typer(add_completion=False)

    @root.command()
    def init() -> None:
        """Stand-in for a built-in command, so the root renders as a group."""

    root.add_typer(app, name="context-hub")
    root.add_typer(alias, name="ch")
    return root


class TestBridgeShape:
    def test_exports_a_typer_app(self):
        # pipecat's loader calls Typer.add_typer, which rejects anything else.
        assert isinstance(app, typer.Typer)
        assert isinstance(alias, typer.Typer)


class TestAlias:
    """`ch` is the same command surface under a shorter name.

    It is a separate Typer object because `hidden` is a property of the app, and
    mounting one app twice would list the same description twice in
    `pipecat --help`.
    """

    def test_alias_is_hidden_from_the_command_list(self):
        result = runner.invoke(_mounted(), ["--help"])
        assert result.exit_code == 0
        assert "context-hub" in result.output
        # The listing shows the canonical name only.
        assert not any(line.strip().startswith("│ ch ") for line in result.output.splitlines())

    def test_alias_exposes_the_same_commands(self):
        by_app = {cmd.name for cmd in app.registered_commands}
        by_alias = {cmd.name for cmd in alias.registered_commands}
        assert by_app == by_alias == set(hub_cli.commands)

    def test_alias_dispatches(self, monkeypatch):
        seen: dict[str, list[str]] = {}

        def _capture(args=None, prog_name=None, **kwargs):
            seen["args"] = list(args or [])
            raise SystemExit(0)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _capture)
        result = runner.invoke(_mounted(), ["ch", "status"])
        assert result.exit_code == 0
        assert "status" in seen["args"]

    def test_usage_names_the_command_the_user_typed(self, monkeypatch):
        """A usage error under `ch` should not tell you to run `context-hub`."""
        seen: dict[str, str] = {}

        def _capture(args=None, prog_name="", **kwargs):
            seen["prog_name"] = prog_name
            raise SystemExit(0)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _capture)
        runner.invoke(_mounted(), ["ch", "status"])
        assert seen["prog_name"] == "pipecat ch"
        runner.invoke(_mounted(), ["context-hub", "status"])
        assert seen["prog_name"] == "pipecat context-hub"

    def test_every_click_command_is_bridged(self):
        """Parity: a command added to the CLI must not silently miss the plugin."""
        bridged = {cmd.name for cmd in app.registered_commands}
        assert bridged == set(hub_cli.commands)

    def test_short_help_comes_from_the_click_command(self):
        by_name = {cmd.name: cmd for cmd in app.registered_commands}
        for name, command in hub_cli.commands.items():
            assert by_name[name].short_help == command.get_short_help_str(limit=_SHORT_HELP_LIMIT)

    def test_short_help_is_not_truncated(self):
        """`get_short_help_str` truncates at 45 characters by default.

        Click applies that limit only after sizing it to the terminal, so
        taking the default here ellipsises descriptions that the direct CLI
        renders in full.
        """
        by_name = {cmd.name: cmd for cmd in app.registered_commands}
        longer_than_the_default = [
            name
            for name, command in hub_cli.commands.items()
            if len(command.get_short_help_str(limit=_SHORT_HELP_LIMIT)) > 45
        ]
        # Guards the guard: if every summary got short, this test proves nothing.
        assert longer_than_the_default, "no command summary exceeds click's default limit"
        for name in longer_than_the_default:
            short_help = by_name[name].short_help
            assert short_help is not None
            assert not short_help.endswith("...")


class TestBridgeDispatch:
    def test_group_help_lists_hub_commands(self):
        result = runner.invoke(_mounted(), ["context-hub", "--help"])
        assert result.exit_code == 0
        assert "refresh" in result.output
        assert "check-deprecation" in result.output

    def test_subcommand_help_is_the_real_one(self):
        """Regression guard: without help_option_names=[] this renders the stub.

        The stub's help has no options, so asserting on a real flag is what
        distinguishes the two.
        """
        result = runner.invoke(_mounted(), ["context-hub", "refresh", "--help"])
        assert result.exit_code == 0
        assert "--force" in result.output
        assert "--framework-version" in result.output

    def test_unknown_option_is_a_usage_error(self):
        result = runner.invoke(_mounted(), ["context-hub", "refresh", "--bogus"])
        assert result.exit_code == 2

    def test_unknown_subcommand_is_a_usage_error(self):
        result = runner.invoke(_mounted(), ["context-hub", "nosuchcommand"])
        assert result.exit_code == 2


class TestExitCodeTranslation:
    """Click exits must survive the hop into typer unchanged."""

    @pytest.mark.parametrize("code", [0, 1, 2])
    def test_systemexit_code_is_preserved(self, code, monkeypatch):
        def _boom(*args, **kwargs):
            raise SystemExit(code)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _boom)
        result = runner.invoke(_mounted(), ["context-hub", "status"])
        assert result.exit_code == code

    def test_systemexit_none_is_success(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise SystemExit(None)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _boom)
        result = runner.invoke(_mounted(), ["context-hub", "status"])
        assert result.exit_code == 0

    def test_systemexit_string_is_failure(self, monkeypatch):
        """A str code means an error message, not an exit status."""

        def _boom(*args, **kwargs):
            raise SystemExit("something went wrong")

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _boom)
        result = runner.invoke(_mounted(), ["context-hub", "status"])
        assert result.exit_code == 1


class TestLogLevelForwarding:
    def test_group_log_level_reaches_the_click_group(self, monkeypatch):
        seen: dict[str, list[str]] = {}

        def _capture(args=None, **kwargs):
            seen["args"] = list(args or [])
            raise SystemExit(0)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _capture)
        result = runner.invoke(_mounted(), ["context-hub", "--log-level", "DEBUG", "status"])
        assert result.exit_code == 0
        assert seen["args"][:2] == ["--log-level", "DEBUG"]
        assert "status" in seen["args"]

    def test_defaults_to_info(self, monkeypatch):
        seen: dict[str, list[str]] = {}

        def _capture(args=None, **kwargs):
            seen["args"] = list(args or [])
            raise SystemExit(0)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _capture)
        runner.invoke(_mounted(), ["context-hub", "status"])
        assert seen["args"][:2] == ["--log-level", "INFO"]
