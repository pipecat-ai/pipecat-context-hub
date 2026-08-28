"""Tests for the ``install`` command.

This command shells out to other tools and points clients at a server command,
so the things worth pinning are: which command it registers, that it never
edits a config file it does not own, and that the read-only paths stay
read-only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from pipecat_context_hub.cli import main
from pipecat_context_hub.cli_install import (
    _EXIT_MANUAL_SETUP,
    _EXIT_REGISTRATION_LOST,
    _claude_config,
    _config_matches_command,
    _detect_cli_clients,
    _mcp_json,
    _register_with_cli,
    _server_command,
)

runner = CliRunner()


def _absent_then_add(*, returncode: int = 0, stderr: str = ""):
    """A fake `subprocess.run`: `mcp get` reports the entry absent, and the
    terminal `mcp add` call returns the given result."""

    def fake_run(argv, **_kwargs):
        if argv[2] == "get":
            return MagicMock(
                returncode=1, stdout="", stderr='No MCP server named "pipecat-context-hub".'
            )
        return MagicMock(returncode=returncode, stdout="", stderr=stderr)

    return fake_run


class TestServerCommand:
    def test_names_this_interpreter_even_with_a_script_on_path(self):
        """A script on PATH now does not mean one at the client's session start.

        Any project venv carrying ``pipecat-ai[cli]`` puts ``pipecat-context-hub`` on
        PATH. Registering that bare name yielded a config that started only in the shell
        that wrote it, and failed with ENOENT everywhere else.
        """
        with patch("pipecat_context_hub.cli_install.shutil.which", return_value="/usr/bin/x"):
            assert _server_command() == [
                sys.executable,
                "-P",
                "-m",
                "pipecat_context_hub",
                "serve",
            ]

    def test_isolates_module_resolution_from_launch_cwd(self):
        """`-P` must be present so a client launched from an untrusted directory
        (e.g. one containing its own `pipecat_context_hub/` package) can't have
        that directory's copy shadow the real installed one via `sys.path[0]`.
        This matters most now that a fresh registration is `user`-scoped and can
        be launched from any directory, not just the one `install` ran in."""
        command = _server_command()
        assert command[0] == sys.executable
        assert command[1] == "-P"
        assert command[2:] == ["-m", "pipecat_context_hub", "serve"]

    def test_is_independent_of_the_environment(self):
        """What gets registered must not depend on where install happened to run."""
        seen = []
        for which in ("/usr/bin/x", None):
            with patch("pipecat_context_hub.cli_install.shutil.which", return_value=which):
                seen.append(_server_command())
        assert seen[0] == seen[1]

    def test_never_registers_the_pipecat_front_door(self):
        """Registering `pipecat context-hub serve` would load typer on every server start."""
        assert _server_command()[0] != "pipecat"


class TestClaudeConfig:
    """`_claude_config` reads Claude's private config files directly; exercise
    the real file-parsing logic instead of mocking it out."""

    def test_project_scope_reads_mcp_json_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        entry = {"type": "stdio", "command": "x", "args": ["serve"]}
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"pipecat-context-hub": entry}})
        )
        assert _claude_config("project") == entry

    def test_user_scope_reads_top_level_mcp_servers(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        entry = {"type": "stdio", "command": "x", "args": ["serve"]}
        (tmp_path / ".claude.json").write_text(
            json.dumps({"mcpServers": {"pipecat-context-hub": entry}})
        )
        assert _claude_config("user") == entry

    def test_local_scope_matches_cwd_exactly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)
        entry = {"type": "stdio", "command": "x", "args": ["serve"]}
        (tmp_path / ".claude.json").write_text(
            json.dumps(
                {"projects": {str(tmp_path): {"mcpServers": {"pipecat-context-hub": entry}}}}
            )
        )
        assert _claude_config("local") == entry

    def test_local_scope_matches_via_resolved_symlink(self, tmp_path, monkeypatch):
        """A registration stored under a symlinked project path must still be
        found when the process reports a different (but equivalent) cwd."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Patch Path.cwd directly rather than os.chdir(link): on POSIX,
        # os.getcwd() already resolves symlinks at the OS level, which would
        # silently defeat this test.
        monkeypatch.setattr(Path, "cwd", lambda: real)
        entry = {"type": "stdio", "command": "x", "args": ["serve"]}
        (tmp_path / ".claude.json").write_text(
            json.dumps({"projects": {str(link): {"mcpServers": {"pipecat-context-hub": entry}}}})
        )
        assert _claude_config("local") == entry

    def test_missing_key_still_raises_key_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
        with pytest.raises(KeyError):
            _claude_config("project")


class TestConfigMatchesCommand:
    def test_explicit_null_args_does_not_raise_and_is_treated_as_empty(self):
        """`config.get("args", [])` only applies the default when the key is
        absent, not when it is present with value `None` -- a stored entry with
        `"args": null` must be coalesced to `[]`, not unpacked as `None`."""
        config = {"type": "stdio", "command": "x", "args": None}
        assert _config_matches_command(config, ["x"], default_type="stdio") is True
        assert _config_matches_command(config, ["x", "serve"], default_type="stdio") is False


class TestRegisterWithCli:
    """``mcp add`` refuses a name it already has, so registering must repair, not skip."""

    def _fake_client(
        self,
        *,
        get_code: int,
        get_stdout: str = "",
        get_stderr: str = "",
        add_code: int = 0,
    ):
        """A client CLI whose `mcp get` and `mcp add` return the given codes."""
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[1:3] == ["mcp", "get"]:
                return MagicMock(returncode=get_code, stdout=get_stdout, stderr=get_stderr)
            return MagicMock(returncode=add_code, stdout="", stderr="")

        return calls, fake_run

    def _subcommands(self, calls: list[list[str]]) -> list[str]:
        return [argv[2] for argv in calls]

    def test_a_matching_entry_is_left_alone(self):
        """Removing a correct registration to rewrite it risks losing it for nothing."""
        recorded = (
            f"Command: {sys.executable}\n  Args: -P -m pipecat_context_hub serve\n"
            "To remove this server, run: claude mcp remove pipecat-context-hub -s local"
        )
        calls, fake_run = self._fake_client(get_code=0, get_stdout=recorded)
        existing = {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-P", "-m", "pipecat_context_hub", "serve"],
            "env": {},
        }
        with (
            patch("pipecat_context_hub.cli_install.subprocess.run", fake_run),
            patch("pipecat_context_hub.cli_install._claude_config", return_value=existing),
        ):
            assert _register_with_cli("claude-code", _server_command()) == "ok"
        assert self._subcommands(calls) == ["get"]

    def test_a_stale_entry_is_replaced(self):
        """The bare-name registrations already written must not survive a reinstall."""
        recorded = (
            "Command: pipecat-context-hub\n  Args: serve\n"
            "To remove this server, run: claude mcp remove pipecat-context-hub -s user"
        )
        calls, fake_run = self._fake_client(get_code=0, get_stdout=recorded)
        existing = {"type": "stdio", "command": "pipecat-context-hub", "args": ["serve"]}
        with (
            patch("pipecat_context_hub.cli_install.subprocess.run", fake_run),
            patch("pipecat_context_hub.cli_install._claude_config", return_value=existing),
        ):
            assert _register_with_cli("claude-code", _server_command()) == "ok"
        assert self._subcommands(calls) == ["get", "remove", "add"]

    @pytest.mark.parametrize("scope", ["local", "project"])
    def test_mismatched_entry_is_repaired_at_its_own_scope_not_promoted_to_user(self, scope):
        """A mismatched entry that already exists at `local` or `project` scope must
        be replaced at that SAME scope, not silently promoted to the fresh-registration
        `user` scope -- that would override a deliberate prior scoping choice."""
        recorded = (
            "Command: pipecat-context-hub\n  Args: serve\n"
            f"To remove this server, run: claude mcp remove pipecat-context-hub -s {scope}"
        )
        calls, fake_run = self._fake_client(get_code=0, get_stdout=recorded)
        existing = {"type": "stdio", "command": "pipecat-context-hub", "args": ["serve"]}
        with (
            patch("pipecat_context_hub.cli_install.subprocess.run", fake_run),
            patch("pipecat_context_hub.cli_install._claude_config", return_value=existing),
        ):
            assert _register_with_cli("claude-code", _server_command()) == "ok"
        remove = next(c for c in calls if c[2] == "remove")
        add = next(c for c in calls if c[2] == "add")
        assert remove[3:6] == ["pipecat-context-hub", "-s", scope]
        assert add[2:5] == ["add", "-s", scope]

    def test_unparseable_get_failure_fails_closed_without_attempting_repair(self):
        """`mcp get` failing with an unrecognized error (not a clean "not found")
        means an entry may or may not be there, and its scope is unknown. Rather
        than attempting an unscoped, destructive `remove` with no captured
        rollback state, this fails closed: report failure and leave whatever is
        there untouched."""
        calls, fake_run = self._fake_client(get_code=1, get_stderr="internal error: corrupt state")
        with patch("pipecat_context_hub.cli_install.subprocess.run", fake_run):
            assert _register_with_cli("claude-code", _server_command()) == "failed"
        assert self._subcommands(calls) == ["get"]

    def test_codex_mismatch_uses_atomic_overwrite(self):
        recorded = json.dumps(
            {
                "transport": {
                    "type": "stdio",
                    "command": "pipecat-context-hub",
                    "args": ["serve"],
                }
            }
        )
        calls, fake_run = self._fake_client(get_code=0, get_stdout=recorded)
        with patch("pipecat_context_hub.cli_install.subprocess.run", fake_run):
            assert _register_with_cli("codex", _server_command()) == "ok"
        assert self._subcommands(calls) == ["get", "add"]

    def test_registers_when_the_client_cannot_report_one(self):
        """An unambiguously absent server can be registered without removal."""
        calls, fake_run = self._fake_client(
            get_code=1, get_stderr='No MCP server named "pipecat-context-hub".'
        )
        with patch("pipecat_context_hub.cli_install.subprocess.run", fake_run):
            assert _register_with_cli("claude-code", _server_command()) == "ok"
        assert "add" in self._subcommands(calls)

    def test_a_fresh_claude_entry_is_registered_for_every_directory(self):
        """A directory-scoped entry leaves every other project without the server."""
        calls, fake_run = self._fake_client(
            get_code=1, get_stderr='No MCP server named "pipecat-context-hub".'
        )
        with patch("pipecat_context_hub.cli_install.subprocess.run", fake_run):
            assert _register_with_cli("claude-code", _server_command()) == "ok"
        add = next(c for c in calls if c[2] == "add")
        assert add[2:5] == ["add", "-s", "user"]

    def test_codex_is_registered_without_a_scope(self):
        """Codex has no scope concept; a `-s` would be rejected."""
        calls, fake_run = self._fake_client(
            get_code=1, get_stderr='No MCP server named "pipecat-context-hub".'
        )
        with patch("pipecat_context_hub.cli_install.subprocess.run", fake_run):
            assert _register_with_cli("codex", _server_command()) == "ok"
        add = next(c for c in calls if c[2] == "add")
        assert "-s" not in add

    def test_a_failed_add_is_reported(self):
        calls, fake_run = self._fake_client(
            get_code=1,
            get_stderr='No MCP server named "pipecat-context-hub".',
            add_code=1,
        )
        with patch("pipecat_context_hub.cli_install.subprocess.run", fake_run):
            assert _register_with_cli("claude-code", _server_command()) == "failed"

    def test_failed_replacement_restores_the_exact_existing_entry(self):
        existing = {
            "type": "stdio",
            "command": "old-command",
            "args": ["arg with spaces"],
            "env": {"TOKEN": "old-value"},
        }
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            subcommand = argv[2]
            if subcommand == "get":
                stdout = (
                    "Command: old-command\n"
                    "To remove this server, run: claude mcp remove "
                    "pipecat-context-hub -s user"
                )
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            if subcommand == "add":
                return MagicMock(returncode=1, stdout="", stderr="permission denied")
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("pipecat_context_hub.cli_install.subprocess.run", fake_run),
            patch("pipecat_context_hub.cli_install._claude_config", return_value=existing),
        ):
            assert _register_with_cli("claude-code", _server_command()) == "failed"

        assert self._subcommands(calls) == ["get", "remove", "add", "add-json"]
        rollback = calls[-1]
        assert rollback[3:6] == ["-s", "user", "pipecat-context-hub"]
        assert json.loads(rollback[6]) == existing

    def test_get_timeout_falls_through_to_a_plain_registration(self):
        """A get failure means nothing is confirmed to exist, so it's safe to
        fall through to a plain, non-destructive `mcp add` — never a `remove`."""
        calls: list[list[str]] = []
        first_call = True

        def fake_run(argv, **kwargs):
            nonlocal first_call
            calls.append(argv)
            if first_call:
                first_call = False
                raise subprocess.TimeoutExpired(argv, timeout=60)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("pipecat_context_hub.cli_install.subprocess.run", fake_run):
            assert _register_with_cli("claude-code", _server_command()) == "ok"

        assert self._subcommands(calls) == ["get", "add"]
        add = next(c for c in calls if c[2] == "add")
        assert "-s" not in add

    def test_unknown_registration_state_is_not_treated_as_fresh(self):
        """`mcp get` failing to run at all (e.g. a timeout) means `unknown`, not
        `absent` -- an entry may already exist at some uninspected scope. Applying
        the fresh-registration `-s user` here would risk creating a stray duplicate
        instead of leaving a possibly-existing registration alone."""
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[2] == "get":
                raise subprocess.TimeoutExpired(argv, timeout=60)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("pipecat_context_hub.cli_install.subprocess.run", fake_run):
            assert _register_with_cli("claude-code", _server_command()) == "ok"

        add = next(c for c in calls if c[2] == "add")
        assert "-s" not in add

    def test_failed_replacement_with_failed_rollback_is_reported_as_corrupted(self):
        """When both the replacement add and the rollback fail, the previous
        registration is gone, not merely unchanged — that must be surfaced
        distinctly from a plain failure."""
        existing = {
            "type": "stdio",
            "command": "old-command",
            "args": ["arg with spaces"],
        }

        def fake_run(argv, **kwargs):
            subcommand = argv[2]
            if subcommand == "get":
                stdout = (
                    "Command: old-command\n"
                    "To remove this server, run: claude mcp remove "
                    "pipecat-context-hub -s user"
                )
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            if subcommand == "add":
                return MagicMock(returncode=1, stdout="", stderr="permission denied")
            if subcommand == "add-json":
                return MagicMock(returncode=1, stdout="", stderr="rollback also failed")
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("pipecat_context_hub.cli_install.subprocess.run", fake_run),
            patch("pipecat_context_hub.cli_install._claude_config", return_value=existing),
        ):
            assert _register_with_cli("claude-code", _server_command()) == "corrupted"

    def test_remove_timeout_still_attempts_rollback(self):
        """`mcp remove` itself failing (not just returning nonzero) must still
        trigger a rollback attempt for a captured Claude entry."""
        existing = {
            "type": "stdio",
            "command": "old-command",
            "args": ["arg with spaces"],
        }
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            subcommand = argv[2]
            if subcommand == "get":
                stdout = (
                    "Command: old-command\n"
                    "To remove this server, run: claude mcp remove "
                    "pipecat-context-hub -s user"
                )
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            if subcommand == "remove":
                raise subprocess.TimeoutExpired(argv, timeout=60)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("pipecat_context_hub.cli_install.subprocess.run", fake_run),
            patch("pipecat_context_hub.cli_install._claude_config", return_value=existing),
        ):
            assert _register_with_cli("claude-code", _server_command()) == "failed"

        assert self._subcommands(calls) == ["get", "remove", "add-json"]

    def test_misleading_scope_fragment_in_args_is_not_mistaken_for_the_real_scope(self):
        """An Args line that happens to contain `mcp remove ... -s user` must not
        be matched ahead of the real instruction line reporting `-s local`."""
        recorded = (
            "Command: pipecat-context-hub\n"
            "  Args: --note 'run: claude mcp remove pipecat-context-hub -s user'\n"
            "To remove this server, run: claude mcp remove pipecat-context-hub -s local"
        )
        calls, fake_run = self._fake_client(get_code=0, get_stdout=recorded)
        existing = {"type": "stdio", "command": "pipecat-context-hub", "args": ["serve"]}
        with (
            patch("pipecat_context_hub.cli_install.subprocess.run", fake_run),
            patch(
                "pipecat_context_hub.cli_install._claude_config", return_value=existing
            ) as mock_cfg,
        ):
            assert _register_with_cli("claude-code", _server_command()) == "ok"
        # must have read the scope reported on the *instruction* line, not the Args line
        mock_cfg.assert_called_once_with("local")

    def test_scope_fragment_without_the_real_instruction_line_is_treated_as_unparseable(self):
        """No literal 'To remove this server, run:' line at all — must not
        fall back to matching a scope-shaped fragment elsewhere in stdout."""
        recorded = (
            "Command: pipecat-context-hub\n"
            "  Args: --note 'mcp remove pipecat-context-hub -s user'\n"
        )
        calls, fake_run = self._fake_client(get_code=0, get_stdout=recorded)
        with patch("pipecat_context_hub.cli_install.subprocess.run", fake_run):
            assert _register_with_cli("claude-code", _server_command()) == "failed"
        assert self._subcommands(calls) == ["get"]


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
        assert result.exit_code == _EXIT_MANUAL_SETUP
        assert "mcpServers" in result.output
        assert ".cursor/mcp.json" in result.output
        run.assert_not_called()

    def test_manual_setup_is_distinguishable_from_a_configured_client(self):
        """Both outcomes print and neither is an error, so only the exit code separates them.

        A wrapper that captures the output — `pipecat init` does — otherwise reports a
        registration that never happened, and swallows the config block the user needed.
        """
        with patch("pipecat_context_hub.cli_install.subprocess.run"):
            manual = runner.invoke(main, ["install", "--client", "cursor", "--no-refresh"])

        with (
            patch("pipecat_context_hub.cli_install.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pipecat_context_hub.cli_install.subprocess.run",
                side_effect=_absent_then_add(),
            ),
        ):
            registered = runner.invoke(main, ["install", "--client", "claude-code", "--no-refresh"])
        assert manual.exit_code == _EXIT_MANUAL_SETUP
        assert registered.exit_code == 0

    def test_cli_client_is_registered_through_its_own_cli(self):
        with (
            patch("pipecat_context_hub.cli_install.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pipecat_context_hub.cli_install.subprocess.run",
                side_effect=_absent_then_add(),
            ) as run,
        ):
            result = runner.invoke(main, ["install", "--client", "claude-code", "--no-refresh"])

        assert result.exit_code == 0
        argv = run.call_args.args[0]
        assert argv[:6] == ["claude", "mcp", "add", "-s", "user", "pipecat-context-hub"]
        # The `--` separator keeps the server's own args out of claude's parser.
        assert argv[6] == "--"
        assert argv[7:] == [sys.executable, "-P", "-m", "pipecat_context_hub", "serve"]

    def test_client_cli_failure_is_reported_as_error_exit(self):
        """A failed client registration is surfaced via a nonzero exit, not silently ignored."""

        with (
            patch("pipecat_context_hub.cli_install.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pipecat_context_hub.cli_install.subprocess.run",
                side_effect=_absent_then_add(returncode=1, stderr="already exists"),
            ),
        ):
            result = runner.invoke(main, ["install", "--client", "claude-code", "--no-refresh"])

        assert result.exit_code != 0
        assert "already exists" in result.output
        assert "Failed to register with: claude-code" in result.output

    def test_corrupted_registration_gets_its_own_exit_code(self):
        """A repair that loses the previous registration must be distinguishable
        from a plain failure by exit code alone, not just message text — a caller
        scripting around this command cannot rely on parsing stderr."""
        existing = {"type": "stdio", "command": "old-command", "args": []}

        def fake_run(argv, **_kwargs):
            subcommand = argv[2]
            if subcommand == "get":
                stdout = (
                    "Command: old-command\n"
                    "To remove this server, run: claude mcp remove "
                    "pipecat-context-hub -s user"
                )
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            if subcommand == "add":
                return MagicMock(returncode=1, stdout="", stderr="permission denied")
            if subcommand == "add-json":
                return MagicMock(returncode=1, stdout="", stderr="rollback also failed")
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("pipecat_context_hub.cli_install.shutil.which", return_value="/usr/bin/claude"),
            patch("pipecat_context_hub.cli_install.subprocess.run", side_effect=fake_run),
            patch("pipecat_context_hub.cli_install._claude_config", return_value=existing),
        ):
            result = runner.invoke(main, ["install", "--client", "claude-code", "--no-refresh"])

        assert result.exit_code == _EXIT_REGISTRATION_LOST
        assert result.exit_code != _EXIT_MANUAL_SETUP
        assert "previous registration lost" in result.output

    def test_vscode_client_prints_servers_schema(self):
        with patch("pipecat_context_hub.cli_install.subprocess.run") as run:
            result = runner.invoke(main, ["install", "--client", "vscode", "--no-refresh"])
        assert result.exit_code == _EXIT_MANUAL_SETUP
        assert '"servers"' in result.output
        assert '"type": "stdio"' in result.output
        assert "mcpServers" not in result.output
        run.assert_not_called()

    def test_zed_client_prints_context_servers_schema(self):
        with patch("pipecat_context_hub.cli_install.subprocess.run") as run:
            result = runner.invoke(main, ["install", "--client", "zed", "--no-refresh"])
        assert result.exit_code == _EXIT_MANUAL_SETUP
        assert '"context_servers"' in result.output
        assert '"source": "custom"' in result.output
        assert "mcpServers" not in result.output
        run.assert_not_called()

    def test_multi_client_partial_failure_reports_only_failed_client(self):
        """One client succeeds and one fails: the exit is nonzero, the
        failure message names only the failed client, and the successful
        client's registration still went through."""

        def _run_side_effect(argv, **_kwargs):
            if argv[2] == "get":
                return MagicMock(
                    returncode=1,
                    stdout="",
                    stderr=f"No MCP server named '{argv[-1]}' found.",
                )
            if argv[0] == "codex":
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
        registered = [c.args[0][2] for c in run.call_args_list if c.args[0][1] == "mcp"]
        assert registered.count("add") == 2
        assert "registered. Restart claude-code" in result.output
        assert "Failed to register with: codex" in result.output
        assert "claude-code" not in result.output.split("Failed to register with:")[1]

    def test_no_client_detected_falls_back_to_manual_instructions(self):
        with (
            patch("pipecat_context_hub.cli_install.shutil.which", return_value=None),
            patch("pipecat_context_hub.cli_install.subprocess.run") as run,
        ):
            result = runner.invoke(main, ["install", "--no-refresh"])

        assert result.exit_code == _EXIT_MANUAL_SETUP
        assert "mcpServers" in result.output
        for client in ("cursor", "vscode", "zed"):
            assert client in result.output
        run.assert_not_called()

    def test_no_refresh_skips_the_index_build(self):
        with patch("pipecat_context_hub.cli.refresh") as refresh:
            result = runner.invoke(main, ["install", "--client", "cursor", "--no-refresh"])
        assert result.exit_code == _EXIT_MANUAL_SETUP
        refresh.assert_not_called()
