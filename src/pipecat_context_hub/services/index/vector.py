"""ChromaDB-backed vector index for embedding similarity search.

Wraps ChromaDB's PersistentClient to store ChunkedRecord embeddings and
support filtered vector queries. Uses collection name ``latest`` as the
single namespace for v0.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent
from overrides import override

from pipecat_context_hub.services.index.chroma_schema import (
    chromadb_format_verified,
    query_chroma_sqlite,
)
from pipecat_context_hub.services.index.errors import (
    CompactionSwapError,
    IncompatibleIndexFormatError,
    InterruptedCompactionError,
)
from pipecat_context_hub.services.index.hnsw_validation import (
    HnswWriteGuard,
    validate_persisted_hnsw,
)
from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

logger = logging.getLogger(__name__)

COLLECTION_NAME = "latest"
# Collections VectorIndex.compact() writes; see its docstring.
COMPACT_STAGING_NAME = f"{COLLECTION_NAME}_compact"
COMPACT_BACKUP_NAME = f"{COLLECTION_NAME}_precompact"

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
    # No migrations table (not a Chroma db) or unreadable: rows is None or
    # [(None,)], and chromadb surfaces its own error.
    rows = query_chroma_sqlite(
        chroma_path, "SELECT MAX(version) FROM migrations WHERE dir='sysdb'", immutable=True
    )
    row = rows[0] if rows else None
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


class VectorIndex:
    """ChromaDB vector index for embedding similarity search.

    This class handles storage and retrieval of vector embeddings using
    ChromaDB's PersistentClient. It persists data to a local directory
    so indexes survive process restarts.
    """

    def __init__(self, chroma_path: Path, *, recover: bool = False) -> None:
        """Open the index at *chroma_path*.

        Opening never writes to the index unless *recover* is set, which only
        ``refresh`` does (it holds the exclusive refresh lock): it lets this
        open restore a compaction interrupted between its two renames.
        Readers hold that lock only shared, so for them the same state raises
        :class:`InterruptedCompactionError` instead.
        """
        self._chroma_path = chroma_path
        self._recover = recover
        self._client: Any = None
        self._collection: Any = None
        self._closed = True
        self._op_lock = threading.RLock()
        self._open_client()

    def _open_client(self) -> None:
        """Open the persistent Chroma client and the ``latest`` collection."""
        # Probe for a pre-1.0 on-disk format BEFORE constructing the client,
        # which would otherwise crash opaquely or rewrite 0.6 state.
        _detect_incompatible_format(self._chroma_path)
        # Status reads metadata without loading the HNSW graph. Reject the
        # observed oversized-graph corruption before the native client can
        # initialize successfully and later SIGSEGV on the first search.
        validate_persisted_hnsw(self._chroma_path.resolve(), COLLECTION_NAME)
        # A compaction interrupted mid-swap leaves the live graph under the
        # backup name, and _open_collection may load it.
        validate_persisted_hnsw(self._chroma_path.resolve(), COMPACT_BACKUP_NAME)
        self._chroma_path.mkdir(parents=True, exist_ok=True)
        client: Any = chromadb.PersistentClient(
            path=str(self._chroma_path),
            settings=ChromaSettings(
                anonymized_telemetry=False,
                chroma_product_telemetry_impl=(
                    "pipecat_context_hub.services.index.vector.NoOpProductTelemetryClient"
                ),
            ),
        )
        try:
            self._collection = self._open_collection(client)
        except BaseException:
            # Release the client's reference to Chroma's per-path System:
            # a leaked one would be reused (stale) by every later open.
            with contextlib.suppress(Exception):
                client.close()
            raise
        self._client = client
        self._closed = False
        self._write_guard = HnswWriteGuard(self._chroma_path.resolve())
        logger.info("VectorIndex initialized at %s", self._chroma_path)

    def _open_collection(self, client: Any) -> Any:
        """Return the ``latest`` collection, first undoing an interrupted compaction."""
        try:
            names = {getattr(c, "name", c) for c in client.list_collections()}
            if COLLECTION_NAME not in names and COMPACT_BACKUP_NAME in names:
                # compact() died between its two renames. get_or_create below
                # would create an empty ``latest`` and strand every record
                # under the backup name (vector search silently empty while
                # FTS still has them).
                if not self._recover:
                    raise InterruptedCompactionError(self._chroma_path)
                logger.warning("Restoring the vector collection from an interrupted compaction")
                client.get_collection(COMPACT_BACKUP_NAME).modify(name=COLLECTION_NAME)
            return client.get_or_create_collection(
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
        with self._op_lock:
            self._release_client()
            shutil.rmtree(self._chroma_path, ignore_errors=True)
            self._open_client()
            logger.info("VectorIndex reset at %s", self._chroma_path)

    def clear(self) -> None:
        """Drop all records by deleting and recreating the collection."""
        with self._op_lock:
            self._client.delete_collection(COLLECTION_NAME)
            self._collection = self._client.get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("VectorIndex cleared")

    def compact(self) -> int:
        """Rebuild the collection so its HNSW graph holds only live records.

        ``delete`` + re-``add`` of the same ids (what ``refresh --force`` does)
        leaves the deleted elements in the persisted graph, so it grows by the
        full corpus on every forced refresh (20k -> 80k elements for 20k live
        records over four rounds, 2026-10-15 spike). A bigger graph is more
        data for the next load to get wrong. This copies the live records into
        a staging collection, swaps it in, and removes the orphaned segment
        directory of the old one. Returns the number of records carried over;
        the old collection is untouched unless the copy verified. If the swap
        fails and cannot be rolled back, raises :class:`CompactionSwapError`
        (fatal) and deletes nothing; refresh's next open restores the backup.
        """
        staging_name = COMPACT_STAGING_NAME
        with self._op_lock:
            old = self._collection
            total = int(old.count())
            for leftover in (staging_name, COMPACT_BACKUP_NAME):  # from an interrupted run
                with contextlib.suppress(Exception):
                    self._client.delete_collection(leftover)
            staging = self._client.create_collection(
                name=staging_name, metadata={"hnsw:space": "cosine"}
            )
            mid_swap = False
            try:
                copied = 0
                while copied < total:
                    page = old.get(
                        limit=_CHROMA_BATCH_SIZE,
                        offset=copied,
                        include=["documents", "metadatas", "embeddings"],
                    )
                    ids = page["ids"]
                    if not ids:
                        break
                    staging.add(
                        ids=ids,
                        documents=page["documents"],
                        metadatas=page["metadatas"],
                        embeddings=page["embeddings"],
                    )
                    # Same per-batch runaway check as upsert(): the staging
                    # graph is written by the same native code.
                    self._write_guard.check()
                    copied += len(ids)
                if staging.count() != total:
                    raise RuntimeError(
                        f"compaction copied {staging.count()} of {total} records; keeping old index"
                    )
                # Swap without ever leaving no ``latest``: park the old
                # collection under a backup name, promote staging, and only
                # then drop the backup. A crash between the two renames leaves
                # the backup, which _open_client renames back.
                old.modify(name=COMPACT_BACKUP_NAME)
                try:
                    staging.modify(name=COLLECTION_NAME)
                except BaseException:
                    try:
                        old.modify(name=COLLECTION_NAME)
                    except Exception as rollback_exc:
                        # No ``latest`` now: readers fail until refresh
                        # restores the backup, so this must not pass for the
                        # non-fatal "index valid but larger" outcome.
                        mid_swap = True
                        raise CompactionSwapError(self._chroma_path) from rollback_exc
                    raise
            except BaseException:
                if not mid_swap:  # mid-swap, destroy nothing: leave it for refresh
                    with contextlib.suppress(Exception):
                        self._client.delete_collection(staging_name)
                with contextlib.suppress(Exception):
                    self._collection = self._client.get_collection(COLLECTION_NAME)
                raise
            self._collection = self._client.get_collection(COLLECTION_NAME)
            try:
                self._client.delete_collection(COMPACT_BACKUP_NAME)
            except Exception:
                logger.warning(
                    "Could not drop the pre-compaction collection; the next compaction removes it",
                    exc_info=True,
                )
            self._write_guard = HnswWriteGuard(self._chroma_path.resolve())
            self._remove_orphan_segment_dirs()
        logger.info("VectorIndex compacted: %d records", total)
        return total

    def _remove_orphan_segment_dirs(self) -> None:
        """Delete UUID segment dirs no longer referenced by chroma.sqlite3.

        Chroma leaves a dropped collection's segment directory (and its
        bloated graph) on disk. Best effort: skipped unless the segment table
        was readable and non-empty, and on a chromadb release whose on-disk
        layout this was not verified against (a changed ``segments`` table
        could otherwise make live data look orphaned).
        """
        if not chromadb_format_verified():
            return
        rows = query_chroma_sqlite(self._chroma_path, "SELECT id FROM segments", immutable=False)
        referenced = {str(row[0]) for row in rows or ()}
        if not referenced:
            return
        for entry in self._chroma_path.iterdir():
            try:
                UUID(entry.name)
            except ValueError:
                continue
            if entry.is_dir() and entry.name not in referenced:
                shutil.rmtree(entry, ignore_errors=True)

    def upsert(self, records: list[ChunkedRecord]) -> int:
        """Upsert records into ChromaDB. Returns count of records written."""
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

        with self._op_lock:
            # ChromaDB limits batch operations to ~5,461 embeddings.
            for i in range(0, len(ids), _CHROMA_BATCH_SIZE):
                end = i + _CHROMA_BATCH_SIZE
                self._collection.upsert(
                    ids=ids[i:end],
                    documents=documents[i:end],
                    metadatas=metadatas[i:end],
                    embeddings=embeddings[i:end],
                )
                # A runaway link-list file grows silently until the disk fills;
                # fail the write the moment the persisted graph exceeds its
                # bound — per batch, so one large upsert can't keep growing it.
                self._write_guard.check()
        logger.debug("Upserted %d records into vector index", len(ids))
        return len(ids)

    def delete_by_content_type(self, content_type: str) -> int:
        """Delete all records matching a content type. Returns count deleted."""
        with self._op_lock:
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
        with self._op_lock:
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
        with self._op_lock:
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
