"""Unit tests for CLI helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from pipecat_context_hub.cli import (
    _PRUNE_ENABLED_VALUES,
    _load_dotenv,
    _prewarm_models,
    _print_refresh_summary,
    _prune_enabled,
    _redact_home,
    _safe_hr,
    _warmup_enabled,
    _write_serve_debug_probe,
    main,
)
from pipecat_context_hub.services.index.fts import METADATA_CONTRACT_VERSION
from pipecat_context_hub.shared import env_loading
from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.env_loading import load_cwd_dotenv, load_global_config


class TestLoadDotenvBackCompatAlias:
    """`cli._load_dotenv` moved to `shared.env_loading.load_cwd_dotenv`
    (dev plan Phase 1); parsing behavior itself is covered by
    tests/unit/test_env_loading.py::TestLoadCwdDotenv against the real
    home. This just pins that the re-export in cli.py is not a stale copy.
    """

    def test_load_dotenv_is_the_real_loader(self):
        assert _load_dotenv is load_cwd_dotenv


class TestWriteServeDebugProbe:
    """`_write_serve_debug_probe()` must never crash `serve` on failure —
    including when `Path.home()` itself raises (RuntimeError, not OSError),
    a gap the original `except OSError` clause missed."""

    def test_writes_probe_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        logger = MagicMock()
        _write_serve_debug_probe(logger)
        probe_path = tmp_path / ".cache" / "pipecat-context-hub" / "serve-debug.log"
        assert probe_path.is_file()
        logger.info.assert_called_once()

    def test_path_home_runtime_error_is_swallowed(self, monkeypatch):
        """Path.home() raises RuntimeError (not OSError) when the home
        directory can't be determined — the probe must log and swallow this,
        not propagate it and crash serve."""

        def _raise_no_home():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "home", _raise_no_home)
        logger = MagicMock()
        _write_serve_debug_probe(logger)  # must not raise
        logger.exception.assert_called_once()

    def test_mkdir_oserror_is_swallowed(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            Path,
            "mkdir",
            MagicMock(side_effect=OSError("permission denied")),
        )
        logger = MagicMock()
        _write_serve_debug_probe(logger)  # must not raise
        logger.exception.assert_called_once()


class TestRedactHome:
    """Tests for the home-directory redaction helper used in startup telemetry."""

    def test_replaces_home_prefix_with_tilde(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        nested = tmp_path / "Library" / "Application Support" / "hub" / "data"
        assert _redact_home(nested) == "~" + str(nested)[len(str(tmp_path)) :]

    def test_exact_home_path_becomes_tilde(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert _redact_home(tmp_path) == "~"

    def test_non_home_path_unchanged(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        unrelated = Path("/var/lib/hub/data")
        assert _redact_home(unrelated) == str(unrelated)

    def test_sibling_of_home_not_redacted(self, monkeypatch, tmp_path: Path):
        # /home/alice should not match /home/alicebob as a prefix.
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "alice")
        (tmp_path / "alice").mkdir()
        sibling = tmp_path / "alicebob" / "data"
        assert _redact_home(sibling) == str(sibling)

    def test_accepts_string_input(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert _redact_home(str(tmp_path / "foo")) == "~" + os.sep + "foo"


_DEFAULT_REPOS = HubConfig().sources.repos
_DEFAULT_REPO_COUNT = len(_DEFAULT_REPOS)


def _sha_metadata(sha: str = "abc123", repos: list[str] | None = None) -> dict[str, str]:
    """Build commit_sha metadata dict for all default repos."""
    repos = repos or _DEFAULT_REPOS
    return {f"repo:{r}:commit_sha": sha for r in repos}


class TestRefreshCommand:
    """Tests for the refresh command's incremental skip logic."""

    @pytest.fixture(autouse=True)
    def _mock_deprecation_map(self):
        """Avoid touching the registry/filesystem during refresh tests."""
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map.build_deprecation_map_from_registry",
            return_value=MagicMock(entries={}, save=MagicMock()),
        ):
            yield

    def _make_mocks(self):
        """Create shared mock objects for refresh tests."""
        mock_index_store = MagicMock()
        mock_index_store.get_metadata = MagicMock(return_value=None)
        mock_index_store.set_metadata = MagicMock()
        mock_index_store.delete_metadata = MagicMock()
        mock_index_store.get_all_metadata = MagicMock(return_value={})
        mock_index_store.delete_by_content_type = AsyncMock(return_value=0)
        mock_index_store.delete_by_repo = AsyncMock(return_value=0)
        mock_index_store.get_index_stats = MagicMock(
            return_value={
                "counts_by_type": {"doc": 100, "code": 200},
                "total": 300,
                "commit_shas": [],
            }
        )
        mock_index_store.reset = MagicMock()
        mock_index_store.close = MagicMock()

        mock_crawler = MagicMock()
        mock_crawler.fetch_llms_txt = AsyncMock(
            return_value="# Page\nSource: https://example.com\nContent here"
        )
        mock_crawler.ingest = AsyncMock(return_value=MagicMock(records_upserted=10, errors=[]))
        mock_crawler.close = AsyncMock()

        mock_github = MagicMock()
        mock_github.clone_or_fetch = MagicMock(return_value=(Path("/tmp/repo"), "abc123"))
        mock_github.ingest = AsyncMock(return_value=MagicMock(records_upserted=20, errors=[]))

        mock_source_ingester = MagicMock()
        mock_source_ingester.ingest = AsyncMock(
            return_value=MagicMock(records_upserted=5, errors=[])
        )

        return mock_index_store, mock_crawler, mock_github, mock_source_ingester

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_force_flag_bypasses_skip(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """--force bypasses all skip logic even when hashes match."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Simulate matching hash/SHA (would skip without --force)
        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--force"])

        assert result.exit_code == 0
        # With --force, docs should be re-ingested despite matching hash
        mock_crawler.ingest.assert_called_once()
        # With --force, repos should be re-ingested despite matching SHA
        assert mock_github.ingest.call_count == _DEFAULT_REPO_COUNT

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_skip_when_sha_matches(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """Refresh skips unchanged sources when hashes/SHAs match."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # Docs should be skipped (matching hash)
        mock_crawler.ingest.assert_not_called()
        # Repos should be skipped (matching SHA)
        mock_github.ingest.assert_not_called()
        mock_source.ingest.assert_not_called()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_recovered_repo_forces_reingest_even_when_sha_matches(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """A repo whose corrupt clone was recovered must be re-ingested even
        when its remote SHA matches the stored one — otherwise the index
        keeps reflecting the empty/broken prior state."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Mark every configured repo as recovered this run.
        config = HubConfig()
        recovered = set(config.sources.effective_repos)
        mock_github.recovered_repos = recovered

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        # Docs hash still matches — docs path untouched.
        mock_crawler.ingest.assert_not_called()
        # But every recovered repo must be re-ingested despite matching SHA.
        assert mock_github.ingest.call_count == _DEFAULT_REPO_COUNT
        assert mock_source.ingest.call_count == _DEFAULT_REPO_COUNT

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_full_ingest_when_sha_differs(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """Refresh re-ingests when stored SHA differs from current."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Stored SHA is old, current is different
        meta = {"docs:content_hash": "old-hash", **_sha_metadata("old-sha")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # Different hash → docs re-ingested
        mock_crawler.ingest.assert_called_once()
        # Different SHA → repos re-ingested (once per changed repo)
        assert mock_github.ingest.call_count == _DEFAULT_REPO_COUNT

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_docs_hash_not_stored_on_ingest_error(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """Docs content hash is not cached when ingest returns errors."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Docs ingest returns errors (e.g. upsert failure)
        mock_crawler.ingest = AsyncMock(
            return_value=MagicMock(records_upserted=0, errors=["Upsert failed"]),
        )
        # Repos unchanged so they don't interfere
        mock_store.get_metadata = MagicMock(
            side_effect=lambda key: _sha_metadata("abc123").get(key)
        )

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # docs:content_hash should NOT have been stored
        set_calls = {call.args[0] for call in mock_store.set_metadata.call_args_list}
        assert "docs:content_hash" not in set_calls

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_repo_sha_not_stored_on_ingest_error(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """Repo commit SHA is not cached when code/source ingest has errors."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # GitHub ingest returns errors for any repo
        mock_github.ingest = AsyncMock(
            return_value=MagicMock(records_upserted=0, errors=["clone failed"]),
        )
        # Docs unchanged
        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        mock_store.get_metadata = MagicMock(
            side_effect=lambda key: {
                "docs:content_hash": content_hash,
            }.get(key)
        )

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # repo:*:commit_sha should NOT have been stored for changed repos with errors
        set_calls = {call.args[0] for call in mock_store.set_metadata.call_args_list}
        for repo in _DEFAULT_REPOS:
            assert f"repo:{repo}:commit_sha" not in set_calls
        # Failed repos should have their cached SHA deleted (P1)
        delete_calls = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        for repo in _DEFAULT_REPOS:
            assert f"repo:{repo}:commit_sha" in delete_calls

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_force_failed_repo_invalidates_cached_sha(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """--force with ingest failure deletes cached SHA so next refresh retries."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # GitHub ingest fails
        mock_github.ingest = AsyncMock(
            return_value=MagicMock(records_upserted=0, errors=["transient error"]),
        )
        # SHA matches (would skip without --force), but --force overrides
        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--force"])

        assert result.exit_code == 0
        # Failed repos should have cached SHA deleted, not preserved
        delete_calls = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        for repo in _DEFAULT_REPOS:
            assert f"repo:{repo}:commit_sha" in delete_calls

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_removed_repo_cleaned_up_with_prune(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """Repos no longer in effective_repos have their data and SHA
        cleaned up — opt-in via --prune as of dev plan Phase 4 (see
        TestPruneSafety for the new no-prune default and TestRefreshCommand
        equivalents there); this test previously asserted unconditional
        deletion, which was the exact behavior Phase 4 changed."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Simulate a previously-indexed repo that is no longer configured
        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        all_meta = {**_sha_metadata("abc123"), "repo:old-org/removed-repo:commit_sha": "def456"}
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        meta = {"docs:content_hash": content_hash, **all_meta}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--prune"])

        assert result.exit_code == 0
        # The removed repo should be cleaned up
        mock_store.delete_by_repo.assert_any_call("old-org/removed-repo")
        mock_store.delete_metadata.assert_any_call("repo:old-org/removed-repo:commit_sha")

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    @patch("pipecat_context_hub.cli._delete_local_index_storage")
    def test_reset_index_forces_full_rebuild(
        self,
        mock_delete_storage,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """--reset-index should wipe state and force a full re-ingest."""
        events: list[str] = []
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_delete_storage.side_effect = lambda *_args, **_kwargs: events.append("delete")

        def _record_store(*_args, **_kwargs):
            events.append("store")
            return mock_store

        mock_is_cls.side_effect = _record_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--reset-index"])

        assert result.exit_code == 0
        mock_delete_storage.assert_called_once()
        assert events[:2] == ["delete", "store"]
        mock_store.reset.assert_not_called()
        mock_crawler.ingest.assert_called_once()
        assert mock_github.ingest.call_count == _DEFAULT_REPO_COUNT
        mock_store.close.assert_called_once()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_tainted_ref_skips_refresh_and_keeps_last_known_good(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """A tainted upstream HEAD should be skipped without deleting a safe cached SHA."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_github.clone_or_fetch.side_effect = lambda repo_slug, _checkout=False, tag=None: (
            Path(f"/tmp/{repo_slug.replace('/', '_')}"),
            "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
        )
        mock_ref_tainted.side_effect = lambda _repo_path, sha, _refs: sha == "badcafe"

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        # Override pipecat with a good known SHA (not the tainted one)
        meta["repo:pipecat-ai/pipecat:commit_sha"] = "good123"
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REFS", "pipecat-ai/pipecat@badcafe")
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        mock_github.ingest.assert_not_called()
        mock_source.ingest.assert_not_called()
        mock_store.delete_by_repo.assert_not_called()
        # delete_metadata should not have been called for any repo SHA keys;
        # the only allowed call is clearing framework_version when not pinned.
        for call in mock_store.delete_metadata.call_args_list:
            assert call.args[0] == "framework_version", (
                f"Unexpected delete_metadata call: {call.args[0]}"
            )
        set_calls = {call.args[0] for call in mock_store.set_metadata.call_args_list}
        assert "repo:pipecat-ai/pipecat:commit_sha" not in set_calls

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_tainted_ref_removes_indexed_tainted_sha(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """If the cached SHA is also tainted, local records are removed."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_github.clone_or_fetch.side_effect = lambda repo_slug, _checkout=False, tag=None: (
            Path(f"/tmp/{repo_slug.replace('/', '_')}"),
            "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
        )
        mock_ref_tainted.side_effect = lambda _repo_path, sha, _refs: sha == "badcafe"

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        # Override pipecat with the tainted SHA to trigger removal
        meta["repo:pipecat-ai/pipecat:commit_sha"] = "badcafe"
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REFS", "pipecat-ai/pipecat@badcafe")
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        mock_store.delete_by_repo.assert_any_call("pipecat-ai/pipecat")
        mock_store.delete_metadata.assert_any_call("repo:pipecat-ai/pipecat:commit_sha")
        mock_github.ingest.assert_not_called()
        mock_source.ingest.assert_not_called()
        set_calls = {call.args[0] for call in mock_store.set_metadata.call_args_list}
        assert "repo:pipecat-ai/pipecat:commit_sha" not in set_calls

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_tainted_framework_does_not_rebuild_deprecation_map(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """A tainted framework checkout must not feed the deprecation-map build.

        `prefetched` holds the tainted checkout (populated before the taint
        check), but rebuilding the map from it would publish deprecation data
        derived from content the index never ingested. The last known-good
        map on disk must be left alone instead.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_github.clone_or_fetch.side_effect = lambda repo_slug, _checkout=False, tag=None: (
            Path(f"/tmp/{repo_slug.replace('/', '_')}"),
            "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
        )
        mock_ref_tainted.side_effect = lambda _repo_path, sha, _refs: sha == "badcafe"

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        meta["repo:pipecat-ai/pipecat:commit_sha"] = "good123"
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REFS", "pipecat-ai/pipecat@badcafe")
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map.build_deprecation_map_from_registry"
        ) as mock_build_dep_map:
            mock_build_dep_map.return_value = MagicMock(entries={}, save=MagicMock())
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        mock_build_dep_map.assert_not_called()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_untainted_framework_still_rebuilds_deprecation_map(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """Control case: an untainted, successfully-cloned framework repo
        still rebuilds the map — guards against the fix over-suppressing."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        meta = _sha_metadata("old-sha")
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map.build_deprecation_map_from_registry"
        ) as mock_build_dep_map:
            mock_build_dep_map.return_value = MagicMock(entries={}, save=MagicMock())
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        mock_build_dep_map.assert_called_once()


class TestResetIndexGlobalConfigInteraction(TestRefreshCommand):
    """`refresh --reset-index` through the real (unmocked) code path for
    `_delete_local_index_storage` — config.toml survival + the
    PIPECAT_HUB_DATA_DIR collision guard (dev plan Phase 1, reset-index
    survival). Subclasses TestRefreshCommand to reuse its `_make_mocks()`
    helper and its autouse `_mock_deprecation_map` fixture; every test here
    deliberately does NOT `@patch` `cli._delete_local_index_storage` —
    mocking it would make "config file still exists" vacuously true, since
    nothing would actually be deleted. `test_reset_index_forces_full_rebuild`
    above does mock it and exercises different behavior (event ordering);
    that pattern is not copied here.
    """

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_reset_index_survives_with_real_delete_override_config_path(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """PIPECAT_HUB_CONFIG_FILE override case: config.toml survives a
        real `shutil.rmtree` of a separate PIPECAT_HUB_DATA_DIR (proving the
        real deletion path ran, not a mock)."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        config_dir = tmp_path / "config-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')

        data_dir = tmp_path / "data-home"
        data_dir.mkdir()
        sentinel_file = data_dir / "sentinel.db"
        sentinel_file.write_text("not empty")

        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(config_file))
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--reset-index"])

        assert result.exit_code == 0, result.output
        # Config file survives and still loads without error afterward.
        assert config_file.exists()
        load_global_config()
        assert os.environ["PIPECAT_HUB_STALE_AFTER_DAYS"] == "45"
        # The real data directory was actually removed (shutil.rmtree ran,
        # not a mock): our sentinel file is gone.
        assert not sentinel_file.exists()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_reset_index_survives_with_real_delete_default_config_path(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """No-override case: DEFAULT_CONFIG_PATH branch, reached by
        monkeypatching `env_loading.DEFAULT_CONFIG_PATH` (not `Path.home`)
        after explicitly clearing the autouse fixture's nonexistent-sentinel
        default for PIPECAT_HUB_CONFIG_FILE. A precondition assertion runs
        before any CLI invocation so a missed `delenv` or an inert
        monkeypatch (module-attribute-lookup-at-call-time contract violated)
        fails loudly here, rather than letting `--reset-index` silently
        target the developer's real default index.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        config_dir = tmp_path / "config-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')

        data_dir = tmp_path / "data-home"
        data_dir.mkdir()
        sentinel_file = data_dir / "sentinel.db"
        sentinel_file.write_text("not empty")

        # Clear the autouse fixture's sentinel default so the real
        # DEFAULT_CONFIG_PATH branch runs, not the sentinel path.
        monkeypatch.delenv("PIPECAT_HUB_CONFIG_FILE", raising=False)
        monkeypatch.setattr(env_loading, "DEFAULT_CONFIG_PATH", config_file)
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
        monkeypatch.chdir(tmp_path)

        # Precondition: fails loudly, before any shutil.rmtree can run, if
        # either the delenv was missed (sentinel still active) or the
        # DEFAULT_CONFIG_PATH monkeypatch is silently inert.
        assert env_loading.resolve_global_config_path() == config_file

        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--reset-index"])

        assert result.exit_code == 0, result.output
        assert config_file.exists()
        assert not sentinel_file.exists()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_reset_index_refuses_colliding_data_dir_before_deletion(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """A higher-precedence PIPECAT_HUB_DATA_DIR (real env var here)
        pointing at/above the active config path must make `--reset-index`
        abort clearly before any deletion runs, config file still present.
        This is `_delete_local_index_storage()`'s defense-in-depth guard —
        distinct from `load_global_config()`'s own collision skip, which
        only covers a colliding value that originates inside config.toml
        itself.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        config_dir = tmp_path / "config-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')

        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(config_file))
        # Collides: config_dir (which contains config_file) is requested as
        # the data dir to reset.
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(config_dir))
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--reset-index"])

        assert result.exit_code != 0
        assert config_file.exists()
        mock_is_cls.assert_not_called()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_reset_index_refuses_symlinked_config_inside_data_dir_before_deletion(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """The active config path (PIPECAT_HUB_CONFIG_FILE) is a symlink
        located INSIDE the requested PIPECAT_HUB_DATA_DIR, whose target is a
        real file OUTSIDE that data dir. Before the Codex-review fix,
        `config_collides_with_dir` checked only the fully-resolved target
        (outside the data dir), so this would have been a false negative and
        `--reset-index` would have rmtree'd the directory containing the
        operator's only path to their config.toml. Must abort before any
        deletion runs, with the symlink (and its target) still present.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        data_dir = tmp_path / "data-home"
        data_dir.mkdir()
        real_config_dir = tmp_path / "elsewhere"
        real_config_dir.mkdir()
        real_config_file = real_config_dir / "real-config.toml"
        real_config_file.write_text('PIPECAT_HUB_STALE_AFTER_DAYS = "45"\n')

        symlink_config_path = data_dir / "config.toml"
        symlink_config_path.symlink_to(real_config_file)

        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(symlink_config_path))
        # Collides: data_dir (which contains the config symlink) is requested
        # as the data dir to reset.
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--reset-index"])

        assert result.exit_code != 0
        assert "Refusing to delete" in result.output
        assert symlink_config_path.is_symlink()
        assert real_config_file.exists()
        mock_is_cls.assert_not_called()


class TestRefreshProvenanceMetadata:
    """Refresh stamps the contract version and the indexed pipecat revision."""

    @pytest.fixture(autouse=True)
    def _mock_deprecation_map(self):
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map.build_deprecation_map_from_registry",
            return_value=MagicMock(entries={}, save=MagicMock()),
        ):
            yield

    def _run_refresh(self, mocks, describe_result, tmp_path, monkeypatch):
        """Invoke refresh with the shared mock harness; return set_metadata as a dict."""
        mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted = mocks
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        monkeypatch.chdir(tmp_path)
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=describe_result,
        ):
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        written = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        # The end-of-refresh metadata pass writes via a single batched call
        # (set_metadata_batch) rather than individual set_metadata calls.
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])
        return mock_store, written

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_stamps_contract_version_and_indexed_revision(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        _store, written = self._run_refresh(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            ("1.6.0", 55),
            tmp_path,
            monkeypatch,
        )

        assert written["metadata_contract_version"] == str(METADATA_CONTRACT_VERSION)
        assert written["indexed_framework_version"] == "1.6.0"
        assert written["indexed_framework_commits_ahead"] == "55"

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_end_of_refresh_metadata_written_via_single_batch_call(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """The end-of-refresh metadata pass calls `set_metadata_batch` exactly
        once and never falls back to bare `set_metadata` for those related
        keys — guards against a regression to per-key writes that could leave
        a reader observing a partially-updated related-key set."""
        mock_store, written = self._run_refresh(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            ("1.6.0", 55),
            tmp_path,
            monkeypatch,
        )

        mock_store.set_metadata_batch.assert_called_once()
        batch_pairs, batch_kwargs = mock_store.set_metadata_batch.call_args
        end_of_refresh_keys = {
            "metadata_contract_version",
            "last_refresh_duration_seconds",
            "last_refresh_records_upserted",
            "last_refresh_error_count",
            "content_type_counts",
            "indexed_framework_version",
            "indexed_framework_commits_ahead",
            "last_refresh_at",
        }
        assert end_of_refresh_keys <= set(batch_pairs[0])
        # None of the batched keys should also have been written individually.
        individually_written = {call.args[0] for call in mock_store.set_metadata.call_args_list}
        assert not (end_of_refresh_keys & individually_written)

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_tainted_framework_is_not_stamped(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """A tainted framework ref is never ingested, so it must not be described.

        `prefetched` is populated as soon as the clone succeeds — before the
        taint check — so the checkout is available even though nothing from it
        reaches the index.
        """
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_github.clone_or_fetch.side_effect = lambda repo_slug, _checkout=False, tag=None: (
            Path(f"/tmp/{repo_slug.replace('/', '_')}"),
            "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
        )
        mock_ref_tainted.side_effect = lambda _repo_path, sha, _refs: sha == "badcafe"

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REFS", "pipecat-ai/pipecat@badcafe")
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("1.6.0", 0),
        ):
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        written = {call.args[0] for call in mock_store.set_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])
        assert "indexed_framework_version" not in written
        assert "indexed_framework_commits_ahead" not in written

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_removed_framework_records_clear_the_stamp(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """When the indexed ref is also tainted its records go, so must the stamp."""
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_github.clone_or_fetch.side_effect = lambda repo_slug, _checkout=False, tag=None: (
            Path(f"/tmp/{repo_slug.replace('/', '_')}"),
            "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
        )
        mock_ref_tainted.side_effect = lambda _repo_path, sha, _refs: sha == "badcafe"
        meta = {"repo:pipecat-ai/pipecat:commit_sha": "badcafe"}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REFS", "pipecat-ai/pipecat@badcafe")
        result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        deleted = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        assert "indexed_framework_version" in deleted
        assert "indexed_framework_commits_ahead" in deleted

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_undescribable_checkout_leaves_stamp_alone(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """A checkout with no reachable tags must not clear a previous stamp."""
        store, written = self._run_refresh(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            (None, None),
            tmp_path,
            monkeypatch,
        )

        assert "indexed_framework_version" not in written
        deleted = {call.args[0] for call in store.delete_metadata.call_args_list}
        assert "indexed_framework_version" not in deleted
        assert "indexed_framework_commits_ahead" not in deleted

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_failed_framework_ingest_is_not_stamped(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """The framework repo's SHA changed (so it's in `changed_repos`, not
        tainted) but ingest fails for it — it must not land in
        `ingested_repos`, and its version must not be stamped, leaving any
        prior known-good stamp untouched rather than describing content that
        was never successfully indexed.
        """
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Framework repo's remote SHA differs from stored -> changed_repos.
        # Other repos are unchanged so they don't interfere.
        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        meta["repo:pipecat-ai/pipecat:commit_sha"] = "old-sha"
        # Leave a prior known-good stamp to verify it survives untouched.
        meta["indexed_framework_version"] = "1.5.0"
        meta["indexed_framework_commits_ahead"] = "3"
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        def _ingest_side_effect(*, repos, **_kwargs):
            if repos == ["pipecat-ai/pipecat"]:
                return MagicMock(records_upserted=0, errors=["ingest failed"])
            return MagicMock(records_upserted=20, errors=[])

        mock_github.ingest = AsyncMock(side_effect=_ingest_side_effect)

        monkeypatch.chdir(tmp_path)
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("1.6.0", 55),
        ) as mock_describe:
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        # The tainted-framework fix's checkout-availability check must not
        # let a failed-but-untainted ingest through either.
        mock_describe.assert_not_called()

        written = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])
        assert "indexed_framework_version" not in written
        assert "indexed_framework_commits_ahead" not in written

        deleted = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            deleted.update(batch_call.kwargs.get("delete_keys") or ())
        assert "indexed_framework_version" not in deleted
        assert "indexed_framework_commits_ahead" not in deleted


class TestPruneEnabledValues:
    """`_PRUNE_ENABLED_VALUES` frozenset exactness and `_prune_enabled()`
    env-var parsing (dev plan docs/dev_plans/20260807-feature-global-config-toml.md,
    Phase 4). Polarity is inverted from `_warmup_enabled()`: `PIPECAT_HUB_PRUNE`
    defaults `False` (deletion is opt-in), so an *unrecognized* value must
    resolve to the safe default, not the enabling one.
    """

    def test_prune_enabled_values_is_value_exact(self) -> None:
        assert _PRUNE_ENABLED_VALUES == frozenset(
            {"1", "true", "True", "TRUE", "yes", "Yes", "YES"}
        )

    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "Yes", "YES"])
    def test_recognized_values_enable(self, value: str, monkeypatch) -> None:
        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, value)
        assert _prune_enabled() is True

    def test_unset_defaults_false(self, monkeypatch) -> None:
        monkeypatch.delenv(env_loading.PRUNE_ENV_VAR, raising=False)
        assert _prune_enabled() is False

    def test_falsy_zero_defaults_false(self, monkeypatch) -> None:
        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, "0")
        assert _prune_enabled() is False

    def test_empty_string_defaults_false(self, monkeypatch) -> None:
        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, "")
        assert _prune_enabled() is False

    @pytest.mark.parametrize("value", ["maybe", "2", "tRuE", "YES!", "on", "TrUe"])
    def test_garbage_and_non_member_case_variants_default_false(
        self, value: str, monkeypatch
    ) -> None:
        """`"tRuE"` is NOT a frozenset member — the frozenset is
        case/value-exact, not case-insensitive — so an arbitrary case
        variant must resolve to the safe default like any other garbage
        value, not `True`."""
        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, value)
        assert _prune_enabled() is False

    def test_whitespace_stripped_before_matching(self, monkeypatch) -> None:
        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, " 1 ")
        assert _prune_enabled() is True

    def test_whitespace_padded_non_member_still_false(self, monkeypatch) -> None:
        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, " tRuE ")
        assert _prune_enabled() is False


class TestPruneSafety(TestRefreshCommand):
    """`refresh` no longer deletes previously-indexed data for a repo that's
    still configured somewhere, just not visible from the current
    invocation's env layering (dev plan
    docs/dev_plans/20260807-feature-global-config-toml.md, Phase 4).
    Deletion becomes an explicit, opt-in action via `--prune`/
    `PIPECAT_HUB_PRUNE`, never an automatic side effect of running
    `refresh` from the "wrong" directory. Tainted-repo cleanup remains
    unconditional and unaffected. Subclasses `TestRefreshCommand` to reuse
    `_make_mocks()` and the autouse `_mock_deprecation_map` fixture.
    """

    _REMOVED_REPO = "old-org/removed-repo"

    def _meta_with_removed_repo(self, sha: str = "abc123") -> dict[str, str]:
        """Metadata simulating a previously-indexed repo no longer
        configured this run (not tainted — an accidental-absence case)."""
        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        return {
            "docs:content_hash": content_hash,
            **_sha_metadata(sha),
            f"repo:{self._REMOVED_REPO}:commit_sha": "def456",
        }

    def _framework_excluded_config(self) -> HubConfig:
        """A `HubConfig` whose `sources.repos` omits the framework repo, so
        it can be "unconfigured this run" without being tainted — tainting
        is a different, unconditional-delete code path this phase does not
        touch, so it can't be used to exercise the framework-metadata
        survival case."""
        base = HubConfig()
        repos = [r for r in base.sources.repos if r != "pipecat-ai/pipecat"]
        sources = base.sources.model_copy(update={"repos": repos})
        return base.model_copy(update={"sources": sources})

    # ----- Happy paths -----

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_no_prune_repo_unconfigured_records_and_metadata_survive(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
        caplog,
    ):
        """Without --prune, an unconfigured-this-run (non-tainted) repo's
        indexed records AND its metadata survive — deleting metadata while
        records survive would orphan the repo from every future cleanup
        pass, since the loop keys off `all_meta`. A warning is logged and a
        summary line is emitted."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        with caplog.at_level("WARNING", logger="pipecat_context_hub.cli"):
            result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        deleted_repos = [call.args[0] for call in mock_store.delete_by_repo.call_args_list]
        assert self._REMOVED_REPO not in deleted_repos
        deleted_meta = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        assert f"repo:{self._REMOVED_REPO}:commit_sha" not in deleted_meta
        assert self._REMOVED_REPO in caplog.text
        assert "--prune" in caplog.text
        assert re.search(r"Skipped pruning.*1", result.output)

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_no_prune_framework_repo_unconfigured_provenance_metadata_survives(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """Same as above, specifically for the framework repo: without
        --prune, `indexed_framework_version`/`indexed_framework_commits_ahead`
        must NOT be deleted alongside the (surviving) framework records."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        custom_config = self._framework_excluded_config()
        all_meta = {
            "docs:content_hash": content_hash,
            **_sha_metadata("abc123", repos=custom_config.sources.repos),
            "repo:pipecat-ai/pipecat:commit_sha": "def456",
        }
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.chdir(tmp_path)
        with patch("pipecat_context_hub.cli.HubConfig", return_value=custom_config):
            runner = CliRunner()
            result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        deleted_repos = [call.args[0] for call in mock_store.delete_by_repo.call_args_list]
        assert "pipecat-ai/pipecat" not in deleted_repos
        deleted_meta = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        assert "repo:pipecat-ai/pipecat:commit_sha" not in deleted_meta
        assert "indexed_framework_version" not in deleted_meta
        assert "indexed_framework_commits_ahead" not in deleted_meta

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_prune_flag_deletes_unconfigured_repo(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """--prune restores today's behavior: the unconfigured repo's
        records and metadata are deleted."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--prune"])

        assert result.exit_code == 0, result.output
        mock_store.delete_by_repo.assert_any_call(self._REMOVED_REPO)
        mock_store.delete_metadata.assert_any_call(f"repo:{self._REMOVED_REPO}:commit_sha")

    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "Yes", "YES"])
    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_prune_env_var_exact_frozenset_members_delete(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        value,
        tmp_path,
        monkeypatch,
    ):
        """`PIPECAT_HUB_PRUNE` set to exactly one of the seven
        `_PRUNE_ENABLED_VALUES` members, alone (no --prune flag), behaves
        the same as --prune. Not tested here: arbitrary case variants like
        `"tRuE"` — the frozenset is value-exact, not case-insensitive, and
        that case belongs with the unhappy-path garbage-value tests below."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, value)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        mock_store.delete_by_repo.assert_any_call(self._REMOVED_REPO)
        mock_store.delete_metadata.assert_any_call(f"repo:{self._REMOVED_REPO}:commit_sha")

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_tainted_repo_still_deleted_without_prune(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """A tainted repo (explicit exclusion via PIPECAT_HUB_TAINTED_REPOS)
        is still deleted unconditionally without --prune — this phase must
        not gate tainted-repo cleanup behind the new flag."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        tainted_slug = _DEFAULT_REPOS[0]
        all_meta = {**_sha_metadata("abc123"), f"repo:{tainted_slug}:commit_sha": "abc123"}
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REPOS", tainted_slug)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        mock_store.delete_by_repo.assert_any_call(tainted_slug)
        mock_store.delete_metadata.assert_any_call(f"repo:{tainted_slug}:commit_sha")

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_two_run_sequence_no_prune_then_prune_cleans_up(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """A no-prune refresh spares the unconfigured repo's records+metadata;
        a later refresh --prune successfully deletes them — proving
        metadata survival doesn't orphan the repo from a later prune (the
        cleanup loop keys off `all_meta`, so the surviving commit_sha key
        must still be found on the second run)."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # In-memory metadata store shared across both runs, mutated by the
        # mocked delete_metadata/get_all_metadata calls exactly like a real
        # IndexStore would be.
        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(side_effect=lambda: dict(all_meta))
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        def _delete_metadata(key: str) -> None:
            all_meta.pop(key, None)

        mock_store.delete_metadata = MagicMock(side_effect=_delete_metadata)

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        result_1 = runner.invoke(main, ["refresh"])
        assert result_1.exit_code == 0, result_1.output
        assert f"repo:{self._REMOVED_REPO}:commit_sha" in all_meta

        mock_store.delete_by_repo.reset_mock()
        mock_store.delete_metadata.reset_mock(side_effect=False)
        mock_store.delete_metadata.side_effect = _delete_metadata

        result_2 = runner.invoke(main, ["refresh", "--prune"])
        assert result_2.exit_code == 0, result_2.output
        mock_store.delete_by_repo.assert_any_call(self._REMOVED_REPO)
        mock_store.delete_metadata.assert_any_call(f"repo:{self._REMOVED_REPO}:commit_sha")
        assert f"repo:{self._REMOVED_REPO}:commit_sha" not in all_meta

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_prune_flag_with_nothing_to_prune_is_clean_noop(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """--prune with no unconfigured repos this run is a clean no-op: no
        pruning warning, no "Skipped pruning" summary line."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Only currently-configured repos are recorded — nothing to prune.
        mock_store.get_all_metadata = MagicMock(return_value=_sha_metadata("abc123"))
        mock_store.get_metadata = MagicMock(
            side_effect=lambda key: _sha_metadata("abc123").get(key)
        )

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--prune"])

        assert result.exit_code == 0, result.output
        mock_store.delete_by_repo.assert_not_called()
        assert "Skipped pruning" not in result.output

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_prune_flag_wins_over_prune_env_var_set_falsy(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """--prune passed together with PIPECAT_HUB_PRUNE=0 set: the flag
        wins (prune = prune_flag or _prune_enabled()) — deletion proceeds."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, "0")
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--prune"])

        assert result.exit_code == 0, result.output
        mock_store.delete_by_repo.assert_any_call(self._REMOVED_REPO)

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_markbackman_repro_config_toml_ab_project_env_a_only_spares_b(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
        caplog,
    ):
        """The original repro (pipecat-ai/pipecat#5122, comment 5209812757):
        `config.toml` configures repo A+B; a project-local `.env` configures
        only repo A (per-key whole-string override, not a list merge —
        `.env` wins entirely for PIPECAT_HUB_EXTRA_REPOS). `refresh` run
        from that project directory without --prune must NOT delete repo
        B's indexed records."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        repo_a = "custom-org/repo-a"
        repo_b = "custom-org/repo-b"

        config_dir = tmp_path / "config-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(f'PIPECAT_HUB_EXTRA_REPOS = "{repo_a},{repo_b}"\n')

        project_dir = tmp_path / "customer-a"
        project_dir.mkdir()
        (project_dir / ".env").write_text(f"PIPECAT_HUB_EXTRA_REPOS={repo_a}\n")

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        all_meta = {
            "docs:content_hash": content_hash,
            **_sha_metadata("abc123"),
            f"repo:{repo_a}:commit_sha": "shaA",
            f"repo:{repo_b}:commit_sha": "shaB",
        }
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(config_file))
        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        with caplog.at_level("WARNING", logger="pipecat_context_hub.cli"):
            result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        deleted_repos = [call.args[0] for call in mock_store.delete_by_repo.call_args_list]
        assert repo_b not in deleted_repos
        deleted_meta = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        assert f"repo:{repo_b}:commit_sha" not in deleted_meta
        assert repo_b in caplog.text

    # ----- Unhappy / edge paths -----

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_prune_env_var_falsy_zero_records_survive(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """PIPECAT_HUB_PRUNE=0 (falsy) → records survive, same as unset."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, "0")
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        deleted_repos = [call.args[0] for call in mock_store.delete_by_repo.call_args_list]
        assert self._REMOVED_REPO not in deleted_repos

    @pytest.mark.parametrize("value", ["maybe", "2", "", "tRuE"])
    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_prune_env_var_unrecognized_value_still_parses_and_defaults_false(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        value,
        tmp_path,
        monkeypatch,
    ):
        """An unrecognized/garbage PIPECAT_HUB_PRUNE value (including a
        non-frozenset case variant like "tRuE") must not make the CLI
        invocation itself fail — the full CliRunner invocation still parses
        successfully and _prune_enabled() resolves to False (safe default),
        records survive. This is the case the no-Click-envvar design and
        inverted-polarity fix exist to get right: Click's boolean parser
        must never get a chance to reject garbage before the helper can
        default safely."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, value)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        deleted_repos = [call.args[0] for call in mock_store.delete_by_repo.call_args_list]
        assert self._REMOVED_REPO not in deleted_repos

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_prune_env_var_whitespace_padded_member_resolves_true(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """PIPECAT_HUB_PRUNE=" 1 " (whitespace-padded, a frozenset member
        after stripping) resolves True end-to-end: deletion proceeds."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        monkeypatch.setenv(env_loading.PRUNE_ENV_VAR, " 1 ")
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        mock_store.delete_by_repo.assert_any_call(self._REMOVED_REPO)

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_config_toml_prune_is_skip_listed_and_has_no_effect(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """A config.toml setting PIPECAT_HUB_PRUNE is skip-listed by the
        Phase 1 loader (never reaches os.environ) and so has no effect on
        refresh's prune behavior: records still survive without --prune,
        even though the global file "asked" for pruning."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))

        config_dir = tmp_path / "config-home"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('PIPECAT_HUB_PRUNE = "true"\n')

        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(config_file))
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        assert env_loading.PRUNE_ENV_VAR not in os.environ
        deleted_repos = [call.args[0] for call in mock_store.delete_by_repo.call_args_list]
        assert self._REMOVED_REPO not in deleted_repos

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    @patch("pipecat_context_hub.cli._delete_local_index_storage")
    def test_prune_combined_with_reset_index_no_interaction_bug(
        self,
        mock_delete_storage,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """--prune combined with --reset-index in the same invocation: both
        apply independently, no interaction bug. --reset-index clears the
        whole local index before the cleanup pass would even find anything
        to prune, so the prune branch is a no-op that still logs cleanly
        rather than erroring on a missing index."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # A freshly-reset index has no stale repo metadata left to prune —
        # only the currently-configured default repos, modelling the real
        # empty-metadata state a genuine `shutil.rmtree` + reconstruct
        # would leave (this test's IndexStore is fully mocked, so it can't
        # observe that directly; the absence of an unconfigured repo in
        # `all_meta` stands in for it).
        mock_store.get_all_metadata = MagicMock(return_value=_sha_metadata("abc123"))
        mock_store.get_metadata = MagicMock(
            side_effect=lambda key: _sha_metadata("abc123").get(key)
        )

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--prune", "--reset-index"])

        assert result.exit_code == 0, result.output
        mock_delete_storage.assert_called_once()
        # --reset-index forces a full re-ingest, which legitimately deletes
        # and re-creates records for every currently-configured repo — that
        # is unrelated to pruning. The assertion that matters here is that
        # the *cleanup* pass found nothing to prune: no repo outside the
        # configured default set was deleted, and no prune-skip warning or
        # summary line was emitted.
        deleted_repos = {call.args[0] for call in mock_store.delete_by_repo.call_args_list}
        assert deleted_repos <= set(_DEFAULT_REPOS)
        assert "Skipped pruning" not in result.output

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_delete_by_repo_error_propagates_during_prune(
        self,
        mock_si_cls,
        mock_ref_tainted,
        mock_gh_cls,
        mock_dc_cls,
        mock_eiw_cls,
        mock_es_cls,
        mock_is_cls,
        tmp_path,
        monkeypatch,
    ):
        """index_store.delete_by_repo raising during a --prune delete: the
        error propagates and is not swallowed silently, consistent with
        today's unconditional delete-path error handling for tainted
        repos."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))
        mock_store.delete_by_repo = AsyncMock(side_effect=RuntimeError("boom"))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--prune"])

        assert result.exit_code != 0
        assert result.exception is not None
        assert "boom" in str(result.exception)


class TestRefreshIncompatibleIndex:
    """`refresh` against a pre-1.0 Chroma dir exits 2 with a bug-report hint."""

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_incompatible_index_exits_with_bug_report_hint(
        self, mock_is_cls, tmp_path, monkeypatch, caplog
    ):
        from pipecat_context_hub.services.index import IncompatibleIndexFormatError
        from pipecat_context_hub.services.index.errors import RESET_INDEX_REMEDIATION
        from pipecat_context_hub.shared.support_links import BUG_REPORT_ISSUE_URL

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        chroma_path = tmp_path / ".pipecat-context-hub" / "chroma"
        mock_is_cls.side_effect = IncompatibleIndexFormatError(chroma_path)

        monkeypatch.chdir(tmp_path)
        with caplog.at_level("ERROR", logger="pipecat_context_hub.cli"):
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 2
        # Existing remediation (embedded verbatim in exc.__str__) before the
        # Phase-2 bug-report hint appended to it.
        assert RESET_INDEX_REMEDIATION in caplog.text
        assert BUG_REPORT_ISSUE_URL in caplog.text
        assert caplog.text.index(RESET_INDEX_REMEDIATION) < caplog.text.index(BUG_REPORT_ISSUE_URL)
        # exc.__str__ embeds the absolute chroma_path; redaction must hold
        # with the appended hint in place.
        assert str(chroma_path) not in caplog.text
        assert str(tmp_path) not in caplog.text


class TestServeEmptyIndex:
    """Serve must fail fast on empty or unopenable indexes rather than hang.

    Each index-unready branch also gets a Phase-2 bug-report hint appended
    after its existing remediation — verified via ``caplog`` since these
    paths log through ``logging``, not ``click.echo``.
    """

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_empty_index_exits_nonzero(self, mock_is_cls, tmp_path, monkeypatch, caplog):
        from pipecat_context_hub.shared.support_links import BUG_REPORT_ISSUE_URL

        mock_store = MagicMock()
        mock_store.get_index_stats = MagicMock(
            return_value={
                "counts_by_type": {},
                "total": 0,
                "commit_shas": [],
            }
        )
        mock_store.close = MagicMock()
        mock_is_cls.return_value = mock_store

        monkeypatch.chdir(tmp_path)
        with caplog.at_level("ERROR", logger="pipecat_context_hub.cli"):
            result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 2
        mock_store.close.assert_called_once()
        assert "refresh" in caplog.text
        assert BUG_REPORT_ISSUE_URL in caplog.text
        assert caplog.text.index("refresh") < caplog.text.index(BUG_REPORT_ISSUE_URL)

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_open_failure_exits_nonzero(self, mock_is_cls, tmp_path, monkeypatch, caplog):
        from pipecat_context_hub.shared.support_links import BUG_REPORT_ISSUE_URL

        mock_is_cls.side_effect = RuntimeError("corrupt sqlite")

        monkeypatch.chdir(tmp_path)
        with caplog.at_level("ERROR", logger="pipecat_context_hub.cli"):
            result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 2
        assert "--reset-index" in caplog.text
        assert BUG_REPORT_ISSUE_URL in caplog.text
        assert caplog.text.index("--reset-index") < caplog.text.index(BUG_REPORT_ISSUE_URL)

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_incompatible_index_format_exits_with_bug_report_hint(
        self, mock_is_cls, tmp_path, monkeypatch, caplog
    ):
        """The third serve-startup index-unready branch (:263-274), distinct
        from the generic open-failure branch above."""
        from pipecat_context_hub.services.index import IncompatibleIndexFormatError
        from pipecat_context_hub.services.index.errors import RESET_INDEX_REMEDIATION
        from pipecat_context_hub.shared.support_links import BUG_REPORT_ISSUE_URL

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        chroma_path = tmp_path / ".pipecat-context-hub" / "chroma"
        mock_is_cls.side_effect = IncompatibleIndexFormatError(chroma_path)

        monkeypatch.chdir(tmp_path)
        with caplog.at_level("ERROR", logger="pipecat_context_hub.cli"):
            result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 2
        assert RESET_INDEX_REMEDIATION in caplog.text
        assert BUG_REPORT_ISSUE_URL in caplog.text
        assert caplog.text.index(RESET_INDEX_REMEDIATION) < caplog.text.index(BUG_REPORT_ISSUE_URL)
        assert str(chroma_path) not in caplog.text
        assert str(tmp_path) not in caplog.text

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_stats_failure_closes_store(self, mock_is_cls, tmp_path, monkeypatch):
        """If IndexStore opens but get_index_stats raises, close() is called."""
        mock_store = MagicMock()
        mock_store.get_index_stats = MagicMock(side_effect=RuntimeError("fts broken"))
        mock_store.close = MagicMock()
        mock_is_cls.return_value = mock_store

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 2
        mock_store.close.assert_called_once()

    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_post_open_exception_closes_store(
        self, mock_is_cls, mock_es_cls, tmp_path, monkeypatch
    ):
        """An exception after successful open must still close the store."""
        mock_store = MagicMock()
        mock_store.get_index_stats = MagicMock(
            return_value={
                "counts_by_type": {"doc": 1},
                "total": 1,
                "commit_shas": [],
            }
        )
        mock_store.close = MagicMock()
        mock_is_cls.return_value = mock_store
        mock_es_cls.side_effect = RuntimeError("embedding model missing")

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code != 0
        mock_store.close.assert_called_once()


class TestServeRerankerTelemetry:
    """`serve`'s startup reranker-disabled log line gets the same
    ``BUG_REPORT_ISSUE_URL`` treatment as the other two emitters (the
    cli_query warning and the MCP degraded-hub clause) for `not_cached`,
    and never for `config_disabled` (a deliberate operator choice).
    """

    def _mock_store(self) -> MagicMock:
        store = MagicMock()
        store.get_index_stats = MagicMock(
            return_value={"counts_by_type": {"doc": 1}, "total": 1, "commit_shas": []}
        )
        store.close = MagicMock()
        return store

    def _patch_common(self, stack, mock_store: MagicMock) -> dict[str, object]:
        """Patch the index/embedding/transport layer and capture the kwargs
        `create_server` was called with, without letting `serve_stdio`
        actually block on stdio."""
        stack.enter_context(
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=mock_store,
            )
        )
        stack.enter_context(
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                return_value=MagicMock(),
            )
        )
        stack.enter_context(
            patch("pipecat_context_hub.server.transport.serve_stdio", return_value=None)
        )
        captured_kwargs: dict[str, object] = {}

        def _fake_create_server(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        stack.enter_context(
            patch(
                "pipecat_context_hub.server.main.create_server",
                side_effect=_fake_create_server,
            )
        )
        return captured_kwargs

    def test_not_cached_includes_bug_report_url(self, tmp_path, monkeypatch, caplog):
        import contextlib

        from pipecat_context_hub.shared.support_links import BUG_REPORT_ISSUE_URL

        monkeypatch.chdir(tmp_path)
        with contextlib.ExitStack() as stack:
            self._patch_common(stack, self._mock_store())
            stack.enter_context(
                patch(
                    "pipecat_context_hub.shared.reranker.probe_reranker",
                    return_value=("cross-encoder/ms-marco-MiniLM-L-6-v2", None, "not_cached"),
                )
            )
            with caplog.at_level("WARNING", logger="pipecat_context_hub.cli"):
                result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 0, result.output
        assert "refresh" in caplog.text
        assert BUG_REPORT_ISSUE_URL in caplog.text
        assert caplog.text.index("refresh") < caplog.text.index(BUG_REPORT_ISSUE_URL)

    def test_config_disabled_omits_bug_report_url(self, tmp_path, monkeypatch, caplog):
        import contextlib

        from pipecat_context_hub.shared.support_links import BUG_REPORT_ISSUE_URL

        monkeypatch.chdir(tmp_path)
        with contextlib.ExitStack() as stack:
            self._patch_common(stack, self._mock_store())
            stack.enter_context(
                patch(
                    "pipecat_context_hub.shared.reranker.probe_reranker",
                    return_value=(
                        "cross-encoder/ms-marco-MiniLM-L-6-v2",
                        None,
                        "config_disabled",
                    ),
                )
            )
            with caplog.at_level("WARNING", logger="pipecat_context_hub.cli"):
                result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 0, result.output
        assert BUG_REPORT_ISSUE_URL not in caplog.text

    def test_reranker_status_provider_reports_load_failed_on_runtime_flip(
        self, tmp_path, monkeypatch
    ):
        """Phase-4 reachability guard, not just an assertion about
        instruction prose: force a constructed reranker's ``.enabled`` false
        (simulating a runtime ONNX load failure inside the long-lived
        `serve` process) and confirm the live `_reranker_status()` closure
        passed to `create_server` reports `disabled_reason="load_failed"` —
        the exact mechanism the MCP degraded-hub clause names.
        """
        import contextlib

        fake_cross_encoder = MagicMock()
        fake_cross_encoder.enabled = False

        monkeypatch.chdir(tmp_path)
        with contextlib.ExitStack() as stack:
            captured_kwargs = self._patch_common(stack, self._mock_store())
            stack.enter_context(
                patch(
                    "pipecat_context_hub.shared.reranker.probe_reranker",
                    return_value=("cross-encoder/ms-marco-MiniLM-L-6-v2", None, None),
                )
            )
            stack.enter_context(
                patch(
                    "pipecat_context_hub.services.retrieval.cross_encoder.CrossEncoderReranker",
                    return_value=fake_cross_encoder,
                )
            )
            result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 0, result.output
        provider = cast(Callable[[], Any], captured_kwargs["reranker_status_provider"])
        status = provider()
        assert status.enabled is False
        assert status.disabled_reason == "load_failed"


class TestSafeHr:
    def test_utf8_returns_box_drawing(self, monkeypatch):
        fake_stdout = MagicMock()
        fake_stdout.encoding = "utf-8"
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(5) == "\u2500" * 5

    def test_cp1252_falls_back_to_ascii(self, monkeypatch):
        fake_stdout = MagicMock()
        fake_stdout.encoding = "cp1252"
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(4) == "----"

    def test_cp1254_falls_back_to_ascii(self, monkeypatch):
        fake_stdout = MagicMock()
        fake_stdout.encoding = "cp1254"
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(3) == "---"

    def test_cp437_keeps_box_drawing(self, monkeypatch):
        """cp437 is an OEM codepage that does include U+2500 — keep the glyph."""
        fake_stdout = MagicMock()
        fake_stdout.encoding = "cp437"
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(3) == "\u2500\u2500\u2500"

    def test_missing_encoding_falls_back_to_ascii(self, monkeypatch):
        fake_stdout = MagicMock(spec=[])  # no .encoding attribute
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(2) == "--"


class TestPrintRefreshSummaryEncoding:
    def test_does_not_raise_on_cp1254(self, monkeypatch):
        import io

        cp1254_stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1254", errors="strict", write_through=True
        )
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", cp1254_stdout)

        source_status: dict[str, dict[str, str | int]] = {
            "pipecat-ai/pipecat": {
                "status": "updated",
                "sha": "abcdef12",
                "existing": 100,
                "updated": 200,
            },
        }
        # Should not raise UnicodeEncodeError; _safe_hr falls back to ASCII.
        _print_refresh_summary(source_status, 200, 0, 1.2)
        cp1254_stdout.flush()
        raw = cp1254_stdout.buffer.getvalue().decode("cp1254")
        assert "\u2500" not in raw
        assert "-" * 8 in raw

    def test_does_not_raise_on_cp437_with_placeholder_rows(self, monkeypatch):
        """cp437 cannot encode U+2014 em dash — every placeholder must fall back."""
        import io

        cp437_stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp437", errors="strict", write_through=True
        )
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", cp437_stdout)

        # Exercise every placeholder code path: docs row (sha="—"),
        # skipped repo (updated="—"), error repo, and zero-existing repo.
        source_status: dict[str, dict[str, str | int]] = {
            "docs.pipecat.ai": {
                "status": "updated",
                "sha": "\u2014",
                "existing": 0,
                "updated": 500,
            },
            "pipecat-ai/pipecat": {
                "status": "skipped",
                "sha": "abcdef12",
                "existing": 1000,
                "updated": "\u2014",
            },
            "pipecat-ai/other": {
                "status": "error",
                "sha": "\u2014",
                "existing": 0,
                "updated": "\u2014",
            },
        }
        # No exception — and the em dash (which cp437 cannot encode) must
        # have been swapped for an ASCII placeholder on every row.
        _print_refresh_summary(source_status, 500, 1, 2.3)
        cp437_stdout.flush()
        raw = cp437_stdout.buffer.getvalue().decode("cp437")
        assert "\u2014" not in raw

    def test_non_encodable_sha_value_normalized(self, monkeypatch):
        """Any non-encodable cell value — not just the current em-dash
        sentinel — must be swapped for the ASCII placeholder. Guards
        against sentinel-drift silently re-introducing the crash."""
        import io

        cp437_stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp437", errors="strict", write_through=True
        )
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", cp437_stdout)

        # U+2026 (ellipsis) is not encodable in cp437 either; use it as a
        # stand-in for any future sentinel drift.
        source_status: dict[str, dict[str, str | int]] = {
            "some-source": {
                "status": "updated",
                "sha": "\u2026",
                "existing": 10,
                "updated": 20,
            },
        }
        _print_refresh_summary(source_status, 20, 0, 1.0)
        cp437_stdout.flush()
        raw = cp437_stdout.buffer.getvalue().decode("cp437")
        assert "\u2026" not in raw

    def test_recovered_repos_surfaced_in_summary(self, capsys):
        source_status: dict[str, dict[str, str | int]] = {
            "pipecat-ai/pipecat": {
                "status": "updated",
                "sha": "abcdef12",
                "existing": 0,
                "updated": 5,
            },
        }
        _print_refresh_summary(
            source_status,
            5,
            0,
            1.0,
            recovered_repos=["pipecat-ai/pipecat"],
        )
        out = capsys.readouterr().out
        assert "Recovered 1 corrupt clone(s)" in out
        assert "pipecat-ai/pipecat" in out


class TestWarmupEnabled:
    """PIPECAT_HUB_WARMUP env var parsing."""

    @pytest.mark.parametrize("value", ["0", "false", "False", "FALSE", "no", "No", "NO"])
    def test_disabled_values(self, value: str) -> None:
        assert _warmup_enabled({"PIPECAT_HUB_WARMUP": value}) is False

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on", "garbage", ""])
    def test_enabled_by_default(self, value: str) -> None:
        # Any value other than the disabled set enables pre-warm — empty
        # string and garbage both fall through to the "warm" default.
        assert _warmup_enabled({"PIPECAT_HUB_WARMUP": value}) is True

    def test_unset_enables(self) -> None:
        assert _warmup_enabled({}) is True

    def test_whitespace_tolerated(self) -> None:
        assert _warmup_enabled({"PIPECAT_HUB_WARMUP": "  0  "}) is False
        assert _warmup_enabled({"PIPECAT_HUB_WARMUP": "  false  "}) is False


class TestPrewarmModels:
    """Unit tests for `_prewarm_models` — covers success, failure, skip,
    and cross-encoder-absent paths. Uses MagicMock for the embedding
    service and cross-encoder so no real model load happens."""

    def test_warmup_disabled_skips(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("PIPECAT_HUB_WARMUP", "0")
        embed = MagicMock()
        ce = MagicMock()
        with caplog.at_level("INFO", logger="pipecat_context_hub.cli"):
            _prewarm_models(embed, ce)
        embed.embed_query.assert_not_called()
        ce.ensure_model.assert_not_called()
        assert any("pre-warm skipped" in r.message for r in caplog.records)

    def test_warmup_runs_both_when_enabled(self, monkeypatch, caplog) -> None:
        monkeypatch.delenv("PIPECAT_HUB_WARMUP", raising=False)
        embed = MagicMock()
        ce = MagicMock()
        with caplog.at_level("INFO", logger="pipecat_context_hub.cli"):
            _prewarm_models(embed, ce)
        embed.embed_query.assert_called_once_with("warmup")
        ce.ensure_model.assert_called_once_with()
        messages = [r.message for r in caplog.records]
        assert any("Embedding model pre-warmed" in m for m in messages)
        assert any("Cross-encoder pre-warmed" in m for m in messages)

    def test_no_cross_encoder_skips_ce_silently(self, monkeypatch, caplog) -> None:
        monkeypatch.delenv("PIPECAT_HUB_WARMUP", raising=False)
        embed = MagicMock()
        with caplog.at_level("INFO", logger="pipecat_context_hub.cli"):
            _prewarm_models(embed, None)
        embed.embed_query.assert_called_once()
        messages = [r.message for r in caplog.records]
        assert any("Embedding model pre-warmed" in m for m in messages)
        assert not any("Cross-encoder" in m for m in messages)

    def test_embedding_failure_is_non_fatal(self, monkeypatch, caplog) -> None:
        """Embedding pre-warm failure logs and proceeds to cross-encoder —
        the lazy-load path in production will handle first-query loads."""
        monkeypatch.delenv("PIPECAT_HUB_WARMUP", raising=False)
        embed = MagicMock()
        embed.embed_query.side_effect = RuntimeError("cold-start boom")
        ce = MagicMock()
        with caplog.at_level("ERROR", logger="pipecat_context_hub.cli"):
            _prewarm_models(embed, ce)  # must not raise
        ce.ensure_model.assert_called_once_with()
        assert any("Embedding model pre-warm failed" in r.message for r in caplog.records)

    def test_cross_encoder_failure_is_non_fatal(self, monkeypatch, caplog) -> None:
        monkeypatch.delenv("PIPECAT_HUB_WARMUP", raising=False)
        embed = MagicMock()
        ce = MagicMock()
        ce.ensure_model.side_effect = RuntimeError("weights missing")
        with caplog.at_level("ERROR", logger="pipecat_context_hub.cli"):
            _prewarm_models(embed, ce)  # must not raise
        assert any("Cross-encoder pre-warm failed" in r.message for r in caplog.records)
