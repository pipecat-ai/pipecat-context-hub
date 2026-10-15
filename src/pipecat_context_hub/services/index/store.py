"""Unified index store combining ChromaDB vector search and SQLite FTS5.

Implements both ``IndexWriter`` and ``IndexReader`` protocols by delegating
to the VectorIndex and FTSIndex backends.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pipecat_context_hub.services.index.errors import (
    IndexNotBuiltError,
    MissingVectorCollectionError,
)
from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.services.index.vector import VectorIndex
from pipecat_context_hub.shared.config import StorageConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

logger = logging.getLogger(__name__)


class IndexStore:
    """Unified index store satisfying both IndexWriter and IndexReader.

    Writes go to both ChromaDB (vector) and SQLite FTS5 (keyword) backends.
    Reads are dispatched to the appropriate backend based on the search type.
    """

    #: Set by the open: the keyword half has records but the vector half has
    #: none (it was lost; only a *rebuild* open gets past that, recreating it).
    vector_lost: bool

    def __init__(
        self,
        config: StorageConfig,
        *,
        recover: bool = False,
        create: bool = False,
        rebuild: bool = False,
    ) -> None:
        """Open both backends. *recover*: see :class:`VectorIndex`.

        *create* (``refresh`` only) lets the open create what is missing;
        without it nothing is created. Either way, a vector half that is
        missing or empty while the keyword half has records raises
        :class:`MissingVectorCollectionError` before the vector half is
        touched (the keyword half's only possible earlier write is completing
        an incomplete schema, see :class:`FTSIndex`), unless *rebuild* (the
        caller re-ingests every source, refilling it) lets *create* recreate
        it; :attr:`vector_lost` then reports that it did. Readers (and a
        refresh without ``--force``) therefore never run on an index whose
        vector search would silently come back empty.
        """
        if rebuild and not create:
            raise ValueError("rebuild=True requires create=True")
        if create:
            config.data_dir.mkdir(parents=True, exist_ok=True)
        elif (
            not config.sqlite_path.is_file()
            and not (config.chroma_path / "chroma.sqlite3").is_file()
        ):
            raise IndexNotBuiltError(config.data_dir)
        self._data_dir = config.data_dir
        # The keyword half first: whether it holds records decides whether the
        # vector half may be created.
        self._fts = FTSIndex(config.sqlite_path, create=create)
        vector: VectorIndex | None = None
        try:
            indexed = self._fts.has_records()
            vector = VectorIndex(
                config.chroma_path,
                recover=recover,
                create=create and (rebuild or not indexed),
            )
            # Keyword records with no vectors: only *rebuild* may proceed.
            self.vector_lost = indexed and vector.count() == 0
            if self.vector_lost and not rebuild:
                raise MissingVectorCollectionError(config.chroma_path)
        except BaseException:
            # Close what came up so callers don't inherit a leaked Chroma
            # handle (or SQLite connection) on a half-constructed store.
            for backend in (vector, self._fts):
                if backend is None:
                    continue
                try:
                    backend.close()
                except Exception:
                    logger.exception("Index backend close failed during rollback")
            raise
        self._vector = vector
        logger.info("IndexStore initialized with data_dir=%s", config.data_dir)

    @property
    def data_dir(self) -> Path:
        """Path to the index data directory."""
        return self._data_dir

    def clear(self) -> None:
        """Drop all records from both indexes for a clean rebuild."""
        self._vector.clear()
        self._fts.clear()
        logger.info("IndexStore cleared")

    def compact_vector(self) -> int:
        """Rebuild the vector graph without dead elements. Returns records kept."""
        return self._vector.compact()

    def reset(self) -> None:
        """Delete all persisted search data and cached metadata."""
        vector_error: Exception | None = None
        fts_error: Exception | None = None
        try:
            self._vector.reset()
        except Exception as exc:
            vector_error = exc
        try:
            self._fts.reset()
        except Exception as exc:
            fts_error = exc
        if vector_error is not None and fts_error is not None:
            raise vector_error from fts_error
        if fts_error is not None:
            raise fts_error
        if vector_error is not None:
            raise vector_error
        logger.info("IndexStore reset")

    async def upsert(self, records: list[ChunkedRecord]) -> int:
        """Insert or update records in both indexes. Returns count written."""
        vector_count = self._vector.upsert(records)
        try:
            fts_count = self._fts.upsert(records)
        except Exception:
            logger.exception("FTS upsert failed; vector index may have diverged")
            raise
        if vector_count != fts_count:
            logger.warning("Index divergence: vector=%d fts=%d records", vector_count, fts_count)
        return vector_count

    async def delete_by_content_type(self, content_type: str) -> int:
        """Delete records by content type from both indexes. Returns count deleted."""
        vector_count = self._vector.delete_by_content_type(content_type)
        try:
            fts_count = self._fts.delete_by_content_type(content_type)
        except Exception:
            logger.exception("FTS delete_by_content_type failed; indexes may have diverged")
            raise
        if vector_count != fts_count:
            logger.warning(
                "Delete divergence (content_type=%s): vector=%d fts=%d",
                content_type,
                vector_count,
                fts_count,
            )
        return vector_count

    async def delete_by_repo(self, repo: str) -> int:
        """Delete records by repo from both indexes. Returns count deleted."""
        vector_count = self._vector.delete_by_repo(repo)
        try:
            fts_count = self._fts.delete_by_repo(repo)
        except Exception:
            logger.exception("FTS delete_by_repo failed; indexes may have diverged")
            raise
        if vector_count != fts_count:
            logger.warning(
                "Delete divergence (repo=%s): vector=%d fts=%d",
                repo,
                vector_count,
                fts_count,
            )
        return vector_count

    async def delete_by_source(self, source_url: str) -> int:
        """Delete records by source URL from both indexes. Returns count deleted."""
        vector_count = self._vector.delete_by_source(source_url)
        try:
            fts_count = self._fts.delete_by_source(source_url)
        except Exception:
            logger.exception("FTS delete_by_source failed; indexes may have diverged")
            raise
        if vector_count != fts_count:
            logger.warning(
                "Delete divergence (source_url=%s): vector=%d fts=%d",
                source_url,
                vector_count,
                fts_count,
            )
        return vector_count

    async def vector_search(self, query: IndexQuery) -> list[IndexResult]:
        """Return results ranked by embedding similarity.

        Offloads the synchronous ChromaDB HNSW query to a thread so it
        doesn't block the event loop during concurrent MCP tool calls.
        """
        return await asyncio.to_thread(self._vector.search, query)

    async def keyword_search(self, query: IndexQuery) -> list[IndexResult]:
        """Return results ranked by FTS5 keyword relevance.

        Offloads the synchronous SQLite FTS5 query to a thread so it
        doesn't block the event loop during concurrent MCP tool calls.
        """
        return await asyncio.to_thread(self._fts.search, query)

    def set_metadata(self, key: str, value: str) -> None:
        """Store a key-value pair in persistent index metadata."""
        self._fts.set_metadata(key, value)

    def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key, or None if not found."""
        return self._fts.get_metadata(key)

    def delete_metadata(self, key: str) -> None:
        """Remove a metadata key if it exists."""
        self._fts.delete_metadata(key)

    def set_metadata_batch(self, pairs: dict[str, str], *, delete_keys: Iterable[str] = ()) -> None:
        """Upsert and delete several metadata keys atomically in one commit."""
        self._fts.set_metadata_batch(pairs, delete_keys=delete_keys)

    def get_all_metadata(self) -> dict[str, str]:
        """Return all persistent index metadata as a dict."""
        return self._fts.get_all_metadata()

    def get_counts_by_repo(self) -> dict[str, int]:
        """Return record counts grouped by repo."""
        return self._fts.get_counts_by_repo()

    def get_index_stats(self) -> dict[str, Any]:
        """Return index statistics (counts by type, total, commit SHAs)."""
        return self._fts.get_index_stats()

    def vector_count(self) -> int:
        """Records in the vector half (``get_index_stats`` counts the keyword half)."""
        return self._vector.count()

    def get_vector_page(self, *, offset: int, limit: int, include: list[Any]) -> dict[str, Any]:
        """Raw vector records for export tools: see :meth:`VectorIndex.get_page`."""
        return self._vector.get_page(offset=offset, limit=limit, include=include)

    def close(self) -> None:
        """Close underlying database connections."""
        try:
            self._vector.close()
        finally:
            self._fts.close()
