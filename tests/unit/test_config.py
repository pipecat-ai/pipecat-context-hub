"""Tests for HubConfig and sub-configs — defaults, serialization, computed fields."""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pipecat_context_hub.shared import env_loading
from pipecat_context_hub.shared.config import (
    _DATA_DIR_ENV,
    _DEFAULT_RERANKER_MODEL,
    _EXTRA_REPOS_ENV,
    _RERANKER_MODEL_ENV,
    _TAINTED_REFS_ENV,
    _TAINTED_REPOS_ENV,
    ChunkingConfig,
    EmbeddingConfig,
    HubConfig,
    RerankerConfig,
    ServerConfig,
    SourceConfig,
    StorageConfig,
)
from pipecat_context_hub.shared.markdown import extract_section

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PIPECAT_HUB_VAR_RE = re.compile(r"PIPECAT_HUB_[A-Z_]+")


def _round_trip(model_instance):
    """Serialize to JSON and back; assert equality."""
    json_str = model_instance.model_dump_json()
    rebuilt = type(model_instance).model_validate_json(json_str)
    assert rebuilt == model_instance
    return rebuilt


class TestChunkingConfig:
    def test_defaults(self):
        c = ChunkingConfig()
        assert c.doc_max_tokens == 512
        assert c.doc_overlap_tokens == 50
        assert c.code_max_tokens == 256
        assert c.code_overlap_tokens == 25
        assert c.code_prefer_function_boundaries is True

    def test_round_trip(self):
        _round_trip(ChunkingConfig())

    def test_custom_values(self):
        c = ChunkingConfig(doc_max_tokens=1024, code_prefer_function_boundaries=False)
        rebuilt = _round_trip(c)
        assert rebuilt.doc_max_tokens == 1024
        assert rebuilt.code_prefer_function_boundaries is False


class TestEmbeddingConfig:
    def test_defaults(self):
        e = EmbeddingConfig()
        assert e.model_name == "all-MiniLM-L6-v2"
        assert e.dimension == 384

    def test_round_trip(self):
        _round_trip(EmbeddingConfig())


class TestStorageConfig:
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_DATA_DIR_ENV, None)
            s = StorageConfig()
        assert s.data_dir == Path.home() / ".pipecat-context-hub"
        assert s.sqlite_filename == "metadata.db"
        assert s.chroma_dirname == "chroma"

    def test_data_dir_env_override(self):
        with patch.dict(os.environ, {_DATA_DIR_ENV: "/tmp/hub-override"}):
            s = StorageConfig()
        assert s.data_dir == Path("/tmp/hub-override")
        assert s.chroma_path == Path("/tmp/hub-override/chroma")

    def test_explicit_data_dir_beats_env(self):
        with patch.dict(os.environ, {_DATA_DIR_ENV: "/tmp/hub-override"}):
            s = StorageConfig(data_dir=Path("/tmp/explicit"))
        assert s.data_dir == Path("/tmp/explicit")

    def test_data_dir_env_blank_falls_back(self):
        with patch.dict(os.environ, {_DATA_DIR_ENV: "   "}):
            s = StorageConfig()
        assert s.data_dir == Path.home() / ".pipecat-context-hub"

    def test_data_dir_env_expands_user(self):
        with patch.dict(os.environ, {_DATA_DIR_ENV: "~/scratch-hub"}):
            s = StorageConfig()
        assert s.data_dir == Path.home() / "scratch-hub"

    def test_computed_paths(self):
        s = StorageConfig(data_dir=Path("/tmp/test-hub"))
        assert s.sqlite_path == Path("/tmp/test-hub/metadata.db")
        assert s.chroma_path == Path("/tmp/test-hub/chroma")

    def test_computed_fields_in_model_dump(self):
        """computed_field values must appear in model_dump() for serialization."""
        s = StorageConfig(data_dir=Path("/tmp/test-hub"))
        dumped = s.model_dump()
        assert "sqlite_path" in dumped
        assert "chroma_path" in dumped
        assert dumped["sqlite_path"] == Path("/tmp/test-hub/metadata.db")

    def test_round_trip(self):
        _round_trip(StorageConfig(data_dir=Path("/tmp/test-hub")))


class TestServerConfig:
    def test_defaults(self):
        s = ServerConfig()
        assert s.transport == "stdio"
        assert s.log_level == "INFO"
        assert s.idle_timeout_secs == 1800.0
        assert s.parent_watch_interval_secs == 2.0

    def test_round_trip(self):
        _round_trip(ServerConfig())


class TestServerConfigEffectiveIdleTimeout:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", raising=False)
        assert ServerConfig().effective_idle_timeout_secs == 1800.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", "60")
        assert ServerConfig().effective_idle_timeout_secs == 60.0

    def test_env_zero_disables(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", "0")
        assert ServerConfig().effective_idle_timeout_secs == 0.0

    def test_env_invalid_falls_back_to_field(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", "garbage")
        assert ServerConfig(idle_timeout_secs=42.0).effective_idle_timeout_secs == 42.0

    def test_env_negative_clamped_to_zero(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", "-30")
        assert ServerConfig().effective_idle_timeout_secs == 0.0

    def test_field_override_when_no_env(self, monkeypatch):
        monkeypatch.delenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", raising=False)
        assert ServerConfig(idle_timeout_secs=300.0).effective_idle_timeout_secs == 300.0

    def test_env_nan_falls_back_to_field(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", "nan")
        assert ServerConfig(idle_timeout_secs=42.0).effective_idle_timeout_secs == 42.0

    def test_env_inf_falls_back_to_field(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", "inf")
        assert ServerConfig(idle_timeout_secs=42.0).effective_idle_timeout_secs == 42.0


class TestServerConfigIdleTimeoutExplicitlySet:
    """Gates the smart auto-disable: serve only disables the idle
    watchdog when the operator did NOT choose a value themselves.
    """

    def test_false_at_default(self, monkeypatch):
        monkeypatch.delenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", raising=False)
        assert ServerConfig().idle_timeout_explicitly_set is False

    def test_true_when_env_set(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", "60")
        assert ServerConfig().idle_timeout_explicitly_set is True

    def test_true_when_env_set_to_zero(self, monkeypatch):
        # Operator explicitly disabling it still counts as explicit.
        monkeypatch.setenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", "0")
        assert ServerConfig().idle_timeout_explicitly_set is True

    def test_true_when_field_non_default(self, monkeypatch):
        monkeypatch.delenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", raising=False)
        assert ServerConfig(idle_timeout_secs=300.0).idle_timeout_explicitly_set is True

    def test_true_when_field_explicitly_set_to_default_value(self, monkeypatch):
        # An embedder who deliberately pins the default must still be
        # honored — `model_fields_set` distinguishes this from "never set".
        monkeypatch.delenv("PIPECAT_HUB_IDLE_TIMEOUT_SECS", raising=False)
        from pipecat_context_hub.shared.config import _DEFAULT_IDLE_TIMEOUT_SECS

        cfg = ServerConfig(idle_timeout_secs=_DEFAULT_IDLE_TIMEOUT_SECS)
        assert cfg.idle_timeout_explicitly_set is True


class TestServerConfigEffectiveParentWatchInterval:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("PIPECAT_HUB_PARENT_WATCH_INTERVAL", raising=False)
        assert ServerConfig().effective_parent_watch_interval_secs == 2.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_PARENT_WATCH_INTERVAL", "0.5")
        assert ServerConfig().effective_parent_watch_interval_secs == 0.5

    def test_env_zero_disables(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_PARENT_WATCH_INTERVAL", "0")
        assert ServerConfig().effective_parent_watch_interval_secs == 0.0

    def test_env_invalid_falls_back_to_field(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_PARENT_WATCH_INTERVAL", "garbage")
        assert (
            ServerConfig(parent_watch_interval_secs=1.5).effective_parent_watch_interval_secs == 1.5
        )

    def test_env_negative_clamped_to_zero(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_PARENT_WATCH_INTERVAL", "-1")
        assert ServerConfig().effective_parent_watch_interval_secs == 0.0

    def test_tiny_positive_floored_to_minimum(self, monkeypatch):
        # Prevents misconfiguration like "0.0001" from CPU-spinning os.getppid().
        monkeypatch.setenv("PIPECAT_HUB_PARENT_WATCH_INTERVAL", "0.0001")
        assert ServerConfig().effective_parent_watch_interval_secs == 0.1

    def test_at_floor_unchanged(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_PARENT_WATCH_INTERVAL", "0.1")
        assert ServerConfig().effective_parent_watch_interval_secs == 0.1

    def test_env_nan_falls_back_to_field(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_PARENT_WATCH_INTERVAL", "nan")
        assert (
            ServerConfig(parent_watch_interval_secs=1.5).effective_parent_watch_interval_secs == 1.5
        )

    def test_env_inf_falls_back_to_field(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_PARENT_WATCH_INTERVAL", "inf")
        assert (
            ServerConfig(parent_watch_interval_secs=1.5).effective_parent_watch_interval_secs == 1.5
        )


class TestSourceConfig:
    def test_defaults(self):
        s = SourceConfig()
        assert s.docs_url == "https://docs.pipecat.ai/"
        assert s.docs_llms_txt_url == "https://docs.pipecat.ai/llms-full.txt"
        assert s.repos == [
            "pipecat-ai/pipecat",
            "pipecat-ai/pipecat-examples",
            "daily-co/daily-python",
            "pipecat-ai/pipecat-client-web",
            "pipecat-ai/pipecat-client-web-transports",
            "pipecat-ai/pipecat-client-react-native-transports",
            "pipecat-ai/voice-ui-kit",
            "pipecat-ai/pipecat-prebuilt",
        ]

    def test_default_repos_membership(self):
        """Legibility companion to the exact-match snapshot in ``test_defaults``.

        The snapshot above asserts the full ordered list (and must be edited on
        every repo change); this names the load-bearing defaults explicitly so a
        reader sees *which* repos are guaranteed present without diffing a list
        literal. Covers the repo promoted to defaults most recently
        (RN transports) and pins ``pipecat-cli`` as a deliberate non-default —
        its CLI usage is already covered by the indexed ``docs.pipecat.ai``
        pages, so the repo source stays opt-in via ``PIPECAT_HUB_EXTRA_REPOS``.
        """
        repos = SourceConfig().repos
        for slug in (
            "pipecat-ai/pipecat",
            "pipecat-ai/pipecat-client-react-native-transports",
        ):
            assert slug in repos
        assert "pipecat-ai/pipecat-cli" not in repos
        # Archived upstream; flows now ships in pipecat-ai/pipecat as `pipecat.flows`.
        assert "pipecat-ai/pipecat-flows" not in repos

    def test_custom_llms_txt_url(self):
        s = SourceConfig(docs_llms_txt_url="https://example.com/docs.txt")
        assert s.docs_llms_txt_url == "https://example.com/docs.txt"

    def test_round_trip(self):
        _round_trip(SourceConfig())

    def test_effective_repos_without_env(self):
        """Without env var, effective_repos equals repos."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_EXTRA_REPOS_ENV, None)
            s = SourceConfig()
            assert s.effective_repos == s.repos

    def test_effective_repos_with_env(self):
        """Env var appends extra repos to defaults."""
        with patch.dict(os.environ, {_EXTRA_REPOS_ENV: "org/repo-a,org/repo-b"}):
            s = SourceConfig()
            assert s.effective_repos == s.repos + ["org/repo-a", "org/repo-b"]

    def test_effective_repos_deduplicates(self):
        """Env var duplicates of default repos are ignored."""
        with patch.dict(os.environ, {_EXTRA_REPOS_ENV: "pipecat-ai/pipecat,org/new"}):
            s = SourceConfig()
            assert s.effective_repos == s.repos + ["org/new"]

    def test_effective_repos_strips_whitespace(self):
        """Whitespace around slugs is trimmed."""
        with patch.dict(os.environ, {_EXTRA_REPOS_ENV: " org/a , org/b "}):
            s = SourceConfig()
            assert "org/a" in s.effective_repos
            assert "org/b" in s.effective_repos

    def test_effective_repos_ignores_empty_env(self):
        """Empty or whitespace-only env var adds nothing."""
        with patch.dict(os.environ, {_EXTRA_REPOS_ENV: "  "}):
            s = SourceConfig()
            assert s.effective_repos == s.repos

    def test_effective_repos_excludes_tainted_repos(self):
        """Tainted repos are removed from the effective refresh list."""
        with patch.dict(
            os.environ,
            {
                _EXTRA_REPOS_ENV: "org/repo-a",
                _TAINTED_REPOS_ENV: "pipecat-ai/pipecat,org/repo-a",
            },
        ):
            s = SourceConfig()
            expected = [r for r in s.repos if r != "pipecat-ai/pipecat"]
            assert s.effective_repos == expected
            assert s.tainted_repos == ["pipecat-ai/pipecat", "org/repo-a"]

    def test_tainted_refs_by_repo_parses_env(self):
        """Tainted refs are parsed from org/repo@ref entries."""
        with patch.dict(
            os.environ,
            {
                _TAINTED_REFS_ENV: "pipecat-ai/pipecat@v0.0.9,pipecat-ai/pipecat@deadbeef,broken-entry"
            },
        ):
            s = SourceConfig()
            assert s.tainted_refs_by_repo == {
                "pipecat-ai/pipecat": ["v0.0.9", "deadbeef"],
            }


class TestRerankerConfigEffectiveModel:
    """Env-var resolution for PIPECAT_HUB_RERANKER_MODEL."""

    _ALT_MODEL = "cross-encoder/ms-marco-MiniLM-L-12-v2"
    _TINY_MODEL = "cross-encoder/ms-marco-TinyBERT-L-2-v2"

    def test_unset_returns_field_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_RERANKER_MODEL_ENV, None)
            assert RerankerConfig().effective_model == _DEFAULT_RERANKER_MODEL

    def test_env_selects_allowed_model(self):
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: self._ALT_MODEL}):
            assert RerankerConfig().effective_model == self._ALT_MODEL

    def test_env_tiny_model(self):
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: self._TINY_MODEL}):
            assert RerankerConfig().effective_model == self._TINY_MODEL

    def test_invalid_env_falls_back_to_field(self, caplog):
        # Field is the default (valid), so fallback is the default.
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: "cross-encoder/not-real"}):
            with caplog.at_level("WARNING"):
                model = RerankerConfig().effective_model
        assert model == _DEFAULT_RERANKER_MODEL
        # Warning must name the actual fallback target, not the invalid env value.
        unknown_msgs = [r.getMessage() for r in caplog.records if "Unknown" in r.getMessage()]
        assert any(_DEFAULT_RERANKER_MODEL in m for m in unknown_msgs)

    def test_invalid_env_and_invalid_field_warn_with_accurate_target(self, caplog):
        # Both env and field are invalid — fallback must be the hardcoded
        # default, and the env-warning must name that (not the bad field).
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: "cross-encoder/bad-env"}):
            cfg = RerankerConfig(cross_encoder_model="cross-encoder/bad-field")
            with caplog.at_level("WARNING"):
                model = cfg.effective_model
        assert model == _DEFAULT_RERANKER_MODEL
        messages = [r.getMessage() for r in caplog.records]
        # Env-fallback warning names default (not the invalid field).
        env_warn = next(m for m in messages if "bad-env" in m)
        assert _DEFAULT_RERANKER_MODEL in env_warn
        assert "bad-field" not in env_warn
        # Field-invalid warning is also emitted.
        assert any("bad-field" in m and "not allowlisted" in m for m in messages)

    def test_empty_env_uses_field(self):
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: "   "}):
            assert RerankerConfig().effective_model == _DEFAULT_RERANKER_MODEL

    def test_env_is_whitespace_trimmed(self):
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: f"  {self._ALT_MODEL}  "}):
            assert RerankerConfig().effective_model == self._ALT_MODEL

    def test_invalid_field_unset_env_falls_back_to_default(self, caplog):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_RERANKER_MODEL_ENV, None)
            cfg = RerankerConfig(cross_encoder_model="cross-encoder/not-real")
            with caplog.at_level("WARNING"):
                assert cfg.effective_model == _DEFAULT_RERANKER_MODEL
        assert any(
            "not-real" in r.getMessage() and "not allowlisted" in r.getMessage()
            for r in caplog.records
        )


class TestHubConfig:
    def test_defaults(self):
        h = HubConfig()
        assert h.chunking.doc_max_tokens == 512
        assert h.embedding.model_name == "all-MiniLM-L6-v2"
        assert h.storage.sqlite_filename == "metadata.db"
        assert h.server.transport == "stdio"
        assert h.sources.docs_url == "https://docs.pipecat.ai/"

    def test_round_trip(self):
        _round_trip(HubConfig())

    def test_nested_override(self):
        h = HubConfig(
            chunking=ChunkingConfig(doc_max_tokens=1024),
            storage=StorageConfig(data_dir=Path("/tmp/custom")),
            sources=SourceConfig(docs_llms_txt_url="https://example.com/docs.txt"),
        )
        rebuilt = _round_trip(h)
        assert rebuilt.chunking.doc_max_tokens == 1024
        assert rebuilt.storage.data_dir == Path("/tmp/custom")
        assert rebuilt.storage.sqlite_path == Path("/tmp/custom/metadata.db")
        assert rebuilt.sources.docs_llms_txt_url == "https://example.com/docs.txt"


class TestConfigTomlExampleParity:
    """Parity: config.toml.example's key set must match docs/README.md's
    Environment Variables table, the same way test_every_click_command_is_bridged
    (tests/unit/test_plugin.py) catches an unbridged CLI command.

    docs/README.md's Environment Variables table, not .env.example, is the
    parity partner (see dev plan Architecture Decisions) — .env.example is a
    curated subset of repo bundles, not the full var registry.
    """

    def _example_keys(self) -> set[str]:
        example_text = (_REPO_ROOT / "config.toml.example").read_text(encoding="utf-8")
        return set(_PIPECAT_HUB_VAR_RE.findall(example_text))

    def _readme_keys(self) -> set[str]:
        readme_text = (_REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        section = extract_section(readme_text, "Environment Variables")
        assert section is not None, (
            "docs/README.md has no 'Environment Variables' heading — "
            "extract_section() found nothing to scope the parity check to."
        )
        return set(_PIPECAT_HUB_VAR_RE.findall(section))

    def test_config_toml_example_matches_readme_env_var_table(self):
        example_keys = self._example_keys()
        readme_keys = self._readme_keys()

        # Neither extraction may vacuously pass by finding nothing — a format
        # change (e.g. renaming the heading, or de-indenting the example's
        # `# KEY = value` lines out of match) must fail loudly, not silently
        # collapse both sides to the empty set and report "equal".
        assert example_keys, "config.toml.example: no PIPECAT_HUB_* keys found"
        assert readme_keys, "README Environment Variables section: no PIPECAT_HUB_* keys found"

        if example_keys != readme_keys:
            only_in_example = example_keys - readme_keys
            only_in_readme = readme_keys - example_keys
            raise AssertionError(
                "config.toml.example and docs/README.md's Environment Variables "
                "table have drifted apart.\n"
                f"Only in config.toml.example: {sorted(only_in_example)}\n"
                f"Only in README table: {sorted(only_in_readme)}"
            )

        # PIPECAT_HUB_PRUNE and the rest of _INVOCATION_SCOPED_KEYS are
        # invocation-scoped, not machine config, and must never appear in
        # either parity source (see dev plan Requirements / Phase 1).
        internal_vars = {
            "PIPECAT_HUB_PRUNE",
            "PIPECAT_HUB_DEBUG_PROBE",
            "PIPECAT_HUB_CONFIG_FILE",
            "PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK",
            "PIPECAT_HUB_STABILITY_OUTPUT",
        }
        assert internal_vars.isdisjoint(example_keys | readme_keys), (
            "An invocation-scoped key leaked into config.toml.example or the "
            "README's Environment Variables table."
        )

        # Drift alarm: this test's independent copy of the invocation-scoped
        # set must track shared/env_loading.py's real one, not silently
        # diverge from it.
        assert internal_vars == env_loading._INVOCATION_SCOPED_KEYS

        # `load_global_config()` warns about any PIPECAT_HUB_* key outside
        # `_KNOWN_KEYS` (typo detection). That set is a third copy of the same
        # registry, so pin it here too — otherwise adding a documented var
        # would make the loader warn about a perfectly valid setting.
        assert env_loading._KNOWN_KEYS == example_keys, (
            "shared/env_loading.py's _KNOWN_KEYS has drifted from the documented "
            "registry.\n"
            f"Only in _KNOWN_KEYS: {sorted(env_loading._KNOWN_KEYS - example_keys)}\n"
            f"Only in config.toml.example: {sorted(example_keys - env_loading._KNOWN_KEYS)}"
        )

    def test_every_env_var_the_code_reads_is_in_the_registry(self):
        """Code-anchored leg of the parity chain (round-2 finding #6).

        `config.toml.example` <-> README <-> `_KNOWN_KEYS` pins three
        *documentation* copies of the registry to each other, but nothing
        pinned any of them to the code that actually reads these vars. A new
        `PIPECAT_HUB_*` setting added to `shared/config.py` without a doc
        update produced a silently-unsupported `config.toml` key (the loader
        would warn "typo?" about a perfectly valid setting) and stayed green
        through every existing test.

        String *constants* only, via `ast` — a comment mentioning a var (e.g.
        the loader's own `PIPECAT_HUB_DATADIR` typo example) is prose, not a
        var the code reads.
        """
        allowed = env_loading._KNOWN_KEYS | env_loading._INVOCATION_SCOPED_KEYS
        found: dict[str, str] = {}
        src_root = _REPO_ROOT / "src"
        for path in sorted(src_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for match in _PIPECAT_HUB_VAR_RE.findall(node.value):
                        found.setdefault(match, str(path.relative_to(_REPO_ROOT)))

        # Non-vacuity: the scan must actually be finding var names.
        assert len(found) >= len(env_loading._KNOWN_KEYS), found

        unregistered = {k: v for k, v in found.items() if k not in allowed}
        assert not unregistered, (
            "These PIPECAT_HUB_* vars are read (or named) in src/ but appear in "
            "neither the documented registry (config.toml.example + docs/README.md + "
            "_KNOWN_KEYS) nor _INVOCATION_SCOPED_KEYS. Document them, or add them to "
            f"_INVOCATION_SCOPED_KEYS if they are not machine config:\n{unregistered}"
        )


def _import_script(path: Path, module_name_hint: str):
    """Import a non-package script file via file-path loading.

    Neither `dashboard/scripts/` nor `scripts/` is a package, so a normal
    `import` statement can't reach these files (dev plan Phase 3). A unique
    module name per call avoids `sys.modules` collisions across the three
    scripts (and across repeated test invocations in the same session).
    """
    unique_name = f"_pch_test_{module_name_hint}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(unique_name, path)
    assert spec is not None and spec.loader is not None, f"could not load spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(unique_name, None)
    return module


# Directories holding standalone (non-package) entry points, and the config
# classes whose construction marks a script as one. Module-level so *both*
# legs of the entry-point contract — the source scan
# (TestDashboardScriptConfigParity) and the behavioral resolution check
# (TestDashboardScriptDataDirResolution) — are driven by one discovery pass.
# Round-3 finding #7: a hand-maintained list on the behavioral leg would let a
# fourth entry-point script be covered by the scan while silently escaping the
# behavioral test, which is the exact drift the discovery-based scan removed.
_ENTRY_POINT_DIRS = ("dashboard/scripts", "scripts")
_CONFIG_CLASSES = ("HubConfig", "StorageConfig")


def _discover_config_entry_points() -> dict[str, tuple[Path, str, int]]:
    """Every standalone script that constructs a config object.

    Returns ``{rel_path: (path, first_constructed_class, offset)}``.
    """
    discovered: dict[str, tuple[Path, str, int]] = {}
    for rel_dir in _ENTRY_POINT_DIRS:
        for path in sorted((_REPO_ROOT / rel_dir).glob("*.py")):
            source = path.read_text(encoding="utf-8")
            hits = [(source.find(f"{cls}("), cls) for cls in _CONFIG_CLASSES if f"{cls}(" in source]
            if not hits:
                continue
            offset, cls = min(hits)
            discovered[path.relative_to(_REPO_ROOT).as_posix()] = (path, cls, offset)
    return discovered


class TestDashboardScriptConfigParity:
    """Source-level parity: each independent entry point that constructs
    StorageConfig()/HubConfig() outside cli.py:main() must call
    ``load_env_layers()`` — the shared bootstrap that performs
    load_cwd_dotenv() then load_global_config() in that order — before that
    construction, the same single call cli.py:main() makes (dev
    plan Phase 3). Mirrors test_every_click_command_is_bridged
    (tests/unit/test_plugin.py:87-90): a script that forgets (or reorders)
    the loader calls would silently diverge its resolved config.toml /
    PIPECAT_HUB_DATA_DIR from refresh/serve without this test failing.

    This is a *call-order* check only — see TestDashboardScriptDataDirResolution
    below for the behavioral companion that proves the loaded value is
    actually used (dev plan Acceptance Criteria: "verified by ... a
    behavioral test ... not source-scan alone").
    """

    # The registry under test is *discovered* (see
    # `_discover_config_entry_points()` above), not hand-maintained, so a
    # fourth script added later can't silently escape this contract — the
    # failure mode `load_env_layers()` was introduced to eliminate (round-2
    # finding #5). Mirrors test_every_click_command_is_bridged, which derives
    # its set from `hub_cli.commands` rather than listing commands.
    #
    # Non-vacuity floor only: these must always be discovered. Never the
    # source of the set under test.
    _KNOWN_ENTRY_POINTS = frozenset(
        {
            "dashboard/scripts/extract_dashboard.py",
            "dashboard/scripts/extract_embeddings.py",
            "scripts/smoke_check_removals.py",
        }
    )

    _discover_entry_points = staticmethod(_discover_config_entry_points)

    def test_discovery_finds_the_known_entry_points(self):
        """Guards the discovery pass itself: a glob that stops matching (a
        moved directory, a renamed script) must fail loudly rather than
        vacuously pass over an empty set."""
        discovered = set(self._discover_entry_points())
        missing = self._KNOWN_ENTRY_POINTS - discovered
        assert not missing, (
            f"config-constructing entry points no longer discovered: {sorted(missing)}. "
            "Update _ENTRY_POINT_DIRS/_KNOWN_ENTRY_POINTS if they legitimately moved."
        )

    def test_scripts_call_loaders_before_construction(self):
        for rel_path, (path, config_cls, construct_idx) in self._discover_entry_points().items():
            source = path.read_text(encoding="utf-8")
            bootstrap_idx = source.find("load_env_layers()")
            assert bootstrap_idx != -1, f"{rel_path}: no load_env_layers() call found"
            assert bootstrap_idx < construct_idx, (
                f"{rel_path}: expected load_env_layers() before {config_cls}() "
                f"construction; found at offsets {bootstrap_idx}, {construct_idx}"
            )
            # The ordering contract must live in the shared bootstrap, not be
            # hand-replicated here — that hand-replication is exactly what
            # this abstraction removed.
            assert "load_cwd_dotenv()" not in source, (
                f"{rel_path}: calls load_cwd_dotenv() directly; use load_env_layers()"
            )
            assert "load_global_config()" not in source, (
                f"{rel_path}: calls load_global_config() directly; use load_env_layers()"
            )

    def test_cli_main_uses_the_shared_bootstrap(self):
        """cli.py:main() is the fourth copy the abstraction replaced."""
        source = (_REPO_ROOT / "src" / "pipecat_context_hub" / "cli.py").read_text(encoding="utf-8")
        assert "env_loading.load_env_layers()" in source

    def test_load_env_layers_calls_both_loaders_in_order(self):
        """Behavioural companion to the source scan: the shared bootstrap
        really does run cwd .env before config.toml."""
        calls: list[str] = []
        with (
            patch.object(env_loading, "load_cwd_dotenv", lambda: calls.append("cwd")),
            patch.object(env_loading, "load_global_config", lambda: calls.append("global")),
        ):
            env_loading.load_env_layers()
        assert calls == ["cwd", "global"]


# Derived from the same discovery pass as the source-level scan above, not
# hand-maintained (round-3 finding #7): a fourth entry-point script must not be
# able to appear in the scan while silently escaping this behavioral check.
_BOOTSTRAP_TARGETS = [
    pytest.param(path, path.stem, id=path.name)
    for path, _cls, _offset in _discover_config_entry_points().values()
]


def _bootstrapped_data_dir(cfg: object) -> Path:
    """The resolved data dir of whatever config object ``_bootstrap()`` returned.

    Duck-typed rather than a per-script lambda: ``smoke_check_removals.py``'s
    ``_bootstrap()`` returns a ``HubConfig`` (it needs other HubConfig fields
    elsewhere in the script), so its ``data_dir`` is one level deeper than the
    dashboard scripts', which bootstrap a bare ``StorageConfig``. Resolving
    that difference here keeps the target list purely discovery-driven.
    """
    storage: Any = getattr(cfg, "storage", cfg)
    data_dir = storage.data_dir
    assert isinstance(data_dir, Path)
    return data_dir


class TestDashboardScriptDataDirResolution:
    """Behavioral companion to TestDashboardScriptConfigParity: with
    PIPECAT_HUB_CONFIG_FILE pointed at a tmp_path config.toml setting
    PIPECAT_HUB_DATA_DIR, each script's _bootstrap() must resolve to that
    same data_dir — proving the loaded config.toml value is actually
    *used*, run uniformly against all three entry points (dev plan
    Acceptance Criteria and Phase 3, found in review: an earlier draft only
    exercised extract_dashboard.py, leaving the other two covered by the
    weaker source-level scan alone).
    """

    @pytest.mark.parametrize("script_path, module_hint", _BOOTSTRAP_TARGETS)
    def test_bootstrap_honors_config_toml_data_dir(
        self, tmp_path, monkeypatch, script_path, module_hint
    ):
        data_dir = tmp_path / "resolved-data"
        config_file = tmp_path / "config.toml"
        # Escape backslashes for TOML: on Windows, a raw WindowsPath
        # interpolated into a double-quoted TOML string is invalid TOML
        # (backslash is an escape-sequence introducer there), which
        # tomllib would fail to parse in the windows-smoke CI job.
        escaped_data_dir = str(data_dir).replace("\\", "\\\\")
        config_file.write_text(f'PIPECAT_HUB_DATA_DIR = "{escaped_data_dir}"\n', encoding="utf-8")

        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(config_file))
        monkeypatch.delenv("PIPECAT_HUB_DATA_DIR", raising=False)
        # No cwd .env in tmp_path — isolates this test from a real .env a
        # developer might have in the repo root or their actual cwd.
        monkeypatch.chdir(tmp_path)

        module = _import_script(script_path, module_hint)
        resolved = _bootstrapped_data_dir(module._bootstrap())

        assert resolved == data_dir.expanduser().resolve(strict=False)


def _discover_benchmark_env_reads() -> dict[str, set[str]]:
    """Every ``PIPECAT_HUB_*`` var the benchmark modules read from the real env.

    Returns ``{rel_path: {env_var_value, ...}}``. Discovery, not a hand-kept
    list: the module-level ``_NAME = "PIPECAT_HUB_…"`` constants are resolved,
    then only those actually *read* (``os.environ.get(_NAME)`` /
    ``os.getenv(_NAME)`` / ``os.environ[_NAME]`` / ``_NAME in os.environ``)
    count. The read/write distinction is load-bearing: ``test_chromadb_perf``'s
    ``_HUB_DATA_DIR_ENV`` is *written* into a subprocess env and must NOT be
    passed through the isolation fixture.
    """
    reads: dict[str, set[str]] = {}
    for path in sorted((_REPO_ROOT / "tests" / "benchmarks").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            value = node.value
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value.startswith("PIPECAT_HUB_")
            ):
                constants[target.id] = value.value

        found: set[str] = set()

        def _record(node: ast.AST, _constants=constants, _found=found) -> None:
            if isinstance(node, ast.Name) and node.id in _constants:
                _found.add(_constants[node.id])

        for expr in ast.walk(tree):
            # os.environ.get(_NAME, ...) / os.getenv(_NAME, ...)
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
                if expr.func.attr in {"get", "getenv"} and expr.args:
                    _record(expr.args[0])
            # os.environ[_NAME]
            elif isinstance(expr, ast.Subscript):
                _record(expr.slice)
            # _NAME in os.environ
            elif isinstance(expr, ast.Compare) and any(
                isinstance(op, ast.In | ast.NotIn) for op in expr.ops
            ):
                _record(expr.left)

        if found:
            reads[path.relative_to(_REPO_ROOT).as_posix()] = found
    return reads


class TestBenchmarkEnvPassthroughParity:
    """Round-9 finding #5: `tests/conftest.py`'s `_FIXTURE_PASSTHROUGH_KEYS`
    was a hand-maintained third copy of a set that already exists as module
    constants across the four benchmark modules, with nothing linking them —
    unlike every other registry on this branch (`_KNOWN_KEYS`, the entry-point
    discovery above), which is parity-pinned or discovery-driven precisely to
    prevent this drift class.

    Both drift directions are silent and both are wrong:

    * a benchmark gaining a new opt-in var that nobody adds here has it wiped
      by the fixture's setup-time `PIPECAT_HUB_*` sweep, so the developer's
      explicit `FOO=1 pytest …` produces a *skip* that looks like a pass;
    * an allowlist entry no benchmark reads any more leaves a real shell
      export visible during unrelated tests, which is the leak the setup-time
      sweep exists to close.

    So this asserts set equality, discovery-side, with a non-vacuity floor.
    """

    _KNOWN_BENCHMARK_MODULES = frozenset(
        {
            "tests/benchmarks/test_runtime_stability.py",
            "tests/benchmarks/test_chromadb_perf.py",
            "tests/benchmarks/test_retrieval_quality.py",
            "tests/benchmarks/test_chromadb_parity.py",
        }
    )

    def test_discovery_finds_the_known_benchmark_modules(self):
        """Guards the discovery pass itself — a moved directory or a renamed
        module must fail loudly rather than vacuously pass over an empty set."""
        discovered = set(_discover_benchmark_env_reads())
        missing = self._KNOWN_BENCHMARK_MODULES - discovered
        assert not missing, (
            f"benchmark modules no longer discovered: {sorted(missing)}. "
            "Update the glob or _KNOWN_BENCHMARK_MODULES if they legitimately moved."
        )

    def test_every_read_benchmark_env_var_is_passed_through(self):
        from tests.conftest import _FIXTURE_PASSTHROUGH_KEYS

        read_vars = set().union(*_discover_benchmark_env_reads().values())
        assert read_vars == set(_FIXTURE_PASSTHROUGH_KEYS), (
            "tests/conftest.py's _FIXTURE_PASSTHROUGH_KEYS has drifted from the "
            "PIPECAT_HUB_* vars the benchmark modules actually read.\n"
            f"  read but not allowlisted (would be wiped -> silent skip): "
            f"{sorted(read_vars - set(_FIXTURE_PASSTHROUGH_KEYS))}\n"
            f"  allowlisted but never read (stale -> leaks into unrelated tests): "
            f"{sorted(set(_FIXTURE_PASSTHROUGH_KEYS) - read_vars)}"
        )
