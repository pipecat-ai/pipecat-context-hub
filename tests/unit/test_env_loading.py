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
    load_cwd_dotenv,
    load_global_config,
    resolve_global_config_path,
)


def _use_config_file(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Point the loader's lookup path at `path` (the test-hermeticity seam)."""
    monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(path))


class TestLoadCwdDotenv:
    """Tests for the cwd .env parser — moved verbatim from test_cli.py's
    TestLoadDotenv (module move, dev plan Phase 1). `cli._load_dotenv` is a
    thin re-export of this function; see TestLoadDotenvBackCompatAlias in
    test_cli.py for the alias-identity check.
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
        assert not config_path.is_relative_to(data_dir)
        assert not data_dir.is_relative_to(config_path.parent)


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
        real_config_file.write_text(f'PIPECAT_HUB_CONFIG_FILE = "{decoy_path}"\n')
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
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{config_dir}"\n')
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
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{data_dir}"\n')
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
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{config_dir}"\n')
        _use_config_file(monkeypatch, config_file)
        monkeypatch.delenv("PIPECAT_HUB_DATA_DIR", raising=False)
        assert resolve_global_config_path().is_file()
        load_global_config()
        assert "PIPECAT_HUB_DATA_DIR" not in os.environ


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
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{data_dir}"\n')
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

        return conftest_module._isolate_env_vars.__wrapped__(tmp_path)

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
