"""Unified index store combining ChromaDB vector search and SQLite FTS5.

Implements both ``IndexWriter`` and ``IndexReader`` protocols by delegating
to the VectorIndex and FTSIndex backends.
"""

from __future__ import annotations

import asyncio
import logging
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.services.index.vector import VectorIndex
from pipecat_context_hub.shared.config import StorageConfig
from pipecat_context_hub.shared.paths import is_reparse_link
from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

logger = logging.getLogger(__name__)


def _backend_path_identities(path: Path, data_dir: Path) -> dict[Path, tuple[int, int]]:
    """Snapshot existing path components so replacements can be detected."""
    identities: dict[Path, tuple[int, int]] = {}
    current = path
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"Unsafe index backend path: {path.name}") from exc
        if info is not None:
            if current != data_dir and is_reparse_link(current):
                raise ValueError(f"Unsafe index backend path: {path.name}")
            identities[current] = (info.st_dev, info.st_ino)
        if current == data_dir or current == current.parent:
            break
        current = current.parent
    return identities


def _verify_backend_path_identities(path: Path, identities: dict[Path, tuple[int, int]]) -> None:
    """Fail closed if a backend path component changed while it was opening."""
    try:
        for component, expected in identities.items():
            info = component.lstat()
            if (info.st_dev, info.st_ino) != expected or is_reparse_link(component):
                raise ValueError(f"Unsafe index backend path: {path.name}")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Unsafe"):
            raise
        raise ValueError(f"Unsafe index backend path: {path.name}") from exc


def _validate_existing_backend_file(path: Path, data_dir: Path) -> None:
    """Refuse unsafe persisted files before any backend can open them.

    SQLite and Chroma both follow symlinks when opening their files.  Keeping
    this check at the writer-capable store boundary prevents refreshes from
    accidentally writing through an operator- or attacker-planted link.
    """
    try:
        path.relative_to(data_dir)
        resolved = path.resolve(strict=True)
        resolved.relative_to(data_dir)
        info = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Unsafe index backend path: {path.name}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise ValueError(f"Unsafe index backend path: {path.name}")


def _validate_configured_backend_path(path: Path, data_dir: Path) -> None:
    """Reject backend paths that would escape ``data_dir`` before creation."""
    try:
        path.resolve(strict=False).relative_to(data_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Unsafe index backend path: {path.name}") from exc


def _validate_backend_directory(path: Path, data_dir: Path) -> None:
    """Reject backend directories that could redirect writes."""
    try:
        if path.is_symlink():
            raise ValueError(f"Unsafe index backend path: {path.name}")
        path.resolve(strict=False).relative_to(data_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Unsafe"):
            raise
        raise ValueError(f"Unsafe index backend path: {path.name}") from exc


class IndexStore:
    """Unified index store satisfying both IndexWriter and IndexReader.

    Writes go to both ChromaDB (vector) and SQLite FTS5 (keyword) backends.
    Reads are dispatched to the appropriate backend based on the search type.
    """

    def __init__(self, config: StorageConfig) -> None:
        data_dir = config.data_dir.expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        config = config.model_copy(update={"data_dir": data_dir})
        self._data_dir = data_dir
        _validate_backend_directory(config.chroma_path, data_dir)
        _validate_configured_backend_path(config.sqlite_path, data_dir)
        for backend_file in (
            config.sqlite_path,
            config.chroma_path / "chroma.sqlite3",
        ):
            if backend_file.exists() or backend_file.is_symlink():
                _validate_existing_backend_file(backend_file, data_dir)
        chroma_identities = _backend_path_identities(config.chroma_path, data_dir)
        sqlite_identities = _backend_path_identities(config.sqlite_path, data_dir)
        self._vector = VectorIndex(config.chroma_path)
        try:
            _verify_backend_path_identities(config.chroma_path, chroma_identities)
            _validate_backend_directory(config.chroma_path, data_dir)
            chroma_file = config.chroma_path / "chroma.sqlite3"
            if chroma_file.exists() or chroma_file.is_symlink():
                _validate_existing_backend_file(chroma_file, data_dir)
            self._fts = FTSIndex(config.sqlite_path)
            _verify_backend_path_identities(config.sqlite_path, sqlite_identities)
            _validate_existing_backend_file(config.sqlite_path, data_dir)
        except Exception:
            # Roll back the already-opened vector backend so callers don't
            # inherit a leaked Chroma handle/lock on a half-constructed store.
            try:
                self._vector.close()
            except Exception:
                logger.exception("Vector backend close failed during rollback")
            raise
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

    def close(self) -> None:
        """Close underlying database connections."""
        try:
            self._vector.close()
        finally:
            self._fts.close()
