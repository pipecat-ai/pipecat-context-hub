"""Tests for the Typer bridge that mounts this CLI as ``pipecat mcp``.

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
from pipecat_context_hub.plugin import app

runner = CliRunner()


def _mounted() -> typer.Typer:
    """A pipecat-like root app with the bridge mounted, as _build_app does."""
    root = typer.Typer(add_completion=False)

    @root.command()
    def init() -> None:
        """Stand-in for a built-in command, so the root renders as a group."""

    root.add_typer(app, name="mcp")
    return root


class TestBridgeShape:
    def test_exports_a_typer_app(self):
        # pipecat's loader calls Typer.add_typer, which rejects anything else.
        assert isinstance(app, typer.Typer)

    def test_every_click_command_is_bridged(self):
        """Parity: a command added to the CLI must not silently miss the plugin."""
        bridged = {cmd.name for cmd in app.registered_commands}
        assert bridged == set(hub_cli.commands)

    def test_short_help_comes_from_the_click_command(self):
        by_name = {cmd.name: cmd for cmd in app.registered_commands}
        for name, command in hub_cli.commands.items():
            assert by_name[name].short_help == command.get_short_help_str()


class TestBridgeDispatch:
    def test_group_help_lists_hub_commands(self):
        result = runner.invoke(_mounted(), ["mcp", "--help"])
        assert result.exit_code == 0
        assert "refresh" in result.output
        assert "check-deprecation" in result.output

    def test_subcommand_help_is_the_real_one(self):
        """Regression guard: without help_option_names=[] this renders the stub.

        The stub's help has no options, so asserting on a real flag is what
        distinguishes the two.
        """
        result = runner.invoke(_mounted(), ["mcp", "refresh", "--help"])
        assert result.exit_code == 0
        assert "--force" in result.output
        assert "--framework-version" in result.output

    def test_unknown_option_is_a_usage_error(self):
        result = runner.invoke(_mounted(), ["mcp", "refresh", "--bogus"])
        assert result.exit_code == 2

    def test_unknown_subcommand_is_a_usage_error(self):
        result = runner.invoke(_mounted(), ["mcp", "nosuchcommand"])
        assert result.exit_code == 2


class TestExitCodeTranslation:
    """Click exits must survive the hop into typer unchanged."""

    @pytest.mark.parametrize("code", [0, 1, 2])
    def test_systemexit_code_is_preserved(self, code, monkeypatch):
        def _boom(*args, **kwargs):
            raise SystemExit(code)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _boom)
        result = runner.invoke(_mounted(), ["mcp", "status"])
        assert result.exit_code == code

    def test_systemexit_none_is_success(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise SystemExit(None)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _boom)
        result = runner.invoke(_mounted(), ["mcp", "status"])
        assert result.exit_code == 0

    def test_systemexit_string_is_failure(self, monkeypatch):
        """A str code means an error message, not an exit status."""

        def _boom(*args, **kwargs):
            raise SystemExit("something went wrong")

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _boom)
        result = runner.invoke(_mounted(), ["mcp", "status"])
        assert result.exit_code == 1


class TestLogLevelForwarding:
    def test_group_log_level_reaches_the_click_group(self, monkeypatch):
        seen: dict[str, list[str]] = {}

        def _capture(args=None, **kwargs):
            seen["args"] = list(args or [])
            raise SystemExit(0)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _capture)
        result = runner.invoke(_mounted(), ["mcp", "--log-level", "DEBUG", "status"])
        assert result.exit_code == 0
        assert seen["args"][:2] == ["--log-level", "DEBUG"]
        assert "status" in seen["args"]

    def test_defaults_to_info(self, monkeypatch):
        seen: dict[str, list[str]] = {}

        def _capture(args=None, **kwargs):
            seen["args"] = list(args or [])
            raise SystemExit(0)

        monkeypatch.setattr("pipecat_context_hub.plugin.hub_cli.main", _capture)
        runner.invoke(_mounted(), ["mcp", "status"])
        assert seen["args"][:2] == ["--log-level", "INFO"]
