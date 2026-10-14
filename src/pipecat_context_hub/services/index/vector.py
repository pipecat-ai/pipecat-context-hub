"""ChromaDB-backed vector index for embedding similarity search.

Wraps ChromaDB's PersistentClient to store ChunkedRecord embeddings and
support filtered vector queries. Uses collection name ``latest`` as the
single namespace for v0.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent
from overrides import override

from pipecat_context_hub.services.index.errors import IncompatibleIndexFormatError
from pipecat_context_hub.services.index.hnsw_validation import validate_persisted_hnsw
from pipecat_context_hub.services.index.locking import index_lock
from pipecat_context_hub.services.index.safe_sqlite import connect_readonly
from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult
from pipecat_context_hub.shared.paths import is_reparse_link

logger = logging.getLogger(__name__)

COLLECTION_NAME = "latest"

# ChromaDB limits batch operations to ~5,461 embeddings. Use a safe limit.
_CHROMA_BATCH_SIZE = 5000

# chromadb 1.0 introduced sysdb schema migration 10 (00010-collection-schema).
# A persisted index whose highest applied sysdb migration is below this was
# written by a pre-1.0 (0.6-era) chromadb and is not loadable by 1.x.
_MIN_SUPPORTED_SYSDB_MIGRATION = 10


def _detect_incompatible_format(chroma_path: Path) -> None:
    """Raise :class:`IncompatibleIndexFormatError` for a pre-1.0 Chroma dir.

    Non-mutating: opens ``chroma.sqlite3`` read-only and ``immutable`` (no
    ``-wal``/``-shm`` creation, no schema writes) and inspects the sysdb
    migration version. Must run *before* ``chromadb.PersistentClient(...)``,
    which would otherwise raise an opaque ``InternalError`` or rewrite state.

    Stays silent (lets chromadb surface its own error) when the directory has
    no ``chroma.sqlite3``, the file is not a readable SQLite database, or it has
    no recognizable ``migrations`` table.
    """
    db_path = chroma_path / "chroma.sqlite3"
    try:
        db_info = db_path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno == getattr(errno, "ENOTSUP", 95):
            # Keep the established package-level diagnostic used when a
            # read-only Chroma snapshot cannot be made safely.
            raise ValueError(
                "Secure Chroma snapshots require descriptor-relative SQLite opens"
            ) from exc
        raise IncompatibleIndexFormatError(
            chroma_path, reason="unable to inspect chroma.sqlite3; refusing native loading"
        ) from exc
    if is_reparse_link(db_path) or not stat.S_ISREG(db_info.st_mode):
        raise IncompatibleIndexFormatError(
            chroma_path, reason="unsafe chroma.sqlite3 path; refusing native loading"
        )
    try:
        with connect_readonly(db_path, immutable=True) as conn:
            try:
                has_migrations = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
                ).fetchone()
                if has_migrations is None:
                    return
                row = conn.execute(
                    "SELECT MAX(version) FROM migrations WHERE dir='sysdb'"
                ).fetchone()
            except sqlite3.Error:
                return
    except OSError as exc:
        if exc.errno == getattr(errno, "ENOTSUP", 95):
            raise ValueError(
                "Secure Chroma snapshots require descriptor-relative SQLite opens"
            ) from exc
        raise IncompatibleIndexFormatError(
            chroma_path, reason="unsafe chroma.sqlite3 path; refusing native loading"
        ) from exc

    max_version = row[0] if row else None
    if max_version is not None and max_version < _MIN_SUPPORTED_SYSDB_MIGRATION:
        raise IncompatibleIndexFormatError(chroma_path, detected_sysdb_migration=max_version)


class NoOpProductTelemetryClient(ProductTelemetryClient):
    """Disable Chroma product telemetry cleanly for local clients."""

    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        return


def _record_to_metadata(
    record: ChunkedRecord,
) -> dict[str, str | int | float | bool]:
    """Extract ChromaDB-storable metadata from a ChunkedRecord.

    ChromaDB metadata values must be str, int, float, or bool.
    We store everything as str for simplicity and filter on exact match.
    """
    meta: dict[str, str | int | float | bool] = {
        "source_url": record.source_url,
        "content_type": record.content_type,
        "path": record.path,
        "indexed_at": record.indexed_at.isoformat(),
    }
    if record.repo is not None:
        meta["repo"] = record.repo
    if record.commit_sha is not None:
        meta["commit_sha"] = record.commit_sha
    # Store capability_tags as comma-separated string for ChromaDB
    tags = record.metadata.get("capability_tags")
    if tags and isinstance(tags, list):
        meta["capability_tags"] = ",".join(str(t) for t in tags)
    # Persist additional metadata fields used by filters
    for key in ("foundational_class", "language", "domain", "execution_mode"):
        val = record.metadata.get(key)
        if val is not None:
            meta[key] = str(val)
    for key in ("line_start", "line_end"):
        val = record.metadata.get(key)
        if val is not None:
            meta[key] = int(val)
    # Version pin metadata
    version_pin = record.metadata.get("pipecat_version_pin")
    if version_pin is not None:
        meta["pipecat_version_pin"] = str(version_pin)
    # Source API metadata fields
    for key in (
        "module_path",
        "class_name",
        "chunk_type",
        "method_name",
        "method_signature",
        "return_type",
    ):
        val = record.metadata.get(key)
        if val is not None:
            meta[key] = str(val)
    # Boolean metadata fields
    for key in ("is_dataclass", "is_abstract"):
        val = record.metadata.get(key)
        if val is not None:
            meta[key] = bool(val)
    # List metadata stored as JSON string (lossless for values containing commas)
    base_classes = record.metadata.get("base_classes")
    if base_classes and isinstance(base_classes, list):
        meta["base_classes"] = json.dumps(base_classes)
    imports = record.metadata.get("imports")
    if imports and isinstance(imports, list):
        meta["imports"] = json.dumps(imports)
    yields = record.metadata.get("yields")
    if yields and isinstance(yields, list):
        meta["yields"] = json.dumps(yields)
    calls = record.metadata.get("calls")
    if calls and isinstance(calls, list):
        meta["calls"] = json.dumps(calls)
    fields = record.metadata.get("fields")
    if fields and isinstance(fields, list):
        meta["fields"] = json.dumps(fields)
    rst_refs = record.metadata.get("rst_refs")
    if rst_refs and isinstance(rst_refs, list):
        meta["rst_refs"] = json.dumps(rst_refs)
    related_types = record.metadata.get("related_types")
    if related_types and isinstance(related_types, list):
        meta["related_types"] = json.dumps(related_types)
    return meta


def _metadata_to_record_fields(
    chunk_id: str,
    document: str,
    meta: Mapping[str, Any],
) -> ChunkedRecord:
    """Reconstruct a ChunkedRecord from ChromaDB stored data."""
    extra_meta: dict[str, Any] = {}
    capability_tags_str = meta.get("capability_tags", "")
    if capability_tags_str:
        extra_meta["capability_tags"] = capability_tags_str.split(",")
    # Round-trip version pin
    version_pin = meta.get("pipecat_version_pin")
    if version_pin is not None:
        extra_meta["pipecat_version_pin"] = version_pin
    # Round-trip optional metadata fields
    for key in ("foundational_class", "language", "domain", "execution_mode"):
        val = meta.get(key)
        if val is not None:
            extra_meta[key] = val
    for key in ("line_start", "line_end"):
        val = meta.get(key)
        if val is not None:
            extra_meta[key] = int(val)
    for key in (
        "module_path",
        "class_name",
        "chunk_type",
        "method_name",
        "method_signature",
        "return_type",
    ):
        val = meta.get(key)
        if val is not None:
            extra_meta[key] = val
    for key in ("is_dataclass", "is_abstract"):
        val = meta.get(key)
        if val is not None:
            extra_meta[key] = bool(val)
    base_classes_str = meta.get("base_classes", "")
    if base_classes_str:
        try:
            extra_meta["base_classes"] = json.loads(base_classes_str)
        except (json.JSONDecodeError, TypeError):
            # Fallback for legacy comma-separated format
            extra_meta["base_classes"] = base_classes_str.split(",")
    imports_str = meta.get("imports", "")
    if imports_str:
        try:
            extra_meta["imports"] = json.loads(imports_str)
        except (json.JSONDecodeError, TypeError):
            extra_meta["imports"] = []
    yields_str = meta.get("yields", "")
    if yields_str:
        try:
            extra_meta["yields"] = json.loads(yields_str)
        except (json.JSONDecodeError, TypeError):
            extra_meta["yields"] = []
    calls_str = meta.get("calls", "")
    if calls_str:
        try:
            extra_meta["calls"] = json.loads(calls_str)
        except (json.JSONDecodeError, TypeError):
            extra_meta["calls"] = []
    fields_str = meta.get("fields", "")
    if fields_str:
        try:
            extra_meta["fields"] = json.loads(fields_str)
        except (json.JSONDecodeError, TypeError):
            extra_meta["fields"] = []
    rst_refs_str = meta.get("rst_refs", "")
    if rst_refs_str:
        try:
            extra_meta["rst_refs"] = json.loads(rst_refs_str)
        except (json.JSONDecodeError, TypeError):
            extra_meta["rst_refs"] = []
    related_types_str = meta.get("related_types", "")
    if related_types_str:
        try:
            extra_meta["related_types"] = json.loads(related_types_str)
        except (json.JSONDecodeError, TypeError):
            extra_meta["related_types"] = []

    return ChunkedRecord(
        chunk_id=chunk_id,
        content=document,
        content_type=meta.get("content_type", "doc"),
        source_url=meta.get("source_url", ""),
        repo=meta.get("repo"),
        path=meta.get("path", ""),
        commit_sha=meta.get("commit_sha"),
        indexed_at=datetime.fromisoformat(meta["indexed_at"]),
        metadata=extra_meta,
    )


def _build_where_clause(filters: dict[str, Any]) -> dict[str, Any] | None:
    """Convert IndexQuery.filters into a ChromaDB ``where`` clause.

    ChromaDB 0.6.x ``where`` supports: $eq, $ne, $in, $nin, $gt, $gte, $lt, $lte.
    Only ``repo`` and ``content_type`` can be pushed down as exact-match filters.
    ``path`` (prefix) and ``capability_tags`` (substring) are applied as
    post-filters in :meth:`VectorIndex.search`.
    """
    conditions: list[dict[str, Any]] = []

    if "repo" in filters:
        conditions.append({"repo": {"$eq": filters["repo"]}})
    if "content_type" in filters:
        conditions.append({"content_type": {"$eq": filters["content_type"]}})
    if "foundational_class" in filters:
        conditions.append({"foundational_class": {"$eq": filters["foundational_class"]}})
    if "language" in filters:
        conditions.append({"language": {"$eq": filters["language"]}})
    if "domain" in filters:
        conditions.append({"domain": {"$eq": filters["domain"]}})
    if "execution_mode" in filters:
        conditions.append({"execution_mode": {"$eq": filters["execution_mode"]}})
    # class_name is handled as a post-filter (prefix match) — see _apply_post_filters
    if "method_name" in filters:
        conditions.append({"method_name": {"$eq": filters["method_name"]}})
    if "chunk_type" in filters:
        conditions.append({"chunk_type": {"$eq": filters["chunk_type"]}})
    if "is_dataclass" in filters:
        conditions.append({"is_dataclass": {"$eq": filters["is_dataclass"]}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _apply_post_filters(results: list[IndexResult], filters: dict[str, Any]) -> list[IndexResult]:
    """Apply filters that ChromaDB cannot handle natively.

    - path: prefix match
    - capability_tags: substring match on comma-separated metadata string
    """
    filtered = results

    if "path" in filters:
        prefix = filters["path"]
        filtered = [r for r in filtered if r.chunk.path.startswith(prefix)]

    if "capability_tags" in filters:
        tag = filters["capability_tags"]
        tags_to_match: list[str] = tag if isinstance(tag, list) else [str(tag)]
        filtered = [r for r in filtered if _record_has_tags(r, tags_to_match)]

    if "class_name" in filters:
        prefix = filters["class_name"]
        filtered = [
            r for r in filtered if r.chunk.metadata.get("class_name", "").startswith(prefix)
        ]

    if "module_path" in filters:
        prefix = filters["module_path"]
        filtered = [
            r for r in filtered if r.chunk.metadata.get("module_path", "").startswith(prefix)
        ]

    if "yields" in filters:
        val = filters["yields"]
        filtered = [r for r in filtered if val in r.chunk.metadata.get("yields", [])]
    if "calls" in filters:
        val = filters["calls"]
        filtered = [r for r in filtered if val in r.chunk.metadata.get("calls", [])]

    return filtered


def _record_has_tags(result: IndexResult, tags: list[str]) -> bool:
    """Check if a result's metadata contains all requested capability tags."""
    record_tags = result.chunk.metadata.get("capability_tags", [])
    if isinstance(record_tags, str):
        record_tags = record_tags.split(",")
    return all(t in record_tags for t in tags)


def _copy_tree_no_follow(source: Path, destination: Path) -> None:
    """Copy a published tree without ever following a swapped link.

    ``shutil.copytree(..., symlinks=True)`` still opens a file by pathname
    after its directory entry was inspected.  A concurrent replacement can
    therefore turn a regular file into a symlink between those operations.
    Descriptor reads plus identity checks make the copied snapshot fail closed
    instead of reading outside the published tree.
    """
    if os.open not in getattr(os, "supports_dir_fd", set()):
        # Pathname copies cannot make the parent no-reparse guarantee on
        # Windows. Refuse the snapshot rather than copying bytes through a
        # junction that can be swapped after lstat().
        raise ValueError("Secure Chroma snapshots are unavailable")

    def copy_dir(source_fd: int, source_name: str, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for entry in os.scandir(source_fd):
            name = entry.name
            target_path = target / name
            entry_info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if stat.S_ISLNK(entry_info.st_mode):
                raise ValueError(f"Unsafe read-only Chroma path: {name}")
            if stat.S_ISDIR(entry_info.st_mode):
                child_flags = (
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                )
                child_fd = os.open(name, child_flags, dir_fd=source_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (entry_info.st_dev, entry_info.st_ino):
                        raise ValueError(f"Unsafe read-only Chroma path: {name}")
                    copy_dir(child_fd, name, target_path)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(entry_info.st_mode):
                raise ValueError(f"Unsafe read-only Chroma path: {name}")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            source_file_fd = os.open(name, flags, dir_fd=source_fd)
            output_fd = -1
            try:
                opened = os.fstat(source_file_fd)
                if (opened.st_dev, opened.st_ino) != (entry_info.st_dev, entry_info.st_ino):
                    raise ValueError(f"Unsafe read-only Chroma path: {name}")
                output_fd = os.open(
                    target_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with (
                    os.fdopen(source_file_fd, "rb") as input_stream,
                    os.fdopen(output_fd, "wb") as output,
                ):
                    source_file_fd = -1
                    output_fd = -1
                    shutil.copyfileobj(input_stream, output)
                after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (entry_info.st_dev, entry_info.st_ino):
                    raise ValueError(f"Unsafe read-only Chroma path: {name}")
            finally:
                if source_file_fd != -1:
                    os.close(source_file_fd)
                if output_fd != -1:
                    os.close(output_fd)

    source_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    try:
        source_info = os.fstat(source_fd)
        if is_reparse_link(source) or not stat.S_ISDIR(source_info.st_mode):
            raise ValueError(f"Unsafe read-only Chroma path: {source.name}")
        copy_dir(source_fd, source.name, destination)
    finally:
        os.close(source_fd)


def _remove_tree_no_follow(
    root: Path,
    *,
    expected_parent: tuple[int, int] | None = None,
    expected_root: tuple[int, int] | None = None,
) -> None:
    """Remove a directory through descriptor-relative, no-follow operations."""
    if os.open not in getattr(os, "supports_dir_fd", set()):
        raise ValueError("Secure Chroma reset is unavailable")
    root = root.absolute()
    if is_reparse_link(root):
        raise ValueError(f"Unsafe Chroma reset path: {root.name}")
    try:
        parent_info = root.parent.lstat()
        root_info = root.lstat()
    except OSError as exc:
        raise ValueError(f"Unsafe Chroma reset path: {root.name}") from exc
    if not stat.S_ISDIR(parent_info.st_mode) or is_reparse_link(root.parent):
        raise ValueError(f"Unsafe Chroma reset path: {root.name}")
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"Unsafe Chroma reset path: {root.name}")
    if expected_parent is not None and (parent_info.st_dev, parent_info.st_ino) != expected_parent:
        raise ValueError(f"Unsafe Chroma reset path: {root.name}")
    if expected_root is not None and (root_info.st_dev, root_info.st_ino) != expected_root:
        raise ValueError(f"Unsafe Chroma reset path: {root.name}")
    parent_fd = os.open(
        str(root.parent),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    root_fd = -1
    try:
        opened_parent = os.fstat(parent_fd)
        if (opened_parent.st_dev, opened_parent.st_ino) != (
            parent_info.st_dev,
            parent_info.st_ino,
        ):
            raise ValueError(f"Unsafe Chroma reset path: {root.name}")
        if (
            expected_parent is not None
            and (opened_parent.st_dev, opened_parent.st_ino) != expected_parent
        ):
            raise ValueError(f"Unsafe Chroma reset path: {root.name}")
        root_fd = os.open(
            root.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (root_info.st_dev, root_info.st_ino):
            raise ValueError(f"Unsafe Chroma reset path: {root.name}")
        if expected_root is not None and (opened_root.st_dev, opened_root.st_ino) != expected_root:
            raise ValueError(f"Unsafe Chroma reset path: {root.name}")

        def remove_children(directory_fd: int, directory_path: Path) -> None:
            for entry in os.scandir(directory_fd):
                info = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                entry_path = directory_path / entry.name
                if stat.S_ISDIR(info.st_mode) and not is_reparse_link(entry_path):
                    child_fd = os.open(
                        entry.name,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        child_info = os.fstat(child_fd)
                        if (child_info.st_dev, child_info.st_ino) != (info.st_dev, info.st_ino):
                            raise ValueError(f"Unsafe Chroma reset path: {entry.name}")
                        remove_children(child_fd, entry_path)
                    finally:
                        os.close(child_fd)
                    after = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError(f"Unsafe Chroma reset path: {entry.name}")
                    os.rmdir(entry.name, dir_fd=directory_fd)
                else:
                    # Unlink links as links; never recurse through them.
                    os.unlink(entry.name, dir_fd=directory_fd)

        remove_children(root_fd, root)
        current_root = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino):
            raise ValueError(f"Unsafe Chroma reset path: {root.name}")
        os.rmdir(root.name, dir_fd=parent_fd)
    finally:
        if root_fd != -1:
            os.close(root_fd)
        os.close(parent_fd)


class VectorIndex:
    """ChromaDB vector index for embedding similarity search.

    This class handles storage and retrieval of vector embeddings using
    ChromaDB's PersistentClient. It persists data to a local directory
    so indexes survive process restarts.
    """

    def __init__(
        self, chroma_path: Path, *, read_only: bool = False, lock_held: bool = False
    ) -> None:
        self._chroma_path = chroma_path
        self._read_only = read_only
        self._lock_held = lock_held
        self._readonly_tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._client: Any = None
        self._collection: Any = None
        self._closed = True
        self._generation_identity: tuple[tuple[int, int], tuple[int, int] | None] | None = None
        self._op_lock = threading.RLock()
        try:
            self._open_client(lock_held=lock_held)
        except BaseException:
            if self._readonly_tempdir is not None:
                self._readonly_tempdir.cleanup()
                self._readonly_tempdir = None
            raise

    def _copy_read_only_snapshot(self) -> Path:
        """Copy a validated Chroma tree before opening the writer-oriented API.

        Chroma's local ``PersistentClient`` has no read-only mode and may write
        migrations, locks, or collection metadata while opening.  A private
        snapshot keeps those writes away from the published index.  Symlinks
        and reparse points are copied as links first, then rejected before the
        snapshot is opened, so a read-only query cannot follow an external
        path supplied by a corrupt or swapped index.
        """
        tempdir = tempfile.TemporaryDirectory(prefix="pipecat-context-hub-chroma-")
        snapshot = Path(tempdir.name) / "chroma"
        try:
            _copy_tree_no_follow(self._chroma_path, snapshot)
            for path in snapshot.rglob("*"):
                if is_reparse_link(path):
                    raise ValueError(f"Unsafe read-only Chroma path: {path.name}")
                if not path.is_dir() and not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError(f"Unsafe read-only Chroma path: {path.name}")
        except BaseException:
            tempdir.cleanup()
            raise
        self._readonly_tempdir = tempdir
        return snapshot

    def _source_identity(self) -> tuple[tuple[int, int], tuple[int, int] | None]:
        """Return the directory/database identity for the current generation."""
        try:
            directory = self._chroma_path.lstat()
            if is_reparse_link(self._chroma_path) or not stat.S_ISDIR(directory.st_mode):
                raise ValueError("unsafe Chroma generation path")
            db_path = self._chroma_path / "chroma.sqlite3"
            try:
                db = db_path.lstat()
            except FileNotFoundError:
                db = None
            if db is not None and (is_reparse_link(db_path) or not stat.S_ISREG(db.st_mode)):
                raise ValueError("unsafe Chroma database path")
            return (
                (directory.st_dev, directory.st_ino),
                None if db is None else (db.st_dev, db.st_ino),
            )
        except (OSError, RuntimeError) as exc:
            raise ValueError("Chroma generation is unavailable") from exc

    def _open_client(self, *, lock_held: bool = False) -> None:
        """Open the persistent Chroma client and the ``latest`` collection."""
        # Validation, snapshotting, and native opening must observe one
        # generation. A writer lock also coordinates WAL-backed SQLite state;
        # ``allow_wal`` is safe only under this lock.
        lock_context = (
            contextlib.nullcontext()
            if lock_held
            else index_lock(
                self._chroma_path.parent,
                exclusive=not self._read_only,
                create=not self._read_only,
            )
        )
        # The format probe is read-only and can fail before taking a
        # coordination lock (important for legacy/corrupt directories that
        # have no sibling lock). Native opening and all later probes remain
        # inside the generation lock below.
        _detect_incompatible_format(self._chroma_path)
        with lock_context:
            validate_persisted_hnsw(self._chroma_path.resolve(), COLLECTION_NAME, allow_wal=True)
            client_path = self._chroma_path
            if self._read_only:
                client_path = self._copy_read_only_snapshot()
                validate_persisted_hnsw(client_path, COLLECTION_NAME, allow_wal=True)
            else:
                self._chroma_path.mkdir(parents=True, exist_ok=True)
            # Recheck the published root immediately before native Chroma
            # opens it. The descriptor-safe probes above must not hand a
            # replaced/symlinked path to a writer-oriented native library.
            native_identity = self._source_identity()
            if native_identity != self._source_identity():
                raise ValueError("Chroma generation changed before native open")
            self._client = chromadb.PersistentClient(
                path=str(client_path),
                settings=ChromaSettings(
                    anonymized_telemetry=False,
                    chroma_product_telemetry_impl=(
                        "pipecat_context_hub.services.index.vector.NoOpProductTelemetryClient"
                    ),
                ),
            )
            try:
                if self._read_only:
                    self._collection = self._client.get_collection(name=COLLECTION_NAME)
                else:
                    self._collection = self._client.get_or_create_collection(
                        name=COLLECTION_NAME,
                        metadata={"hnsw:space": "cosine"},
                    )
            except (KeyError, ValueError) as exc:
                # The persisted collection configuration is unreadable. This is the
                # one corruption the pre-open sysdb-migration probe cannot catch:
                # an older chromadb (0.6) process that wrote to this 1.x directory
                # leaves a ``config_json_str`` without the ``_type`` discriminator,
                # so chromadb's own ``CollectionConfigurationInternal.from_json``
                # raises ``KeyError: '_type'`` (or a ValueError on an unknown type)
                # while loading the existing collection. A fresh/empty directory
                # never hits this path (creation does not parse an existing config),
                # so translating it here only ever fires on a genuinely poisoned
                # index. Surface the typed, actionable error instead of the opaque
                # one so ``serve``/``refresh`` print the reset remediation.
                raise IncompatibleIndexFormatError(
                    self._chroma_path,
                    reason=(
                        "the persisted collection configuration is unreadable "
                        f"({exc!r}); this happens when an older chromadb (0.6) "
                        "process writes to a 1.x index directory"
                    ),
                ) from exc
            # Native Chroma accepts only a pathname. Bind the resulting
            # client to the identities validated immediately before opening
            # and fail closed if a replacement won the final open window.
            try:
                opened_identity = self._source_identity()
            except BaseException:
                # A native client may have opened a pathname that was swapped
                # during construction. Release it before surfacing the
                # fail-closed identity error; the pathname API cannot undo
                # bytes a hostile native open may already have touched.
                try:
                    self._client.close()
                finally:
                    self._client = None
                    self._collection = None
                raise
            if not self._read_only and (
                opened_identity[0] != native_identity[0]
                or (native_identity[1] is not None and opened_identity[1] != native_identity[1])
            ):
                try:
                    self._client.close()
                finally:
                    self._client = None
                    self._collection = None
                raise ValueError("Chroma generation changed during native open")
        self._closed = False
        self._generation_identity = self._source_identity()
        logger.info("VectorIndex initialized at %s", self._chroma_path)

    def reopen_if_replaced(self, *, lock_held: bool = False, force: bool = False) -> None:
        """Reopen after replacement, or force a new in-place generation."""
        with self._op_lock:
            if not force and self._source_identity() == self._generation_identity:
                return
            self._release_client()
            self._open_client(lock_held=lock_held or self._lock_held)

    def _release_client(self) -> None:
        """Release this client's Chroma resources via the public close API.

        ChromaDB 1.x exposes ``Client.close()``, which is refcount-aware: it
        stops the underlying shared System and evicts it from
        ``SharedSystemClient._identifier_to_system`` only when the last client
        for the persistence path closes. This eviction is essential — calling
        the private ``_system.stop()`` directly (the 0.6 workaround) left a
        dead Rust-backed system in the cache, so the next ``PersistentClient``
        on the same path reused it and failed tenant validation with
        ``'RustBindingsAPI' object has no attribute 'bindings'``.
        """
        if self._closed:
            return

        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.exception("Failed to close Chroma client at %s", self._chroma_path)

        self._collection = None
        self._client = None
        self._closed = True
        if self._readonly_tempdir is not None:
            self._readonly_tempdir.cleanup()
            self._readonly_tempdir = None

    def close(self) -> None:
        """Release this client's Chroma resources."""
        with self._op_lock:
            self._release_client()

    def reset(self) -> None:
        """Delete persisted Chroma state and recreate the collection.

        Reset is intended for exclusive maintenance flows such as
        ``refresh --reset-index``. ``Client.close()`` evicts this path's
        System from ChromaDB's shared cache, so the subsequent ``_open_client``
        rebuilds a fresh System over the recreated directory.
        """
        if self._read_only:
            raise RuntimeError("read-only VectorIndex cannot reset")
        with self._op_lock:
            try:
                parent_before = self._chroma_path.parent.lstat()
                root_before = self._chroma_path.lstat()
            except OSError as exc:
                raise ValueError("Unsafe Chroma reset path") from exc
            if (
                is_reparse_link(self._chroma_path.parent)
                or not stat.S_ISDIR(parent_before.st_mode)
                or is_reparse_link(self._chroma_path)
                or not stat.S_ISDIR(root_before.st_mode)
            ):
                raise ValueError("Unsafe Chroma reset path")
            with index_lock(self._chroma_path.parent, exclusive=True):
                self._release_client()
                _remove_tree_no_follow(
                    self._chroma_path,
                    expected_parent=(parent_before.st_dev, parent_before.st_ino),
                    expected_root=(root_before.st_dev, root_before.st_ino),
                )
                if self._chroma_path.exists() or self._chroma_path.is_symlink():
                    raise OSError(f"Chroma reset did not remove {self._chroma_path}")
                # Reopen before releasing the generation lock. A concurrent
                # refresh must not publish a new generation between delete and
                # collection creation.
                self._open_client(lock_held=True)
                logger.info("VectorIndex reset at %s", self._chroma_path)

    def clear(self) -> None:
        """Drop all records by deleting and recreating the collection."""
        if self._read_only:
            raise RuntimeError("read-only VectorIndex cannot clear")
        with self._op_lock:
            with index_lock(self._chroma_path.parent, exclusive=True):
                self.reopen_if_replaced(lock_held=True)
                self._client.delete_collection(COLLECTION_NAME)
                self._collection = self._client.get_or_create_collection(
                    name=COLLECTION_NAME,
                    metadata={"hnsw:space": "cosine"},
                )
            logger.info("VectorIndex cleared")

    def upsert(self, records: list[ChunkedRecord]) -> int:
        """Upsert records into ChromaDB. Returns count of records written."""
        if self._read_only:
            raise RuntimeError("read-only VectorIndex cannot upsert")
        if not records:
            return 0

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[Mapping[str, str | int | float | bool]] = []
        embeddings: list[Sequence[float]] = []

        for record in records:
            if record.embedding is None:
                logger.warning("Skipping record %s: no embedding", record.chunk_id)
                continue
            ids.append(record.chunk_id)
            documents.append(record.content)
            # chromadb 1.x metadata must be str | int | float | bool (None is
            # rejected). _record_to_metadata enforces this by omitting absent
            # fields; the exact field->type map is pinned in
            # tests/unit/test_metadata_types.py::EXPECTED_METADATA_TYPES.
            metadatas.append(_record_to_metadata(record))
            embeddings.append(record.embedding)

        if not ids:
            return 0

        with self._op_lock, index_lock(self._chroma_path.parent, exclusive=True):
            self.reopen_if_replaced(lock_held=True)
            # ChromaDB limits batch operations to ~5,461 embeddings.
            for i in range(0, len(ids), _CHROMA_BATCH_SIZE):
                end = i + _CHROMA_BATCH_SIZE
                self._collection.upsert(
                    ids=ids[i:end],
                    documents=documents[i:end],
                    metadatas=metadatas[i:end],
                    embeddings=embeddings[i:end],
                )
        logger.debug("Upserted %d records into vector index", len(ids))
        return len(ids)

    def delete_by_content_type(self, content_type: str) -> int:
        """Delete all records matching a content type. Returns count deleted."""
        if self._read_only:
            raise RuntimeError("read-only VectorIndex cannot delete records")
        with self._op_lock, index_lock(self._chroma_path.parent, exclusive=True):
            self.reopen_if_replaced(lock_held=True)
            where_clause: dict[str, Any] = {"content_type": {"$eq": content_type}}
            existing = self._collection.get(where=where_clause, include=[])
            ids = existing["ids"]
            count = len(ids)
            if count > 0:
                # ChromaDB limits batch operations to ~5,000 records.
                for i in range(0, count, _CHROMA_BATCH_SIZE):
                    batch = ids[i : i + _CHROMA_BATCH_SIZE]
                    self._collection.delete(ids=batch)
                logger.debug(
                    "Deleted %d records from vector index for content_type=%s", count, content_type
                )
            return count

    def delete_by_repo(self, repo: str) -> int:
        """Delete all records matching a repo. Returns count deleted."""
        if self._read_only:
            raise RuntimeError("read-only VectorIndex cannot delete records")
        with self._op_lock, index_lock(self._chroma_path.parent, exclusive=True):
            self.reopen_if_replaced(lock_held=True)
            where_clause: dict[str, Any] = {"repo": {"$eq": repo}}
            existing = self._collection.get(where=where_clause, include=[])
            ids = existing["ids"]
            count = len(ids)
            if count > 0:
                for i in range(0, count, _CHROMA_BATCH_SIZE):
                    batch = ids[i : i + _CHROMA_BATCH_SIZE]
                    self._collection.delete(ids=batch)
                logger.debug("Deleted %d records from vector index for repo=%s", count, repo)
            return count

    def delete_by_source(self, source_url: str) -> int:
        """Delete all records matching a source URL. Returns count deleted."""
        if self._read_only:
            raise RuntimeError("read-only VectorIndex cannot delete records")
        with self._op_lock, index_lock(self._chroma_path.parent, exclusive=True):
            self.reopen_if_replaced(lock_held=True)
            where_clause: dict[str, Any] = {"source_url": {"$eq": source_url}}
            existing = self._collection.get(where=where_clause, include=[])
            ids = existing["ids"]
            count = len(ids)
            if count > 0:
                for i in range(0, count, _CHROMA_BATCH_SIZE):
                    batch = ids[i : i + _CHROMA_BATCH_SIZE]
                    self._collection.delete(ids=batch)
                logger.debug(
                    "Deleted %d records from vector index for source %s", count, source_url
                )
            return count

    def search(self, query: IndexQuery) -> list[IndexResult]:
        """Search by embedding similarity. Returns results ranked by score."""
        if query.query_embedding is None:
            logger.warning("vector_search called without query_embedding")
            return []

        with self._op_lock:
            # The outer IndexStore lock serializes this check with refresh;
            # reopen in-place if another process replaced the Chroma tree.
            self.reopen_if_replaced(lock_held=True)
            needs_post_filter = any(
                k in query.filters
                for k in ("path", "capability_tags", "class_name", "module_path", "yields", "calls")
            )
            where = _build_where_clause(query.filters)

            # Clamp n_results to collection size to prevent ChromaDB crash
            collection_count = self._collection.count()
            if collection_count == 0:
                return []

            # Over-fetch when post-filtering to ensure enough results survive.
            n_results = query.limit * 3 if needs_post_filter else query.limit
            n_results = min(n_results, collection_count)

            kwargs: dict[str, Any] = {
                "query_embeddings": [query.query_embedding],
                "n_results": n_results,
                "include": ["documents", "metadatas", "distances"],
            }
            if where is not None:
                kwargs["where"] = where

            results = self._collection.query(**kwargs)

        items: list[IndexResult] = []
        result_ids = results["ids"][0] if results["ids"] else []
        result_docs = results["documents"][0] if results["documents"] else []
        result_metas = results["metadatas"][0] if results["metadatas"] else []
        result_dists = results["distances"][0] if results["distances"] else []

        for chunk_id, doc, meta, dist in zip(result_ids, result_docs, result_metas, result_dists):
            # ChromaDB cosine distance: 0 = identical, 2 = opposite
            # Convert to similarity score: 1 - (distance / 2) gives 0..1 range
            score = 1.0 - (dist / 2.0)
            record = _metadata_to_record_fields(chunk_id, doc, meta)
            items.append(
                IndexResult(
                    chunk=record,
                    score=score,
                    match_type="vector",
                )
            )

        if needs_post_filter:
            items = _apply_post_filters(items, query.filters)

        return items[: query.limit]
