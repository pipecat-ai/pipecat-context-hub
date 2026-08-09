"""Unit tests for shared/env_loading.py — cwd .env + global config.toml loading.

Covers dev plan Phase 1 (docs/dev_plans/20260807-feature-global-config-toml.md):
precedence (real env > cwd .env > config.toml > defaults), malformed/unreadable
config.toml handling, array coercion, the invocation-scoped skip-list, the
PIPECAT_HUB_DATA_DIR collision guard, the "Loaded N key(s)" log line, and the
rewritten `_isolate_env_vars` autouse fixture in tests/conftest.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pipecat_context_hub.shared import env_loading
from pipecat_context_hub.shared.config import HubConfig, StorageConfig
from pipecat_context_hub.shared.env_loading import (
    _INVOCATION_SCOPED_KEYS,
    PRUNE_ENV_VAR,
    config_collides_with_dir,
    load_cwd_dotenv,
    load_global_config,
    resolve_global_config_path,
)


def _use_config_file(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Point the loader's lookup path at `path` (the test-hermeticity seam)."""
    monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(path))


def _toml_str(value: object) -> str:
    """Escape a value for embedding in a TOML basic (double-quoted) string.

    Needed anywhere a `Path` is interpolated into hand-written config.toml
    content in these tests: on Windows, path separators are backslashes,
    which TOML's basic-string grammar treats as escape-sequence introducers
    (e.g. `\\U...` as an 8-hex-digit unicode escape) — writing a raw
    WindowsPath into an f-string TOML value produces invalid TOML that
    `tomllib` fails to parse.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


class TestLoadCwdDotenv:
    """Tests for the cwd .env parser — moved verbatim from test_cli.py's
    TestLoadDotenv (module move, dev plan Phase 1). This module is the only
    home for the loader now: `cli._load_dotenv` was a private re-export with
    no callers and has been removed.
    """

    def test_basic_unquoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("FOO=bar\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FOO", raising=False)
        load_cwd_dotenv()
        assert os.environ["FOO"] == "bar"

    def test_double_quoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text('KEY="hello world"\n')
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        load_cwd_dotenv()
        assert os.environ["KEY"] == "hello world"

    def test_single_quoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("KEY='hello world'\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        load_cwd_dotenv()
        assert os.environ["KEY"] == "hello world"

    def test_inline_comment_stripped_unquoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("KEY=value # this is a comment\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        load_cwd_dotenv()
        assert os.environ["KEY"] == "value"

    def test_inline_comment_stripped_quoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text('KEY="org/a,org/b" # note\n')
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        load_cwd_dotenv()
        assert os.environ["KEY"] == "org/a,org/b"

    def test_hash_inside_quotes_preserved(self, tmp_path: Path, monkeypatch):
        """Hash inside quotes is NOT treated as a comment."""
        (tmp_path / ".env").write_text('KEY="color #fff"\n')
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        load_cwd_dotenv()
        assert os.environ["KEY"] == "color #fff"

    def test_comment_lines_skipped(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("# comment\nKEY=val\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        load_cwd_dotenv()
        assert os.environ["KEY"] == "val"

    def test_empty_lines_skipped(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("\n\nKEY=val\n\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        load_cwd_dotenv()
        assert os.environ["KEY"] == "val"

    def test_existing_env_not_overwritten(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("KEY=from_file\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KEY", "from_shell")
        load_cwd_dotenv()
        assert os.environ["KEY"] == "from_shell"

    def test_no_env_file(self, tmp_path: Path, monkeypatch):
        """No .env file is fine — no error raised."""
        monkeypatch.chdir(tmp_path)
        load_cwd_dotenv()  # should not raise

    def test_repo_slugs_with_inline_comment(self, tmp_path: Path, monkeypatch):
        """Realistic case: PIPECAT_HUB_EXTRA_REPOS with inline comment."""
        (tmp_path / ".env").write_text(
            'PIPECAT_HUB_EXTRA_REPOS="org/repo-a,org/repo-b" # community repos\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PIPECAT_HUB_EXTRA_REPOS", raising=False)
        load_cwd_dotenv()
        assert os.environ["PIPECAT_HUB_EXTRA_REPOS"] == "org/repo-a,org/repo-b"


class TestResolveGlobalConfigPath:
    """Tests for the shared lookup-path resolver."""

    def test_default_when_no_override(self, monkeypatch):
        monkeypatch.delenv("PIPECAT_HUB_CONFIG_FILE", raising=False)
        assert resolve_global_config_path() == env_loading.DEFAULT_CONFIG_PATH

    def test_env_override_wins(self, tmp_path: Path, monkeypatch):
        override = tmp_path / "custom" / "config.toml"
        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(override))
        assert resolve_global_config_path() == override

    def test_monkeypatched_default_is_observed(self, tmp_path: Path, monkeypatch):
        """DEFAULT_CONFIG_PATH must be read via module-attribute lookup at
        call time — a captured default or an aliased import would make this
        monkeypatch silently inert (dev plan Phase 1)."""
        patched = tmp_path / "mp-default" / "config.toml"
        monkeypatch.delenv("PIPECAT_HUB_CONFIG_FILE", raising=False)
        monkeypatch.setattr(env_loading, "DEFAULT_CONFIG_PATH", patched)
        assert resolve_global_config_path() == patched

    def test_default_config_path_not_relative_to_default_data_dir(self):
        """Path-invariant check, no monkeypatching: the real default lookup
        path and the real default StorageConfig data dir must not nest
        inside each other (Architecture Decisions — refresh --reset-index
        rmtrees data_dir wholesale, so config.toml must default outside it).
        """
        data_dir = StorageConfig().data_dir
        config_path = env_loading.DEFAULT_CONFIG_PATH
        # None only when Path.home() failed at import time (test env has a
        # resolvable home) — narrow for mypy, assert for a clear failure if
        # that assumption is ever wrong.
        assert config_path is not None
        assert not config_path.is_relative_to(data_dir)
        assert not data_dir.is_relative_to(config_path.parent)

    def test_unresolvable_home_returns_none_not_override(self, monkeypatch):
        """No PIPECAT_HUB_CONFIG_FILE override + unresolvable default (module
        attribute simulated as None, standing in for a real Path.home()
        RuntimeError at import time — see test_import_survives_unresolvable_home
        below for the actual import-time crash coverage) resolves to None,
        not a raised exception."""
        monkeypatch.delenv("PIPECAT_HUB_CONFIG_FILE", raising=False)
        monkeypatch.setattr(env_loading, "DEFAULT_CONFIG_PATH", None)
        assert resolve_global_config_path() is None

    def test_import_survives_unresolvable_home(self, monkeypatch):
        """Module-level DEFAULT_CONFIG_PATH computation must not crash import
        when Path.home() raises RuntimeError (e.g. a hardened container with
        no /etc/passwd entry for the running UID) — reproduces the actual
        import-time failure mode, not just the module-attribute stand-in
        above. Every entry point (cli.py's console-script main, the
        dashboard scripts, scripts/smoke_check_removals.py) imports this
        module at the top level, so a crash here would take all of them
        down before argv is even parsed."""
        import importlib
        import pathlib

        original_home = pathlib.Path.home

        def _raise_no_home(*args, **kwargs):
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(pathlib.Path, "home", _raise_no_home)
        try:
            reloaded = importlib.reload(env_loading)
            assert reloaded.DEFAULT_CONFIG_PATH is None
        finally:
            monkeypatch.setattr(pathlib.Path, "home", original_home)
            importlib.reload(env_loading)  # restore real DEFAULT_CONFIG_PATH for later tests


class TestLoadGlobalConfigBasics:
    """File presence, precedence, and the first-writer-wins contract."""

    def test_missing_file_returns_silently(self, tmp_path: Path, monkeypatch, caplog):
        _use_config_file(monkeypatch, tmp_path / "does-not-exist" / "config.toml")
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert caplog.records == []
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ

    def test_empty_file(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        _use_config_file(monkeypatch, config_file)
        load_global_config()  # should not raise
        assert not any(
            k.startswith("PIPECAT_HUB_") and k != "PIPECAT_HUB_CONFIG_FILE" for k in os.environ
        )

    def test_fills_unset_var(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "45"

    def test_real_env_var_wins_over_config_toml(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "99"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "7")
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "7"

    def test_cwd_dotenv_wins_over_config_toml(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "99"\n')
        _use_config_file(monkeypatch, config_file)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".env").write_text("PIPECAT_HUB_STALE_AFTER_DAYS=7\n")
        monkeypatch.chdir(project_dir)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        # Same call order as every real entry point: cwd .env, then config.toml.
        load_cwd_dotenv()
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "7"


class TestLoadGlobalConfigErrors:
    """Malformed/unreadable config.toml must warn and never raise."""

    def test_malformed_toml_syntax_warns_and_does_not_raise(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        config_file = tmp_path / "config.toml"
        config_file.write_text("PIPECAT_HUB_STALE_AFTER_DAYS = [1, 2\n")  # unterminated array
        _use_config_file(monkeypatch, config_file)
        with caplog.at_level("WARNING"):
            load_global_config()  # should not raise
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert str(config_file) in caplog.text

    def test_invalid_utf8_warns_and_does_not_raise(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        # A lone 0x80 byte inside a value token, where tomllib actually
        # attempts to decode it (not trailing after content it could stop
        # parsing before reaching). write_bytes(), not write_text(), since
        # Path.write_text() always encodes through a valid str/bytes
        # boundary and cannot itself produce invalid UTF-8.
        config_file.write_bytes(b'PIPECAT_HUB_STALE_AFTER_DAYS = "bad\x80value"\n')
        _use_config_file(monkeypatch, config_file)
        with caplog.at_level("WARNING"):
            load_global_config()  # should not raise
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert str(config_file) in caplog.text

    def test_read_failure_directory_as_path_warns_and_does_not_raise(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        config_dir_as_file = tmp_path / "config.toml"
        config_dir_as_file.mkdir()  # a directory where the loader expects a file
        _use_config_file(monkeypatch, config_dir_as_file)
        with caplog.at_level("WARNING"):
            load_global_config()  # should not raise
        assert str(config_dir_as_file) in caplog.text


class TestLoadGlobalConfigKeyFiltering:
    """Non-PIPECAT_HUB_ keys, non-scalar values, and invocation-scoped keys."""

    def test_non_prefixed_key_skipped_with_warning(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('HF_HOME = "/somewhere"\n')
        _use_config_file(monkeypatch, config_file)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "HF_HOME" not in os.environ
        assert "HF_HOME" in caplog.text

    def test_table_value_skipped_with_warning(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[PIPECAT_HUB_NESTED]\nfoo = 1\n")
        _use_config_file(monkeypatch, config_file)
        with caplog.at_level("WARNING"):
            load_global_config()  # should not raise
        assert "PIPECAT_HUB_NESTED" not in os.environ
        assert "PIPECAT_HUB_NESTED" in caplog.text

    def test_mixed_type_array_skipped_with_warning(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_EXTRA_REPOS = ["a", 1]\n')
        _use_config_file(monkeypatch, config_file)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "PIPECAT_HUB_EXTRA_REPOS" not in os.environ
        assert "PIPECAT_HUB_EXTRA_REPOS" in caplog.text

    def test_nested_array_skipped_with_warning(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_EXTRA_REPOS = [["a"], ["b"]]\n')
        _use_config_file(monkeypatch, config_file)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "PIPECAT_HUB_EXTRA_REPOS" not in os.environ
        assert "PIPECAT_HUB_EXTRA_REPOS" in caplog.text

    def test_invocation_scoped_keys_pinned(self):
        """Drift-alarm: the exact five-key skip-list, not just membership."""
        assert _INVOCATION_SCOPED_KEYS == frozenset(
            {
                PRUNE_ENV_VAR,
                "PIPECAT_HUB_DEBUG_PROBE",
                "PIPECAT_HUB_CONFIG_FILE",
                "PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK",
                "PIPECAT_HUB_STABILITY_OUTPUT",
            }
        )

    @pytest.mark.parametrize(
        "key,toml_value",
        [
            (PRUNE_ENV_VAR, "true"),
            ("PIPECAT_HUB_DEBUG_PROBE", "1"),
            ("PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK", "1"),
            ("PIPECAT_HUB_STABILITY_OUTPUT", '"/tmp/out.json"'),
        ],
    )
    def test_invocation_scoped_key_skipped_with_warning(
        self, key: str, toml_value: str, tmp_path: Path, monkeypatch, caplog
    ):
        config_file = tmp_path / "config.toml"
        config_file.write_text(f"{key} = {toml_value}\n")
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv(key, raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert key not in os.environ
        assert "invocation-scoped" in caplog.text
        assert key in caplog.text

    def test_config_file_key_in_config_toml_has_no_meaning(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """PIPECAT_HUB_CONFIG_FILE set *inside* config.toml cannot redirect
        the loader's own lookup path from inside the file it locates —
        enforced by the skip-list, not merely true by absence of a consumer.
        """
        real_config_file = tmp_path / "config.toml"
        decoy_path = tmp_path / "decoy" / "config.toml"
        real_config_file.write_text(f'PIPECAT_HUB_CONFIG_FILE = "{_toml_str(decoy_path)}"\n')
        _use_config_file(monkeypatch, real_config_file)
        before = os.environ["PIPECAT_HUB_CONFIG_FILE"]
        with caplog.at_level("WARNING"):
            load_global_config()
        assert os.environ["PIPECAT_HUB_CONFIG_FILE"] == before
        assert resolve_global_config_path() == real_config_file
        assert "invocation-scoped" in caplog.text


class TestLoadGlobalConfigArrayCoercion:
    """Homogeneous scalar arrays coerce to CSV strings; heterogeneous/comma-bearing skip."""

    def test_string_array_coerces_to_csv(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_EXTRA_REPOS = ["a", "b", "c"]\n')
        _use_config_file(monkeypatch, config_file)
        load_global_config()
        assert os.environ["PIPECAT_HUB_EXTRA_REPOS"] == "a,b,c"

    def test_empty_array_coerces_to_empty_string(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("PIPECAT_HUB_EXTRA_REPOS = []\n")
        _use_config_file(monkeypatch, config_file)
        load_global_config()
        assert os.environ["PIPECAT_HUB_EXTRA_REPOS"] == ""

    def test_non_string_scalar_array_coerces_to_csv(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("PIPECAT_HUB_EXTRA_REPOS = [1, 2]\n")
        _use_config_file(monkeypatch, config_file)
        load_global_config()
        assert os.environ["PIPECAT_HUB_EXTRA_REPOS"] == "1,2"

    def test_array_element_with_comma_skipped_with_warning(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_EXTRA_REPOS = ["org/repo,a"]\n')
        _use_config_file(monkeypatch, config_file)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "PIPECAT_HUB_EXTRA_REPOS" not in os.environ
        assert "PIPECAT_HUB_EXTRA_REPOS" in caplog.text


class TestLoadGlobalConfigDataDirCollision:
    """PIPECAT_HUB_DATA_DIR from config.toml must not swallow the active config file."""

    def test_colliding_data_dir_skipped_with_warning(self, tmp_path: Path, monkeypatch, caplog):
        config_dir = tmp_path / "cfg-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{_toml_str(config_dir)}"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATA_DIR", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "PIPECAT_HUB_DATA_DIR" not in os.environ
        assert "PIPECAT_HUB_DATA_DIR" in caplog.text

    def test_non_colliding_data_dir_accepted(self, tmp_path: Path, monkeypatch):
        config_dir = tmp_path / "cfg-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        data_dir = tmp_path / "data-home"
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{_toml_str(data_dir)}"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATA_DIR", raising=False)
        load_global_config()
        assert os.environ["PIPECAT_HUB_DATA_DIR"] == str(
            data_dir.expanduser().resolve(strict=False)
        )

    def test_nonexistent_active_config_never_triggers_collision_skip(
        self, tmp_path: Path, monkeypatch
    ):
        """A colliding PIPECAT_HUB_DATA_DIR only matters once there's a real
        file in effect to protect — this is exercised end-to-end via
        `_delete_local_index_storage()` in test_cli.py; here we confirm the
        loader itself only guards while reading an existing config file (the
        file being read always exists by construction of this loader call),
        i.e. the guard is keyed on `.is_file()`, not path existence in the
        abstract.
        """
        config_dir = tmp_path / "cfg-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{_toml_str(config_dir)}"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATA_DIR", raising=False)
        resolved_config_path = resolve_global_config_path()
        assert resolved_config_path is not None
        assert resolved_config_path.is_file()
        load_global_config()
        assert "PIPECAT_HUB_DATA_DIR" not in os.environ

    def test_colliding_data_dir_no_warning_when_real_env_var_already_wins(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """The collision check must run AFTER the precedence gate: if a real
        env var already set PIPECAT_HUB_DATA_DIR to something ordinary,
        config.toml's colliding value was never eligible to be applied, and
        no misleading 'would relocate the data dir' warning should fire for
        it — that warning is only meaningful when config.toml's value is
        actually the one in contention."""
        config_dir = tmp_path / "cfg-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{_toml_str(config_dir)}"\n')
        _use_config_file(monkeypatch, config_file)
        real_data_dir = tmp_path / "real-data-dir"
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(real_data_dir))
        with caplog.at_level("WARNING"):
            load_global_config()
        assert os.environ["PIPECAT_HUB_DATA_DIR"] == str(real_data_dir)
        assert "PIPECAT_HUB_DATA_DIR" not in caplog.text

    def test_symlink_inside_candidate_dir_pointing_outside_is_collision(
        self, tmp_path: Path, monkeypatch
    ):
        """The active config path is a symlink located INSIDE candidate_dir,
        whose target is a real file OUTSIDE candidate_dir. rmtree(candidate_dir)
        would still unlink the symlink hop itself, destroying the operator's
        only path to their config — this is the exact false-negative the
        Codex finding described: checking only the fully-resolved target
        would miss it because the resolved target lives outside candidate_dir.
        The `physical` check (lexical path with only the parent dereferenced)
        is what catches this.
        """
        candidate_dir = tmp_path / "candidate-data-dir"
        candidate_dir.mkdir()
        real_config_dir = tmp_path / "elsewhere"
        real_config_dir.mkdir()
        real_config_file = real_config_dir / "real-config.toml"
        real_config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')

        symlink_path = candidate_dir / "config.toml"
        try:
            symlink_path.symlink_to(real_config_file)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        _use_config_file(monkeypatch, symlink_path)
        collision = config_collides_with_dir(candidate_dir)
        assert collision is not None

    def test_symlink_outside_candidate_dir_pointing_inside_is_collision(
        self, tmp_path: Path, monkeypatch
    ):
        """Mirror-image case: the active config path lives OUTSIDE
        candidate_dir but is a symlink whose real target lives INSIDE it —
        rmtree(candidate_dir) would destroy the actual config content even
        though the symlink path entry itself survives. Proves the
        pre-existing fully-resolved-target check wasn't lost by the fix.
        """
        candidate_dir = tmp_path / "candidate-data-dir"
        candidate_dir.mkdir()
        real_config_file = candidate_dir / "real-config.toml"
        real_config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')

        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        symlink_path = outside_dir / "config.toml"
        try:
            symlink_path.symlink_to(real_config_file)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        _use_config_file(monkeypatch, symlink_path)
        collision = config_collides_with_dir(candidate_dir)
        assert collision is not None


class TestLoadGlobalConfigCrashSafety:
    """Codex adversarial review findings: a valid-TOML value that fails at
    the Path/os.environ boundary (embedded NUL, unresolvable `~user` home)
    must warn and skip that key, not crash `load_global_config()` outright."""

    def test_nul_byte_scalar_value_warns_and_is_skipped_not_raised(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A basic TOML string containing `\\u0000` is valid TOML — tomllib
        parses it into a genuine NUL character. Setting that as an
        environment variable raises `ValueError: embedded null byte`; Guard
        B must catch it, warn, and skip only that key without raising."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45\\u0000"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()  # should not raise
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" in caplog.text

    def test_nul_byte_scalar_value_does_not_block_other_valid_keys(
        self, tmp_path: Path, monkeypatch
    ):
        """One bad key (NUL byte) must not stop the rest of the file from
        loading — a second, valid key in the same file is still applied."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            'PIPECAT_HUB_STALE_AFTER_DAYS = "45\\u0000"\nPIPECAT_HUB_WARMUP = false\n'
        )
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        monkeypatch.delenv("PIPECAT_HUB_WARMUP", raising=False)
        load_global_config()  # should not raise
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert os.environ["PIPECAT_HUB_WARMUP"] == "False"

    def test_nul_byte_data_dir_warns_and_is_skipped_not_raised(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """PIPECAT_HUB_DATA_DIR containing a NUL byte fails inside
        `Path(coerced)` construction itself (ValueError: embedded null
        byte) — Guard A must catch it, warn, and skip the key."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_DATA_DIR = "/some/dir\\u0000"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATA_DIR", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()  # should not raise
        assert "PIPECAT_HUB_DATA_DIR" not in os.environ
        assert "PIPECAT_HUB_DATA_DIR" in caplog.text

    def test_unresolvable_user_home_data_dir_warns_and_is_skipped_not_raised(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """`~doesnotexist12345/data` is a `~user`-prefixed path for a
        nonexistent user; `Path.expanduser()` raises `RuntimeError` when it
        can't resolve that user's home directory. Guard A must catch it,
        warn, and skip the key rather than raising out of
        `load_global_config()`."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_DATA_DIR = "~doesnotexist12345/data"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATA_DIR", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()  # should not raise
        assert "PIPECAT_HUB_DATA_DIR" not in os.environ
        assert "PIPECAT_HUB_DATA_DIR" in caplog.text


class TestLoadGlobalConfigLogging:
    """The "Loaded N key(s)" line is the manual-verification step's sole observable."""

    def test_logs_loaded_count_when_keys_written(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\nPIPECAT_HUB_WARMUP = false\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        monkeypatch.delenv("PIPECAT_HUB_WARMUP", raising=False)
        with caplog.at_level("INFO"):
            load_global_config()
        loaded_records = [r for r in caplog.records if "Loaded" in r.getMessage()]
        assert len(loaded_records) == 1
        assert "Loaded 2 key(s)" in loaded_records[0].getMessage()
        assert str(config_file) in loaded_records[0].getMessage()

    def test_no_loaded_line_when_file_empty(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        _use_config_file(monkeypatch, config_file)
        with caplog.at_level("INFO"):
            load_global_config()
        assert not any("Loaded" in r.getMessage() for r in caplog.records)

    def test_no_loaded_line_when_every_entry_is_skip_listed(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Entries present but every one skipped (non-prefixed +
        invocation-scoped + non-scalar) — emission condition is "keys
        written", not "file has entries": only warnings, no "Loaded" line.
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            f'HF_HOME = "/x"\n{PRUNE_ENV_VAR} = true\n[PIPECAT_HUB_NESTED]\nfoo = 1\n'
        )
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv(PRUNE_ENV_VAR, raising=False)
        with caplog.at_level("INFO"):
            load_global_config()
        assert not any("Loaded" in r.getMessage() for r in caplog.records)
        assert any(r.levelname == "WARNING" for r in caplog.records)


class TestLoadGlobalConfigBehavioralRoundTrip:
    """TOML-native booleans and PIPECAT_HUB_DATA_DIR must produce the correct
    *behavioral* outcome through the real consumers, not just the right
    string in os.environ."""

    def test_warmup_false_disables_prewarm(self, tmp_path: Path, monkeypatch):
        from pipecat_context_hub.cli import _warmup_enabled

        config_file = tmp_path / "config.toml"
        config_file.write_text("PIPECAT_HUB_WARMUP = false\n")
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_WARMUP", raising=False)
        load_global_config()
        assert os.environ["PIPECAT_HUB_WARMUP"] == "False"
        assert _warmup_enabled() is False

    def test_reranker_false_disables_reranker(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("PIPECAT_HUB_RERANKER_ENABLED = false\n")
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_RERANKER_ENABLED", raising=False)
        load_global_config()
        assert HubConfig().reranker.effective_enabled is False

    def test_data_dir_relocates_storage(self, tmp_path: Path, monkeypatch):
        config_dir = tmp_path / "cfg-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        data_dir = tmp_path / "relocated-data"
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{_toml_str(data_dir)}"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATA_DIR", raising=False)
        load_global_config()
        assert HubConfig().storage.data_dir == data_dir.expanduser().resolve(strict=False)


class TestIsolateEnvVarsFixtureBehavior:
    """Directly drives the rewritten autouse `_isolate_env_vars` fixture's
    generator (not reliance on suite execution order) to pin its setup-time
    delete and teardown-time restore.
    """

    def _drive_fixture(self, tmp_path: Path):
        """Return the raw generator behind the pytest fixture wrapper."""
        import tests.conftest as conftest_module

        return conftest_module._isolate_env_vars.__wrapped__(tmp_path)  # type: ignore[attr-defined]

    def test_pre_existing_var_absent_during_test_then_restored(self, tmp_path: Path):
        os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] = "real-shell-value"
        try:
            gen = self._drive_fixture(tmp_path / "outer")
            next(gen)  # run setup, pause at yield
            try:
                # (i) absent *during* the test body, not just restored after.
                assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
                os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] = "test-set-value"
                del os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"]
            finally:
                with pytest.raises(StopIteration):
                    next(gen)  # run teardown
            # (ii) the original pre-existing value is restored afterward.
            assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "real-shell-value"
        finally:
            os.environ.pop("PIPECAT_HUB_STALE_AFTER_DAYS", None)

    def test_full_passthrough_allowlist_visible_others_absent(self, tmp_path: Path):
        passthrough_keys = [
            "PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK",
            "PIPECAT_HUB_STABILITY_OUTPUT",
            "PIPECAT_HUB_ENABLE_PERF_BENCHMARK",
            "PIPECAT_HUB_PERF_OUTPUT",
            "PIPECAT_HUB_PERF_DATA_DIR",
            "PIPECAT_HUB_ENABLE_QUALITY_BENCHMARK",
            "PIPECAT_HUB_BENCHMARK_OUTPUT",
            "PIPECAT_HUB_PARITY_REFERENCE",
        ]
        non_allowlisted_key = "PIPECAT_HUB_STALE_AFTER_DAYS"
        for key in [*passthrough_keys, non_allowlisted_key]:
            os.environ[key] = "1"
        try:
            gen = self._drive_fixture(tmp_path / "outer")
            next(gen)
            try:
                for key in passthrough_keys:
                    assert key in os.environ, f"{key} should stay visible (benchmark allowlist)"
                assert non_allowlisted_key not in os.environ
            finally:
                with pytest.raises(StopIteration):
                    next(gen)
        finally:
            for key in [*passthrough_keys, non_allowlisted_key]:
                os.environ.pop(key, None)

    def test_config_file_defaults_to_nonexistent_sentinel(self, tmp_path: Path):
        os.environ.pop("PIPECAT_HUB_CONFIG_FILE", None)
        gen = self._drive_fixture(tmp_path / "outer")
        next(gen)
        try:
            sentinel = os.environ["PIPECAT_HUB_CONFIG_FILE"]
            assert not Path(sentinel).exists()
            assert resolve_global_config_path() == Path(sentinel)
        finally:
            with pytest.raises(StopIteration):
                next(gen)


def _fs_is_case_insensitive(directory: Path) -> bool:
    """Probe whether `directory`'s volume folds case (macOS APFS, Windows NTFS)."""
    probe = directory / "casefold_probe"
    probe.mkdir()
    return (directory / "CASEFOLD_PROBE").exists()


class TestConfigCollidesWithDirIdentity:
    """`config_collides_with_dir()` is a deletion guard, so it must compare
    *filesystem identity*, not path strings, and must normalize its own input.

    Regression coverage for two round-1 review findings:
      #1 a differently-spelled but identical `candidate_dir` (case-folded on a
         case-insensitive volume, or reached through a symlinked ancestor)
         escaped the guard, so `refresh --reset-index` would rmtree the
         directory holding config.toml;
      #2 the function published an "already absolute + resolved" precondition
         it did not enforce, so any caller that got it wrong failed *open*.
    """

    def test_symlinked_alias_to_config_dir_is_detected(self, tmp_path: Path, monkeypatch):
        """A `candidate_dir` naming the config dir through a symlink is the
        same directory on disk — string containment misses it, stat identity
        does not."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        config_file = real_dir / "config.toml"
        config_file.write_text("")
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")
        _use_config_file(monkeypatch, config_file)
        # Not normalized by the caller either — exercises finding #2's fix at
        # the same time.
        assert config_collides_with_dir(alias) is not None

    def test_differently_cased_dir_is_detected_on_case_insensitive_fs(
        self, tmp_path: Path, monkeypatch
    ):
        """macOS/Windows: `Path.resolve()` preserves the caller's spelling, so
        `~/.CONFIG/...` and `~/.config/...` compare unequal while naming one
        directory."""
        config_dir = tmp_path / "cfgdir"
        config_dir.mkdir()
        if not _fs_is_case_insensitive(tmp_path):
            pytest.skip("case-sensitive filesystem; casing cannot alias a directory here")
        config_file = config_dir / "config.toml"
        config_file.write_text("")
        _use_config_file(monkeypatch, config_file)
        shouted = tmp_path / "CFGDIR"
        assert config_collides_with_dir(shouted) is not None

    def test_unnormalized_candidate_dir_is_normalized_internally(self, tmp_path: Path, monkeypatch):
        """A `candidate_dir` carrying `..` segments must still match — the
        function normalizes rather than trusting its callers."""
        config_dir = tmp_path / "cfgdir"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("")
        _use_config_file(monkeypatch, config_file)
        roundabout = tmp_path / "cfgdir" / "nested" / ".."
        assert config_collides_with_dir(roundabout) is not None

    def test_unrelated_dir_still_returns_none(self, tmp_path: Path, monkeypatch):
        """The identity check must not turn the guard into a blanket refusal."""
        config_dir = tmp_path / "cfgdir"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("")
        _use_config_file(monkeypatch, config_file)
        other = tmp_path / "somewhere-else"
        other.mkdir()
        assert config_collides_with_dir(other) is None


class TestResolveGlobalConfigPathExpansion:
    def test_tilde_override_is_expanded(self, monkeypatch):
        """`PIPECAT_HUB_CONFIG_FILE=~/foo/config.toml` used to resolve to a
        literal `~` directory: FileNotFoundError, missing-file-silent branch,
        no warning, no config."""
        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", "~/foo/config.toml")
        resolved = resolve_global_config_path()
        assert resolved is not None
        assert "~" not in resolved.parts
        assert resolved == Path.home() / "foo" / "config.toml"


class TestLoadGlobalConfigDiagnostics:
    """Warnings must name the thing the operator actually has to fix."""

    def test_unsupported_type_warning_names_the_real_type(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A bare TOML date is neither scalar nor list — the warning used to
        claim `(table)`, sending the operator after a syntax error they
        don't have."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("PIPECAT_HUB_STALE_AFTER_DAYS = 2026-08-08\n")
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert "date" in caplog.text
        assert "(table)" not in caplog.text

    def test_nested_table_warning_names_the_discarded_keys(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Writing settings under a `[section]` header is a likely first
        attempt; naming only the section leaves the operator blind to which
        of their settings was dropped."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[some_table]\n"
            'PIPECAT_HUB_DATA_DIR = "/nested/ignored"\n'
            'PIPECAT_HUB_EXTRA_REPOS = "org/repo"\n'
        )
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATA_DIR", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "PIPECAT_HUB_DATA_DIR" not in os.environ
        assert "PIPECAT_HUB_DATA_DIR" in caplog.text
        assert "PIPECAT_HUB_EXTRA_REPOS" in caplog.text
        assert "some_table" in caplog.text

    def test_unknown_key_warns_but_is_still_set(self, tmp_path: Path, monkeypatch, caplog):
        """A typo'd key used to be indistinguishable from a working one: a
        success log and a config that silently does nothing. Warn — but still
        set it, so a config.toml written for a newer hub keeps working."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_DATADIR = "/typo"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATADIR", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert os.environ["PIPECAT_HUB_DATADIR"] == "/typo"
        assert "PIPECAT_HUB_DATADIR" in caplog.text
        assert "not a recognized" in caplog.text

    def test_known_key_does_not_warn(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "not a recognized" not in caplog.text

    def test_shadowed_taint_list_warns(self, tmp_path: Path, monkeypatch, caplog):
        """Exclusion controls are replaced, not unioned, by a higher layer —
        a narrower `.env` value silently drops a deliberate machine-global
        exclusion, so say so out loud.

        Uses a non-blank shadowing value: as of round-4 finding #7 a blank
        higher-layer value no longer shadows at all (it is inert downstream),
        so config.toml wins outright and there is nothing to warn about — see
        `TestBlankHigherLayerValueDoesNotShadow`.
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_TAINTED_REPOS = "org/bad"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REPOS", "org/unrelated")
        with caplog.at_level("WARNING"):
            load_global_config()
        assert os.environ["PIPECAT_HUB_TAINTED_REPOS"] == "org/unrelated"
        assert "PIPECAT_HUB_TAINTED_REPOS" in caplog.text
        assert "NOT in effect" in caplog.text

    def test_shadowed_ordinary_key_does_not_warn(self, tmp_path: Path, monkeypatch, caplog):
        """Only exclusion controls get the shadowing warning — every other
        key being outranked is ordinary, expected precedence."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "99"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "7")
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "NOT in effect" not in caplog.text


class TestLoadEnvLayers:
    """The shared bootstrap: one call, both layers, right order."""

    def test_loads_both_layers(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "99"\n')
        _use_config_file(monkeypatch, config_file)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".env").write_text("PIPECAT_HUB_EXTRA_REPOS=org/from-dotenv\n")
        monkeypatch.chdir(project_dir)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        monkeypatch.delenv("PIPECAT_HUB_EXTRA_REPOS", raising=False)

        env_loading.load_env_layers()

        assert os.environ["PIPECAT_HUB_EXTRA_REPOS"] == "org/from-dotenv"
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "99"

    def test_dotenv_layer_wins_over_config_toml(self, tmp_path: Path, monkeypatch):
        """Ordering, not just coverage: `.env` must be written first so its
        value wins first-writer-wins."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "99"\n')
        _use_config_file(monkeypatch, config_file)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".env").write_text("PIPECAT_HUB_STALE_AFTER_DAYS=7\n")
        monkeypatch.chdir(project_dir)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)

        env_loading.load_env_layers()

        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "7"


class TestConfigPathResolutionCrashSafety:
    """Round-2 finding #2: `Path.resolve(strict=False)` raises `RuntimeError`
    — not an `OSError` subclass — for a symlink loop on Python <=3.12, so a
    `PIPECAT_HUB_CONFIG_FILE` pointing into a loop aborted
    `refresh --reset-index` with an uncaught traceback instead of degrading to
    "no collision detected". Entry points never crash on a bad config.
    """

    def test_resolve_runtime_error_is_contained(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        _use_config_file(monkeypatch, config_file)

        real_resolve = Path.resolve

        def _boom(self, strict=False):
            raise RuntimeError("Symlink loop from %r" % (self,))

        monkeypatch.setattr(Path, "resolve", _boom)
        try:
            assert config_collides_with_dir(tmp_path / "elsewhere") is None
        finally:
            monkeypatch.setattr(Path, "resolve", real_resolve)

    def test_symlink_loop_config_path_does_not_raise(self, tmp_path: Path, monkeypatch):
        """The real-world shape of the same bug, on whichever Python versions
        actually raise for it."""
        loop_a = tmp_path / "loop_a"
        loop_b = tmp_path / "loop_b"
        try:
            loop_a.symlink_to(loop_b)
            loop_b.symlink_to(loop_a)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")
        _use_config_file(monkeypatch, loop_a / "config.toml")
        # Must not raise; a config that can't be resolved can't be protected.
        assert config_collides_with_dir(tmp_path / "data") is None
        load_global_config()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
class TestConfigFileOwnershipGuard:
    """Round-2 finding #8: `config.toml` sits at a fixed, predictable path and
    its contents are promoted straight into `os.environ`, so a file another
    local principal can rewrite is a persistent injection point."""

    def _write(self, tmp_path: Path, monkeypatch, mode: int) -> Path:
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "42"\n')
        config_file.chmod(mode)
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        return config_file

    def test_world_writable_config_is_ignored(self, tmp_path: Path, monkeypatch, caplog):
        self._write(tmp_path, monkeypatch, 0o666)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert "world-writable" in caplog.text

    def test_foreign_owner_config_is_ignored(self, tmp_path: Path, monkeypatch, caplog):
        self._write(tmp_path, monkeypatch, 0o600)
        # Patches `os.fstat`, not `Path.stat`: round-3 finding #4 moved the
        # trust gate onto the *opened descriptor* so the bytes handed to
        # tomllib are the bytes that were validated (TOCTOU close).
        real_fstat = os.fstat

        class _ForeignStat:
            def __init__(self, st):
                self._st = st

            def __getattr__(self, name):
                return getattr(self._st, name)

            st_uid = os.getuid() + 12345

        def _fstat(fd, *args, **kwargs):
            return _ForeignStat(real_fstat(fd, *args, **kwargs))

        monkeypatch.setattr(os, "fstat", _fstat)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert "not the current user" in caplog.text

    def test_group_writable_config_warns_but_still_loads(self, tmp_path: Path, monkeypatch, caplog):
        """User-private-group distros (`umask 002`) create 0664 files whose
        group holds only the owner — refusing those would break a correctly
        configured machine, so warn rather than overrule."""
        self._write(tmp_path, monkeypatch, 0o664)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "42"
        assert "group-writable" in caplog.text

    def test_private_config_loads_without_warning(self, tmp_path: Path, monkeypatch, caplog):
        self._write(tmp_path, monkeypatch, 0o600)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "42"
        assert "writable" not in caplog.text


class TestSkipWarningsAreNotContradictory:
    """Round-2 finding #11: invocation-scoped keys are deliberately absent
    from `_KNOWN_KEYS`, so checking typos first emitted "typo?; setting it
    anyway" immediately followed by "invocation-scoped; skipping" for the
    same key."""

    def test_invocation_scoped_key_emits_only_the_scoping_warning(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        config_file = tmp_path / "config.toml"
        config_file.write_text(f"{PRUNE_ENV_VAR} = true\n")
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv(PRUNE_ENV_VAR, raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert PRUNE_ENV_VAR not in os.environ
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1, messages
        assert "invocation-scoped" in messages[0]
        assert "typo" not in messages[0]


class TestShadowedExclusionWarningNamesEntries:
    """Round-2 finding #3: precedence stays first-writer-wins (the plan's
    Objective states "whole-string override, not a list merge"), so the
    mitigation for a `.env` clearing a machine-global taint list is a warning
    that names the slugs that actually lost protection."""

    def test_names_the_lost_slugs(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_TAINTED_REPOS = "org/bad,org/worse"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REPOS", "org/bad")
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "org/worse" in caplog.text
        assert "NOT in effect" in caplog.text

    def test_names_the_lost_slugs_from_an_array_value(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_TAINTED_REPOS = ["org/bad", "org/worse"]\n')
        _use_config_file(monkeypatch, config_file)
        # A *non-blank* shadowing value: a blank one no longer shadows at all
        # (round-4 finding #7 — see TestBlankHigherLayerValueDoesNotShadow),
        # so it would leave nothing lost to name.
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REPOS", "org/unrelated")
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "org/bad" in caplog.text
        assert "org/worse" in caplog.text

    def test_no_entries_lost_still_warns_generically(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_TAINTED_REPOS = "org/bad"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REPOS", "org/bad,org/other")
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "NOT in effect" in caplog.text


class TestConfigPathLogsAreHomeRedacted:
    """Round-2 finding #7: the "Loaded N key(s)" line is exactly what an
    operator pastes into a bug report, and the default config path carries the
    OS username."""

    def test_loaded_line_is_redacted(self, tmp_path: Path, monkeypatch, caplog):
        home = tmp_path / "home"
        config_dir = home / ".config" / "pipecat-context-hub"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "42"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        with caplog.at_level("INFO"):
            load_global_config()
        loaded = [r.getMessage() for r in caplog.records if "Loaded" in r.getMessage()]
        assert loaded, caplog.text
        assert str(home) not in loaded[0]
        # Built with `Path`, not a "/"-joined literal: `redact_home` preserves
        # `os.sep`, so on the windows-latest CI leg the redacted suffix is
        # `~\\.config\\...`. A hard-coded "/" fails there every run.
        expected_suffix = str(Path("~", ".config", "pipecat-context-hub", "config.toml"))
        assert loaded[0].endswith(expected_suffix)


class TestConfigFileCrashSafety:
    """Round-3 findings #2 and #3: a config *path* the operator can name but
    the loader cannot safely open must degrade to "no config available",
    never hang or raise, per this module's "entry points never crash on a bad
    config" contract.
    """

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
    def test_fifo_config_path_is_skipped_without_blocking(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Round-3 finding #2: a FIFO passed the ownership/mode trust gate,
        and the subsequent `open("rb")` blocked in the kernel until a writer
        appeared — hanging every CLI invocation before argv was dispatched.
        Run on a worker thread so the pre-fix hang fails the test instead of
        wedging the suite.
        """
        import threading

        fifo = tmp_path / "config.toml"
        os.mkfifo(fifo)
        _use_config_file(monkeypatch, fifo)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)

        done = threading.Event()
        error: list[BaseException] = []

        def _run() -> None:
            try:
                load_global_config()
            except BaseException as exc:  # noqa: BLE001 - reported below
                error.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=_run, daemon=True)
        with caplog.at_level("WARNING"):
            worker.start()
            finished = done.wait(timeout=10)

        assert finished, "load_global_config() blocked on a FIFO config path"
        assert not error, error
        assert str(fifo) in caplog.text
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ

    def test_unknown_user_override_does_not_crash(self, monkeypatch, caplog):
        """Round-3 finding #3: `Path.expanduser()` raises RuntimeError for a
        user it can't resolve, so `PIPECAT_HUB_CONFIG_FILE=~nosuchuser/...`
        took down every command with an uncaught exception before the
        loader's own guarded read could classify it.
        """
        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", "~definitely-no-such-user-9f3a/config.toml")
        with caplog.at_level("WARNING"):
            assert resolve_global_config_path() is None
            load_global_config()  # must not raise
        assert "PIPECAT_HUB_CONFIG_FILE" in caplog.text

    def test_directory_config_path_is_skipped_with_warning(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A directory is not a regular file; the loader names it and moves on."""
        config_dir = tmp_path / "config.toml"
        config_dir.mkdir()
        _use_config_file(monkeypatch, config_dir)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert str(config_dir) in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode semantics")
class TestConfigFileTrustGate:
    """Round-3 finding #4: the trust gate validated the config file's own
    owner/mode but not the writability of the directory that authorizes
    unlink-and-replace, nor the identity of a symlink standing in for it.
    """

    def test_world_writable_parent_dir_is_refused(self, tmp_path: Path, monkeypatch, caplog):
        config_dir = tmp_path / "loose-dir"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "42"\n')
        config_file.chmod(0o600)
        config_dir.chmod(0o777)  # world-writable, no sticky bit
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        try:
            with caplog.at_level("WARNING"):
                load_global_config()
        finally:
            config_dir.chmod(0o755)
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert "world-writable" in caplog.text

    def test_sticky_world_writable_parent_dir_is_allowed(self, tmp_path: Path, monkeypatch):
        """`/tmp`-style dirs are safe: the sticky bit is what withdraws the
        unlink permission the refusal above is actually about."""
        config_dir = tmp_path / "sticky-dir"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "42"\n')
        config_file.chmod(0o600)
        config_dir.chmod(0o1777)
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        try:
            load_global_config()
        finally:
            config_dir.chmod(0o755)
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "42"

    def test_self_owned_symlinked_config_still_loads(self, tmp_path: Path, monkeypatch):
        """Regression floor for the fix itself: pointing the config path at a
        dotfiles checkout (GNU stow et al.) is a legitimate, common setup and
        must not be refused just because it is a symlink."""
        real_dir = tmp_path / "dotfiles"
        real_dir.mkdir()
        real_file = real_dir / "hub-config.toml"
        real_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "42"\n')
        link_dir = tmp_path / "config-home"
        link_dir.mkdir()
        link = link_dir / "config.toml"
        try:
            link.symlink_to(real_file)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")
        _use_config_file(monkeypatch, link)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "42"


class TestBlankHigherLayerValueDoesNotShadow:
    """Round-4 finding #7: `key in os.environ` treated an empty-string value
    as "present", so `export PIPECAT_HUB_STALE_AFTER_DAYS=` skipped
    config.toml's value as shadowed — while the shadowing blank is itself
    inert, because every consumer in shared/config.py reads its var as
    `.get(NAME, "").strip()` and falls back to the field default. Net effect
    before the fix: neither layer applied.
    """

    def test_blank_real_env_var_lets_config_toml_win(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "")
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "45"

    def test_whitespace_only_real_env_var_lets_config_toml_win(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "   ")
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "45"

    def test_blank_exclusion_list_emits_no_shadow_warning(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A blank taint list is not a shadow, so warning that machine-global
        exclusions are "NOT in effect" would be actively wrong — they are."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_TAINTED_REPOS = "org/bad"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REPOS", "")
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "NOT in effect" not in caplog.text
        assert os.environ["PIPECAT_HUB_TAINTED_REPOS"] == "org/bad"

    def test_non_blank_real_env_var_still_wins(self, tmp_path: Path, monkeypatch):
        """The precedence rule itself is unchanged for real values."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "7")
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "7"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
class TestReadFailureWarningIsHomeRedacted:
    """Round-4 finding #3: `config_path` was redacted in the read-failure
    warning, but the interpolated `OSError` was not — and OSError.__str__
    appends the absolute filename ("[Errno 13] Permission denied: '/Users/…'"),
    putting the home directory straight back into the log line.
    """

    def test_permission_error_text_is_redacted(self, tmp_path: Path, monkeypatch, caplog):
        if os.getuid() == 0:
            pytest.skip("root bypasses file permission bits")
        home = tmp_path / "home"
        config_dir = home / ".config" / "pipecat-context-hub"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')
        config_file.chmod(0o000)
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        try:
            with caplog.at_level("WARNING"):
                load_global_config()
        finally:
            config_file.chmod(0o600)
        assert "Failed to read config.toml" in caplog.text
        assert str(home) not in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode bits required")
class TestConfigAncestorDirectoriesAreTrusted:
    """Round-4 finding #8: the containing-directory trust check was
    single-level and ran against the *unresolved* parent, so (a) a symlinked
    config's real holding directory was never examined, and (b) write access
    on any ancestor above the parent — enough to substitute a directory
    symlink and redirect the whole lookup — passed every check.
    """

    def _write_config(self, path: Path) -> None:
        path.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')
        path.chmod(0o600)

    def test_world_writable_ancestor_above_the_parent_is_refused(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        ancestor = tmp_path / "ancestor"
        config_dir = ancestor / "pipecat-context-hub"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.toml"
        self._write_config(config_file)
        # World-writable, no sticky bit: any local account can swap
        # `pipecat-context-hub` for a directory of its own.
        ancestor.chmod(0o777)
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        try:
            with caplog.at_level("WARNING"):
                load_global_config()
        finally:
            ancestor.chmod(0o755)
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert "world-writable" in caplog.text

    def test_symlinked_configs_real_holding_directory_is_checked(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """The config path's own parent is fine; the directory that actually
        holds the opened inode is world-writable. Pre-fix, that directory was
        never examined — `fstat` describes the target, not where it lives.
        """
        good_dir = tmp_path / "good"
        good_dir.mkdir(mode=0o755)
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        real_config = shared_dir / "real-config.toml"
        self._write_config(real_config)
        shared_dir.chmod(0o777)

        link = good_dir / "config.toml"
        try:
            link.symlink_to(real_config)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        _use_config_file(monkeypatch, link)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        try:
            with caplog.at_level("WARNING"):
                load_global_config()
        finally:
            shared_dir.chmod(0o755)
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert "world-writable" in caplog.text

    def test_ordinary_nested_config_still_loads(self, tmp_path: Path, monkeypatch):
        """The ancestor walk must not refuse a normal, correctly-permissioned
        config — it stats every directory up to the filesystem root.
        """
        config_dir = tmp_path / "a" / "b" / "pipecat-context-hub"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.toml"
        self._write_config(config_file)
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "45"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
class TestSymlinkChainIsWalked:
    """Round-5 findings #1/#2: both guard functions validated only the two
    *endpoints* of the config path (its lexical form and its fully-resolved
    target), never the intermediate path entries a multi-hop symlink chain
    actually traverses. Those intermediates are real, deletable, replaceable
    filesystem objects — one is a data-loss hole in the deletion guard, the
    other a confused-deputy redirect through the read-trust gate.
    """

    def _write_config(self, path: Path) -> None:
        path.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')
        path.chmod(0o600)

    def test_intermediate_symlink_hop_inside_candidate_dir_is_collision(
        self, tmp_path: Path, monkeypatch
    ):
        """Finding #1. The config path's directory is a symlink whose target
        is *itself* a symlink living inside `candidate_dir`, which in turn
        points back outside it. Both endpoints the guard used to check —
        the lexical path (`outside/link/config.toml`) and the fully-resolved
        target (`elsewhere/config.toml`) — land outside `candidate_dir`, so
        the guard did not fire; yet `rmtree(candidate_dir)` deletes the
        middle hop and the active config path stops resolving.
        """
        candidate_dir = tmp_path / "candidate-data-dir"
        candidate_dir.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        self._write_config(elsewhere / "config.toml")

        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            # candidate_dir/hop -> elsewhere ; outside/link -> candidate_dir/hop
            (candidate_dir / "hop").symlink_to(elsewhere, target_is_directory=True)
            (outside / "link").symlink_to(candidate_dir / "hop", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        config_path = outside / "link" / "config.toml"
        _use_config_file(monkeypatch, config_path)
        collision = config_collides_with_dir(candidate_dir)
        assert collision is not None

    def test_world_writable_holder_of_a_symlinked_ancestor_is_refused(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Finding #2. `holder` is world-writable without the sticky bit, so
        any local account can replace `holder/link` and redirect the whole
        lookup. Every directory the old gate stat'd — the lexical parent
        (`holder/link`, which stat() follows straight through to the
        well-permissioned target) and the resolved chain (`good/…`) — looks
        fine, because `holder` itself is never examined.
        """
        holder = tmp_path / "holder"
        holder.mkdir()
        good = tmp_path / "good"
        good.mkdir(mode=0o755)
        self._write_config(good / "config.toml")
        try:
            (holder / "link").symlink_to(good, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")
        holder.chmod(0o777)

        _use_config_file(monkeypatch, holder / "link" / "config.toml")
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        try:
            with caplog.at_level("WARNING"):
                load_global_config()
        finally:
            holder.chmod(0o755)
        assert "PIPECAT_HUB_STALE_AFTER_DAYS" not in os.environ
        assert "world-writable" in caplog.text

    def test_deeply_symlinked_but_well_permissioned_config_still_loads(
        self, tmp_path: Path, monkeypatch
    ):
        """Regression floor for the chain walk: a multi-hop, correctly
        permissioned dotfiles-style layout must still load."""
        real_dir = tmp_path / "dotfiles" / "pipecat"
        real_dir.mkdir(parents=True)
        self._write_config(real_dir / "hub.toml")
        stage = tmp_path / "stage"
        stage.mkdir()
        home_cfg = tmp_path / "home" / ".config"
        home_cfg.mkdir(parents=True)
        try:
            (stage / "pipecat").symlink_to(real_dir, target_is_directory=True)
            (home_cfg / "pipecat-context-hub").symlink_to(
                stage / "pipecat", target_is_directory=True
            )
            (real_dir / "config.toml").symlink_to(real_dir / "hub.toml")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        _use_config_file(monkeypatch, home_cfg / "pipecat-context-hub" / "config.toml")
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "45"

    def test_symlink_loop_in_an_ancestor_does_not_hang_or_raise(self, tmp_path: Path, monkeypatch):
        """The chain walk is hop-bounded: an ELOOP ancestor must degrade to
        'nothing proven' rather than spinning or raising."""
        loop_a = tmp_path / "loop-a"
        loop_b = tmp_path / "loop-b"
        try:
            loop_a.symlink_to(loop_b, target_is_directory=True)
            loop_b.symlink_to(loop_a, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")
        _use_config_file(monkeypatch, loop_a / "config.toml")
        assert config_collides_with_dir(tmp_path / "data") is None
        load_global_config()  # must not raise


class TestUnknownKeyWarningIsNotContradicted:
    """Round-5 finding #3: the "not a recognized PIPECAT_HUB_* setting (typo?);
    setting it anyway" warning fired *before* four checks that can still skip
    the key, producing the same contradictory-warning pair round 3 removed one
    branch earlier.
    """

    def test_shadowed_unknown_key_emits_no_typo_warning(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_DATADIR = "/typo"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.setenv("PIPECAT_HUB_DATADIR", "/from-real-env")
        with caplog.at_level("WARNING"):
            load_global_config()
        assert os.environ["PIPECAT_HUB_DATADIR"] == "/from-real-env"
        assert "setting it anyway" not in caplog.text

    def test_skipped_unknown_key_emits_no_typo_warning(self, tmp_path: Path, monkeypatch, caplog):
        """An unrecognized key whose value the loader then rejects outright
        must not first be announced as "setting it anyway"."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("PIPECAT_HUB_DATADIR = 1979-05-27\n")
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATADIR", raising=False)
        with caplog.at_level("WARNING"):
            load_global_config()
        assert "PIPECAT_HUB_DATADIR" not in os.environ
        assert "setting it anyway" not in caplog.text
        assert "unsupported value type" in caplog.text


class TestConfigFdOwnershipTransfer:
    """Round-5 finding #4: `fd_owned = False` was set *after* `os.fdopen`
    returned, but CPython's `io.open` closes the descriptor itself when
    construction fails — so a raising `fdopen` left the `finally` block
    closing an fd the interpreter had already closed, which after fd-number
    reuse closes an unrelated file.
    """

    def test_fdopen_failure_does_not_close_the_fd_twice(self, tmp_path: Path, monkeypatch, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_STALE_AFTER_DAYS", raising=False)

        real_close = os.close
        handed_over: list[int] = []
        closed_by_loader: list[int] = []

        def fake_fdopen(fd, *args, **kwargs):
            handed_over.append(fd)
            real_close(fd)  # exactly what io.open does on construction failure
            raise OSError("simulated fdopen failure")

        def tracking_close(fd):
            closed_by_loader.append(fd)
            return real_close(fd)

        monkeypatch.setattr(os, "fdopen", fake_fdopen)
        monkeypatch.setattr(os, "close", tracking_close)
        with caplog.at_level("WARNING"):
            load_global_config()

        assert handed_over, "fdopen was never reached"
        assert handed_over[0] not in closed_by_loader, (
            "loader closed a descriptor whose ownership had already transferred "
            "to os.fdopen — double close"
        )
