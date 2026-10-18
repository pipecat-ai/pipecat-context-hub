"""Unit tests for the get_hub_status tool and index metadata persistence.

Tests cover:
1. FTSIndex metadata CRUD (set/get/get_all).
2. FTSIndex.get_index_stats with various content types.
3. handle_get_hub_status returns valid JSON with expected fields.
4. Hub status when no metadata exists (fresh install).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest

from pipecat_context_hub.server.main import _SERVER_VERSION
from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.shared.types import ChunkedRecord, HubStatusOutput

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 2, 26, tzinfo=UTC)


@pytest.fixture
def fts_index(tmp_path):
    """Create an FTSIndex with a temporary database."""
    db_path = tmp_path / "metadata.db"
    index = FTSIndex(db_path, create=True)
    yield index
    index.close()


def _make_record(
    content_type: Literal["doc", "code", "readme", "source"],
    chunk_id: str,
    commit_sha: str | None = "abc123",
) -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id=chunk_id,
        content=f"test content for {chunk_id}",
        content_type=content_type,
        source_url=f"https://example.com/{chunk_id}",
        path=f"test/{chunk_id}.py",
        commit_sha=commit_sha,
        indexed_at=NOW,
    )


# ---------------------------------------------------------------------------
# FTSIndex metadata tests
# ---------------------------------------------------------------------------


class TestFTSMetadata:
    def test_set_and_get_metadata(self, fts_index):
        fts_index.set_metadata("test_key", "test_value")
        assert fts_index.get_metadata("test_key") == "test_value"

    def test_get_metadata_missing_key(self, fts_index):
        assert fts_index.get_metadata("nonexistent") is None

    def test_set_metadata_upsert(self, fts_index):
        fts_index.set_metadata("key", "value1")
        fts_index.set_metadata("key", "value2")
        assert fts_index.get_metadata("key") == "value2"

    def test_get_all_metadata_empty(self, fts_index):
        assert fts_index.get_all_metadata() == {}

    def test_get_all_metadata_multiple(self, fts_index):
        fts_index.set_metadata("a", "1")
        fts_index.set_metadata("b", "2")
        fts_index.set_metadata("c", "3")
        result = fts_index.get_all_metadata()
        assert result == {"a": "1", "b": "2", "c": "3"}

    def test_metadata_persists_across_reopens(self, tmp_path):
        db_path = tmp_path / "metadata.db"
        index1 = FTSIndex(db_path, create=True)
        index1.set_metadata("persist_key", "persist_value")
        index1.close()

        index2 = FTSIndex(db_path, create=True)
        assert index2.get_metadata("persist_key") == "persist_value"
        index2.close()

    def test_set_metadata_batch_writes_all_pairs(self, fts_index):
        fts_index.set_metadata_batch({"a": "1", "b": "2", "c": "3"})
        assert fts_index.get_all_metadata() == {"a": "1", "b": "2", "c": "3"}

    def test_set_metadata_batch_upserts_existing_keys(self, fts_index):
        fts_index.set_metadata("a", "old")
        fts_index.set_metadata_batch({"a": "new", "b": "2"})
        assert fts_index.get_all_metadata() == {"a": "new", "b": "2"}

    def test_set_metadata_batch_deletes_requested_keys(self, fts_index):
        fts_index.set_metadata("stale", "gone")
        fts_index.set_metadata("keep", "1")
        fts_index.set_metadata_batch({"new": "value"}, delete_keys=["stale"])
        assert fts_index.get_all_metadata() == {"keep": "1", "new": "value"}

    def test_set_metadata_batch_commits_once(self, fts_index):
        """A single commit for the whole batch — not one per key.

        Guards the atomicity claim: readers should never observe a partially
        written related-key set. We can't observe SQLite's internal commit
        boundary directly, so we assert on the connection's commit() call
        count instead, which is the mechanism `set_metadata_batch` uses to
        make the write atomic.
        """
        # sqlite3.Connection is a C-level immutable type — its `commit` method
        # cannot be patched in place, so we swap in a thin proxy on the
        # FTSIndex instance's own `_conn` attribute (a plain, patchable
        # instance attribute) that counts commits while delegating
        # everything else to the real connection.
        real_conn = fts_index._conn
        commit_calls = []

        class _CommitCountingProxy:
            def __getattr__(self, name):
                return getattr(real_conn, name)

            def commit(self):
                commit_calls.append(1)
                return real_conn.commit()

        fts_index._conn = _CommitCountingProxy()
        try:
            fts_index.set_metadata_batch({"a": "1", "b": "2", "c": "3"}, delete_keys=["stale"])
        finally:
            fts_index._conn = real_conn
        assert len(commit_calls) == 1

    def test_set_metadata_batch_empty_pairs_with_deletes(self, fts_index):
        """Pure-delete batches (no new pairs) still work and still commit."""
        fts_index.set_metadata("stale", "gone")
        fts_index.set_metadata_batch({}, delete_keys=["stale"])
        assert fts_index.get_all_metadata() == {}


# ---------------------------------------------------------------------------
# FTSIndex stats tests
# ---------------------------------------------------------------------------


class TestFTSIndexStats:
    def test_stats_empty_index(self, fts_index):
        stats = fts_index.get_index_stats()
        assert stats["total"] == 0
        assert stats["counts_by_type"] == {}
        assert stats["commit_shas"] == []

    def test_stats_single_type(self, fts_index):
        records = [_make_record("doc", f"d{i}") for i in range(3)]
        fts_index.upsert(records)
        stats = fts_index.get_index_stats()
        assert stats["total"] == 3
        assert stats["counts_by_type"] == {"doc": 3}

    def test_stats_multiple_types(self, fts_index):
        records = [
            _make_record("doc", "d1"),
            _make_record("doc", "d2"),
            _make_record("code", "c1"),
            _make_record("source", "s1"),
            _make_record("source", "s2"),
            _make_record("source", "s3"),
        ]
        fts_index.upsert(records)
        stats = fts_index.get_index_stats()
        assert stats["total"] == 6
        assert stats["counts_by_type"] == {"doc": 2, "code": 1, "source": 3}

    def test_stats_distinct_commit_shas(self, fts_index):
        records = [
            _make_record("doc", "d1", commit_sha="sha1"),
            _make_record("code", "c1", commit_sha="sha2"),
            _make_record("source", "s1", commit_sha="sha1"),  # duplicate
            _make_record("source", "s2", commit_sha=None),  # null
        ]
        fts_index.upsert(records)
        stats = fts_index.get_index_stats()
        assert sorted(stats["commit_shas"]) == ["sha1", "sha2"]

    def test_stats_after_delete(self, fts_index):
        records = [
            _make_record("doc", "d1"),
            _make_record("code", "c1"),
        ]
        fts_index.upsert(records)
        fts_index.delete_by_content_type("doc")
        stats = fts_index.get_index_stats()
        assert stats["total"] == 1
        assert stats["counts_by_type"] == {"code": 1}


# ---------------------------------------------------------------------------
# handle_get_hub_status tests
# ---------------------------------------------------------------------------


class TestHandleGetHubStatus:
    def _mock_index_store(
        self,
        stats: dict[str, Any] | None = None,
        metadata: dict[str, str] | None = None,
        data_dir: Path | None = None,
    ) -> MagicMock:
        store = MagicMock()
        store.get_index_stats.return_value = stats or {
            "total": 0,
            "counts_by_type": {},
            "commit_shas": [],
        }
        store.get_all_metadata.return_value = metadata or {}
        store.data_dir = data_dir or Path("/tmp/test")
        return store

    async def test_fresh_install_no_metadata(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        store = self._mock_index_store()
        result_json = await handle_get_hub_status(
            {}, store, configured_repos=frozenset(), tainted_repos=frozenset()
        )
        output = HubStatusOutput.model_validate_json(result_json)

        assert output.server_version == _SERVER_VERSION
        assert output.last_refresh_at is None
        assert output.last_refresh_duration_seconds is None
        assert output.total_records == 0
        assert output.counts_by_type == {}
        assert output.commit_shas == []

    async def test_with_refresh_metadata(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        store = self._mock_index_store(
            stats={
                "total": 10017,
                "counts_by_type": {"doc": 3520, "code": 1422, "source": 5075},
                "commit_shas": ["abc123", "def456"],
            },
            metadata={
                "last_refresh_at": "2026-02-26T10:00:00+00:00",
                "last_refresh_duration_seconds": "42.5",
                "last_refresh_records_upserted": "10017",
                "last_refresh_error_count": "0",
            },
        )
        result_json = await handle_get_hub_status(
            {}, store, configured_repos=frozenset(), tainted_repos=frozenset()
        )
        output = HubStatusOutput.model_validate_json(result_json)

        assert output.server_version == _SERVER_VERSION
        assert output.last_refresh_at == "2026-02-26T10:00:00+00:00"
        assert output.last_refresh_duration_seconds == 42.5
        assert output.total_records == 10017
        assert output.counts_by_type == {"doc": 3520, "code": 1422, "source": 5075}
        assert sorted(output.commit_shas) == ["abc123", "def456"]

    async def test_indexed_framework_version_absent_by_default(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        store = self._mock_index_store()
        result_json = await handle_get_hub_status(
            {}, store, configured_repos=frozenset(), tainted_repos=frozenset()
        )
        output = HubStatusOutput.model_validate_json(result_json)
        assert output.indexed_framework_version is None
        assert output.indexed_framework_commits_ahead is None

    async def test_indexed_framework_version_surfaced(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        # Unpinned refresh: framework_version (the pin) is absent, but the
        # observed provenance is still reported.
        store = self._mock_index_store(
            metadata={
                "indexed_framework_version": "1.5.0",
                "indexed_framework_commits_ahead": "80",
            },
        )
        result_json = await handle_get_hub_status(
            {}, store, configured_repos=frozenset(), tainted_repos=frozenset()
        )
        output = HubStatusOutput.model_validate_json(result_json)

        assert output.framework_version is None
        assert output.indexed_framework_version == "1.5.0"
        assert output.indexed_framework_commits_ahead == 80

    async def test_indexed_framework_exact_release(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        # Zero commits ahead must round-trip as 0, not collapse to None.
        store = self._mock_index_store(
            metadata={
                "framework_version": "v1.5.0",
                "indexed_framework_version": "1.5.0",
                "indexed_framework_commits_ahead": "0",
            },
        )
        result_json = await handle_get_hub_status(
            {}, store, configured_repos=frozenset(), tainted_repos=frozenset()
        )
        output = HubStatusOutput.model_validate_json(result_json)

        assert output.framework_version == "v1.5.0"
        assert output.indexed_framework_commits_ahead == 0

    async def test_malformed_commits_ahead_degrades_to_none(self, caplog):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        # A corrupted or hand-edited metadata value must not crash the whole
        # status call — it degrades to None, same as if the key were absent.
        store = self._mock_index_store(
            metadata={"indexed_framework_commits_ahead": "not-a-number"},
        )
        with caplog.at_level("WARNING"):
            result_json = await handle_get_hub_status(
                {}, store, configured_repos=frozenset(), tainted_repos=frozenset()
            )
        output = HubStatusOutput.model_validate_json(result_json)

        assert output.indexed_framework_commits_ahead is None
        assert any("not-a-number" in r.message for r in caplog.records)

    async def test_malformed_duration_degrades_to_none(self, caplog):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        store = self._mock_index_store(
            metadata={"last_refresh_duration_seconds": "not-a-float"},
        )
        with caplog.at_level("WARNING"):
            result_json = await handle_get_hub_status(
                {}, store, configured_repos=frozenset(), tainted_repos=frozenset()
            )
        output = HubStatusOutput.model_validate_json(result_json)

        assert output.last_refresh_duration_seconds is None
        assert any("not-a-float" in r.message for r in caplog.records)

    async def test_index_path_returned(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        # index_path echoes the OS-native data dir (a Windows data dir is a
        # backslash path), so compare against str(Path(...)), not a POSIX literal.
        data_dir = Path("/home/user/.pipecat-context-hub")
        store = self._mock_index_store(data_dir=data_dir)
        result_json = await handle_get_hub_status(
            {}, store, configured_repos=frozenset(), tainted_repos=frozenset()
        )
        output = HubStatusOutput.model_validate_json(result_json)
        assert output.index_path == str(data_dir)

    async def test_returns_valid_json(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        store = self._mock_index_store()
        result_json = await handle_get_hub_status(
            {}, store, configured_repos=frozenset(), tainted_repos=frozenset()
        )
        parsed = json.loads(result_json)
        assert "server_version" in parsed
        assert "total_records" in parsed
        assert "counts_by_type" in parsed


# ---------------------------------------------------------------------------
# Catalog coverage fields (indexed / configured-no-records / not-indexed groups)
# ---------------------------------------------------------------------------

_CATALOG_TEXT = """
verified = 2026-10-17

[groups.acme]
kind = "vendor"
description = "Acme"
aliases = ["acme"]
repos = [{ slug = "acme/one" }]

[groups.beta]
kind = "vendor"
description = "Beta"
aliases = ["beta"]
repos = [{ slug = "beta/one" }]

[groups.gamma]
kind = "topic"
description = "Gamma"
aliases = ["gamma"]
repos = [{ slug = "gamma/one" }]
"""


class _StatusStore:
    """Fake serving index for status: counts, stats, metadata, data_dir only."""

    data_dir = Path("/tmp/hub")

    def __init__(self, counts: Any) -> None:
        self._counts = counts

    def get_index_stats(self) -> dict[str, Any]:
        return {"total": 0, "counts_by_type": {}, "commit_shas": []}

    def get_all_metadata(self) -> dict[str, str]:
        return {}

    def get_counts_by_repo(self) -> Any:
        if isinstance(self._counts, Exception):
            raise self._counts
        return self._counts


class _NoCountsStore:
    """Like ``_StatusStore`` but lacking ``get_counts_by_repo`` entirely."""

    data_dir = Path("/tmp/hub")

    def get_index_stats(self) -> dict[str, Any]:
        return {"total": 0, "counts_by_type": {}, "commit_shas": []}

    def get_all_metadata(self) -> dict[str, str]:
        return {}


@pytest.fixture
def catalog_text(monkeypatch):
    """Serve ``_CATALOG_TEXT`` as the shipped catalog; clear the load cache around the test."""
    from pipecat_context_hub.shared import repo_catalog

    text = {"value": _CATALOG_TEXT}
    repo_catalog.load_catalog.cache_clear()
    monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: text["value"])
    yield text
    repo_catalog.load_catalog.cache_clear()


async def _status(store: Any, **kwargs: Any) -> HubStatusOutput:
    from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

    return HubStatusOutput.model_validate_json(await handle_get_hub_status({}, store, **kwargs))


class TestHubStatusCoverageFields:
    def test_new_fields_default_to_empty_lists(self):
        output = HubStatusOutput.model_validate({"server_version": "x"})
        assert output.indexed_repos == []
        assert output.configured_no_records == []
        assert output.catalog_groups_not_indexed == []

    async def test_indexed_configured_no_records_and_uncovered(self, catalog_text):
        store = _StatusStore({"acme/one": 5, "beta/one": 0, "docs.pipecat.ai": 9})
        output = await _status(
            store,
            configured_repos=frozenset({"acme/one", "beta/one"}),
            tainted_repos=frozenset(),
        )
        assert [s.casefold() for s in output.indexed_repos] == ["acme/one"]
        assert output.configured_no_records == ["beta/one"]
        # beta is configured but empty; gamma is not configured at all.
        assert output.catalog_groups_not_indexed == ["beta", "gamma"]

    async def test_case_variant_slug_counts_as_indexed(self, catalog_text):
        store = _StatusStore({"owner/repo": 3})
        output = await _status(
            store, configured_repos=frozenset({"Owner/Repo"}), tainted_repos=frozenset()
        )
        assert "owner/repo" in [s.casefold() for s in output.indexed_repos]
        assert output.configured_no_records == []

    async def test_tainted_configured_repo_is_not_reported_no_records(self, catalog_text):
        # The tainted skip lives in the shared predicate, case-insensitively.
        store = _StatusStore({"acme/one": 5})
        output = await _status(
            store,
            configured_repos=frozenset({"acme/one", "Beta/One"}),
            tainted_repos=frozenset({"beta/ONE"}),
        )
        assert output.configured_no_records == []
        assert "beta" not in output.catalog_groups_not_indexed

    async def test_fully_indexed_catalog_has_no_uncovered_groups(self, catalog_text):
        store = _StatusStore({"acme/one": 1, "beta/one": 1, "gamma/one": 1})
        output = await _status(store, configured_repos=frozenset(), tainted_repos=frozenset())
        assert output.catalog_groups_not_indexed == []

    @pytest.mark.parametrize("omitted", ["configured_repos", "tainted_repos"])
    async def test_repo_sets_are_required(self, catalog_text, omitted):
        """No default: an omitted set would silently misreport coverage."""
        kwargs: dict[str, Any] = {"configured_repos": frozenset(), "tainted_repos": frozenset()}
        del kwargs[omitted]
        with pytest.raises(TypeError, match=omitted):
            await _status(_StatusStore({"acme/one": 1}), **kwargs)


class TestHubStatusUnreadableCounts:
    """A counts failure leaves the three fields empty and never raises."""

    @pytest.mark.parametrize(
        "store",
        [
            pytest.param(_StatusStore(RuntimeError("boom")), id="raises"),
            pytest.param(_NoCountsStore(), id="method-missing"),
            pytest.param(_StatusStore(MagicMock()), id="non-mapping"),
        ],
    )
    async def test_fields_empty_and_response_returned(self, catalog_text, store):
        output = await _status(
            store,
            configured_repos=frozenset({"acme/one"}),
            tainted_repos=frozenset(),
        )
        assert output.indexed_repos == []
        assert output.configured_no_records == []
        assert output.catalog_groups_not_indexed == []
        assert output.server_version == _SERVER_VERSION

    async def test_bare_magicmock_store(self, catalog_text):
        store = MagicMock()
        store.get_index_stats.return_value = {"total": 0, "counts_by_type": {}, "commit_shas": []}
        store.get_all_metadata.return_value = {}
        store.data_dir = Path("/tmp/hub")
        output = await _status(
            store, configured_repos=frozenset({"acme/one"}), tainted_repos=frozenset()
        )
        assert output.indexed_repos == []
        assert output.configured_no_records == []
        assert output.catalog_groups_not_indexed == []


class TestHubStatusCatalogLoadFailure:
    @pytest.mark.parametrize("broken", ["this is [not toml", "verified = 2026-10-17\n"])
    async def test_broken_catalog_gives_empty_groups_only(self, catalog_text, broken):
        from pipecat_context_hub.shared import repo_catalog

        catalog_text["value"] = broken
        repo_catalog.load_catalog.cache_clear()
        assert repo_catalog.load_catalog() is None
        output = await _status(
            _StatusStore({"acme/one": 2}),
            configured_repos=frozenset({"acme/one", "beta/one"}),
            tainted_repos=frozenset(),
        )
        assert output.catalog_groups_not_indexed == []
        # The count itself is still readable, so the other fields still work.
        assert [s.casefold() for s in output.indexed_repos] == ["acme/one"]
        assert output.configured_no_records == ["beta/one"]
