"""Unit tests for CLI helpers."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from pipecat_context_hub import cli as cli_module
from pipecat_context_hub.cli import (
    _OPT_IN_ENABLED_VALUES,
    _debug_probe_enabled,
    _delete_local_index_storage,
    _log_serve_cwd,
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
from pipecat_context_hub.services.ingest.github_ingest import CloneResult
from pipecat_context_hub.shared import env_loading
from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.env_loading import load_global_config


class TestWriteServeDebugProbe:
    """`_write_serve_debug_probe()` must never crash `serve` on failure —
    including when `Path.home()` itself raises (RuntimeError, not OSError),
    a gap the original `except OSError` clause missed."""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="the probe write is O_NOFOLLOW-gated (see cli.py's "
        "_write_serve_debug_probe) and os.O_NOFOLLOW does not exist on "
        "Windows, so the function deliberately skips writing there — see "
        "test_probe_skipped_without_o_nofollow_on_windows for that path.",
    )
    def test_writes_probe_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        _write_serve_debug_probe()
        probe_path = tmp_path / ".cache" / "pipecat-context-hub" / "serve-debug.log"
        assert probe_path.is_file()
        logger.info.assert_called_once()

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="pins the deliberate Windows-only skip path (no O_NOFOLLOW); "
        "see test_writes_probe_file for the POSIX behavior this platform "
        "doesn't share.",
    )
    def test_probe_skipped_without_o_nofollow_on_windows(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        _write_serve_debug_probe()  # must not raise
        probe_path = tmp_path / ".cache" / "pipecat-context-hub" / "serve-debug.log"
        assert not probe_path.exists()
        logger.warning.assert_called_once()

    def test_path_home_runtime_error_is_swallowed(self, monkeypatch):
        """Path.home() raises RuntimeError (not OSError) when the home
        directory can't be determined — the probe must log and swallow this,
        not propagate it and crash serve."""

        def _raise_no_home():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "home", _raise_no_home)
        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        _write_serve_debug_probe()  # must not raise
        logger.warning.assert_called_once()

    def test_mkdir_oserror_is_swallowed(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            Path,
            "mkdir",
            MagicMock(side_effect=OSError("permission denied")),
        )
        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        _write_serve_debug_probe()  # must not raise
        logger.warning.assert_called_once()

    def test_failure_log_does_not_leak_the_unredacted_home_path(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Round-9 finding #3: the failure path used `logger.exception`, whose
        traceback carried the absolute `~/.cache/pipecat-context-hub/…` path —
        bypassing the redaction the success path and `_log_serve_cwd` both
        apply, in exactly the stderr lines operators are asked to paste into
        bug reports. The failure must be reported without the raw home path
        (and therefore without the OS username) anywhere in the record.
        """
        home = tmp_path / "home-dir"
        (home / ".cache" / "pipecat-context-hub").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        # OSError's __str__ appends the filename it failed on — the same shape
        # of leak the traceback had, so this also pins the message redaction.
        monkeypatch.setattr(
            os,
            "open",
            MagicMock(
                side_effect=OSError(
                    13,
                    "Permission denied",
                    str(home / ".cache" / "pipecat-context-hub" / "serve-debug.log"),
                )
            ),
        )
        with caplog.at_level("WARNING"):
            _write_serve_debug_probe()  # must not raise
        assert caplog.records, "the failure must still be reported"
        rendered = "\n".join(
            r.getMessage() + ("\n" + str(r.exc_info) if r.exc_info else "") for r in caplog.records
        )
        assert str(home) not in rendered
        assert "~" in rendered


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
        # A real dict, not the auto-created MagicMock: `refresh` compares
        # per-repo record counts numerically (`pre_counts.get(slug, 0) > 0`
        # gates both the prune-skip warning and the unchanged-SHA shortcut),
        # and a MagicMock has no ordering against int.
        #
        # Populated for every configured repo because an indexed repo is the
        # baseline these tests assume: as of round-10 finding #2 the
        # unchanged-SHA skip also requires records to exist, so an empty dict
        # would mean "SHA matches but the index is empty", forcing a re-ingest
        # in every test that only meant to say "nothing changed".
        mock_index_store.get_counts_by_repo = MagicMock(
            return_value={r: 13 for r in _DEFAULT_REPOS}
        )
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
        mock_github.clone_or_fetch = MagicMock(
            return_value=CloneResult(Path("/tmp/repo"), "abc123", None)
        )
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
    def test_matching_sha_with_no_indexed_records_re_ingests(
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
        """Round-10 finding #2: the unchanged-SHA shortcut trusted the stored
        ``repo:<slug>:commit_sha`` key as proof that the repo's records are in
        the index. They can diverge — a repo left unconfigured for a run keeps
        its SHA key while its records are gone (or were never written), and an
        interrupted delete can strip records without touching metadata. Taking
        the shortcut then leaves the repo silently absent from the index until
        someone runs ``--force``. Skipping now requires records to actually
        exist.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        meta = _sha_metadata("abc123")
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))
        # Every configured repo's SHA matches, but one of them has no indexed
        # records at all.
        counts = {r: 11 for r in _DEFAULT_REPOS}
        counts[_DEFAULT_REPOS[0]] = 0
        mock_store.get_counts_by_repo = MagicMock(return_value=counts)

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        with caplog.at_level("WARNING", logger="pipecat_context_hub.cli"):
            result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        # Only the record-less repo is re-ingested; the rest still skip.
        assert mock_github.ingest.call_count == 1
        deleted_repos = [call.args[0] for call in mock_store.delete_by_repo.call_args_list]
        assert deleted_repos == [_DEFAULT_REPOS[0]]
        assert "no indexed records" in caplog.text

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
            CloneResult(
                Path(f"/tmp/{repo_slug.replace('/', '_')}"),
                "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
                tag,
            )
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
            CloneResult(
                Path(f"/tmp/{repo_slug.replace('/', '_')}"),
                "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
                tag,
            )
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
    def test_tainted_ref_delete_failure_preserves_retry_metadata(
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
        """Round 6 Gate 1/Codex Finding #1 (High): this inline tainted-ref
        cleanup site (indexed ref is *also* tainted -> purge local records) is
        a third `delete_by_repo` call site distinct from
        `_delete_repo_index_data`. Before the fix, a failed
        `index_store.delete_by_repo` here was caught and logged but execution
        fell through unconditionally to the metadata deletes anyway -- wiping
        `stored_sha_key` and (for the framework repo) both provenance keys
        even though the purge never happened. That leaves a future refresh
        with no bookkeeping trail that this repo's stale/tainted records may
        still be sitting in the index. The fix mirrors
        `_delete_repo_index_data`: metadata deletes only run once
        `delete_by_repo` actually succeeds.

        Round 7 Gate 1/Codex + Architecture Finding #1 (High + Minor,
        converged): this site now calls `_delete_repo_index_data` directly
        instead of duplicating its try/except/else logic, and (like the
        helper's other two call sites) appends a message to `all_errors` on
        failure. Before that fix a failed purge here left
        `last_refresh_error_count=0` even though FTS-side stale tainted
        records could still be retrievable -- this test also asserts that
        failure is now surfaced through the persisted refresh-summary
        metadata batch.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_github.clone_or_fetch.side_effect = lambda repo_slug, _checkout=False, tag=None: (
            CloneResult(
                Path(f"/tmp/{repo_slug.replace('/', '_')}"),
                "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
                tag,
            )
        )
        mock_ref_tainted.side_effect = lambda _repo_path, sha, _refs: sha == "badcafe"

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {
            "docs:content_hash": content_hash,
            **_sha_metadata("abc123"),
            "indexed_framework_version": "1.6.0",
            "indexed_framework_commits_ahead": "0",
        }
        # Both the upstream HEAD and the cached SHA are tainted, so the
        # "indexed ref is also tainted" branch is taken.
        meta["repo:pipecat-ai/pipecat:commit_sha"] = "badcafe"
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))
        mock_store.delete_by_repo = AsyncMock(side_effect=RuntimeError("boom"))

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REFS", "pipecat-ai/pipecat@badcafe")
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        assert result.exception is None
        mock_store.delete_by_repo.assert_any_call("pipecat-ai/pipecat")
        # The failed delete must NOT be followed by metadata deletion --
        # stored_sha_key and both framework provenance keys survive so a
        # future refresh can still discover and retry this repo.
        deleted_keys = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        assert "repo:pipecat-ai/pipecat:commit_sha" not in deleted_keys
        assert "indexed_framework_version" not in deleted_keys
        assert "indexed_framework_commits_ahead" not in deleted_keys
        # The failure is now visible via the refresh summary's error count,
        # not just a log line -- mirroring the other two
        # `_delete_repo_index_data` call sites.
        batch_calls = mock_store.set_metadata_batch.call_args_list
        assert batch_calls, "expected set_metadata_batch to be called"
        metadata_to_set = batch_calls[-1].args[0]
        assert metadata_to_set["last_refresh_error_count"] != "0"
        assert "last_refresh_errored_at" in metadata_to_set
        # Round 9 Finding #3: a failed cleanup also flags the repo so a
        # future unchanged-SHA refresh can't trust `indexed_records > 0`
        # alone (stale FTS rows from the partial delete would otherwise
        # satisfy that check even though the vector store is now empty).
        set_calls = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        assert set_calls.get("repo:pipecat-ai/pipecat:cleanup_failed") == "1"

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_cleanup_failed_flag_forces_reingest_and_is_cleared_on_success(
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
        """Round 9 Finding #3 regression: a repo whose stale cleanup
        previously failed (leaving `repo:<slug>:cleanup_failed` set) must NOT
        take the unchanged-SHA skip shortcut on a subsequent refresh, even
        though `stored_sha == commit_sha` and `indexed_records > 0` (stale
        FTS rows). It must be re-ingested, and once that re-ingest succeeds
        cleanly, the flag must be cleared so future runs can trust the
        shortcut again.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        target_repo = "pipecat-ai/pipecat-flows"

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {
            "docs:content_hash": content_hash,
            **_sha_metadata("abc123"),
            f"repo:{target_repo}:cleanup_failed": "1",
        }
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        ingested_repo_calls: list[str] = []

        def _ingest_side_effect(*, repos, **_kwargs):
            ingested_repo_calls.extend(repos)
            return MagicMock(records_upserted=20, errors=[])

        mock_github.ingest = AsyncMock(side_effect=_ingest_side_effect)
        mock_github.clone_or_fetch.side_effect = lambda repo_slug, _checkout=False, tag=None: (
            CloneResult(Path(f"/tmp/{repo_slug.replace('/', '_')}"), "abc123", tag)
        )

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        # SHA is unchanged and records "exist" (per get_counts_by_repo), but
        # the cleanup_failed flag must force a real re-ingest, not a skip.
        assert target_repo in ingested_repo_calls

        # A clean re-ingest (no errors) must clear the flag.
        deleted_keys = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        assert f"repo:{target_repo}:cleanup_failed" in deleted_keys

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
            CloneResult(
                Path(f"/tmp/{repo_slug.replace('/', '_')}"),
                "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
                tag,
            )
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
        try:
            symlink_config_path.symlink_to(real_config_file)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

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

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_reset_index_refuses_data_dir_containing_active_dotenv(
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
        """The project `.env` is a config source too, and `--reset-index` must
        not delete it either. Exact Codex round-9 scenario: a project `.env`
        sets `PIPECAT_HUB_DATA_DIR=.`, so the data dir *is* the cwd holding
        that `.env` — and, before this guard, `shutil.rmtree` took out the
        whole project tree along with the config file that pointed at it.
        `config_collides_with_dir` never fired here because the machine-global
        `config.toml` lives elsewhere entirely.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        dotenv_path = project_dir / ".env"
        dotenv_path.write_text("PIPECAT_HUB_DATA_DIR=.\n")
        sentinel_file = project_dir / "bot.py"
        sentinel_file.write_text("# the operator's actual work")

        monkeypatch.chdir(project_dir)

        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--reset-index"])

        assert result.exit_code != 0
        assert "Refusing to delete" in result.output
        assert dotenv_path.exists()
        assert sentinel_file.exists()
        mock_is_cls.assert_not_called()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_reset_index_refuses_symlinked_dotenv_inside_data_dir(
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
        """The `.env` guard gets the same resolution-chain treatment the
        `config.toml` guard does: a cwd `.env` that is a symlink *into* the
        data dir has both walked endpoints outside it (the link lives in the
        project, its target lives elsewhere) yet is destroyed by the delete.
        A resolved-target-only check would be a false negative here.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        data_dir = tmp_path / "data-home"
        data_dir.mkdir()
        real_dotenv = data_dir / "shared.env"
        real_dotenv.write_text("PIPECAT_HUB_STALE_AFTER_DAYS=45\n")

        dotenv_link = project_dir / ".env"
        try:
            dotenv_link.symlink_to(real_dotenv)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
        monkeypatch.chdir(project_dir)

        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--reset-index"])

        assert result.exit_code != 0
        assert "Refusing to delete" in result.output
        assert real_dotenv.exists()
        mock_is_cls.assert_not_called()


class TestRefreshFtsFaultInjection:
    """`IndexStore`'s FTS-side exceptions are no longer swallowed (Round 5
    Finding #2): `upsert`/`delete_by_repo`/`delete_by_content_type`/
    `delete_by_source` now re-raise after logging. `refresh` must not crash
    when that happens — the three previously-unguarded `delete_by_repo` call
    sites need to catch and handle it.

    The store-level `upsert` fault-injection regression test (proving
    `IndexWriter.upsert` failures now surface as `IngestResult.errors`
    instead of a silently-swallowed vector/FTS divergence) lives in
    `test_github_ingest.py::TestGitHubRepoIngester::
    test_ingest_upsert_failure_is_reported_as_error`, using the real
    `IndexStore` with its FTS index fault-injected — that is the seam where
    the `store.py` fix (Part A) and the pre-existing `github_ingest.py`
    error handling (Part B, unchanged) actually meet. Once
    `IngestResult.errors` is non-empty, `refresh`'s per-repo
    `repo_has_errors`/purge/`ingested_repos` handling is pre-existing Round 4
    logic (see `TestRefreshRecordReplacementGate::
    test_partial_error_ingest_purges_records_not_just_metadata`) and is not
    re-tested here.
    """

    @pytest.fixture(autouse=True)
    def _mock_deprecation_map(self):
        """Avoid touching the registry/filesystem during refresh tests."""
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map.build_deprecation_map_from_registry",
            return_value=MagicMock(entries={}, save=MagicMock()),
        ):
            yield

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_fts_delete_by_repo_failure_during_pre_ingest_delete_is_handled(
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
        """`IndexStore.delete_by_repo` now re-raises an FTS-side failure. The
        pre-ingest delete inside the `changed_repos` loop must catch it: the
        refresh completes without an uncaught exception, the failing repo is
        skipped for the rest of that iteration (never reaches `github.ingest`
        for it), is recorded as errored, and the error is reflected in the
        persisted `last_refresh_error_count`.
        """
        target_repo = "pipecat-ai/pipecat-flows"
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        async def _delete_by_repo_side_effect(repo):
            if repo == target_repo:
                raise RuntimeError("FTS delete_by_repo failed; indexes may have diverged")
            return 0

        mock_store.delete_by_repo = AsyncMock(side_effect=_delete_by_repo_side_effect)

        ingested_repo_calls: list[str] = []

        def _ingest_side_effect(*, repos, **_kwargs):
            ingested_repo_calls.extend(repos)
            return MagicMock(records_upserted=20, errors=[])

        mock_github.ingest = AsyncMock(side_effect=_ingest_side_effect)

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["refresh"])

        # No uncaught exception propagated out of the CLI invocation.
        assert result.exit_code == 0, result.output
        assert target_repo not in ingested_repo_calls
        assert target_repo in result.output
        assert "error" in result.output.lower()

        written = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])
        assert int(written["last_refresh_error_count"]) >= 1

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_fts_delete_by_content_type_failure_during_docs_delete_is_handled(
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
        """`IndexStore.delete_by_content_type` now re-raises an FTS-side
        failure too. The docs-ingest branch (the stale-docs delete before
        re-crawling) must catch it: the refresh completes without an
        uncaught exception, docs re-ingest is skipped this run, and the
        error is recorded and reflected in the persisted
        `last_refresh_error_count` — mirroring the pre-ingest-delete
        handling above, for the one other previously-unguarded call site
        the round-5 verifier's blast-radius sweep found.
        """
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        mock_store.delete_by_content_type = AsyncMock(
            side_effect=RuntimeError("FTS delete_by_content_type failed; indexes may have diverged")
        )

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["refresh"])

        # No uncaught exception propagated out of the CLI invocation.
        assert result.exit_code == 0, result.output
        mock_crawler.ingest.assert_not_called()
        assert "error" in result.output.lower()

        written = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])
        assert int(written["last_refresh_error_count"]) >= 1

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_docs_content_hash_clear_failure_does_not_crash_refresh(
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
        """Round 9 Finding #4 regression: round 8's own recovery call —
        `index_store.delete_metadata("docs:content_hash")`, added inside the
        `delete_by_content_type` failure handler to guarantee future
        retries — is itself unprotected. If the SAME underlying storage
        fault also breaks `delete_metadata` (e.g. a disk/lock issue
        affecting the whole SQLite file), the exception must not propagate
        unhandled and abort the whole refresh; it must be recorded in
        `all_errors` and refresh must complete.
        """
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        mock_store.delete_by_content_type = AsyncMock(
            side_effect=RuntimeError("FTS delete_by_content_type failed; indexes may have diverged")
        )

        def _delete_metadata_side_effect(key):
            if key == "docs:content_hash":
                raise RuntimeError("sqlite database is locked")

        mock_store.delete_metadata = MagicMock(side_effect=_delete_metadata_side_effect)

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["refresh"])

        # No uncaught exception propagated out of the CLI invocation, even
        # though BOTH the primary delete and the stamp-clearing recovery
        # call failed.
        assert result.exit_code == 0, result.output
        assert result.exception is None
        mock_crawler.ingest.assert_not_called()

        written = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])
        # Both failures are recorded — the error count reflects more than
        # just the primary `delete_by_content_type` failure.
        assert int(written["last_refresh_error_count"]) >= 2

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_docs_content_hash_cleared_after_delete_failure_and_next_refresh_retries(
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
        """Round-8 gauntlet Finding #1 regression.

        When `delete_by_content_type("doc")` raises, the handler must clear
        `docs:content_hash` (not merely record the error) — otherwise the
        stale stamp still matches the (never re-fetched) content hash, and a
        *subsequent non-force* refresh takes the "docs unchanged, skipping"
        shortcut forever, permanently suppressing automatic recovery.

        Reproduces the real scenario: `docs:content_hash` is already stamped
        (matching the crawler's current content — a prior refresh succeeded),
        so a plain non-force refresh would take the unchanged-skip branch
        without even reaching the delete. `--force` is what gets a refresh
        past that skip and into the failing delete, exactly as it would for
        an operator retrying after an earlier failure. Uses a stateful fake
        metadata store (a plain dict wired through get/set/delete_metadata)
        so the second `invoke()` in this test actually observes what the
        first one left behind.

        1. After the failed forced delete, `docs:content_hash` is absent —
           not left stamped at its old (now-stale) value.
        2. A second, non-force refresh (where the delete now succeeds) does
           NOT take the unchanged-skip branch — `delete_by_content_type` and
           `crawler.ingest` are invoked again.
        """
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        import hashlib

        # Matches mock_crawler.fetch_llms_txt's fixed return value from
        # `_make_mocks` — a prior successful refresh already stamped this.
        content_hash = hashlib.sha256(
            b"# Page\nSource: https://example.com\nContent here"
        ).hexdigest()

        # Stateful fake metadata store so get_metadata reflects prior
        # set_metadata/delete_metadata calls across the two invocations below.
        meta: dict[str, str] = {"docs:content_hash": content_hash}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))
        mock_store.set_metadata = MagicMock(
            side_effect=lambda key, value: meta.__setitem__(key, value)
        )
        mock_store.delete_metadata = MagicMock(side_effect=lambda key: meta.pop(key, None))

        mock_store.delete_by_content_type = AsyncMock(
            side_effect=RuntimeError("FTS delete_by_content_type failed; indexes may have diverged")
        )

        monkeypatch.chdir(tmp_path)

        # --- First refresh (--force, so the delete is reached despite the
        # matching hash): the delete fails. ---
        result = CliRunner().invoke(main, ["refresh", "--force"])
        assert result.exit_code == 0, result.output
        mock_crawler.ingest.assert_not_called()
        assert "docs:content_hash" not in meta, (
            "docs:content_hash must be cleared, not left stamped, after a "
            "failed delete_by_content_type — otherwise a future refresh's "
            "unchanged-hash check silently skips re-ingesting docs forever"
        )

        # --- Second, non-force refresh: the delete now succeeds. ---
        mock_store.delete_by_content_type = AsyncMock(return_value=0)
        mock_crawler.ingest.reset_mock()

        result2 = CliRunner().invoke(main, ["refresh"])
        assert result2.exit_code == 0, result2.output
        mock_store.delete_by_content_type.assert_awaited_once_with("doc")
        mock_crawler.ingest.assert_awaited_once()
        assert meta.get("docs:content_hash") == content_hash


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
            CloneResult(
                Path(f"/tmp/{repo_slug.replace('/', '_')}"),
                "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
                tag,
            )
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
            CloneResult(
                Path(f"/tmp/{repo_slug.replace('/', '_')}"),
                "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
                tag,
            )
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

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_latest_pin_normalized_and_stamped_from_resolved_tag(
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
        """`--framework-version` with mixed case/whitespace ('  LATEST  ') is
        persisted as the canonical 'latest' sentinel, and once the framework
        repo is cloned, `indexed_framework_version` is stamped from the tag
        `clone_or_fetch` resolved and verified — not from a `git describe`-derived
        value, and not from a second resolution run afterwards — with
        `indexed_framework_commits_ahead` fixed at "0".
        """
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False
        mock_github.clone_or_fetch = MagicMock(
            side_effect=lambda repo_slug, _checkout=False, tag=None: CloneResult(
                Path("/tmp/repo"), "abc123", "v1.10.0" if tag else None
            )
        )

        monkeypatch.chdir(tmp_path)
        # `describe_framework_checkout` must NOT be consulted for the stamp
        # when a `latest`-resolved tag is available; give it a distinct value
        # so a regression back to `git describe` would be caught.
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("9.9.9", 42),
        ):
            result = CliRunner().invoke(main, ["refresh", "--framework-version", "  LATEST  "])

        assert result.exit_code == 0, result.output
        written = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])

        assert written["framework_version"] == "latest"
        assert written["indexed_framework_version"] == "1.10.0"
        assert written["indexed_framework_commits_ahead"] == "0"
        framework_calls = [
            call
            for call in mock_github.ingest.call_args_list
            if call.kwargs.get("repos") == ["pipecat-ai/pipecat"]
        ]
        # Already normalised to a release version by `exact_release_version`, so
        # the chunk pin and the metadata stamp are literally the same value.
        assert framework_calls[0].kwargs["framework_checkout_version"] == "1.10.0"

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_non_release_pin_falls_back_to_describe_for_stamp_and_chunk_pin(
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
        """Git — and this tool's tag-input validation — accept a branch-shaped tag
        like ``some-feature-tag``. It names no release, so it must not be stamped
        verbatim as `indexed_framework_version` with `commits_ahead="0"`: that
        publishes a non-version against a contract that promises one, and breaks
        every downstream `Version()` comparison (including `check_deprecation`).

        Both consumers of the pin — the metadata stamp and the per-chunk
        `pipecat_version_pin` — fall back to the unpinned paths instead.
        """
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False
        mock_github.clone_or_fetch = MagicMock(
            side_effect=lambda repo_slug, _checkout=False, tag=None: CloneResult(
                Path("/tmp/repo"), "abc123", tag
            )
        )

        monkeypatch.chdir(tmp_path)
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("1.6.0", 42),
        ) as mock_describe:
            result = CliRunner().invoke(
                main, ["refresh", "--framework-version", "some-feature-tag"]
            )

        assert result.exit_code == 0, result.output
        mock_describe.assert_called_once()

        written = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])

        # The operator's pin is still recorded verbatim…
        assert written["framework_version"] == "some-feature-tag"
        # …but provenance comes from describe's floor, not from the tag.
        assert written["indexed_framework_version"] == "1.6.0"
        assert written["indexed_framework_commits_ahead"] == "42"

        framework_calls = [
            call
            for call in mock_github.ingest.call_args_list
            if call.kwargs.get("repos") == ["pipecat-ai/pipecat"]
        ]
        # None routes _ingest_repo to its existing unpinned version extraction.
        assert framework_calls[0].kwargs["framework_checkout_version"] is None

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_undeterminable_provenance_clears_stale_stamp(
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
        """`framework_provenance_ready` is True (the framework repo's records
        were replaced this run) but *neither* producer can derive a version:
        `exact_release_version` rejects the checked-out tag, and
        `describe_framework_checkout` also returns ``(None, None)`` (a
        branch-shaped checkout with no reachable release tag at all).

        The prior run's exact-provenance stamp is now stale — it describes a
        revision the index no longer holds — and must be explicitly deleted,
        not silently left in place alongside fresh, unrelated records.
        """
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False
        mock_github.clone_or_fetch = MagicMock(
            side_effect=lambda repo_slug, _checkout=False, tag=None: CloneResult(
                Path("/tmp/repo"), "abc123", tag
            )
        )

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        meta["repo:pipecat-ai/pipecat:commit_sha"] = "old-sha"
        # Seed a prior known-good stamp that must be cleared once the framework
        # repo's records are replaced by an undeterminable checkout.
        meta["indexed_framework_version"] = "1.5.0"
        meta["indexed_framework_commits_ahead"] = "3"
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=(None, None),
        ) as mock_describe:
            result = CliRunner().invoke(
                main, ["refresh", "--framework-version", "some-feature-tag"]
            )

        assert result.exit_code == 0, result.output
        mock_describe.assert_called_once()

        written = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])
        assert "indexed_framework_version" not in written
        assert "indexed_framework_commits_ahead" not in written

        deleted = {call.args[0] for call in mock_store.delete_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            deleted.update(batch_call.kwargs.get("delete_keys") or ())
        assert "indexed_framework_version" in deleted
        assert "indexed_framework_commits_ahead" in deleted

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_failed_framework_checkout_is_not_stamped(
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
        tainted) but its checkout fails — so it never reaches `delete_by_repo`
        and its indexed records still describe the old revision. Its version
        must not be stamped, leaving the prior known-good stamp untouched
        rather than describing a revision the index does not hold.

        The discriminator is record *replacement*, not error-free ingest: an
        ingest that errored *after* the delete has still replaced the records,
        and is covered by `TestRefreshRecordReplacementGate`.
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

        # Only the framework repo is in `changed_repos`; its checkout fails.
        mock_github.checkout_commit = MagicMock(side_effect=RuntimeError("checkout failed"))

        monkeypatch.chdir(tmp_path)
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("1.6.0", 55),
        ) as mock_describe:
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        # The framework's records were never deleted, so nothing about it is
        # restamped.
        mock_store.delete_by_repo.assert_not_awaited()
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

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_failed_latest_checkout_keeps_map_and_provenance_aligned(
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
        """A `latest` checkout that failed before `delete_by_repo` must leave the
        prior map and the prior stamp together, both describing the old revision
        the index still holds."""
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        framework_slug = "pipecat-ai/pipecat"

        def _clone_side_effect(repo_slug, _checkout=False, tag=None):
            sha = "new-framework-sha" if repo_slug == framework_slug else "abc123"
            return CloneResult(Path(f"/tmp/{repo_slug.replace('/', '_')}"), sha, tag)

        mock_github.clone_or_fetch.side_effect = _clone_side_effect

        import hashlib

        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        meta[f"repo:{framework_slug}:commit_sha"] = "old-framework-sha"
        meta["indexed_framework_version"] = "1.5.0"
        meta["indexed_framework_commits_ahead"] = "0"
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        def _checkout_side_effect(repo_path, _commit_sha):
            if repo_path.name == framework_slug.replace("/", "_"):
                raise RuntimeError("latest checkout failed")

        mock_github.checkout_commit = MagicMock(side_effect=_checkout_side_effect)

        from pipecat_context_hub.services.ingest.deprecation_map import (
            DeprecationEntry,
            DeprecationMap,
        )

        data_dir = tmp_path / "hub-data"
        prior_map_path = data_dir / "deprecation_map.json"
        prior_map = DeprecationMap(
            entries={"OldSymbol": DeprecationEntry(old_path="OldSymbol")},
            pipecat_commit_sha="old-framework-sha",
        )
        prior_map.save(prior_map_path)
        prior_bytes = prior_map_path.read_bytes()

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map.build_deprecation_map_from_registry"
        ) as mock_build:
            result = CliRunner().invoke(main, ["refresh", "--framework-version", "latest"])

        assert result.exit_code == 0, result.output
        mock_build.assert_not_called()
        assert prior_map_path.read_bytes() == prior_bytes
        assert DeprecationMap.load(prior_map_path).pipecat_commit_sha == "old-framework-sha"

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


class TestRefreshRecordReplacementGate:
    """The deprecation-map rebuild and the provenance stamp follow *record
    replacement*, not error-free ingest.

    Once ``delete_by_repo`` has run for the framework repo, the index no longer
    holds the previous revision's records — so both the map and the stamp must
    describe the new checkout, whole or partial. A framework repo whose
    ``checkout_commit`` failed never reached the delete, so its records still
    describe the old revision and everything about it is left alone.

    These tests deliberately do **not** mock ``build_deprecation_map_from_registry``:
    the assertions are on the deprecation map's on-disk *content*, which is the
    only way to tell a rebuilt map from an untouched one.
    """

    FRAMEWORK_SLUG = "pipecat-ai/pipecat"

    def _write_registry(self, checkout: Path, subject: str) -> None:
        """Write a minimal real pipecat deprecation registry into *checkout*."""
        import json as _json

        from pipecat_context_hub.services.ingest.deprecation_map import REGISTRY_RELATIVE_PATH

        registry_path = checkout / REGISTRY_RELATIVE_PATH
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            _json.dumps(
                {
                    "deprecations": [
                        {
                            "subject": subject,
                            "module": "pipecat.services.example",
                            "kind": "class",
                            "deprecated_in": "1.6.0",
                            "message": f"{subject} is deprecated",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def _prior_map(self, data_dir: Path) -> Path:
        """Persist a deprecation map describing the *previous* framework revision."""
        from pipecat_context_hub.services.ingest.deprecation_map import (
            DeprecationEntry,
            DeprecationMap,
        )

        path = data_dir / "deprecation_map.json"
        DeprecationMap(
            entries={"OldOnlySymbol": DeprecationEntry(old_path="OldOnlySymbol")},
            pipecat_commit_sha="old-framework-sha",
        ).save(path)
        return path

    def _harness(
        self,
        mocks,
        tmp_path,
        monkeypatch,
        *,
        framework_ingest_errors: list[str],
        framework_records_upserted: int = 5000,
    ):
        """Arrange a refresh where only the framework repo changed.

        Returns ``(mock_store, mock_github, framework_checkout, dep_map_path)``.
        """
        import hashlib

        mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted = mocks
        mock_store, mock_crawler, mock_github, mock_source = TestRefreshCommand._make_mocks(
            TestRefreshCommand()
        )
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        framework_checkout = tmp_path / "fw-checkout"
        framework_checkout.mkdir()
        self._write_registry(framework_checkout, "NewOnlySymbol")

        def _clone_side_effect(repo_slug, _checkout=False, tag=None):
            if repo_slug == self.FRAMEWORK_SLUG:
                return CloneResult(framework_checkout, "new-framework-sha", tag)
            return CloneResult(tmp_path / repo_slug.replace("/", "_"), "abc123", tag)

        mock_github.clone_or_fetch.side_effect = _clone_side_effect

        content = "# Page\nSource: https://example.com\nContent here"
        meta = {
            "docs:content_hash": hashlib.sha256(content.encode()).hexdigest(),
            **_sha_metadata("abc123"),
            f"repo:{self.FRAMEWORK_SLUG}:commit_sha": "old-framework-sha",
            "indexed_framework_version": "1.5.0",
            "indexed_framework_commits_ahead": "0",
        }
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        def _ingest_side_effect(*, repos, **_kwargs):
            if repos == [self.FRAMEWORK_SLUG]:
                return MagicMock(
                    records_upserted=framework_records_upserted,
                    errors=list(framework_ingest_errors),
                )
            return MagicMock(records_upserted=20, errors=[])

        mock_github.ingest = AsyncMock(side_effect=_ingest_side_effect)

        data_dir = tmp_path / "hub-data"
        data_dir.mkdir()
        dep_map_path = self._prior_map(data_dir)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
        return mock_store, mock_github, framework_checkout, dep_map_path

    @staticmethod
    def _written(mock_store) -> dict[str, str]:
        written = {call.args[0]: call.args[1] for call in mock_store.set_metadata.call_args_list}
        for batch_call in mock_store.set_metadata_batch.call_args_list:
            written.update(batch_call.args[0])
        return written

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_deprecation_map_preserved_when_framework_ingest_has_nonfatal_errors(
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
        """Regression (Round 3 Finding #1): ``delete_by_repo`` runs before ingest
        completes, so a partially-failed framework ingest (errors present, even
        with a substantial number of records written) must NOT be treated as a
        clean replacement. The provenance stamp and the deprecation map must stay
        exactly where they were — describing content the index can no longer
        promise is complete — rather than advancing to describe a revision whose
        ingest did not fully succeed.

        Before the fix, gating on record *replacement* (``delete_by_repo`` having
        run) rather than on error-free ingest (``not repo_has_errors``) advanced
        both the map and the stamp here even though ``framework_ingest_errors``
        is non-empty.
        """
        from pipecat_context_hub.services.ingest.deprecation_map import DeprecationMap

        mock_store, _mock_github, _checkout, dep_map_path = self._harness(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            tmp_path,
            monkeypatch,
            framework_ingest_errors=["Failed to read foo.py: invalid utf-8"],
        )
        prior_bytes = dep_map_path.read_bytes()

        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("1.6.0", 0),
        ):
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output

        assert dep_map_path.read_bytes() == prior_bytes
        preserved = DeprecationMap.load(dep_map_path)
        assert preserved.pipecat_commit_sha == "old-framework-sha"
        assert "OldOnlySymbol" in preserved.entries
        assert "NewOnlySymbol" not in preserved.entries

        written = self._written(mock_store)
        assert "indexed_framework_version" not in written
        assert "indexed_framework_commits_ahead" not in written

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_stamp_not_advanced_when_deprecation_map_save_fails(
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
        """Round 9 Finding #1 regression: ``dep_map.save()`` can fail (disk or
        permission error) even when the framework registry parses fine and
        ingest is error-free — a failure mode outside
        ``DeprecationRegistryError``'s coverage. Before the fix,
        ``indexed_framework_version``/``indexed_framework_commits_ahead`` were
        gated on ``framework_provenance_ready`` alone, independent of whether
        the deprecation map was actually published — so the stamp could
        advance to describe the new checkout while ``deprecation_map.json`` on
        disk still describes the old one. The stamp must now be gated on
        ``deprecation_map_published``, so the two always move together: both
        the on-disk map and the metadata stamp must stay exactly where they
        were.
        """
        from pipecat_context_hub.services.ingest.deprecation_map import DeprecationMap

        mock_store, _mock_github, _checkout, dep_map_path = self._harness(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            tmp_path,
            monkeypatch,
            framework_ingest_errors=[],
        )
        prior_bytes = dep_map_path.read_bytes()

        with (
            patch(
                "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
                return_value=("1.6.0", 0),
            ),
            patch.object(DeprecationMap, "save", side_effect=OSError("disk full")),
        ):
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output

        # The map on disk is byte-for-byte unchanged — save() never completed.
        assert dep_map_path.read_bytes() == prior_bytes
        preserved = DeprecationMap.load(dep_map_path)
        assert preserved.pipecat_commit_sha == "old-framework-sha"
        assert "OldOnlySymbol" in preserved.entries

        written = self._written(mock_store)
        assert "indexed_framework_version" not in written
        assert "indexed_framework_commits_ahead" not in written

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_zero_records_ingest_error_after_delete_is_not_stamped(
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
        """Regression (Round 3 Finding #1, the reported scenario exactly):
        ``github.ingest()`` returns errors AND ``records_upserted=0`` for the
        framework repo, after ``delete_by_repo`` has already removed the old
        records. Nothing new was written, so the provenance stamp must not
        advance and the deprecation map must not be rebuilt from the (now
        record-less) checkout.
        """
        from pipecat_context_hub.services.ingest.deprecation_map import DeprecationMap

        mock_store, _mock_github, _checkout, dep_map_path = self._harness(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            tmp_path,
            monkeypatch,
            framework_ingest_errors=["Failed to clone/read: network error"],
            framework_records_upserted=0,
        )
        prior_bytes = dep_map_path.read_bytes()

        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("1.6.0", 0),
        ):
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output

        assert dep_map_path.read_bytes() == prior_bytes
        preserved = DeprecationMap.load(dep_map_path)
        assert preserved.pipecat_commit_sha == "old-framework-sha"

        written = self._written(mock_store)
        assert "indexed_framework_version" not in written
        assert "indexed_framework_commits_ahead" not in written

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_partial_error_ingest_purges_records_not_just_metadata(
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
        """Regression (Round 4 Finding #1): a framework ingest that writes some
        records (``records_upserted>0``) but also errors (``repo_has_errors``)
        must not leave those partial *records* behind, even though the
        provenance *metadata* correctly withholds itself already (per Round 3).

        ``delete_by_repo`` runs once up front (stale-cleanup) and must run a
        SECOND time for the same repo once the error is detected, so the repo
        ends the run with zero records rather than a silent partial mix of
        old-deleted + new-partial records. Before the fix, ``delete_by_repo``
        was only called once per repo and the partial records from the failed
        ingest were left in the index.
        """
        mock_store, _mock_github, _checkout, _dep_map_path = self._harness(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            tmp_path,
            monkeypatch,
            framework_ingest_errors=["Failed to read foo.py: invalid utf-8"],
            framework_records_upserted=3500,
        )

        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("1.6.0", 0),
        ):
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output

        deleted_calls = [
            call.args[0]
            for call in mock_store.delete_by_repo.call_args_list
            if call.args[0] == self.FRAMEWORK_SLUG
        ]
        # Once for stale-cleanup before ingest, once more to purge the
        # partial records left by the errored ingest.
        assert deleted_calls == [self.FRAMEWORK_SLUG, self.FRAMEWORK_SLUG], (
            f"expected delete_by_repo({self.FRAMEWORK_SLUG!r}) to run twice "
            f"(pre-ingest cleanup + post-error purge), got: {deleted_calls}"
        )

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_zero_records_error_free_ingest_still_advances(
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
        """The legitimate empty-repo case must not regress from the Finding #1
        fix: a framework ingest that completes with zero errors but genuinely
        zero records (e.g. an empty repo state) is still a clean replacement —
        the stamp and the deprecation map must advance normally.
        """
        from pipecat_context_hub.services.ingest.deprecation_map import DeprecationMap

        mock_store, _mock_github, _checkout, dep_map_path = self._harness(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            tmp_path,
            monkeypatch,
            framework_ingest_errors=[],
            framework_records_upserted=0,
        )

        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("1.6.0", 0),
        ):
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output

        rebuilt = DeprecationMap.load(dep_map_path)
        assert rebuilt.pipecat_commit_sha == "new-framework-sha"
        assert "NewOnlySymbol" in rebuilt.entries
        assert "OldOnlySymbol" not in rebuilt.entries

        written = self._written(mock_store)
        assert written["indexed_framework_version"] == "1.6.0"
        assert written["indexed_framework_commits_ahead"] == "0"

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_deprecation_map_preserved_when_framework_checkout_fails(
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
        """The other half of the invariant: a framework repo whose checkout failed
        never reached ``delete_by_repo``, so its indexed records still describe the
        old revision — the map and the stamp must stay exactly where they were.

        Guards against over-loosening the gate to "the framework was cloned".
        """
        mock_store, mock_github, _checkout, dep_map_path = self._harness(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            tmp_path,
            monkeypatch,
            framework_ingest_errors=[],
        )
        prior_bytes = dep_map_path.read_bytes()

        def _checkout_side_effect(repo_path, _commit_sha):
            if repo_path.name == "fw-checkout":
                raise RuntimeError("checkout failed")

        mock_github.checkout_commit = MagicMock(side_effect=_checkout_side_effect)

        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
            return_value=("1.6.0", 0),
        ) as mock_describe:
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        assert dep_map_path.read_bytes() == prior_bytes
        deleted_repos = [call.args[0] for call in mock_store.delete_by_repo.call_args_list]
        assert self.FRAMEWORK_SLUG not in deleted_repos
        mock_describe.assert_not_called()

        written = self._written(mock_store)
        assert "indexed_framework_version" not in written
        assert "indexed_framework_commits_ahead" not in written

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_deprecation_map_rebuild_follows_error_free_ingest(
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
        """State the corrected coupling directly (Round 3 Finding #1): the map is
        rewritten **iff** the framework's ingest both ran (checkout succeeded)
        *and* completed error-free — not merely because ``delete_by_repo`` ran.
        Asserted across all four combinations of checkout failure x ingest
        errors, since record replacement alone (the pre-fix gate) is no longer
        sufficient.
        """
        from pipecat_context_hub.services.ingest.deprecation_map import DeprecationMap

        cases = [
            (False, []),
            (False, ["partial failure"]),
            (True, []),
            (True, ["partial failure"]),
        ]
        for fail_checkout, ingest_errors in cases:
            run_dir = tmp_path / f"run-{'fail' if fail_checkout else 'ok'}-{len(ingest_errors)}"
            run_dir.mkdir()
            mock_store, mock_github, _checkout, dep_map_path = self._harness(
                (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
                run_dir,
                monkeypatch,
                framework_ingest_errors=ingest_errors,
            )
            if fail_checkout:

                def _boom(repo_path, _commit_sha):
                    if repo_path.name == "fw-checkout":
                        raise RuntimeError("checkout failed")

                mock_github.checkout_commit = MagicMock(side_effect=_boom)

            with patch(
                "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
                return_value=("1.6.0", 0),
            ):
                result = CliRunner().invoke(main, ["refresh"])

            assert result.exit_code == 0, result.output
            rebuilt = DeprecationMap.load(dep_map_path).pipecat_commit_sha == "new-framework-sha"
            expected_rebuilt = not fail_checkout and not ingest_errors
            assert rebuilt is expected_rebuilt, (
                f"fail_checkout={fail_checkout} ingest_errors={ingest_errors}: "
                f"expected rebuilt={expected_rebuilt} but got {rebuilt}"
            )

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_corrupt_registry_preserves_existing_map_and_records_error(
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
        """Round-8 gauntlet Finding #2 regression.

        A registry that is *present but unreadable/corrupt* must not be
        published as an empty deprecation map — that would silently
        overwrite (and hide) a previously-good map. It must be distinguished
        from the legitimate "registry doesn't exist yet" case (covered by
        ``test_tainted_framework_does_not_rebuild_deprecation_map`` and
        friends, which still publish an empty map safely for that case).

        Deliberately does not mock ``build_deprecation_map_from_registry``,
        same rationale as the rest of this class: the assertion is on the
        deprecation map's on-disk *content*.
        """
        import logging

        mock_store, _mock_github, framework_checkout, dep_map_path = self._harness(
            (mock_si_cls, mock_gh_cls, mock_dc_cls, mock_is_cls, mock_ref_tainted),
            tmp_path,
            monkeypatch,
            framework_ingest_errors=[],
        )
        prior_bytes = dep_map_path.read_bytes()

        from pipecat_context_hub.services.ingest.deprecation_map import REGISTRY_RELATIVE_PATH

        registry_path = framework_checkout / REGISTRY_RELATIVE_PATH
        registry_path.write_text("not json", encoding="utf-8")

        with (
            patch(
                "pipecat_context_hub.services.ingest.github_ingest.describe_framework_checkout",
                return_value=("1.6.0", 0),
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = CliRunner().invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        assert dep_map_path.read_bytes() == prior_bytes, (
            "a corrupt/unreadable registry must never overwrite the "
            "previously published deprecation map with an empty one"
        )

        written = self._written(mock_store)
        assert int(written["last_refresh_error_count"]) >= 1
        assert "deprecation registry" in caplog.text.lower()
        assert "unreadable" in caplog.text.lower()


class TestPruneEnabledValues:
    """`_OPT_IN_ENABLED_VALUES` frozenset exactness and `_prune_enabled()`
    env-var parsing (dev plan docs/dev_plans/20260807-feature-global-config-toml.md,
    Phase 4). Polarity is inverted from `_warmup_enabled()`: `PIPECAT_HUB_PRUNE`
    defaults `False` (deletion is opt-in), so an *unrecognized* value must
    resolve to the safe default, not the enabling one.
    """

    def test_prune_enabled_values_is_value_exact(self) -> None:
        assert _OPT_IN_ENABLED_VALUES == frozenset(
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

    def _make_mocks(self):
        """As the parent's, but with real indexed-record counts.

        The prune-skip warning is gated on the repo actually having indexed
        records (round-4 finding #6: warning "leaving 0 indexed record(s) in
        place — pass --prune to remove" advertises a destructive flag whose
        only effect would be dropping a stale metadata key). These tests are
        about repos that *do* have data, so say so.
        """
        mocks = super()._make_mocks()
        mocks[0].get_counts_by_repo = MagicMock(
            return_value={
                **{r: 13 for r in _DEFAULT_REPOS},
                self._REMOVED_REPO: 7,
                "pipecat-ai/pipecat": 42,
            }
        )
        return mocks

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
        `_OPT_IN_ENABLED_VALUES` members, alone (no --prune flag), behaves
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
        # Repo B has real indexed data — that is the whole point of the
        # repro, and the prune-skip warning is gated on record count > 0.
        mock_store.get_counts_by_repo = MagicMock(return_value={repo_a: 11, repo_b: 13})

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

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_unconfigured_repo_with_zero_records_is_not_warned_about(
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
        """Round-4 finding #6: the counter/warning fired for any un-configured
        `repo:*:commit_sha` key regardless of whether that repo had indexed
        records. With none, the operator was told "leaving 0 indexed
        record(s) in place — pass --prune to remove", advertising a
        destructive flag whose only effect would be dropping a stale
        metadata key. Nothing is at risk, so nothing should be said.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))
        # Metadata key present, but no indexed records behind it.
        mock_store.get_counts_by_repo = MagicMock(return_value={})

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        with caplog.at_level("WARNING", logger="pipecat_context_hub.cli"):
            result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        # Still not deleted without --prune…
        deleted_repos = [call.args[0] for call in mock_store.delete_by_repo.call_args_list]
        assert self._REMOVED_REPO not in deleted_repos
        # …but no misleading notice about a flag that would remove nothing.
        assert self._REMOVED_REPO not in caplog.text
        assert "Skipped pruning" not in result.output

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_unconfigured_repo_with_zero_records_still_leaves_a_trace(
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
        """Round-6 finding #3. The zero-record case above was suppressed
        *entirely*, but `get_counts_by_repo()` reads SQLite/FTS only. On a
        divergent index — an interrupted delete leaving vector-only records —
        the count is 0 while records still surface through the hybrid
        retriever, and the operator saw nothing at all to explain them. The
        WARNING and the "Skipped pruning" line stay suppressed (nothing is
        provably at risk); an INFO line restores the diagnostic.
        """
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        all_meta = self._meta_with_removed_repo()
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        mock_store.get_metadata = MagicMock(side_effect=lambda key: all_meta.get(key))
        mock_store.get_counts_by_repo = MagicMock(return_value={})

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        with caplog.at_level("INFO", logger="pipecat_context_hub.cli"):
            result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        info_text = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "INFO")
        assert self._REMOVED_REPO in info_text
        assert "Skipped pruning" not in result.output

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
        """index_store.delete_by_repo raising during a --prune delete used to
        propagate and crash the whole refresh. Round 5's verifier flagged
        that as a real blast-radius gap once `IndexStore.delete_by_repo`
        started re-raising FTS-layer failures instead of swallowing them
        (Finding #2): `_delete_repo_index_data` — the cleanup-pass helper
        shared by both the tainted-repo and `--prune` deletion branches — now
        catches the failure and logs it, consistent with the other two
        best-effort cleanup-style `delete_by_repo` call sites (post-ingest-
        error purge, tainted-ref removal). This is a cleanup pass, not core
        ingestion: leaving a stale, already-unwanted repo's data in place for
        another run is strictly better than aborting an otherwise-successful
        refresh over it. The repo's metadata key is deliberately left in
        place on failure too (not deleted), so a future cleanup pass can
        still find and retry it — see `_delete_repo_index_data`'s docstring
        and the existing "orphan from future cleanup passes" comment above
        this loop."""
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

        # No uncaught exception propagates out of the CLI invocation.
        assert result.exit_code == 0, result.output
        assert result.exception is None
        # The metadata key survives the failed delete, so a later cleanup
        # pass can still discover and retry this repo.
        mock_store.delete_metadata.assert_not_called()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_delete_by_repo_error_during_prune_surfaces_in_all_errors(
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
        """Round 6 Gate 1/Codex Finding #2 (Medium): a failed cleanup-pass
        `delete_by_repo` used to be logged and swallowed with no other
        signal, so `refresh` could exit 0 while `--prune`/tainted-repo
        cleanup silently left stale/divergent records behind. Bounded fix:
        `_delete_repo_index_data` now returns `False` on failure, and its two
        call sites (both outside the `changed_repos` per-repo error-tracking
        loop) append a message to `all_errors` so the failure is visible in
        the refresh summary/error count -- without touching
        `repo_has_errors` or the exit-code contract for the changed_repos
        loop."""
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

        # Still a clean, non-fatal exit -- this is a best-effort cleanup pass,
        # not core ingestion.
        assert result.exit_code == 0, result.output
        assert result.exception is None
        # The failure is now visible via the refresh summary's error count:
        # `last_refresh_error_count` in the persisted metadata batch reflects
        # a non-zero `all_errors`, and the batch also stamps
        # `last_refresh_errored_at`.
        batch_calls = mock_store.set_metadata_batch.call_args_list
        assert batch_calls, "expected set_metadata_batch to be called"
        metadata_to_set = batch_calls[-1].args[0]
        assert metadata_to_set["last_refresh_error_count"] != "0"
        assert "last_refresh_errored_at" in metadata_to_set
        # This cleanup path is outside the changed_repos loop entirely, so it
        # cannot have touched that loop's repo_has_errors/exit-code
        # contract -- reconfirm exit_code is still 0 (checked above) and that
        # the metadata key was left untouched, exactly as before this fix.
        mock_store.delete_metadata.assert_not_called()


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


class TestDataDirSafetyFloor:
    """`_delete_local_index_storage()` is the last thing standing between a
    misconfigured `PIPECAT_HUB_DATA_DIR` and `shutil.rmtree`.

    The config-collision guard structurally cannot cover this: an operator
    who never created a `config.toml` (every user before this branch) gets no
    collision hit at all, so `PIPECAT_HUB_DATA_DIR=/` or `=$HOME` used to
    reach `rmtree` unguarded.
    """

    def _no_config_file(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(tmp_path / "nonexistent.toml"))

    def test_refuses_filesystem_root(self, monkeypatch, tmp_path: Path) -> None:
        self._no_config_file(monkeypatch, tmp_path)
        rmtree = MagicMock()
        monkeypatch.setattr("pipecat_context_hub.cli.shutil.rmtree", rmtree)
        with pytest.raises(click.ClickException):
            _delete_local_index_storage(Path(Path.cwd().anchor))
        rmtree.assert_not_called()

    def test_refuses_home_directory(self, monkeypatch, tmp_path: Path) -> None:
        self._no_config_file(monkeypatch, tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        rmtree = MagicMock()
        monkeypatch.setattr("pipecat_context_hub.cli.shutil.rmtree", rmtree)
        with pytest.raises(click.ClickException):
            _delete_local_index_storage(home)
        rmtree.assert_not_called()

    def test_refuses_ancestor_of_home(self, monkeypatch, tmp_path: Path) -> None:
        home = tmp_path / "users" / "someone"
        home.mkdir(parents=True)
        self._no_config_file(monkeypatch, tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        rmtree = MagicMock()
        monkeypatch.setattr("pipecat_context_hub.cli.shutil.rmtree", rmtree)
        with pytest.raises(click.ClickException):
            _delete_local_index_storage(tmp_path / "users")
        rmtree.assert_not_called()

    def test_allows_a_real_index_directory(self, monkeypatch, tmp_path: Path) -> None:
        """The floor must not become a blanket refusal — a normal data dir,
        including a shallow containerised one like `/data`, still deletes."""
        self._no_config_file(monkeypatch, tmp_path)
        data_dir = tmp_path / "hub-data"
        data_dir.mkdir()
        (data_dir / "marker").write_text("x")
        _delete_local_index_storage(data_dir)
        assert not data_dir.exists()

    def test_refuses_differently_cased_home_on_case_insensitive_fs(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Round-2 finding #1: the floor compared with `==`, but
        `Path.resolve()` preserves the caller's casing on APFS/NTFS, so
        `PIPECAT_HUB_DATA_DIR=/Users/Varun` slipped past a real home of
        `/Users/varun` and reached `shutil.rmtree`. Mirrors
        test_env_loading.py's
        `test_differently_cased_dir_is_detected_on_case_insensitive_fs`.
        """
        self._no_config_file(monkeypatch, tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        if not (tmp_path / "HOME").exists():
            pytest.skip("case-sensitive filesystem; casing cannot alias a directory here")
        monkeypatch.setattr(Path, "home", lambda: home)
        rmtree = MagicMock()
        monkeypatch.setattr("pipecat_context_hub.cli.shutil.rmtree", rmtree)
        with pytest.raises(click.ClickException):
            _delete_local_index_storage(tmp_path / "HOME")
        rmtree.assert_not_called()

    def test_symlinked_data_dir_stays_resolvable_after_reset(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Round-3 finding #1: with `PIPECAT_HUB_DATA_DIR` pointing at a
        symlink to a directory, `rmtree(resolved_data_dir)` deletes the
        link's *target* through the link, leaving the link dangling.
        `IndexStore`'s subsequent `data_dir.mkdir(parents=True,
        exist_ok=True)` then raises `FileExistsError` — `exist_ok` only
        suppresses when the existing path `is_dir()`, which a dangling
        symlink is not — so the reset aborts the rebuild it exists to
        enable.
        """
        self._no_config_file(monkeypatch, tmp_path)
        real = tmp_path / "real-data"
        real.mkdir()
        (real / "marker").write_text("x")
        link = tmp_path / "hub-data"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        _delete_local_index_storage(link)

        # The index contents really were deleted (this is still a reset)...
        assert not (real / "marker").exists()
        # ...but the operator's symlink is not left dangling.
        assert link.is_symlink()
        assert link.is_dir()
        # The exact call IndexStore makes next must not raise.
        link.mkdir(parents=True, exist_ok=True)

    def test_windows_junction_data_dir_stays_resolvable_after_reset(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Round-7 finding #1: on Windows a *directory junction* is followed by
        `resolve()` exactly like a symlink, but `Path.is_symlink()` reports it
        as False (its reparse tag is IO_REPARSE_TAG_MOUNT_POINT, not the
        symlink one). The repair above was gated on `is_symlink()` alone, so a
        junctioned `PIPECAT_HUB_DATA_DIR` had its target deleted through the
        junction and never recreated — leaving the configured path dangling and
        `IndexStore`'s follow-up mkdir raising FileExistsError.

        Junctions cannot be created on POSIX, so this emulates the *observable*
        Windows behavior: a real link whose `is_symlink()` answers False and
        whose `os.path.isjunction()` answers True. That is a simulation of the
        platform, not a test on it — untested against a real `mklink /J`.
        """
        self._no_config_file(monkeypatch, tmp_path)
        real = tmp_path / "real-data"
        real.mkdir()
        (real / "marker").write_text("x")
        link = tmp_path / "hub-data"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        # Windows emulation: junctions are not reported by is_symlink()...
        monkeypatch.setattr(Path, "is_symlink", lambda self: False)
        # ...they are reported by os.path.isjunction (Python 3.12+).
        monkeypatch.setattr(os.path, "isjunction", lambda p: str(p) == str(link), raising=False)

        _delete_local_index_storage(link)

        # Still a real reset: the index contents are gone...
        assert not os.path.exists(str(real / "marker"))
        # ...but the junction's target is put back, so the configured path
        # still resolves to a directory for the rebuild that follows.
        assert os.path.isdir(str(real))
        assert os.path.isdir(str(link))

    def test_plain_directory_data_dir_is_not_recreated(self, monkeypatch, tmp_path: Path) -> None:
        """Control for the two link cases above: for an ordinary directory the
        contract is "gone after reset", so the repair must not fire."""
        self._no_config_file(monkeypatch, tmp_path)
        data_dir = tmp_path / "hub-data"
        data_dir.mkdir()
        (data_dir / "marker").write_text("x")

        _delete_local_index_storage(data_dir)

        assert not data_dir.exists()

    def test_refuses_home_reached_through_a_symlinked_ancestor(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Same identity gap, reachable on case-sensitive filesystems too:
        a symlink hop naming home without matching its resolved spelling."""
        self._no_config_file(monkeypatch, tmp_path)
        real = tmp_path / "real"
        real.mkdir()
        home = real / "home"
        home.mkdir()
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")
        # `Path.home()` here reports the symlinked spelling, so its own
        # `.resolve()` yields `<tmp>/real/home` while the requested data dir
        # is spelled `<tmp>/alias/home` — one directory, two strings.
        monkeypatch.setattr(Path, "home", lambda: home)
        rmtree = MagicMock()
        monkeypatch.setattr("pipecat_context_hub.cli.shutil.rmtree", rmtree)
        with pytest.raises(click.ClickException):
            _delete_local_index_storage(alias / "home")
        rmtree.assert_not_called()

    def test_deletes_the_path_it_validated(self, monkeypatch, tmp_path: Path) -> None:
        """Round-2 finding #12: both guards ran against the expanded/resolved
        path while `rmtree` got the raw one, so a `~`-bearing `data_dir` would
        have been validated and then not deleted (guard/action drift)."""
        self._no_config_file(monkeypatch, tmp_path)
        home = tmp_path / "home"
        data_dir = home / "hub-data"
        data_dir.mkdir(parents=True)
        (data_dir / "marker").write_text("x")
        # `Path.expanduser()` reads HOME/USERPROFILE, not `Path.home()`, so
        # both the tilde expansion and the safety floor see the fake home.
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        rmtree = MagicMock()
        monkeypatch.setattr("pipecat_context_hub.cli.shutil.rmtree", rmtree)
        _delete_local_index_storage(Path("~/hub-data"))
        assert rmtree.call_args.args[0] == data_dir.resolve(strict=False)


class TestServeCwdDiagnosticRedaction:
    """The `serve cwd=` line is what operators paste into the bug-report flow
    `shared/support_links.py` promotes, so it must not carry an absolute home
    path (OS username, often a client/project name)."""

    def test_serve_cwd_log_is_home_redacted(self, monkeypatch, tmp_path: Path, caplog) -> None:
        home = tmp_path / "home"
        project = home / "customer-a"
        project.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(project)
        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        with patch("pipecat_context_hub.cli.Path.cwd", lambda: project):
            _log_serve_cwd()
        logged_cwd = logger.info.call_args[0][1]
        assert str(home) not in str(logged_cwd)
        assert str(logged_cwd).startswith("~")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="the probe write is O_NOFOLLOW-gated and os.O_NOFOLLOW does "
        "not exist on Windows, so no probe file is ever written there — see "
        "TestWriteServeDebugProbe.test_probe_skipped_without_o_nofollow_on_windows.",
    )
    def test_debug_probe_content_is_home_redacted(self, monkeypatch, tmp_path: Path) -> None:
        home = tmp_path / "home"
        project = home / "customer-a"
        project.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(project)
        _write_serve_debug_probe()
        probe = home / ".cache" / "pipecat-context-hub" / "serve-debug.log"
        content = probe.read_text()
        assert str(home) not in content
        assert "cwd=~" in content


class TestPruneSkipNoticePosition:
    """The notice is the whole point of the warn-instead-of-delete change; it
    must not land *after* the line that reads as 'done' in one branch and
    before it in the other."""

    def test_notice_precedes_completion_line_in_empty_branch(self, capsys) -> None:
        _print_refresh_summary({}, 0, 0, 1.0, unpruned_repo_count=2)
        out = capsys.readouterr().out
        assert out.index("Skipped pruning") < out.index("Refresh complete")

    def test_notice_precedes_completion_line_in_table_branch(self, capsys) -> None:
        source_status: dict[str, dict[str, str | int]] = {
            "pipecat-ai/pipecat": {
                "status": "updated",
                "sha": "abcdef12",
                "existing": 0,
                "updated": 5,
            },
        }
        _print_refresh_summary(source_status, 5, 0, 1.0, unpruned_repo_count=2)
        out = capsys.readouterr().out
        assert out.index("Skipped pruning") < out.index("Refresh complete")


class TestServeCwdDiagnosticIsCrashSafe:
    """Round-4 finding #4: `_log_serve_cwd` runs unguarded on every `serve`
    boot, while its *optional* sibling `_write_serve_debug_probe` wraps the
    identical `Path.cwd()` call in `except Exception` because "a debug probe
    must never crash serve". `Path.cwd()` raises FileNotFoundError when the
    process's working directory has been unlinked — so the mandatory
    diagnostic had the inverted robustness.
    """

    def test_unlinked_cwd_does_not_crash_serve_diagnostic(self, monkeypatch) -> None:
        def _raise_no_cwd():
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr("pipecat_context_hub.cli.Path.cwd", _raise_no_cwd)
        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        _log_serve_cwd()  # must not raise
        assert logger.info.call_args[0][1] == "<unavailable>"

    def test_home_runtime_error_does_not_crash_serve_diagnostic(self, monkeypatch) -> None:
        """`redact_home` swallows this itself today, but the guard must not
        depend on that — the whole resolution is inside the try."""

        def _raise_no_home():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "home", _raise_no_home)
        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        _log_serve_cwd()  # must not raise
        logger.info.assert_called_once()


class TestDebugProbeEnabled:
    """Round-4 finding #11: the probe gate was an inline `== "1"`, stricter
    than every sibling PIPECAT_HUB_* boolean flag for no stated reason —
    `PIPECAT_HUB_DEBUG_PROBE=true` silently did nothing while
    `PIPECAT_HUB_PRUNE=true` worked. Both are default-off opt-ins, so they
    share `_OPT_IN_ENABLED_VALUES`.
    """

    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "Yes", "YES"])
    def test_recognized_values_enable(self, value: str, monkeypatch) -> None:
        monkeypatch.setenv(env_loading.DEBUG_PROBE_ENV_VAR, value)
        assert _debug_probe_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "tRuE", "maybe", ""])
    def test_unrecognized_values_stay_disabled(self, value: str, monkeypatch) -> None:
        monkeypatch.setenv(env_loading.DEBUG_PROBE_ENV_VAR, value)
        assert _debug_probe_enabled() is False

    def test_unset_defaults_false(self, monkeypatch) -> None:
        monkeypatch.delenv(env_loading.DEBUG_PROBE_ENV_VAR, raising=False)
        assert _debug_probe_enabled() is False


class TestDebugProbeSymlinkHardening:
    """Round-4 finding #9: the probe opened its log with `Path.open("a")`,
    which follows symlinks, after a default-mode `mkdir`. Anyone able to
    write in the probe directory could pre-create the log as a symlink and
    have `serve` append to an arbitrary file the server user can write.
    """

    def test_symlinked_probe_path_is_not_followed(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        cache_dir = home / ".cache" / "pipecat-context-hub"
        cache_dir.mkdir(parents=True)
        victim = tmp_path / "victim.txt"
        victim.write_text("original\n")
        probe_path = cache_dir / "serve-debug.log"
        try:
            probe_path.symlink_to(victim)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        monkeypatch.setattr(Path, "home", lambda: home)
        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        _write_serve_debug_probe()  # must not raise

        assert victim.read_text() == "original\n"
        # ELOOP arrives as an OSError through the probe's swallow-and-log
        # failure path, which reports a redacted warning rather than a
        # traceback (round-9 finding #3).
        logger.warning.assert_called_once()

    def test_probe_is_skipped_when_platform_lacks_o_nofollow(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Round-6 finding #2. `getattr(os, "O_NOFOLLOW", 0)` degraded to a
        no-op flag on platforms without it (notably Windows), so the very
        symlink the flag exists to refuse would be followed and appended to.
        Absent the protection, the probe — an optional diagnostic — must not
        run at all.
        """
        home = tmp_path / "home"
        cache_dir = home / ".cache" / "pipecat-context-hub"
        cache_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        _write_serve_debug_probe()

        assert not (cache_dir / "serve-debug.log").exists()
        assert logger.warning.call_count == 1

    def test_symlinked_parent_component_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        """Round-8 finding #4: O_NOFOLLOW covers only the leaf file, while
        `mkdir(parents=True)` happily walks a symlinked *intermediate*
        component — e.g. `~/.cache` planted as a symlink by another local
        account. The probe would then create its directory and append its
        diagnostics inside an attacker-chosen tree despite the stated
        protection.
        """
        home = tmp_path / "home"
        home.mkdir()
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        try:
            (home / ".cache").symlink_to(attacker, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        monkeypatch.setattr(Path, "home", lambda: home)
        logger = MagicMock()
        monkeypatch.setattr(cli_module, "_module_logger", logger)
        _write_serve_debug_probe()  # must not raise

        assert not (attacker / "pipecat-context-hub").exists()
        assert logger.warning.call_count == 1

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
    def test_created_probe_file_and_dir_are_owner_only(self, tmp_path: Path, monkeypatch) -> None:
        import stat as stat_module

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write_serve_debug_probe()
        probe_path = home / ".cache" / "pipecat-context-hub" / "serve-debug.log"
        assert stat_module.S_IMODE(probe_path.stat().st_mode) == 0o600
        assert stat_module.S_IMODE(probe_path.parent.stat().st_mode) == 0o700
