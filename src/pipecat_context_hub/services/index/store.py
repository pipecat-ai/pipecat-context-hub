"""Unified index store combining ChromaDB vector search and SQLite FTS5.

Implements both ``IndexWriter`` and ``IndexReader`` protocols by delegating
to the VectorIndex and FTSIndex backends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.services.index.hnsw_validation import validate_persisted_hnsw
from pipecat_context_hub.services.index.locking import index_lock
from pipecat_context_hub.services.index.vector import (
    VectorIndex,
    _detect_incompatible_format,
    _remove_tree_no_follow,
)
from pipecat_context_hub.shared.config import StorageConfig
from pipecat_context_hub.shared.paths import is_reparse_link
from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

logger = logging.getLogger(__name__)

_RESET_STATE_FILENAME = ".reset-state.json"


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


def _capture_sqlite_sidecar_identities(
    sqlite_path: Path, identities: dict[Path, tuple[int, int]]
) -> None:
    """Record newly materialised WAL/SHM files before failed-open rollback."""
    for suffix in ("-wal", "-shm"):
        sidecar = sqlite_path.with_name(sqlite_path.name + suffix)
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if is_reparse_link(sidecar) or not stat.S_ISREG(info.st_mode):
            continue
        identities[sidecar] = (info.st_dev, info.st_ino)


def _validate_existing_backend_file(
    path: Path, data_dir: Path, *, allow_empty: bool = False
) -> None:
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
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or (info.st_size == 0 and not allow_empty)
    ):
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
        if is_reparse_link(path):
            raise ValueError(f"Unsafe index backend path: {path.name}")
        path.resolve(strict=False).relative_to(data_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Unsafe"):
            raise
        raise ValueError(f"Unsafe index backend path: {path.name}") from exc


def _create_backend_file(path: Path) -> bool:
    """Create a missing backend file without following a leaf symlink.

    Backend constructors are allowed to initialise these files, but they must
    never be the first operation that resolves an attacker-created path.  A
    securely-created placeholder gives the identity snapshot an object to
    protect and lets the post-open check detect replacement.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = _open_directory_chain(path.parent)
    try:
        if parent_fd == -1:
            # O_EXCL only protects the leaf. Without a handle-relative,
            # no-reparse parent walk, a junction swap can redirect creation
            # outside the published index between validation and this open.
            raise ValueError("Secure backend file creation is unavailable")
        fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
    except FileExistsError:
        return False
    else:
        os.close(fd)
        return True
    finally:
        if parent_fd != -1:
            os.close(parent_fd)


def _open_directory_chain(path: Path) -> int:
    """Open directory components without following symlinks where supported."""
    if os.open not in getattr(os, "supports_dir_fd", set()):
        return -1
    path = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


class IndexStore:
    """Unified index store satisfying both IndexWriter and IndexReader.

    Writes go to both ChromaDB (vector) and SQLite FTS5 (keyword) backends.
    Reads are dispatched to the appropriate backend based on the search type.
    """

    def __init__(self, config: StorageConfig, *, allow_incomplete_reset: bool = False) -> None:
        data_dir = config.data_dir.expanduser().resolve()
        if _reset_state_exists(data_dir) and not allow_incomplete_reset:
            raise ValueError("index reset is incomplete; run refresh to recover it")
        config = config.model_copy(update={"data_dir": data_dir})
        self._data_dir = data_dir
        vector: VectorIndex | None = None
        fts: FTSIndex | None = None
        created_sqlite = False
        created_chroma_db = False
        chroma_preexisting = False
        chroma_identities: dict[Path, tuple[int, int]] = {}
        sqlite_identities: dict[Path, tuple[int, int]] = {}
        # Validation, placeholder creation, backend opening, and rollback all
        # share one generation lock. A second writer cannot publish a newer
        # path between our failed open and cleanup of the placeholders.
        with index_lock(data_dir, exclusive=True, create_target=True):
            try:
                # The first check is only a fast fail. A reset may acquire the
                # lock while this constructor waits, write its marker, and
                # fail before releasing it. Recheck under the lock so a
                # waiting writer never opens a partial generation.
                if _reset_state_exists(data_dir) and not allow_incomplete_reset:
                    raise ValueError("index reset is incomplete; run refresh to recover it")
                _validate_backend_directory(config.chroma_path, data_dir)
                _validate_configured_backend_path(config.sqlite_path, data_dir)
                chroma_preexisting = config.chroma_path.exists()
                config.chroma_path.mkdir(parents=True, exist_ok=True)
                config.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
                _validate_backend_directory(config.chroma_path, data_dir)
                created_sqlite = _create_backend_file(config.sqlite_path)
                created_chroma_db = _create_backend_file(config.chroma_path / "chroma.sqlite3")
                for backend_file in (
                    config.sqlite_path,
                    config.chroma_path / "chroma.sqlite3",
                ):
                    if backend_file.exists() or backend_file.is_symlink():
                        if backend_file == config.sqlite_path and created_sqlite:
                            continue
                        if (
                            backend_file == config.chroma_path / "chroma.sqlite3"
                            and created_chroma_db
                        ):
                            continue
                        _validate_existing_backend_file(backend_file, data_dir)
                chroma_identities = _backend_path_identities(config.chroma_path, data_dir)
                sqlite_identities = _backend_path_identities(config.sqlite_path, data_dir)
                vector_kwargs: dict[str, Any] = {}
                if isinstance(VectorIndex, type):
                    vector_kwargs["lock_held"] = True
                try:
                    vector = VectorIndex(config.chroma_path, **vector_kwargs)
                except TypeError as exc:
                    # Tests/embedders may replace the constructor with the
                    # pre-lock-held signature; do not mask their real error.
                    if "lock_held" not in str(exc):
                        raise
                    vector = VectorIndex(config.chroma_path)
                self._vector = vector
                _verify_backend_path_identities(config.chroma_path, chroma_identities)
                _validate_backend_directory(config.chroma_path, data_dir)
                chroma_file = config.chroma_path / "chroma.sqlite3"
                if chroma_file.exists() or chroma_file.is_symlink():
                    _validate_existing_backend_file(
                        chroma_file, data_dir, allow_empty=created_chroma_db
                    )
                fts = FTSIndex(config.sqlite_path, lock_held=True)
                self._fts = fts
                self._generation_id = fts.get_metadata("index_generation")
                _verify_backend_path_identities(config.sqlite_path, sqlite_identities)
                _validate_existing_backend_file(config.sqlite_path, data_dir)
            except Exception:
                # FTSIndex can fail after enabling WAL but before returning an
                # object whose close() would remove the aliases. Capture the
                # sidecar identities while they still exist so rollback can
                # unlink only this failed generation.
                if fts is None and created_sqlite:
                    _capture_sqlite_sidecar_identities(config.sqlite_path, sqlite_identities)
                # Roll back the already-opened vector backend so callers don't
                # inherit a leaked Chroma handle/lock on a half-constructed store.
                if vector is not None:
                    try:
                        vector.close()
                    except Exception:
                        logger.exception("Vector backend close failed during rollback")
                if fts is not None:
                    try:
                        fts.close()
                    except Exception:
                        logger.exception("FTS backend close failed during rollback")
                _cleanup_created_backend_state(
                    config,
                    created_sqlite=created_sqlite,
                    created_chroma_db=created_chroma_db,
                    chroma_preexisting=chroma_preexisting,
                    chroma_identities=chroma_identities,
                    sqlite_identities=sqlite_identities,
                )
                raise
        logger.info("IndexStore initialized with data_dir=%s", config.data_dir)

    @property
    def data_dir(self) -> Path:
        """Path to the index data directory."""
        return self._data_dir

    def clear(self) -> None:
        """Drop all records from both indexes for a clean rebuild."""
        with index_lock(self._data_dir, exclusive=True):
            self._vector.clear()
            self._fts.clear()
        logger.info("IndexStore cleared")

    def reset(self) -> None:
        """Delete all persisted search data and cached metadata."""
        # Keep one generation lock across both backends. The nested backend
        # locks are reentrant for this synchronous owner, while readers in
        # worker threads cannot observe a vector/FTS hybrid reset.
        with index_lock(self._data_dir, exclusive=True):
            _write_reset_state(self._data_dir, {"vector_reset": False, "fts_reset": False})
            vector_error: Exception | None = None
            fts_error: Exception | None = None
            try:
                self._vector.reset()
                _write_reset_state(self._data_dir, {"vector_reset": True, "fts_reset": False})
            except Exception as exc:
                vector_error = exc
            if vector_error is None:
                try:
                    self._fts.reset()
                    _write_reset_state(
                        self._data_dir,
                        {"vector_reset": True, "fts_reset": True},
                    )
                except Exception as exc:
                    fts_error = exc
            if vector_error is not None and fts_error is not None:
                raise vector_error from fts_error
            if fts_error is not None:
                raise fts_error
            if vector_error is not None:
                raise vector_error
            marker = self._data_dir / _RESET_STATE_FILENAME
            if marker.is_symlink() or (marker.exists() and not marker.is_file()):
                raise ValueError("Unsafe reset state path")
            marker.unlink(missing_ok=True)
        logger.info("IndexStore reset")

    async def upsert(self, records: list[ChunkedRecord]) -> int:
        """Insert or update records in both indexes. Returns count written."""
        with index_lock(self._data_dir, exclusive=True):
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
        with index_lock(self._data_dir, exclusive=True):
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
        with index_lock(self._data_dir, exclusive=True):
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
        with index_lock(self._data_dir, exclusive=True):
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
        return await asyncio.to_thread(self._locked_vector_search, query)

    async def keyword_search(self, query: IndexQuery) -> list[IndexResult]:
        """Return results ranked by FTS5 keyword relevance.

        Offloads the synchronous SQLite FTS5 query to a thread so it
        doesn't block the event loop during concurrent MCP tool calls.
        """
        return await asyncio.to_thread(self._locked_keyword_search, query)

    async def hybrid_search(self, query: IndexQuery) -> tuple[list[IndexResult], list[IndexResult]]:
        """Read vector and FTS results from one published generation.

        Hybrid retrieval otherwise issues two independently locked reads; a
        refresh can publish between those calls and produce a mixed response.
        Keep both backend reads under one shared generation lock.
        """
        return await asyncio.to_thread(self._locked_hybrid_search, query)

    async def hybrid_search_many(
        self, queries: list[IndexQuery]
    ) -> list[tuple[list[IndexResult], list[IndexResult]]]:
        """Read several hybrid queries from one published generation."""
        return await asyncio.to_thread(self._locked_hybrid_search_many, queries)

    def _locked_vector_search(self, query: IndexQuery) -> list[IndexResult]:
        """Run one synchronous vector read under its own worker-thread lock."""
        # Reopening a writer-oriented native backend may perform migrations;
        # use the exclusive generation lock so in-place refresh detection and
        # reopen stay one safe publication window.
        with index_lock(self._data_dir, exclusive=True):
            self._sync_generation_locked()
            return self._vector.search(query)

    def _locked_keyword_search(self, query: IndexQuery) -> list[IndexResult]:
        """Run one synchronous FTS read under its own worker-thread lock."""
        with index_lock(self._data_dir, exclusive=True):
            self._sync_generation_locked()
            return self._fts.search(query)

    def _locked_hybrid_search(
        self, query: IndexQuery
    ) -> tuple[list[IndexResult], list[IndexResult]]:
        with index_lock(self._data_dir, exclusive=True):
            self._sync_generation_locked()
            return self._vector.search(query), self._fts.search(query)

    def _locked_hybrid_search_many(
        self, queries: list[IndexQuery]
    ) -> list[tuple[list[IndexResult], list[IndexResult]]]:
        with index_lock(self._data_dir, exclusive=True):
            self._sync_generation_locked()
            return [(self._vector.search(query), self._fts.search(query)) for query in queries]

    def _sync_generation_locked(self) -> None:
        """Reopen both native readers when refresh publishes in place."""
        if not hasattr(self._fts, "get_metadata"):
            return
        # A reset can replace metadata.db while this process remains alive.
        # Reopen before reading the marker so the old connection cannot make a
        # replaced generation look current.
        metadata_replaced = False
        reopen_if_replaced = getattr(self._fts, "reopen_if_replaced", None)
        if callable(reopen_if_replaced):
            metadata_replaced = bool(reopen_if_replaced())
        published = self._fts.get_metadata("index_generation")
        if published == self._generation_id and not metadata_replaced:
            return
        self._vector.reopen_if_replaced(lock_held=True, force=True)
        self._fts.reopen()
        self._generation_id = published

    def set_metadata(self, key: str, value: str) -> None:
        """Store a key-value pair in persistent index metadata."""
        with index_lock(self._data_dir, exclusive=True):
            self._fts.set_metadata(key, value)

    def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key, or None if not found."""
        with index_lock(self._data_dir, exclusive=True):
            self._sync_generation_locked()
            return self._fts.get_metadata(key)

    def delete_metadata(self, key: str) -> None:
        """Remove a metadata key if it exists."""
        with index_lock(self._data_dir, exclusive=True):
            self._fts.delete_metadata(key)

    def set_metadata_batch(self, pairs: dict[str, str], *, delete_keys: Iterable[str] = ()) -> None:
        """Upsert and delete several metadata keys atomically in one commit."""
        with index_lock(self._data_dir, exclusive=True):
            self._fts.set_metadata_batch(pairs, delete_keys=delete_keys)

    def get_all_metadata(self) -> dict[str, str]:
        """Return all persistent index metadata as a dict."""
        with index_lock(self._data_dir, exclusive=True):
            self._sync_generation_locked()
            return self._fts.get_all_metadata()

    def get_counts_by_repo(self) -> dict[str, int]:
        """Return record counts grouped by repo."""
        with index_lock(self._data_dir, exclusive=True):
            self._sync_generation_locked()
            return self._fts.get_counts_by_repo()

    def get_index_stats(self) -> dict[str, Any]:
        """Return index statistics (counts by type, total, commit SHAs)."""
        with index_lock(self._data_dir, exclusive=True):
            self._sync_generation_locked()
            return self._fts.get_index_stats()

    def get_status_snapshot(self) -> tuple[dict[str, Any], dict[str, str]]:
        """Read status statistics and provenance under one generation lock."""
        with index_lock(self._data_dir, exclusive=True):
            self._sync_generation_locked()
            return self._fts.get_index_stats(), self._fts.get_all_metadata()

    def close(self) -> None:
        """Close underlying database connections."""
        try:
            self._vector.close()
        finally:
            self._fts.close()


def _cleanup_created_backend_state(
    config: StorageConfig,
    *,
    created_sqlite: bool,
    created_chroma_db: bool,
    chroma_preexisting: bool,
    chroma_identities: dict[Path, tuple[int, int]],
    sqlite_identities: dict[Path, tuple[int, int]],
) -> None:
    """Remove backend state created by a failed writer-store initialization."""
    # A failed identity check may mean a backend root was swapped to a
    # symlink.  Refuse all path-based cleanup in that case; otherwise unlinking
    # a child would follow the swapped directory into an external location.
    try:
        _verify_backend_path_identities(config.chroma_path, chroma_identities)
        _verify_backend_path_identities(config.sqlite_path, sqlite_identities)
    except ValueError:
        logger.warning("Skipping failed-index cleanup after backend path replacement")
        return

    def unlink_backend_file(path: Path, identities: dict[Path, tuple[int, int]]) -> None:
        """Unlink a created leaf through its stable parent descriptor."""
        if os.open not in getattr(os, "supports_dir_fd", set()):
            raise ValueError("Secure backend cleanup is unavailable")
        expected = identities.get(path)
        if expected is None:
            raise ValueError(f"Missing identity for backend cleanup: {path.name}")
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != expected:
                raise ValueError(f"Unsafe index backend path: {path.name}")
            os.unlink(path.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    if created_sqlite:
        for path in (
            config.sqlite_path,
            config.sqlite_path.with_name(config.sqlite_path.name + "-wal"),
            config.sqlite_path.with_name(config.sqlite_path.name + "-shm"),
        ):
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                if path.exists() and not is_reparse_link(path):
                    unlink_backend_file(path, sqlite_identities)
    if created_chroma_db:
        path = config.chroma_path / "chroma.sqlite3"
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            if path.exists() and not is_reparse_link(path):
                unlink_backend_file(path, chroma_identities)
    if not chroma_preexisting:
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            if config.chroma_path.is_dir() and not is_reparse_link(config.chroma_path):
                parent_identity = chroma_identities.get(config.chroma_path.parent)
                root_identity = chroma_identities.get(config.chroma_path)
                _remove_tree_no_follow(
                    config.chroma_path,
                    expected_parent=parent_identity,
                    expected_root=root_identity,
                )


def _existing_index_config(
    config: StorageConfig, *, include_vector: bool = True, allow_wal: bool = False
) -> StorageConfig:
    """Validate an already-built index without creating or opening backends."""
    data_dir = config.data_dir.expanduser().resolve(strict=True)
    if not data_dir.is_dir():
        raise FileNotFoundError(data_dir)
    config = config.model_copy(update={"data_dir": data_dir})
    if _reset_state_exists(data_dir):
        raise ValueError("index reset is incomplete; run refresh to recover it")
    _validate_backend_directory(config.chroma_path, data_dir)
    chroma_db = config.chroma_path / "chroma.sqlite3"
    _validate_existing_backend_file(chroma_db, data_dir)
    # Metadata-only commands still need to reject a legacy Chroma format. They
    # do not construct/snapshot VectorIndex, but reporting an index healthy
    # here would defer the failure to the next semantic query.
    _detect_incompatible_format(config.chroma_path)
    # The bounded HNSW header/size check is cheap and must also protect the
    # status front door; it does not construct Chroma or copy the tree.
    validate_persisted_hnsw(config.chroma_path, "latest", allow_wal=allow_wal)
    _validate_existing_backend_file(config.sqlite_path, data_dir)
    return config


def _write_reset_state(data_dir: Path, state: dict[str, bool]) -> None:
    """Persist reset progress without ever following a marker symlink."""
    marker = data_dir / _RESET_STATE_FILENAME
    if marker.is_symlink():
        raise ValueError("Unsafe reset state path")
    fd, temporary_name = tempfile.mkstemp(prefix=".reset-state-", dir=data_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def _reset_state_exists(data_dir: Path) -> bool:
    marker = data_dir / _RESET_STATE_FILENAME
    return marker.exists() or marker.is_symlink()


class ReadOnlyIndexStore:
    """Reader-only facade over an existing persisted index.

    Unlike :class:`IndexStore`, this boundary never creates directories/files,
    exposes no mutation methods, and opens SQLite with ``mode=ro``. The Chroma
    backend remains behind the same reader-only API so query callers cannot
    accidentally reach refresh/reset operations.
    """

    def __init__(self, config: StorageConfig, *, include_vector: bool = True) -> None:
        data_dir = config.data_dir.expanduser().resolve(strict=True)
        lock_context = index_lock(data_dir, exclusive=False, create=False)
        lock_context.__enter__()
        self._lock_context = lock_context
        try:
            config = _existing_index_config(config, include_vector=include_vector, allow_wal=True)
            self._data_dir = config.data_dir
            chroma_identities = _backend_path_identities(config.chroma_path, self._data_dir)
            sqlite_identities = _backend_path_identities(config.sqlite_path, self._data_dir)
        except BaseException:
            lock_context.__exit__(*sys.exc_info())
            raise
        vector: VectorIndex | None = None
        fts: FTSIndex | None = None
        try:
            if include_vector:
                # Keep the outer legacy/sibling lock across the full reader
                # lifetime.  The concrete backend can reuse that lock without
                # opening a second descriptor; keep the mock-friendly call
                # shape for tests and embedders that replace VectorIndex.
                vector_kwargs: dict[str, Any] = {"read_only": True}
                if isinstance(VectorIndex, type):
                    vector_kwargs["lock_held"] = True
                vector = VectorIndex(config.chroma_path, **vector_kwargs)
                _verify_backend_path_identities(config.chroma_path, chroma_identities)
            fts = FTSIndex(config.sqlite_path, read_only=True, lock_held=True)
            _verify_backend_path_identities(config.sqlite_path, sqlite_identities)
            # Metadata-only readers intentionally leave this unset; keep the
            # private field dynamically typed because callers/tests inspect the
            # full reader's Chroma handle for byte-preservation checks.
            self._vector: Any = vector
            self._fts = fts
        except Exception:
            if vector is not None:
                vector.close()
            # FTSIndex.close reacquires the generation lock after construction;
            # release the reader's lifetime lock before closing it so a shared
            # lock is never upgraded in place.
            self._lock_context.__exit__(*sys.exc_info())
            if fts is not None:
                fts.close()
            raise

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    async def vector_search(self, query: IndexQuery) -> list[IndexResult]:
        if self._vector is None:
            raise RuntimeError("vector search is unavailable on a metadata-only index reader")
        return await asyncio.to_thread(self._vector.search, query)

    async def keyword_search(self, query: IndexQuery) -> list[IndexResult]:
        return await asyncio.to_thread(self._fts.search, query)

    async def hybrid_search(self, query: IndexQuery) -> tuple[list[IndexResult], list[IndexResult]]:
        if self._vector is None:
            raise RuntimeError("hybrid search is unavailable on a metadata-only index reader")
        return await asyncio.to_thread(
            lambda: (self._vector.search(query), self._fts.search(query))
        )

    async def hybrid_search_many(
        self, queries: list[IndexQuery]
    ) -> list[tuple[list[IndexResult], list[IndexResult]]]:
        if self._vector is None:
            raise RuntimeError("hybrid search is unavailable on a metadata-only index reader")
        return await asyncio.to_thread(
            lambda: [(self._vector.search(query), self._fts.search(query)) for query in queries]
        )

    def get_all_metadata(self) -> dict[str, str]:
        return self._fts.get_all_metadata()

    def get_index_stats(self) -> dict[str, Any]:
        return self._fts.get_index_stats()

    def get_status_snapshot(self) -> tuple[dict[str, Any], dict[str, str]]:
        # ReadOnlyIndexStore holds its shared generation lock for its entire
        # lifetime, so both reads are already one immutable snapshot.
        return self._fts.get_index_stats(), self._fts.get_all_metadata()

    def close(self) -> None:
        try:
            if self._vector is not None:
                self._vector.close()
        finally:
            try:
                # The reader lifetime lock is shared. Release it before FTS
                # close reacquires the exclusive generation lock to unlink
                # published sidecars safely.
                self._lock_context.__exit__(None, None, None)
            finally:
                self._fts.close()
