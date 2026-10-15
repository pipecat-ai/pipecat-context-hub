"""Tests for the index store: VectorIndex, FTSIndex, and unified IndexStore."""

from __future__ import annotations

import random
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import Mock, patch

import pytest

from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.services.index.vector import _CHROMA_BATCH_SIZE, VectorIndex
from pipecat_context_hub.shared.config import StorageConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

EMBEDDING_DIM = 384


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
        indexed_at=datetime.now(tz=UTC),
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
        index = VectorIndex(tmp_path / "chroma", create=True)
        yield index
        index.close()

    def test_disables_chroma_telemetry(self, tmp_path: Path):
        mock_client = Mock()
        mock_client.list_collections.return_value = []
        mock_client.get_or_create_collection.return_value = Mock()
        mock_client._identifier = "mock-client"

        with patch(
            "pipecat_context_hub.services.index.vector.chromadb.PersistentClient",
            return_value=mock_client,
        ) as persistent_client:
            index = VectorIndex(tmp_path / "chroma", create=True)
            index.close()

        settings = persistent_client.call_args.kwargs["settings"]
        assert settings.anonymized_telemetry is False
        assert settings.chroma_product_telemetry_impl == (
            "pipecat_context_hub.services.index.vector.NoOpProductTelemetryClient"
        )

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
        idx1 = VectorIndex(chroma_path, create=True)
        records = _make_records(2)
        idx1.upsert(records)

        # Create a new index pointing at the same directory
        idx2 = VectorIndex(chroma_path, create=True)
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
        idx1 = VectorIndex(chroma_path, create=True)
        records = _make_records(3)
        known_id = records[0].chunk_id
        idx1.upsert(records)
        idx1.close()

        # Re-open: _open_client calls get_or_create_collection with cosine.
        idx2 = VectorIndex(chroma_path, create=True)
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
        index = VectorIndex(tmp_path / "chroma", create=True)
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
        index = FTSIndex(tmp_path / "metadata.db", create=True)
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
        idx1 = FTSIndex(db_path, create=True)
        idx1.upsert([_make_record(content="pipecat persists")])
        idx1.close()

        idx2 = FTSIndex(db_path, create=True)
        query = IndexQuery(query_text="pipecat", limit=10)
        results = idx2.search(query)
        assert len(results) == 1
        idx2.close()

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
    """If Vector construction fails after FTS opens, the FTS handle must be
    closed before the exception propagates."""

    def test_vector_failure_closes_fts(self, tmp_path: Path, monkeypatch):
        from unittest.mock import MagicMock

        from pipecat_context_hub.services.index import store as store_mod

        fake_fts = MagicMock()
        fake_fts.has_records.return_value = False
        monkeypatch.setattr(store_mod, "FTSIndex", MagicMock(return_value=fake_fts))
        monkeypatch.setattr(
            store_mod, "VectorIndex", MagicMock(side_effect=RuntimeError("chroma boom"))
        )

        with pytest.raises(RuntimeError, match="chroma boom"):
            IndexStore(StorageConfig(data_dir=tmp_path / "hub-data"), create=True)
        fake_fts.close.assert_called_once()


def _lose_vector_half(config: StorageConfig, *, records: int = 3, empty_latest: bool) -> None:
    """Build an index with *records*, then lose its ``latest`` collection.

    *empty_latest* leaves an empty ``latest`` in its place (what a refresh
    that recreated it before refusing, or ``get_or_create``, used to leave).
    """
    import asyncio

    from pipecat_context_hub.services.index.vector import COLLECTION_NAME

    store = IndexStore(config, create=True)
    asyncio.run(store.upsert(_make_records(records)))
    store._vector._client.delete_collection(COLLECTION_NAME)
    if empty_latest:
        store._vector._client.create_collection(COLLECTION_NAME)
    store.close()


def _collection_names(config: StorageConfig) -> list[str]:
    from pipecat_context_hub.services.index.chroma_schema import query_chroma_sqlite

    rows = query_chroma_sqlite(
        config.chroma_path, "SELECT name FROM collections ORDER BY name", immutable=True
    )
    return [row[0] for row in rows or ()]


class TestLostVectorHalf:
    """Regression (cli.py:105/115/123, errors.py:131): refresh opened with
    create=True, so a lost ``latest`` was recreated empty *before* the CLI
    compared counts and refused; after that refusal readers (which only
    counted the keyword half) served with vector search silently empty. The
    store now refuses a missing or empty vector half beside a populated
    keyword half, for readers and for a refresh without ``rebuild``, before
    anything is written."""

    def test_refused_refresh_open_writes_nothing(self, tmp_path: Path) -> None:
        """I1: no empty ``latest``, no schema change."""
        from pipecat_context_hub.services.index.errors import MissingVectorCollectionError

        config = StorageConfig(data_dir=tmp_path / "hub-data")
        _lose_vector_half(config, empty_latest=False)
        before = _collection_names(config)

        with pytest.raises(MissingVectorCollectionError, match="refresh --force"):
            IndexStore(config, recover=True, create=True)  # refresh without --force
        assert _collection_names(config) == before
        assert "latest" not in before

    @pytest.mark.parametrize("empty_latest", [False, True])
    def test_reader_refuses_a_lost_vector_half(self, tmp_path: Path, empty_latest: bool) -> None:
        """I2: however the state arose, including a pre-existing empty ``latest``."""
        from pipecat_context_hub.services.index.errors import MissingVectorCollectionError
        from pipecat_context_hub.services.index.reopening import open_ready_index_store
        from pipecat_context_hub.shared.interfaces import IndexUnavailableError

        config = StorageConfig(data_dir=tmp_path / "hub-data")
        _lose_vector_half(config, empty_latest=empty_latest)

        with pytest.raises(MissingVectorCollectionError, match="refresh --force"):
            IndexStore(config)
        with pytest.raises(IndexUnavailableError, match="refresh --force"):
            open_ready_index_store(config)

    def test_reader_with_a_missing_vector_database_says_force(self, tmp_path: Path) -> None:
        """I5: with keyword records, plain ``refresh`` is refused, so a missing
        chroma.sqlite3 must not prescribe it."""
        import shutil

        from pipecat_context_hub.services.index.errors import MissingVectorCollectionError

        config = StorageConfig(data_dir=tmp_path / "hub-data")
        _lose_vector_half(config, empty_latest=False)
        shutil.rmtree(config.chroma_path)

        with pytest.raises(MissingVectorCollectionError, match="refresh --force"):
            IndexStore(config)
        with pytest.raises(MissingVectorCollectionError):
            IndexStore(config, recover=True, create=True)
        assert not config.chroma_path.exists()

    @pytest.mark.parametrize("empty_latest", [False, True])
    def test_rebuild_recreates_it(self, tmp_path: Path, empty_latest: bool) -> None:
        """I4: ``refresh --force`` still opens (and refills) it."""
        config = StorageConfig(data_dir=tmp_path / "hub-data")
        _lose_vector_half(config, empty_latest=empty_latest)

        store = IndexStore(config, recover=True, create=True, rebuild=True)
        try:
            assert store.vector_count() == 0
            assert store.vector_empty
        finally:
            store.close()
        assert _collection_names(config) == ["latest"]

    def test_rebuild_without_create_is_rejected(self, tmp_path: Path) -> None:
        """Regression (store.py:34): ``rebuild=True`` without ``create`` was a
        silent no-op (the vector half may only be recreated with *create*)."""
        data_dir = tmp_path / "hub-data"
        with pytest.raises(ValueError, match="rebuild=True requires create=True"):
            IndexStore(StorageConfig(data_dir=data_dir), rebuild=True)
        assert not data_dir.exists()

    def test_first_run_and_healthy_reopen_are_unchanged(self, tmp_path: Path) -> None:
        """I4: an empty data dir and a healthy index open as before."""
        import asyncio

        config = StorageConfig(data_dir=tmp_path / "hub-data")
        store = IndexStore(config, recover=True, create=True)  # first run
        assert store.vector_empty  # created empty: no skip marker may vouch for it
        asyncio.run(store.upsert(_make_records(3)))
        store.close()
        for kwargs in ({}, {"recover": True, "create": True}):
            store = IndexStore(config, **kwargs)
            try:
                assert store.vector_count() == 3
                assert not store.vector_empty
            finally:
                store.close()


class TestIndexStoreReaderCreatesNothing:
    """Regression (store.py:38, vector.py:353): ``create=False`` reached only
    the vector half, so a reader open still created ``index.db`` and its
    schema; and ``create`` defaulted to True, so a reader that forgot to opt
    out (scripts/smoke_check_framework_version.py) created the whole index."""

    def test_default_open_of_a_missing_index_creates_nothing(self, tmp_path: Path) -> None:
        from pipecat_context_hub.services.index.errors import IndexNotBuiltError

        data_dir = tmp_path / "hub-data"
        with pytest.raises(IndexNotBuiltError, match=r"is empty \(0 records\)"):
            IndexStore(StorageConfig(data_dir=data_dir))
        assert not data_dir.exists()

    def test_reader_open_without_a_keyword_index_does_not_create_one(self, tmp_path: Path) -> None:
        from pipecat_context_hub.services.index.errors import MissingKeywordIndexError

        config = StorageConfig(data_dir=tmp_path / "hub-data")
        IndexStore(config, create=True).close()
        config.sqlite_path.unlink()
        with pytest.raises(MissingKeywordIndexError, match="refresh --force"):
            IndexStore(config, create=False)
        assert not config.sqlite_path.exists()
        with pytest.raises(MissingKeywordIndexError):
            FTSIndex(config.sqlite_path)  # the default is a reader too
        assert not config.sqlite_path.exists()

    def test_reader_open_of_an_uninitialized_keyword_index_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        """Regression (fts.py:101): a 0-byte ``index.db`` passed the reader's
        is_file() guard, and the open then set WAL mode and wrote the schema."""
        from pipecat_context_hub.services.index.errors import MissingKeywordIndexError

        db_path = tmp_path / "index.db"
        db_path.touch()
        with pytest.raises(MissingKeywordIndexError):
            FTSIndex(db_path)
        assert db_path.stat().st_size == 0
        assert not db_path.with_name("index.db-wal").exists()
        FTSIndex(db_path, create=True).close()  # a writer still initializes it
        FTSIndex(db_path).close()

    def test_reader_open_of_an_initialized_keyword_index_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        """Regression (fts.py:115): ``create`` gated only existence, so every
        reader open still ran the WAL pragma, the schema DDL and a commit."""
        import sqlite3

        db_path = tmp_path / "index.db"
        FTSIndex(db_path, create=True).close()
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()

        FTSIndex(db_path).close()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        conn.close()

    def test_partially_initialized_keyword_index(self, tmp_path: Path) -> None:
        """Regression (fts.py:103): a database with ``chunks`` rows but no FTS
        table passed the reader's check, and the open then created an empty
        FTS table, so keyword search silently found nothing. A reader now
        refuses it; refresh completes the schema and rebuilds the FTS table."""
        import sqlite3

        from pipecat_context_hub.services.index.errors import MissingKeywordIndexError

        db_path = tmp_path / "index.db"
        fts = FTSIndex(db_path, create=True)
        records = _make_records(3)
        fts.upsert(records)
        fts.close()
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TABLE chunks_fts")
        conn.close()

        with pytest.raises(MissingKeywordIndexError):
            FTSIndex(db_path)
        fts = FTSIndex(db_path, create=True)
        try:
            assert len(fts.search(IndexQuery(query_text="pipecat", limit=10))) == 3
        finally:
            fts.close()

    def test_failed_fts_rebuild_leaves_the_schema_incomplete(self, tmp_path: Path) -> None:
        """Regression (fts.py:123): completing the schema committed before the
        FTS rebuild ran, so a rebuild that failed left every schema object in
        place; the next open skipped the rebuild and readers accepted a
        ``chunks`` table whose FTS index was empty. Both now commit together.
        The rebuild is made to fail by a ``chunks`` table without ``content``."""
        import sqlite3

        db_path = tmp_path / "index.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, body TEXT)")
            conn.execute("INSERT INTO chunks VALUES ('a', 'pipecat')")
        conn.close()

        from pipecat_context_hub.services.index.errors import CorruptKeywordIndexError

        with pytest.raises(CorruptKeywordIndexError, match="content") as exc_info:
            FTSIndex(db_path, create=True)
        assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
        assert "refresh --force --reset-index" in str(exc_info.value)
        with sqlite3.connect(db_path) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        assert names == {"chunks", "sqlite_autoindex_chunks_1"}  # nothing half-committed

    def test_not_a_database_is_typed_unreadable(self, tmp_path: Path) -> None:
        """Regression (cli.py:118): the CLI classified raw sqlite3 errors
        itself; the keyword index now raises one typed error for both."""
        from pipecat_context_hub.services.index.errors import CorruptKeywordIndexError

        db_path = tmp_path / "index.db"
        db_path.write_bytes(b"not a database" * 512)
        for create in (False, True):
            with pytest.raises(CorruptKeywordIndexError, match="not a database"):
                FTSIndex(db_path, create=create)

    def test_a_locked_database_is_not_called_unreadable(self, tmp_path: Path) -> None:
        """A failure of the moment (here SQLITE_BUSY) is not told to reset."""
        import sqlite3

        db_path = tmp_path / "index.db"
        FTSIndex(db_path, create=True).close()
        connect = sqlite3.connect
        holder = connect(db_path)
        holder.execute("PRAGMA journal_mode=DELETE")  # so EXCLUSIVE blocks readers
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with (
                patch("sqlite3.connect", lambda path, **_: connect(path, timeout=0)),
                pytest.raises(sqlite3.OperationalError, match="locked"),
            ):
                FTSIndex(db_path)
        finally:
            holder.rollback()
            holder.close()

    @pytest.mark.parametrize("transient", [True, False])
    def test_reader_open_advises_reset_only_for_a_damaged_index(
        self, tmp_path: Path, transient: bool
    ) -> None:
        """Regression (reopening.py:80): every untyped open failure, including
        a locked keyword database, was told to delete the index with
        ``--reset-index``. A failure of the moment now gets a retry hint; an
        unclassified one keeps the reset advice."""
        import sqlite3

        from pipecat_context_hub.services.index.reopening import open_ready_index_store
        from pipecat_context_hub.shared.interfaces import IndexUnavailableError

        if transient:
            exc: Exception = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
        else:
            exc = RuntimeError("something unexpected")
        config = StorageConfig(data_dir=tmp_path / "hub-data")
        IndexStore(config, create=True).close()  # a built index
        with (
            patch("pipecat_context_hub.services.index.store.FTSIndex", side_effect=exc),
            pytest.raises(IndexUnavailableError) as exc_info,
        ):
            open_ready_index_store(config)
        message = str(exc_info.value)
        assert ("--reset-index" in message) is not transient
        assert ("retry" in message) is transient


class TestIndexStore:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> Generator[IndexStore, None, None]:
        config = StorageConfig(data_dir=tmp_path / "hub-data")
        store = IndexStore(config, create=True)
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

        assert calls == ["vector", "fts"]

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
        store1 = IndexStore(config, create=True)
        records = _make_records(2)
        await store1.upsert(records)
        store1.close()

        store2 = IndexStore(config, create=True)
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
        fts = FTSIndex(tmp_path / "fts.db", create=True)
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
        fts = FTSIndex(tmp_path / "fts.db", create=True)
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
        fts = FTSIndex(tmp_path / "fts.db", create=True)
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
        vi = VectorIndex(tmp_path / "chroma", create=True)
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
        vi = VectorIndex(tmp_path / "chroma", create=True)
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
