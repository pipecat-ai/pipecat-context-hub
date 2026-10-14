"""Tests for the index store: VectorIndex, FTSIndex, and unified IndexStore."""

from __future__ import annotations

import asyncio
import random
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import Mock, patch

import pytest

from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.services.index.locking import generation_lock, index_lock
from pipecat_context_hub.services.index.store import (
    IndexStore,
    ReadOnlyIndexStore,
    _create_backend_file,
)
from pipecat_context_hub.services.index.vector import (
    VectorIndex,
    _CHROMA_BATCH_SIZE,
    _copy_tree_no_follow,
)
from pipecat_context_hub.services.index import safe_sqlite
from pipecat_context_hub.shared.config import StorageConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

EMBEDDING_DIM = 384


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_store_refuses_symlinked_backend_file(tmp_path: Path) -> None:
    """The writer boundary must not open a backend through a symlink."""
    target = tmp_path / "victim.db"
    target.write_bytes(b"keep")
    data_dir = tmp_path / "index"
    data_dir.mkdir()
    (data_dir / "metadata.db").symlink_to(target)
    with pytest.raises(ValueError, match="Unsafe index backend path"):
        IndexStore(StorageConfig(data_dir=data_dir))
    assert target.read_bytes() == b"keep"


@pytest.mark.skipif(os.name == "nt", reason="POSIX WAL snapshot semantics")
def test_read_only_fts_reopen_retains_wal_snapshot_owner(tmp_path: Path) -> None:
    """Reopening a WAL reader must keep its private snapshot alive."""
    db = tmp_path / "metadata.db"
    writer = FTSIndex(db)
    writer.set_metadata("snapshot", "alive")
    reader = FTSIndex(db, read_only=True)
    try:
        assert reader._readonly_snapshot_dir is not None
        reader.reopen()
        assert reader._readonly_snapshot_dir is not None
        assert reader.get_metadata("snapshot") == "alive"
    finally:
        reader.close()
        writer.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics")
def test_legacy_read_releases_parent_guard_before_return(tmp_path: Path) -> None:
    """A legacy read must not leak the parent transition guard."""
    import fcntl

    data_dir = tmp_path / "hub-data"
    data_dir.mkdir()
    (data_dir / "metadata.db").touch()
    with index_lock(data_dir, exclusive=False, create=False):
        pass

    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(parent_fd, fcntl.LOCK_UN)
        os.close(parent_fd)


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics")
def test_legacy_read_rechecks_sibling_without_self_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling published during legacy setup is acquired through its lock."""
    data_dir = tmp_path / "hub-data"
    data_dir.mkdir()
    (data_dir / "metadata.db").touch()
    lock_path = data_dir.parent / f".{data_dir.name}.index.lock"
    original_lstat = Path.lstat
    injected = False

    def publish_sibling(path: Path):
        nonlocal injected
        if path == lock_path and not injected:
            try:
                return original_lstat(path)
            except FileNotFoundError:
                injected = True
                lock_path.touch()
                raise
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", publish_sibling)
    with index_lock(data_dir, exclusive=False, create=False):
        assert lock_path.is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative SQLite opens")
def test_sqlite_snapshot_fallback_never_reopens_published_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS-style hard-link failure must use descriptor bytes, not a pathname."""
    db = tmp_path / "metadata.db"
    with sqlite3.connect(db) as connection:
        connection.execute("create table marker (value text)")
        connection.execute("insert into marker values ('bound')")

    calls: list[str] = []
    original_connect = sqlite3.connect

    def record_connect(database: str, **kwargs: Any):
        calls.append(database)
        return original_connect(database, uri=bool(kwargs.get("uri", False)))

    monkeypatch.setattr(
        safe_sqlite, "_link_open_fd", lambda *_args: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(sqlite3, "connect", record_connect)
    with safe_sqlite.connect_readonly(db, immutable=False) as connection:
        assert connection.execute("select value from marker").fetchone() == ("bound",)
    assert calls
    assert str(db) not in calls[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative reset")
def test_vector_reset_rejects_generation_replaced_while_waiting_for_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reset must delete only the generation captured before lock wait."""
    chroma = tmp_path / "chroma"
    chroma.mkdir()
    (chroma / "original").write_text("keep")
    replacement = tmp_path / "chroma-replacement"

    index = object.__new__(VectorIndex)
    index._chroma_path = chroma
    index._read_only = False
    index._op_lock = threading.RLock()
    index._client = None
    index._collection = None
    index._closed = True
    index._readonly_tempdir = None
    index._lock_held = False

    @contextmanager
    def racing_lock(*_args: object, **_kwargs: object):
        chroma.rename(replacement)
        chroma.mkdir()
        (chroma / "replacement").write_text("must survive")
        yield

    monkeypatch.setattr("pipecat_context_hub.services.index.vector.index_lock", racing_lock)
    with pytest.raises(ValueError, match="Unsafe Chroma reset path"):
        index.reset()
    assert (replacement / "original").read_text() == "keep"
    assert (chroma / "replacement").read_text() == "must survive"


@pytest.mark.parametrize("sqlite_filename", ["../outside.db", str(Path("/tmp/outside.db"))])
def test_store_refuses_sqlite_path_outside_data_dir(tmp_path: Path, sqlite_filename: str) -> None:
    """A missing configured SQLite path must not escape before existence checks."""
    data_dir = tmp_path / "index"
    with pytest.raises(ValueError, match="Unsafe index backend path"):
        IndexStore(StorageConfig(data_dir=data_dir, sqlite_filename=sqlite_filename))
    assert not (tmp_path / "outside.db").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_store_accepts_symlinked_data_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    store = IndexStore(StorageConfig(data_dir=linked))
    store.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_store_refuses_symlinked_chroma_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "index"
    data_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_dir / "chroma").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Unsafe index backend path"):
        IndexStore(StorageConfig(data_dir=data_dir))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement fixture")
def test_store_rejects_backend_root_replaced_during_open(tmp_path: Path) -> None:
    """Validation must cover a data-root replacement between checks and open."""
    data_dir = tmp_path / "index"
    data_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    replacement = tmp_path / "index-original"
    vector = Mock()

    def race(_path: Path) -> Mock:
        data_dir.rename(replacement)
        data_dir.symlink_to(outside, target_is_directory=True)
        return vector

    with patch("pipecat_context_hub.services.index.store.VectorIndex", side_effect=race):
        with pytest.raises(ValueError, match="Unsafe index backend path"):
            IndexStore(StorageConfig(data_dir=data_dir))
    vector.close.assert_called_once()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement fixture")
def test_store_rejects_new_backend_directory_replaced_during_open(tmp_path: Path) -> None:
    """A newly materialised backend must not become an external symlink."""
    data_dir = tmp_path / "index"
    outside = tmp_path / "outside"
    outside.mkdir()
    vector = Mock()

    def race(path: Path) -> Mock:
        path.rename(tmp_path / "chroma-original")
        path.symlink_to(outside, target_is_directory=True)
        return vector

    with patch("pipecat_context_hub.services.index.store.VectorIndex", side_effect=race):
        with pytest.raises(ValueError, match="Unsafe index backend path"):
            IndexStore(StorageConfig(data_dir=data_dir))
    vector.close.assert_called_once()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link fixture")
def test_fts_writer_alias_stays_bound_when_published_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite must not open an attacker-selected replacement database."""
    database = tmp_path / "metadata.db"
    outside = tmp_path / "outside.db"
    real_connect = sqlite3.connect
    swapped = False

    def replace_published_path(path: str, **kwargs: Any):
        nonlocal swapped
        if not swapped and Path(path).name == "metadata.db":
            swapped = True
            database.rename(tmp_path / "metadata-original.db")
            database.symlink_to(outside)
        return real_connect(path, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", replace_published_path)
    with pytest.raises(ValueError, match="unsafe SQLite database path"):
        FTSIndex(database)
    assert not outside.exists()


def test_metadata_only_reader_does_not_construct_chroma(tmp_path: Path) -> None:
    """Status/deprecation reads must not snapshot or initialize Chroma."""
    data_dir = tmp_path / "index"
    writer = IndexStore(StorageConfig(data_dir=data_dir))
    writer.close()
    with patch(
        "pipecat_context_hub.services.index.store.VectorIndex",
        side_effect=AssertionError("metadata-only reader opened Chroma"),
    ):
        reader = ReadOnlyIndexStore(StorageConfig(data_dir=data_dir), include_vector=False)
    assert reader.get_index_stats()["total"] == 0
    reader.close()


def _random_embedding(seed: int = 0) -> list[float]:
    """Generate a deterministic pseudo-random embedding vector."""
    rng = random.Random(seed)
    return [rng.gauss(0, 1) for _ in range(EMBEDDING_DIM)]


def _make_record(
    chunk_id: str = "chunk-1",
    content: str = "Pipecat is a framework for building voice agents.",
    content_type: Literal["doc", "code", "readme", "source"] = "doc",
    source_url: str = "https://docs.pipecat.ai/intro",
    repo: str | None = "pipecat-ai/pipecat",
    path: str = "/docs/intro.md",
    embedding: list[float] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ChunkedRecord:
    if embedding is None:
        embedding = _random_embedding(hash(chunk_id) % 10000)
    return ChunkedRecord(
        chunk_id=chunk_id,
        content=content,
        content_type=content_type,
        source_url=source_url,
        repo=repo,
        path=path,
        indexed_at=datetime.now(tz=timezone.utc),
        embedding=embedding,
        metadata=metadata or {},
    )


def _make_records(
    count: int, source_url: str = "https://docs.pipecat.ai/intro"
) -> list[ChunkedRecord]:
    """Create multiple unique records."""
    return [
        _make_record(
            chunk_id=f"chunk-{i}",
            content=f"Content for chunk {i} about pipecat voice agents.",
            source_url=source_url,
            path=f"/docs/page{i}.md",
            embedding=_random_embedding(i),
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# VectorIndex tests
# ---------------------------------------------------------------------------


class TestVectorIndex:
    @pytest.fixture()
    def vector_index(self, tmp_path: Path) -> Generator[VectorIndex, None, None]:
        index = VectorIndex(tmp_path / "chroma")
        yield index
        index.close()

    def test_disables_chroma_telemetry(self, tmp_path: Path):
        mock_client = Mock()
        mock_client.get_or_create_collection.return_value = Mock()
        mock_client._identifier = "mock-client"

        with patch(
            "pipecat_context_hub.services.index.vector.chromadb.PersistentClient",
            return_value=mock_client,
        ) as persistent_client:
            index = VectorIndex(tmp_path / "chroma")
            index.close()

        settings = persistent_client.call_args.kwargs["settings"]
        assert settings.anonymized_telemetry is False
        assert settings.chroma_product_telemetry_impl == (
            "pipecat_context_hub.services.index.vector.NoOpProductTelemetryClient"
        )

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement fixture")
    def test_native_open_replacement_fails_closed_and_closes_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pathname-only Chroma API must not leave a swapped client alive."""
        chroma = tmp_path / "chroma"
        outside = tmp_path / "outside"
        outside.mkdir()
        replacement = tmp_path / "chroma-original"
        mock_client = Mock()
        mock_client.get_or_create_collection.return_value = Mock()

        def race(*, path: str, settings: Any) -> Mock:
            assert Path(path) == chroma
            chroma.rename(replacement)
            chroma.symlink_to(outside, target_is_directory=True)
            return mock_client

        monkeypatch.setattr(
            "pipecat_context_hub.services.index.vector.chromadb.PersistentClient", race
        )
        with pytest.raises(ValueError, match="unsafe Chroma generation path"):
            VectorIndex(chroma)
        mock_client.close.assert_called_once()
        assert not (outside / "chroma.sqlite3").exists()

    def test_upsert_and_search(self, vector_index: VectorIndex):
        records = _make_records(3)
        count = vector_index.upsert(records)
        assert count == 3

        query = IndexQuery(
            query_text="pipecat",
            query_embedding=records[0].embedding,
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 3
        assert all(isinstance(r, IndexResult) for r in results)
        assert all(r.match_type == "vector" for r in results)
        # First result should be the most similar (same embedding)
        assert results[0].chunk.chunk_id == "chunk-0"
        assert results[0].score > 0

    def test_upsert_empty(self, vector_index: VectorIndex):
        count = vector_index.upsert([])
        assert count == 0

    def test_upsert_no_embedding(self, vector_index: VectorIndex):
        record = _make_record()
        record.embedding = None
        count = vector_index.upsert([record])
        assert count == 0

    def test_upsert_idempotent(self, vector_index: VectorIndex):
        record = _make_record(chunk_id="idem-1", content="original content")
        vector_index.upsert([record])

        # Upsert again with updated content
        updated = _make_record(chunk_id="idem-1", content="updated content")
        vector_index.upsert([updated])

        query = IndexQuery(
            query_text="test",
            query_embedding=updated.embedding,
            limit=10,
        )
        results = vector_index.search(query)
        # Should have exactly 1, not 2
        assert len(results) == 1
        assert results[0].chunk.content == "updated content"

    def test_delete_by_source(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="a1", source_url="https://a.com"),
            _make_record(chunk_id="a2", source_url="https://a.com"),
            _make_record(chunk_id="b1", source_url="https://b.com"),
        ]
        vector_index.upsert(records)

        deleted = vector_index.delete_by_source("https://a.com")
        assert deleted == 2

        # Only b1 should remain
        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "b1"

    def test_delete_nonexistent_source(self, vector_index: VectorIndex):
        deleted = vector_index.delete_by_source("https://nonexistent.com")
        assert deleted == 0

    def test_delete_by_content_type(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="d1", content_type="doc"),
            _make_record(chunk_id="d2", content_type="doc"),
            _make_record(chunk_id="c1", content_type="code"),
        ]
        vector_index.upsert(records)

        deleted = vector_index.delete_by_content_type("doc")
        assert deleted == 2

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content_type == "code"

    def test_delete_by_repo(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="r1", repo="pipecat-ai/pipecat", content_type="code"),
            _make_record(chunk_id="r2", repo="pipecat-ai/pipecat", content_type="source"),
            _make_record(chunk_id="r3", repo="pipecat-ai/pipecat-examples"),
        ]
        vector_index.upsert(records)

        deleted = vector_index.delete_by_repo("pipecat-ai/pipecat")
        assert deleted == 2

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.repo == "pipecat-ai/pipecat-examples"

    def test_delete_by_repo_nonexistent(self, vector_index: VectorIndex):
        deleted = vector_index.delete_by_repo("nonexistent/repo")
        assert deleted == 0

    def test_search_without_embedding(self, vector_index: VectorIndex):
        query = IndexQuery(query_text="test", limit=10)
        results = vector_index.search(query)
        assert results == []

    def test_filter_by_repo(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="r1", repo="pipecat-ai/pipecat"),
            _make_record(chunk_id="r2", repo="pipecat-ai/pipecat-examples"),
        ]
        vector_index.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"repo": "pipecat-ai/pipecat"},
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.repo == "pipecat-ai/pipecat"

    def test_filter_by_content_type(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="d1", content_type="doc"),
            _make_record(chunk_id="c1", content_type="code"),
        ]
        vector_index.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"content_type": "code"},
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content_type == "code"

    def test_filter_by_path_prefix(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="p1", path="/docs/guides/setup.md"),
            _make_record(chunk_id="p2", path="/docs/api/ref.md"),
            _make_record(chunk_id="p3", path="/examples/basic.py"),
        ]
        vector_index.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"path": "/docs/"},
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 2
        paths = {r.chunk.path for r in results}
        assert paths == {"/docs/guides/setup.md", "/docs/api/ref.md"}

    def test_filter_by_capability_tags(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="t1", metadata={"capability_tags": ["tts", "stt"]}),
            _make_record(chunk_id="t2", metadata={"capability_tags": ["vision"]}),
        ]
        vector_index.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"capability_tags": "tts"},
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "t1"

    def test_clear(self, vector_index: VectorIndex):
        """clear() should drop all records so searches return nothing."""
        vector_index.upsert(_make_records(5))
        vector_index.clear()

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        results = vector_index.search(query)
        assert results == []

    def test_clear_then_upsert(self, vector_index: VectorIndex):
        """Index should be usable after clear()."""
        vector_index.upsert(_make_records(3))
        vector_index.clear()

        new_records = [_make_record(chunk_id="new-1", content="fresh data")]
        vector_index.upsert(new_records)

        query = IndexQuery(
            query_text="test",
            query_embedding=new_records[0].embedding,
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "new-1"

    def test_reset_then_upsert(self, vector_index: VectorIndex):
        """reset() should rebuild persisted storage and allow reuse."""
        vector_index.upsert(_make_records(3))
        vector_index.reset()
        assert vector_index._collection.count() == 0

        new_record = _make_record(chunk_id="reset-1", content="fresh reset data")
        vector_index.upsert([new_record])
        query = IndexQuery(
            query_text="test",
            query_embedding=new_record.embedding,
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "reset-1"

    def test_close_is_idempotent(self, vector_index: VectorIndex):
        """close() can be called more than once."""
        vector_index.close()
        vector_index.close()
        assert vector_index._client is None
        assert vector_index._collection is None

    def test_persistence(self, tmp_path: Path):
        """Verify data survives creating a new VectorIndex on the same path."""
        chroma_path = tmp_path / "chroma"
        idx1 = VectorIndex(chroma_path)
        records = _make_records(2)
        idx1.upsert(records)

        # Create a new index pointing at the same directory
        idx2 = VectorIndex(chroma_path)
        query = IndexQuery(
            query_text="test",
            query_embedding=records[0].embedding,
            limit=10,
        )
        results = idx2.search(query)
        assert len(results) == 2
        idx2.close()
        idx1.close()

    def test_get_or_create_preserves_existing_collection(self, tmp_path: Path):
        """get_or_create with explicit hnsw:space must not recreate/empty data.

        chromadb 1.x moved HNSW config to a ``configuration`` API and deprecated
        ``metadata={"hnsw:space": ...}``. Guard against a regression where the
        legacy metadata form silently drops an existing collection's rows
        (risk #11) — re-opening a populated index must keep every record.
        """
        chroma_path = tmp_path / "chroma"
        idx1 = VectorIndex(chroma_path)
        records = _make_records(3)
        known_id = records[0].chunk_id
        idx1.upsert(records)
        idx1.close()

        # Re-open: _open_client calls get_or_create_collection with cosine.
        idx2 = VectorIndex(chroma_path)
        query = IndexQuery(query_text="test", query_embedding=records[0].embedding, limit=10)
        results = idx2.search(query)
        assert len(results) == 3, "get_or_create dropped rows on reopen"
        assert any(r.chunk.chunk_id == known_id for r in results)
        idx2.close()


# ---------------------------------------------------------------------------
# ChromaDB batch stress tests
# ---------------------------------------------------------------------------


class TestVectorIndexBatchStress:
    """Stress tests for ChromaDB batch operations above _CHROMA_BATCH_SIZE.

    These verify that upsert, delete_by_content_type, and delete_by_source
    work correctly when record counts exceed ChromaDB's per-call limit.
    """

    # Just over the batch size to trigger multi-batch code paths.
    OVER_LIMIT = _CHROMA_BATCH_SIZE + 100

    @pytest.fixture()
    def vector_index(self, tmp_path: Path) -> Generator[VectorIndex, None, None]:
        index = VectorIndex(tmp_path / "chroma")
        yield index
        index.close()

    @pytest.mark.benchmark
    def test_upsert_above_batch_size(self, vector_index: VectorIndex):
        """Upsert more records than _CHROMA_BATCH_SIZE without error."""
        records = _make_records(self.OVER_LIMIT)
        count = vector_index.upsert(records)
        assert count == self.OVER_LIMIT
        assert vector_index._collection.count() == self.OVER_LIMIT

    @pytest.mark.benchmark
    def test_delete_by_content_type_above_batch_size(self, vector_index: VectorIndex):
        """Delete more records than _CHROMA_BATCH_SIZE by content_type."""
        records = [
            _make_record(
                chunk_id=f"doc-{i}",
                content=f"doc content {i}",
                content_type="doc",
                embedding=_random_embedding(i),
            )
            for i in range(self.OVER_LIMIT)
        ]
        # Add a few code records that should survive the delete
        survivors = [
            _make_record(
                chunk_id=f"code-{i}",
                content=f"code content {i}",
                content_type="code",
                embedding=_random_embedding(self.OVER_LIMIT + i),
            )
            for i in range(3)
        ]
        vector_index.upsert(records + survivors)
        assert vector_index._collection.count() == self.OVER_LIMIT + 3

        deleted = vector_index.delete_by_content_type("doc")
        assert deleted == self.OVER_LIMIT
        assert vector_index._collection.count() == 3

    @pytest.mark.benchmark
    def test_delete_by_source_above_batch_size(self, vector_index: VectorIndex):
        """Delete more records than _CHROMA_BATCH_SIZE by source URL."""
        source_url = "https://example.com/big-source"
        records = [
            _make_record(
                chunk_id=f"src-{i}",
                content=f"source content {i}",
                source_url=source_url,
                embedding=_random_embedding(i),
            )
            for i in range(self.OVER_LIMIT)
        ]
        other = _make_record(
            chunk_id="other-1",
            content="other content",
            source_url="https://example.com/other",
            embedding=_random_embedding(self.OVER_LIMIT + 1),
        )
        vector_index.upsert(records + [other])
        assert vector_index._collection.count() == self.OVER_LIMIT + 1

        deleted = vector_index.delete_by_source(source_url)
        assert deleted == self.OVER_LIMIT
        assert vector_index._collection.count() == 1

    @pytest.mark.benchmark
    def test_upsert_then_search_above_batch_size(self, vector_index: VectorIndex):
        """Records upserted in multiple batches are all searchable."""
        records = _make_records(self.OVER_LIMIT)
        vector_index.upsert(records)

        # Search for a record from the second batch
        target_idx = _CHROMA_BATCH_SIZE + 50
        query = IndexQuery(
            query_text="test",
            query_embedding=records[target_idx].embedding,
            limit=1,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == f"chunk-{target_idx}"


# ---------------------------------------------------------------------------
# FTSIndex tests
# ---------------------------------------------------------------------------


class TestFTSIndex:
    @pytest.fixture()
    def fts_index(self, tmp_path: Path) -> Generator[FTSIndex, None, None]:
        index = FTSIndex(tmp_path / "metadata.db")
        yield index
        index.close()

    def test_upsert_and_search(self, fts_index: FTSIndex):
        records = _make_records(3)
        count = fts_index.upsert(records)
        assert count == 3

        query = IndexQuery(query_text="pipecat voice", limit=10)
        results = fts_index.search(query)
        assert len(results) == 3
        assert all(r.match_type == "keyword" for r in results)
        assert all(r.score > 0 for r in results)

    def test_read_only_open_does_not_create_sqlite_sidecars(self, tmp_path: Path) -> None:
        path = tmp_path / "metadata.db"
        writer = FTSIndex(path)
        writer.upsert([_make_record()])
        writer.close()
        reader = FTSIndex(path, read_only=True)
        try:
            assert reader.search(IndexQuery(query_text="pipecat", limit=1))
        finally:
            reader.close()
        assert not path.with_name(path.name + "-shm").exists()
        assert not path.with_name(path.name + "-wal").exists()

    def test_read_only_open_reads_committed_wal(self, tmp_path: Path) -> None:
        path = tmp_path / "metadata.db"
        writer = FTSIndex(path)
        writer.upsert([_make_record()])
        try:
            reader = FTSIndex(path, read_only=True)
            try:
                assert reader.search(IndexQuery(query_text="pipecat", limit=1))
            finally:
                reader.close()
        finally:
            writer.close()

    def test_upsert_empty(self, fts_index: FTSIndex):
        count = fts_index.upsert([])
        assert count == 0

    def test_upsert_idempotent(self, fts_index: FTSIndex):
        record = _make_record(chunk_id="idem-1", content="original pipecat content")
        fts_index.upsert([record])

        updated = _make_record(chunk_id="idem-1", content="updated pipecat content")
        fts_index.upsert([updated])

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content == "updated pipecat content"

    def test_delete_by_source(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="a1", source_url="https://a.com", content="pipecat a1"),
            _make_record(chunk_id="a2", source_url="https://a.com", content="pipecat a2"),
            _make_record(chunk_id="b1", source_url="https://b.com", content="pipecat b1"),
        ]
        fts_index.upsert(records)

        deleted = fts_index.delete_by_source("https://a.com")
        assert deleted == 2

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "b1"

    def test_delete_nonexistent_source(self, fts_index: FTSIndex):
        deleted = fts_index.delete_by_source("https://nonexistent.com")
        assert deleted == 0

    def test_delete_by_content_type(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="d1", content_type="doc", content="pipecat doc one"),
            _make_record(chunk_id="d2", content_type="doc", content="pipecat doc two"),
            _make_record(chunk_id="c1", content_type="code", content="pipecat code one"),
        ]
        fts_index.upsert(records)

        deleted = fts_index.delete_by_content_type("doc")
        assert deleted == 2

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content_type == "code"

    def test_delete_by_repo(self, fts_index: FTSIndex):
        records = [
            _make_record(
                chunk_id="r1",
                repo="pipecat-ai/pipecat",
                content_type="code",
                content="pipecat code r1",
            ),
            _make_record(
                chunk_id="r2",
                repo="pipecat-ai/pipecat",
                content_type="source",
                content="pipecat source r2",
            ),
            _make_record(
                chunk_id="r3", repo="pipecat-ai/pipecat-examples", content="pipecat examples r3"
            ),
        ]
        fts_index.upsert(records)

        deleted = fts_index.delete_by_repo("pipecat-ai/pipecat")
        assert deleted == 2

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.repo == "pipecat-ai/pipecat-examples"

    def test_delete_by_repo_nonexistent(self, fts_index: FTSIndex):
        deleted = fts_index.delete_by_repo("nonexistent/repo")
        assert deleted == 0

    def test_search_empty_query(self, fts_index: FTSIndex):
        fts_index.upsert(_make_records(1))
        query = IndexQuery(query_text="", limit=10)
        results = fts_index.search(query)
        assert results == []

    def test_search_whitespace_query(self, fts_index: FTSIndex):
        fts_index.upsert(_make_records(1))
        query = IndexQuery(query_text="   ", limit=10)
        results = fts_index.search(query)
        assert results == []

    def test_filter_by_repo(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="r1", repo="pipecat-ai/pipecat", content="pipecat core"),
            _make_record(chunk_id="r2", repo="pipecat-ai/examples", content="pipecat examples"),
        ]
        fts_index.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"repo": "pipecat-ai/pipecat"},
            limit=10,
        )
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.repo == "pipecat-ai/pipecat"

    def test_filter_by_content_type(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="d1", content_type="doc", content="pipecat doc"),
            _make_record(chunk_id="c1", content_type="code", content="pipecat code"),
        ]
        fts_index.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"content_type": "code"},
            limit=10,
        )
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content_type == "code"

    def test_filter_by_path_prefix(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="p1", path="/docs/guides/setup.md", content="pipecat setup"),
            _make_record(chunk_id="p2", path="/docs/api/ref.md", content="pipecat api"),
            _make_record(chunk_id="p3", path="/examples/basic.py", content="pipecat example"),
        ]
        fts_index.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"path": "/docs/"},
            limit=10,
        )
        results = fts_index.search(query)
        assert len(results) == 2
        paths = {r.chunk.path for r in results}
        assert paths == {"/docs/guides/setup.md", "/docs/api/ref.md"}

    def test_filter_by_capability_tags(self, fts_index: FTSIndex):
        records = [
            _make_record(
                chunk_id="t1",
                content="pipecat with tts",
                metadata={"capability_tags": ["tts", "stt"]},
            ),
            _make_record(
                chunk_id="t2",
                content="pipecat with vision",
                metadata={"capability_tags": ["vision"]},
            ),
        ]
        fts_index.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"capability_tags": "tts"},
            limit=10,
        )
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "t1"

    def test_clear(self, fts_index: FTSIndex):
        """clear() should drop all records so searches return nothing."""
        fts_index.upsert(_make_records(5))
        fts_index.clear()

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert results == []

    def test_clear_then_upsert(self, fts_index: FTSIndex):
        """Index should be usable after clear()."""
        fts_index.upsert(_make_records(3))
        fts_index.clear()

        new_records = [_make_record(chunk_id="new-1", content="fresh pipecat data")]
        fts_index.upsert(new_records)

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "new-1"

    def test_persistence(self, tmp_path: Path):
        """Verify data survives creating a new FTSIndex on the same path."""
        db_path = tmp_path / "metadata.db"
        idx1 = FTSIndex(db_path)
        idx1.upsert([_make_record(content="pipecat persists")])
        idx1.close()

        idx2 = FTSIndex(db_path)
        query = IndexQuery(query_text="pipecat", limit=10)
        results = idx2.search(query)
        assert len(results) == 1
        idx2.close()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link fixture")
    def test_close_does_not_remove_sidecars_used_by_another_writer(self, tmp_path: Path) -> None:
        """A writer close must not detach another connection's WAL/SHM aliases."""
        path = tmp_path / "metadata.db"
        first = FTSIndex(path)
        second = FTSIndex(path)
        try:
            first.close()
            assert path.with_name(path.name + "-wal").exists()
            assert path.with_name(path.name + "-shm").exists()
        finally:
            second.close()
        assert not path.with_name(path.name + "-wal").exists()
        assert not path.with_name(path.name + "-shm").exists()

    def test_result_limit(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id=f"lim-{i}", content=f"pipecat content {i}") for i in range(5)
        ]
        fts_index.upsert(records)

        query = IndexQuery(query_text="pipecat", limit=2)
        results = fts_index.search(query)
        assert len(results) == 2

    def test_delete_metadata(self, fts_index: FTSIndex):
        fts_index.set_metadata("repo:org/repo:commit_sha", "abc123")
        assert fts_index.get_metadata("repo:org/repo:commit_sha") == "abc123"

        fts_index.delete_metadata("repo:org/repo:commit_sha")
        assert fts_index.get_metadata("repo:org/repo:commit_sha") is None

    def test_delete_metadata_nonexistent(self, fts_index: FTSIndex):
        """Deleting a nonexistent key should not raise."""
        fts_index.delete_metadata("nonexistent:key")

    def test_close_is_idempotent(self, fts_index: FTSIndex):
        """close() can be called more than once."""
        fts_index.close()
        fts_index.close()


# ---------------------------------------------------------------------------
# Unified IndexStore tests
# ---------------------------------------------------------------------------


class TestIndexStoreInitRollback:
    """If FTS construction fails after Vector opens, the Vector handle must
    be closed before the exception propagates."""

    def test_fts_failure_closes_vector(self, tmp_path: Path, monkeypatch):
        from unittest.mock import MagicMock
        from pipecat_context_hub.services.index import store as store_mod

        fake_vector = MagicMock()
        monkeypatch.setattr(store_mod, "VectorIndex", MagicMock(return_value=fake_vector))
        monkeypatch.setattr(
            store_mod, "FTSIndex", MagicMock(side_effect=RuntimeError("sqlite boom"))
        )

        with pytest.raises(RuntimeError, match="sqlite boom"):
            IndexStore(StorageConfig(data_dir=tmp_path / "hub-data"))
        fake_vector.close.assert_called_once()

    def test_failed_fts_open_removes_new_sqlite_sidecars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WAL/SHM created before a failed FTS constructor cannot poison retry."""
        from pipecat_context_hub.services.index import store as store_mod

        def fail_after_wal(path: Path, **_kwargs: Any) -> None:
            path.with_name(path.name + "-wal").write_bytes(b"failed generation")
            path.with_name(path.name + "-shm").write_bytes(b"failed generation")
            raise RuntimeError("sqlite boom")

        monkeypatch.setattr(store_mod, "FTSIndex", fail_after_wal)
        data_dir = tmp_path / "hub-data"
        with pytest.raises(RuntimeError, match="sqlite boom"):
            IndexStore(StorageConfig(data_dir=data_dir))
        assert not (data_dir / "metadata.db").exists()
        assert not (data_dir / "metadata.db-wal").exists()
        assert not (data_dir / "metadata.db-shm").exists()

    def test_writer_backend_creation_without_descriptor_relative_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backend creation fails closed without secure directory descriptors."""
        monkeypatch.setattr(os, "supports_dir_fd", set())
        with pytest.raises(ValueError, match="Secure backend file creation"):
            IndexStore(StorageConfig(data_dir=tmp_path / "windows-style"))

    def test_backend_placeholders_are_removed_when_vector_init_fails(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(
            VectorIndex,
            "__init__",
            lambda self, path: (_ for _ in ()).throw(RuntimeError("vector boom")),
        )
        data_dir = tmp_path / "hub-data"
        with pytest.raises(RuntimeError, match="vector boom"):
            IndexStore(StorageConfig(data_dir=data_dir))
        assert not (data_dir / "metadata.db").exists()
        assert not (data_dir / "chroma" / "chroma.sqlite3").exists()
        assert not (data_dir / "chroma").exists()

    def test_read_only_store_exposes_no_writer_methods(self, tmp_path: Path):
        # Build the persisted shape through the writer, then reopen through the
        # reader boundary and assert mutation methods are not part of its API.
        writer = IndexStore(StorageConfig(data_dir=tmp_path / "hub-data"))
        writer.close()
        reader = ReadOnlyIndexStore(StorageConfig(data_dir=tmp_path / "hub-data"))
        try:
            assert not hasattr(reader, "reset")
            assert not hasattr(reader, "upsert")
        finally:
            reader.close()

    def test_read_only_open_does_not_create_missing_lock_file(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "hub-data"
        writer = IndexStore(StorageConfig(data_dir=data_dir))
        writer.close()
        reader = ReadOnlyIndexStore(StorageConfig(data_dir=data_dir))
        try:
            assert not (data_dir / ".index.lock").exists()
            assert (data_dir.parent / f".{data_dir.name}.index.lock").exists()
        finally:
            reader.close()

    def test_read_only_open_does_not_create_legacy_lock_file(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "hub-data"
        writer = IndexStore(StorageConfig(data_dir=data_dir))
        writer.close()
        lock_path = data_dir.parent / f".{data_dir.name}.index.lock"
        lock_path.unlink()
        reader = ReadOnlyIndexStore(StorageConfig(data_dir=data_dir))
        try:
            assert not lock_path.exists()
        finally:
            reader.close()

    def test_generation_lock_creates_missing_nested_parents(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "missing" / "nested" / "hub-data"
        with generation_lock(data_dir):
            assert data_dir.parent.is_dir()
            assert data_dir.parent.parent.is_dir()

    def test_writer_open_refuses_incomplete_reset_marker(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "hub-data"
        data_dir.mkdir()
        (data_dir / ".reset-state.json").write_text('{"vector_reset": true}')
        with pytest.raises(ValueError, match="reset is incomplete"):
            IndexStore(StorageConfig(data_dir=data_dir))

    def test_windows_backend_creation_uses_safe_path_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "nested" / "metadata.db"
        path.parent.mkdir()
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(
            "pipecat_context_hub.services.index.store._open_directory_chain", lambda _path: -1
        )
        with pytest.raises(ValueError, match="Secure backend file creation"):
            _create_backend_file(path)

    def test_windows_snapshot_uses_safe_path_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "chroma.sqlite3").write_bytes(b"safe")
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(os, "supports_dir_fd", set())
        with pytest.raises(ValueError, match="Secure Chroma snapshots"):
            _copy_tree_no_follow(source, tmp_path / "snapshot")

    @pytest.mark.asyncio
    async def test_concurrent_async_searches_do_not_share_lock_reentrancy(
        self, tmp_path: Path
    ) -> None:
        """Each blocking search owns its lock in its worker thread."""
        data_dir = tmp_path / "hub-data"
        data_dir.mkdir()
        store = object.__new__(IndexStore)
        store._data_dir = data_dir

        class FakeBackend:
            def search(self, _query: IndexQuery) -> list[IndexResult]:
                return []

        typed_store = cast(Any, store)
        typed_store._vector = FakeBackend()
        typed_store._fts = FakeBackend()
        query = IndexQuery(query_text="pipecat", limit=1)

        results = await asyncio.gather(store.vector_search(query), store.keyword_search(query))
        assert all(result == [] for result in results)

    def test_read_only_open_preserves_persisted_chroma_bytes_and_mtimes(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "hub-data"
        writer = IndexStore(StorageConfig(data_dir=data_dir))
        writer._vector.upsert([_make_record()])
        writer.close()

        chroma_files = [path for path in (data_dir / "chroma").rglob("*") if path.is_file()]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in chroma_files}
        reader = ReadOnlyIndexStore(StorageConfig(data_dir=data_dir))
        reader.close()
        assert {
            path: (path.read_bytes(), path.stat().st_mtime_ns) for path in chroma_files
        } == before

    def test_read_only_snapshot_without_descriptor_relative_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "windows-style"
        writer = IndexStore(StorageConfig(data_dir=data_dir))
        writer._vector.upsert([_make_record()])
        writer.close()
        monkeypatch.setattr(os, "supports_dir_fd", set())
        with pytest.raises(ValueError, match="Secure Chroma snapshots"):
            ReadOnlyIndexStore(StorageConfig(data_dir=data_dir))

    def test_read_only_open_rejects_missing_collection_without_recreating_it(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "hub-data"
        writer = IndexStore(StorageConfig(data_dir=data_dir))
        writer.close()
        import chromadb

        client: Any = chromadb.PersistentClient(path=str(data_dir / "chroma"))
        client.delete_collection("latest")
        client.close()

        with pytest.raises(Exception, match="not found|does not exist"):
            ReadOnlyIndexStore(StorageConfig(data_dir=data_dir))
        check: Any = chromadb.PersistentClient(path=str(data_dir / "chroma"))
        try:
            assert all(collection.name != "latest" for collection in check.list_collections())
        finally:
            check.close()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement fixture")
    def test_read_only_store_rechecks_backend_identity_after_vector_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "hub-data"
        writer = IndexStore(StorageConfig(data_dir=data_dir))
        writer.close()
        outside = tmp_path / "outside"
        outside.mkdir()
        replacement = tmp_path / "chroma-original"
        fake_vector = Mock()

        def race(_path: Path, *, read_only: bool) -> Mock:
            assert read_only is True
            chroma = data_dir / "chroma"
            chroma.rename(replacement)
            chroma.symlink_to(outside, target_is_directory=True)
            return fake_vector

        monkeypatch.setattr(
            "pipecat_context_hub.services.index.store.VectorIndex", Mock(side_effect=race)
        )
        with pytest.raises(ValueError, match="Unsafe index backend path"):
            ReadOnlyIndexStore(StorageConfig(data_dir=data_dir))
        fake_vector.close.assert_called_once()


class TestIndexStore:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> Generator[IndexStore, None, None]:
        config = StorageConfig(data_dir=tmp_path / "hub-data")
        store = IndexStore(config)
        yield store
        store.close()

    @pytest.mark.asyncio
    async def test_upsert_and_vector_search(self, store: IndexStore):
        records = _make_records(3)
        count = await store.upsert(records)
        assert count == 3

        query = IndexQuery(
            query_text="pipecat",
            query_embedding=records[0].embedding,
            limit=10,
        )
        results = await store.vector_search(query)
        assert len(results) == 3
        assert all(r.match_type == "vector" for r in results)

    def test_long_lived_store_reopens_replaced_generation(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "hub-data"
        first = IndexStore(StorageConfig(data_dir=data_dir))
        record = _make_record(chunk_id="old-generation")
        first._vector.upsert([record])
        try:
            reset_script = (
                "import sys; "
                "from pathlib import Path; "
                "from pipecat_context_hub.services.index.store import IndexStore; "
                "from pipecat_context_hub.shared.config import StorageConfig; "
                "s=IndexStore(StorageConfig(data_dir=Path(sys.argv[1]))); "
                "s.reset(); s.close()"
            )
            subprocess.run(
                [sys.executable, "-c", reset_script, str(data_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            query = IndexQuery(
                query_text="pipecat",
                query_embedding=record.embedding,
                limit=10,
            )
            assert first._vector.search(query) == []
        finally:
            first.close()

    @pytest.mark.asyncio
    async def test_public_hybrid_search_keeps_vector_and_fts_in_one_generation_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hybrid request cannot publish between its two backend reads."""
        import pipecat_context_hub.services.index.store as store_module

        data_dir = tmp_path / "hub-data"
        data_dir.mkdir()
        store = object.__new__(IndexStore)
        store._data_dir = data_dir
        store._generation_id = None
        lock_depth = 0
        observations: list[int] = []

        class Lock:
            def __enter__(self):
                nonlocal lock_depth
                lock_depth += 1

            def __exit__(self, *_args):
                nonlocal lock_depth
                lock_depth -= 1

        def fake_lock(_path: Path, *, exclusive: bool):
            assert exclusive is True
            return Lock()

        class Backend:
            def get_metadata(self, _key: str) -> None:
                return None

            def search(self, _query: IndexQuery) -> list[IndexResult]:
                observations.append(lock_depth)
                return []

        typed_store = cast(Any, store)
        typed_store._vector = Backend()
        typed_store._fts = Backend()
        monkeypatch.setattr(store_module, "index_lock", fake_lock)
        result = await store.hybrid_search(IndexQuery(query_text="pipecat", limit=1))
        assert result == ([], [])
        assert observations == [1, 1]

    @pytest.mark.asyncio
    async def test_upsert_and_keyword_search(self, store: IndexStore):
        records = _make_records(3)
        await store.upsert(records)

        query = IndexQuery(query_text="pipecat voice", limit=10)
        results = await store.keyword_search(query)
        assert len(results) == 3
        assert all(r.match_type == "keyword" for r in results)

    @pytest.mark.asyncio
    async def test_delete_by_content_type_both_indexes(self, store: IndexStore):
        records = [
            _make_record(chunk_id="d1", content_type="doc", content="pipecat doc"),
            _make_record(chunk_id="d2", content_type="doc", content="pipecat doc two"),
            _make_record(chunk_id="c1", content_type="code", content="pipecat code"),
        ]
        await store.upsert(records)

        deleted = await store.delete_by_content_type("doc")
        assert deleted == 2

        # Vector search should only find code
        vq = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.content_type == "code"

        # Keyword search should also only find code
        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1
        assert k_results[0].chunk.content_type == "code"

    @pytest.mark.asyncio
    async def test_delete_by_source_both_indexes(self, store: IndexStore):
        records = [
            _make_record(chunk_id="a1", source_url="https://a.com", content="pipecat a"),
            _make_record(chunk_id="b1", source_url="https://b.com", content="pipecat b"),
        ]
        await store.upsert(records)

        deleted = await store.delete_by_source("https://a.com")
        assert deleted == 1

        # Vector search should only find b1
        vq = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.chunk_id == "b1"

        # Keyword search should also only find b1
        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1
        assert k_results[0].chunk.chunk_id == "b1"

    @pytest.mark.asyncio
    async def test_delete_by_repo_both_indexes(self, store: IndexStore):
        records = [
            _make_record(
                chunk_id="r1",
                repo="pipecat-ai/pipecat",
                content_type="code",
                content="pipecat code",
            ),
            _make_record(
                chunk_id="r2",
                repo="pipecat-ai/pipecat",
                content_type="source",
                content="pipecat source",
            ),
            _make_record(
                chunk_id="r3", repo="pipecat-ai/pipecat-examples", content="pipecat examples"
            ),
        ]
        await store.upsert(records)

        deleted = await store.delete_by_repo("pipecat-ai/pipecat")
        assert deleted == 2

        # Vector search should only find pipecat-examples
        vq = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.repo == "pipecat-ai/pipecat-examples"

        # Keyword search should also only find pipecat-examples
        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1
        assert k_results[0].chunk.repo == "pipecat-ai/pipecat-examples"

    @pytest.mark.asyncio
    async def test_upsert_idempotent(self, store: IndexStore):
        record = _make_record(chunk_id="idem-1", content="original pipecat content")
        await store.upsert([record])

        updated = _make_record(chunk_id="idem-1", content="updated pipecat content")
        await store.upsert([updated])

        # Vector should have 1 record
        vq = IndexQuery(
            query_text="test",
            query_embedding=updated.embedding,
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.content == "updated pipecat content"

        # Keyword should have 1 record
        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1
        assert k_results[0].chunk.content == "updated pipecat content"

    @pytest.mark.asyncio
    async def test_clear(self, store: IndexStore):
        """clear() should drop all records from both indexes."""
        records = _make_records(5)
        await store.upsert(records)
        store.clear()

        vq = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        assert await store.vector_search(vq) == []

        kq = IndexQuery(query_text="pipecat", limit=10)
        assert await store.keyword_search(kq) == []

    @pytest.mark.asyncio
    async def test_clear_then_upsert(self, store: IndexStore):
        """Index should be usable after clear()."""
        await store.upsert(_make_records(3))
        store.clear()

        new_records = [_make_record(chunk_id="new-1", content="fresh pipecat data")]
        await store.upsert(new_records)

        vq = IndexQuery(
            query_text="test",
            query_embedding=new_records[0].embedding,
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.chunk_id == "new-1"

        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1

    @pytest.mark.asyncio
    async def test_reset_clears_records_and_metadata(self, store: IndexStore):
        """reset() should wipe both backends and cached metadata."""
        await store.upsert(_make_records(3))
        store.set_metadata("docs:content_hash", "abc123")

        store.reset()

        vq = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        assert await store.vector_search(vq) == []
        assert await store.keyword_search(IndexQuery(query_text="pipecat", limit=10)) == []
        assert store.get_all_metadata() == {}

        new_record = _make_record(chunk_id="reset-store-1", content="fresh pipecat data")
        await store.upsert([new_record])
        v_results = await store.vector_search(
            IndexQuery(
                query_text="test",
                query_embedding=new_record.embedding,
                limit=10,
            )
        )
        assert len(v_results) == 1
        assert v_results[0].chunk.chunk_id == "reset-store-1"

    def test_reset_attempts_fts_even_if_vector_reset_fails(self, store: IndexStore, monkeypatch):
        """reset() should still clear FTS state after a vector reset failure."""
        calls: list[str] = []

        def fail_vector_reset() -> None:
            calls.append("vector")
            raise RuntimeError("vector reset failed")

        def run_fts_reset() -> None:
            calls.append("fts")

        monkeypatch.setattr(store._vector, "reset", fail_vector_reset)
        monkeypatch.setattr(store._fts, "reset", run_fts_reset)

        with pytest.raises(RuntimeError, match="vector reset failed"):
            store.reset()

        assert calls == ["vector"]
        assert (store.data_dir / ".reset-state.json").is_file()

    def test_reset_state_is_removed_only_after_both_backends_succeed(self, store: IndexStore):
        store.reset()
        assert not (store.data_dir / ".reset-state.json").exists()

    def test_satisfies_writer_protocol(self, store: IndexStore):
        """Verify IndexStore has all IndexWriter methods."""
        assert callable(getattr(store, "upsert", None))
        assert callable(getattr(store, "delete_by_source", None))

    def test_satisfies_reader_protocol(self, store: IndexStore):
        """Verify IndexStore has all IndexReader methods."""
        assert callable(getattr(store, "vector_search", None))
        assert callable(getattr(store, "keyword_search", None))

    @pytest.mark.asyncio
    async def test_persistence(self, tmp_path: Path):
        """Verify data survives creating a new IndexStore on the same path."""
        config = StorageConfig(data_dir=tmp_path / "persist-data")
        store1 = IndexStore(config)
        records = _make_records(2)
        await store1.upsert(records)
        store1.close()

        store2 = IndexStore(config)
        vq = IndexQuery(
            query_text="test",
            query_embedding=records[0].embedding,
            limit=10,
        )
        v_results = await store2.vector_search(vq)
        assert len(v_results) == 2

        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store2.keyword_search(kq)
        assert len(k_results) == 2
        store2.close()

    @pytest.mark.asyncio
    async def test_metadata_filters_vector(self, store: IndexStore):
        """Test that metadata filters work in vector search."""
        records = [
            _make_record(
                chunk_id="f1",
                repo="pipecat-ai/pipecat",
                content_type="doc",
                path="/docs/guide.md",
            ),
            _make_record(
                chunk_id="f2",
                repo="pipecat-ai/pipecat-examples",
                content_type="code",
                path="/examples/bot.py",
            ),
        ]
        await store.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"repo": "pipecat-ai/pipecat", "content_type": "doc"},
            limit=10,
        )
        results = await store.vector_search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "f1"

    @pytest.mark.asyncio
    async def test_metadata_filters_keyword(self, store: IndexStore):
        """Test that metadata filters work in keyword search."""
        records = [
            _make_record(
                chunk_id="f1",
                repo="pipecat-ai/pipecat",
                content_type="doc",
                path="/docs/guide.md",
                content="pipecat guide content",
            ),
            _make_record(
                chunk_id="f2",
                repo="pipecat-ai/pipecat-examples",
                content_type="code",
                path="/examples/bot.py",
                content="pipecat example code",
            ),
        ]
        await store.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"repo": "pipecat-ai/pipecat", "content_type": "doc"},
            limit=10,
        )
        results = await store.keyword_search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "f1"


# ---------------------------------------------------------------------------
# Call-graph metadata filter end-to-end tests
# ---------------------------------------------------------------------------


class TestFTSCallGraphFilters:
    """End-to-end tests for yields/calls filters through FTS search."""

    def test_yields_filter_matches(self, tmp_path: Path):
        """FTS search with yields filter returns matching records."""
        fts = FTSIndex(tmp_path / "fts.db")
        rec_with = _make_record(
            chunk_id="m1",
            content="run_tts method yields audio frames",
            content_type="source",
            metadata={"yields": ["TTSAudioRawFrame", "TTSStoppedFrame"], "calls": []},
        )
        rec_without = _make_record(
            chunk_id="m2",
            content="process_frame handles pipeline frames",
            content_type="source",
            metadata={"yields": [], "calls": ["push_frame"]},
        )
        fts.upsert([rec_with, rec_without])
        query = IndexQuery(query_text="frames", filters={"yields": "TTSAudioRawFrame"}, limit=10)
        results = fts.search(query)
        ids = {r.chunk.chunk_id for r in results}
        assert "m1" in ids
        assert "m2" not in ids

    def test_calls_filter_matches(self, tmp_path: Path):
        """FTS search with calls filter returns matching records."""
        fts = FTSIndex(tmp_path / "fts.db")
        rec_with = _make_record(
            chunk_id="m1",
            content="process_frame calls push_frame to forward data",
            content_type="source",
            metadata={"calls": ["push_frame", "transform"], "yields": []},
        )
        rec_without = _make_record(
            chunk_id="m2",
            content="init method sets up the push_frame service",
            content_type="source",
            metadata={"calls": [], "yields": [], "method_name": "push_frame"},
        )
        fts.upsert([rec_with, rec_without])
        query = IndexQuery(query_text="push_frame", filters={"calls": "push_frame"}, limit=10)
        results = fts.search(query)
        ids = {r.chunk.chunk_id for r in results}
        assert "m1" in ids
        # m2 has method_name=push_frame but empty calls — must NOT match
        assert "m2" not in ids

    def test_yields_filter_no_partial_match(self, tmp_path: Path):
        """Yields filter does not match partial substrings."""
        fts = FTSIndex(tmp_path / "fts.db")
        rec = _make_record(
            chunk_id="m1",
            content="yields TTSAudioRawFrame from method body",
            content_type="source",
            metadata={"yields": ["TTSAudioRawFrame"], "calls": []},
        )
        fts.upsert([rec])
        # "Frame" is a substring of "TTSAudioRawFrame" — should NOT match with quoted pattern
        query = IndexQuery(query_text="yields", filters={"yields": "Frame"}, limit=10)
        results = fts.search(query)
        assert len(results) == 0


class TestVectorCallGraphFilters:
    """End-to-end tests for yields/calls post-filters through vector search."""

    def test_yields_post_filter(self, tmp_path: Path):
        """Vector search with yields filter returns only matching records."""
        vi = VectorIndex(tmp_path / "chroma")
        embedding = _random_embedding(42)
        rec_with = _make_record(
            chunk_id="v1",
            content="yields TTSAudioRawFrame",
            content_type="source",
            embedding=embedding,
            metadata={"yields": ["TTSAudioRawFrame"], "calls": []},
        )
        rec_without = _make_record(
            chunk_id="v2",
            content="yields nothing here",
            content_type="source",
            embedding=_random_embedding(43),
            metadata={"yields": [], "calls": ["push_frame"]},
        )
        vi.upsert([rec_with, rec_without])
        query = IndexQuery(
            query_text="audio",
            query_embedding=embedding,
            filters={"yields": "TTSAudioRawFrame"},
            limit=10,
        )
        results = vi.search(query)
        ids = {r.chunk.chunk_id for r in results}
        assert "v1" in ids
        assert "v2" not in ids
        vi.close()

    def test_calls_post_filter(self, tmp_path: Path):
        """Vector search with calls filter returns only matching records."""
        vi = VectorIndex(tmp_path / "chroma")
        embedding = _random_embedding(44)
        rec_with = _make_record(
            chunk_id="v1",
            content="calls push_frame to forward",
            content_type="source",
            embedding=embedding,
            metadata={"calls": ["push_frame"], "yields": []},
        )
        rec_without = _make_record(
            chunk_id="v2",
            content="calls nothing relevant",
            content_type="source",
            embedding=_random_embedding(45),
            metadata={"calls": [], "yields": [], "method_name": "push_frame"},
        )
        vi.upsert([rec_with, rec_without])
        query = IndexQuery(
            query_text="push",
            query_embedding=embedding,
            filters={"calls": "push_frame"},
            limit=10,
        )
        results = vi.search(query)
        ids = {r.chunk.chunk_id for r in results}
        assert "v1" in ids
        assert "v2" not in ids
        vi.close()
