"""Tests for the ``install`` command.

This command shells out to other tools and points clients at a server command,
so the things worth pinning are: which command it registers, that it never
edits a config file it does not own, and that the read-only paths stay
read-only.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pipecat_context_hub.cli import main
from pipecat_context_hub.cli_install import (
    _detect_cli_clients,
    _mcp_json,
    _server_command,
)

runner = CliRunner()


class TestServerCommand:
    def test_prefers_installed_console_script(self):
        with patch("pipecat_context_hub.cli_install.shutil.which", return_value="/usr/bin/x"):
            assert _server_command() == ["pipecat-context-hub", "serve"]

    def test_falls_back_to_this_interpreter(self):
        """A `--with` co-install exposes no script for this package, only pipecat's.

        Naming the interpreter keeps the client on the installed version, where
        uvx would resolve the latest published one at every server start.
        """
        with patch("pipecat_context_hub.cli_install.shutil.which", return_value=None):
            assert _server_command() == [sys.executable, "-m", "pipecat_context_hub", "serve"]

    def test_never_registers_the_pipecat_front_door(self):
        """Registering `pipecat mcp serve` would load typer on every server start."""
        for which in ("/usr/bin/x", None):
            with patch("pipecat_context_hub.cli_install.shutil.which", return_value=which):
                assert _server_command()[0] != "pipecat"


class TestMcpJson:
    def test_shape_matches_the_mcp_client_convention(self):
        block = json.loads(_mcp_json(["pipecat-context-hub", "serve"]))
        server = block["mcpServers"]["pipecat-context-hub"]
        assert server["command"] == "pipecat-context-hub"
        assert server["args"] == ["serve"]
        assert server["env"] == {}

    def test_multi_arg_command_is_split_out(self):
        block = json.loads(_mcp_json([sys.executable, "-m", "pipecat_context_hub", "serve"]))
        server = block["mcpServers"]["pipecat-context-hub"]
        assert server["command"] == sys.executable
        assert server["args"] == ["-m", "pipecat_context_hub", "serve"]

    def test_vscode_uses_servers_key_and_stdio_type(self):
        """VS Code's mcp.json schema is `servers` with an explicit `type`,
        not the `mcpServers` shape every other client uses."""
        block = json.loads(_mcp_json(["pipecat-context-hub", "serve"], "vscode"))
        assert "mcpServers" not in block
        server = block["servers"]["pipecat-context-hub"]
        assert server["type"] == "stdio"
        assert server["command"] == "pipecat-context-hub"
        assert server["args"] == ["serve"]
        assert server["env"] == {}

    def test_zed_uses_context_servers_key_and_custom_source(self):
        """Zed's settings.json schema is `context_servers` with `source`."""
        block = json.loads(_mcp_json(["pipecat-context-hub", "serve"], "zed"))
        assert "mcpServers" not in block
        server = block["context_servers"]["pipecat-context-hub"]
        assert server["source"] == "custom"
        assert server["command"] == "pipecat-context-hub"
        assert server["args"] == ["serve"]
        assert server["env"] == {}

    def test_cursor_and_unspecified_client_use_mcp_servers_key(self):
        """Cursor (and no client specified) fall back to the common
        `mcpServers` shape — no `type`/`source` wrapper."""
        for client in ("cursor", None):
            block = json.loads(_mcp_json(["pipecat-context-hub", "serve"], client))
            server = block["mcpServers"]["pipecat-context-hub"]
            assert server["command"] == "pipecat-context-hub"
            assert server["args"] == ["serve"]
            assert server["env"] == {}


class TestDetectClients:
    def test_detects_only_what_is_on_path(self):
        with patch(
            "pipecat_context_hub.cli_install.shutil.which",
            side_effect=lambda exe: "/usr/bin/claude" if exe == "claude" else None,
        ):
            assert _detect_cli_clients() == ["claude-code"]

    def test_none_present(self):
        with patch("pipecat_context_hub.cli_install.shutil.which", return_value=None):
            assert _detect_cli_clients() == []


class TestInstallCommand:
    def test_print_config_changes_nothing(self):
        with patch("pipecat_context_hub.cli_install.subprocess.run") as run:
            result = runner.invoke(main, ["install", "--print-config"])
        assert result.exit_code == 0
        assert "mcpServers" in result.output
        run.assert_not_called()

    def test_file_based_client_prints_rather_than_writes(self):
        """Cursor/VS Code/Zed configs are hand-edited; never write them."""
        with patch("pipecat_context_hub.cli_install.subprocess.run") as run:
            result = runner.invoke(main, ["install", "--client", "cursor", "--no-refresh"])
        assert result.exit_code == 0
        assert "mcpServers" in result.output
        assert ".cursor/mcp.json" in result.output
        run.assert_not_called()

    def test_cli_client_is_registered_through_its_own_cli(self):
        with (
            patch("pipecat_context_hub.cli_install.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pipecat_context_hub.cli_install.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ) as run,
        ):
            result = runner.invoke(main, ["install", "--client", "claude-code", "--no-refresh"])

        assert result.exit_code == 0
        argv = run.call_args.args[0]
        assert argv[:4] == ["claude", "mcp", "add", "pipecat-context-hub"]
        # The `--` separator keeps the server's own args out of claude's parser.
        assert argv[4] == "--"
        assert argv[5:] == ["pipecat-context-hub", "serve"]

    def test_client_cli_failure_is_reported_as_error_exit(self):
        """A failed client registration is surfaced via a nonzero exit, not silently ignored."""
        with (
            patch("pipecat_context_hub.cli_install.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pipecat_context_hub.cli_install.subprocess.run",
                return_value=MagicMock(returncode=1, stdout="", stderr="already exists"),
            ),
        ):
            result = runner.invoke(main, ["install", "--client", "claude-code", "--no-refresh"])

        assert result.exit_code != 0
        assert "already exists" in result.output
        assert "Failed to register with: claude-code" in result.output

    def test_vscode_client_prints_servers_schema(self):
        with patch("pipecat_context_hub.cli_install.subprocess.run") as run:
            result = runner.invoke(main, ["install", "--client", "vscode", "--no-refresh"])
        assert result.exit_code == 0
        assert '"servers"' in result.output
        assert '"type": "stdio"' in result.output
        assert "mcpServers" not in result.output
        run.assert_not_called()

    def test_zed_client_prints_context_servers_schema(self):
        with patch("pipecat_context_hub.cli_install.subprocess.run") as run:
            result = runner.invoke(main, ["install", "--client", "zed", "--no-refresh"])
        assert result.exit_code == 0
        assert '"context_servers"' in result.output
        assert '"source": "custom"' in result.output
        assert "mcpServers" not in result.output
        run.assert_not_called()

    def test_multi_client_partial_failure_reports_only_failed_client(self):
        """One client succeeds and one fails: the exit is nonzero, the
        failure message names only the failed client, and the successful
        client's registration still went through."""

        def _run_side_effect(argv, **_kwargs):
            if "codex" in argv:
                return MagicMock(returncode=1, stdout="", stderr="boom")
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "pipecat_context_hub.cli_install.shutil.which",
                side_effect=lambda exe: f"/usr/bin/{exe}",
            ),
            patch(
                "pipecat_context_hub.cli_install.subprocess.run",
                side_effect=_run_side_effect,
            ) as run,
        ):
            result = runner.invoke(
                main,
                ["install", "--client", "claude-code", "--client", "codex", "--no-refresh"],
            )

        assert result.exit_code != 0
        assert run.call_count == 2
        assert "registered. Restart claude-code" in result.output
        assert "Failed to register with: codex" in result.output
        assert "claude-code" not in result.output.split("Failed to register with:")[1]

    def test_no_client_detected_falls_back_to_manual_instructions(self):
        with (
            patch("pipecat_context_hub.cli_install.shutil.which", return_value=None),
            patch("pipecat_context_hub.cli_install.subprocess.run") as run,
        ):
            result = runner.invoke(main, ["install", "--no-refresh"])

        assert result.exit_code == 0
        assert "mcpServers" in result.output
        for client in ("cursor", "vscode", "zed"):
            assert client in result.output
        run.assert_not_called()

    def test_no_refresh_skips_the_index_build(self):
        with patch("pipecat_context_hub.cli.refresh") as refresh:
            result = runner.invoke(main, ["install", "--client", "cursor", "--no-refresh"])
        assert result.exit_code == 0
        refresh.assert_not_called()
