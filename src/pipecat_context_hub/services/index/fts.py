"""SQLite FTS5 keyword index for full-text search.

Stores ChunkedRecord content in an FTS5 virtual table for fast keyword
matching with BM25 ranking. A companion metadata table holds structured
fields for filtering.
"""

from __future__ import annotations

import json
import contextlib
import logging
import os
import sqlite3
import stat
import threading
import tempfile
import shutil
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult
from pipecat_context_hub.shared.paths import is_reparse_link
from pipecat_context_hub.services.index.locking import index_lock

logger = logging.getLogger(__name__)

# Version of the published read contract over `index_metadata` — the shape
# external tooling may depend on when reading this database directly (see
# "Index metadata contract" in docs/README.md). Bump only on a breaking change
# to that table's shape or to the meaning of a documented key; adding a new key
# is backwards compatible and does not warrant a bump. Written on refresh, so
# an index built before the contract was published simply lacks it — consumers
# treat an absent value as "pre-contract", not as an error.
#
# v2: `framework_version` can now hold the literal sentinel "latest" in
# addition to a concrete tag — a value consumers doing their own version
# comparison need to special-case.
# v3: successful default refreshes now record the `latest` sentinel in
# `framework_version`; absence means the index has no framework records to
# attribute a pin to.
METADATA_CONTRACT_VERSION = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    content      TEXT NOT NULL,
    content_type TEXT NOT NULL,
    source_url   TEXT NOT NULL,
    repo         TEXT,
    path         TEXT NOT NULL,
    commit_sha   TEXT,
    indexed_at   TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS index_metadata (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Triggers to keep FTS in sync with the chunks table.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO chunks_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""


def _identity(info: os.stat_result | None) -> tuple[int, int] | None:
    return None if info is None else (info.st_dev, info.st_ino)


def _sidecar_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"unsafe SQLite sidecar path: {path.name}") from exc
    if is_reparse_link(path) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"unsafe SQLite sidecar path: {path.name}")
    return (info.st_dev, info.st_ino)


class _WriterAliasDir:
    """Descriptor-owned temporary directory for a writer SQLite alias."""

    def __init__(
        self, path: Path, published_parent_fd: int, alias_parent_fd: int, directory_fd: int
    ) -> None:
        self.name = str(path)
        self._path = path
        self._published_parent_fd = published_parent_fd
        self._alias_parent_fd = alias_parent_fd
        self._directory_fd = directory_fd

    def cleanup(self) -> None:
        if self._directory_fd == -1:
            return
        for name in (self._path.name, f"{self._path.name}-wal", f"{self._path.name}-shm"):
            with contextlib.suppress(FileNotFoundError, OSError):
                os.unlink(name, dir_fd=self._directory_fd)
        os.close(self._directory_fd)
        self._directory_fd = -1
        with contextlib.suppress(FileNotFoundError, OSError):
            os.rmdir(self._path.name, dir_fd=self._alias_parent_fd)
        os.close(self._alias_parent_fd)
        os.close(self._published_parent_fd)
        self._alias_parent_fd = -1
        self._published_parent_fd = -1

    def stat(self, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)

    def stat_parent(self, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=self._published_parent_fd, follow_symlinks=False)

    def link_to_parent(self, source: str, destination: str) -> None:
        os.link(
            source,
            destination,
            src_dir_fd=self._directory_fd,
            dst_dir_fd=self._published_parent_fd,
            follow_symlinks=False,
        )


def _open_directory_chain(path: Path) -> int:
    """Open a directory path without following a replaceable component."""
    if os.open not in getattr(os, "supports_dir_fd", set()):
        raise OSError("secure SQLite writer aliases are unavailable")
    absolute = path.expanduser().resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _writer_alias(
    sqlite_path: Path,
) -> tuple[_WriterAliasDir, Path, Path, set[Path]]:
    """Bind a writer connection to a descriptor-validated database inode.

    SQLite accepts only pathnames, so opening the published pathname directly
    leaves a replacement window between the caller's no-follow checks and the
    native constructor. A same-filesystem hard link made after opening the
    original descriptor pins the connection to that inode. Existing WAL/SHM
    sidecars receive the same treatment; newly-created sidecars are published
    back to the original names after the schema transaction commits.
    """
    if os.open not in getattr(os, "supports_dir_fd", set()):
        raise OSError("secure SQLite writer aliases are unavailable")
    parent_fd = _open_directory_chain(sqlite_path.parent)
    try:
        alias_parent_fd = _open_directory_chain(sqlite_path.parent.parent)
    except BaseException:
        os.close(parent_fd)
        raise
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    db_fd = -1
    alias_dir: _WriterAliasDir | None = None
    try:
        db_fd = os.open(sqlite_path.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        db_fd = os.open(
            sqlite_path.name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
    try:
        db_info = os.fstat(db_fd)
        if not stat.S_ISREG(db_info.st_mode):
            raise ValueError("unsafe SQLite database path")
        alias_name = None
        for _ in range(100):
            candidate_name = f".pipecat-context-hub-writer-{uuid.uuid4().hex}"
            try:
                os.mkdir(candidate_name, 0o700, dir_fd=alias_parent_fd)
            except FileExistsError:
                continue
            alias_name = candidate_name
            break
        if alias_name is None:
            raise OSError("unable to create secure SQLite writer alias")
        alias = sqlite_path.parent.parent / alias_name / sqlite_path.name
        alias_dir_path = sqlite_path.parent.parent / alias_name
        try:
            alias_dir_fd = os.open(
                alias_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=alias_parent_fd,
            )
        except BaseException:
            with contextlib.suppress(OSError):
                os.rmdir(alias_name, dir_fd=alias_parent_fd)
            raise
        alias_dir = _WriterAliasDir(alias_dir_path, parent_fd, alias_parent_fd, alias_dir_fd)
        published: set[Path] = set()
        try:
            os.link(
                sqlite_path.name,
                sqlite_path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=alias_dir_fd,
                follow_symlinks=False,
            )
            alias_info = os.stat(sqlite_path.name, dir_fd=alias_dir_fd, follow_symlinks=False)
            if (alias_info.st_dev, alias_info.st_ino) != (db_info.st_dev, db_info.st_ino):
                raise ValueError("SQLite database path changed while opening")
            for suffix in ("-wal", "-shm"):
                source = sqlite_path.with_name(sqlite_path.name + suffix)
                try:
                    source_info = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(source_info.st_mode):
                    raise ValueError(f"unsafe SQLite {suffix[1:].upper()} path")
                target = alias.with_name(alias.name + suffix)
                os.link(
                    source.name,
                    target.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=alias_dir_fd,
                    follow_symlinks=False,
                )
                target_info = os.stat(target.name, dir_fd=alias_dir_fd, follow_symlinks=False)
                if (target_info.st_dev, target_info.st_ino) != (
                    source_info.st_dev,
                    source_info.st_ino,
                ):
                    raise ValueError(f"SQLite {suffix[1:].upper()} path changed while opening")
            # The alias directory is a sibling of the published parent.  A
            # replacement of that parent therefore cannot redirect this
            # pathname before SQLite opens it (and its inode is hard-linked to
            # the descriptor-validated database above).
            return alias_dir, alias, alias, published
        except BaseException:
            alias_dir.cleanup()
            raise
    finally:
        if db_fd != -1:
            os.close(db_fd)
        if alias_dir is None:
            os.close(alias_parent_fd)
            os.close(parent_fd)


def _checked_sidecar(path: Path, label: str) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"unsafe SQLite {label} path") from exc
    if is_reparse_link(path) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"unsafe SQLite {label} path")
    return info


class FTSIndex:
    """SQLite FTS5 keyword index for full-text search.

    Maintains an FTS5 virtual table alongside a regular table for metadata
    filtering. Data persists in a SQLite database file on disk.
    """

    def __init__(
        self, sqlite_path: Path, *, read_only: bool = False, lock_held: bool = False
    ) -> None:
        self._sqlite_path = sqlite_path
        self._read_only = read_only
        self._lock_held = lock_held
        self._lock = threading.RLock()
        self._readonly_snapshot_dir: tempfile.TemporaryDirectory[str] | None = None
        self._writer_alias_dir: _WriterAliasDir | None = None
        self._writer_alias_path: Path | None = None
        self._writer_connection_path: Path | None = None
        self._published_sidecars: set[Path] = set()
        self._sidecar_identities: dict[Path, tuple[int, int] | None] = {}
        sidecar_identities: dict[Path, tuple[int, int] | None] = {}
        initial_identity: tuple[int, int] | None = None
        if read_only:
            # ``mode=ro`` (rather than immutable mode) asks SQLite to replay a
            # committed WAL without creating or writing any sidecars. A live
            # writer is coordinated by the index generation lock; a WAL
            # reparse point is always rejected before SQLite sees it.
            wal_path = sqlite_path.with_name(f"{sqlite_path.name}-wal")
            wal_info = _checked_sidecar(wal_path, "WAL")
            shm_path = sqlite_path.with_name(f"{sqlite_path.name}-shm")
            shm_info = _checked_sidecar(shm_path, "SHM")
            sidecar_identities = {
                wal_path: _identity(wal_info),
                shm_path: _identity(shm_info),
            }
            try:
                initial_info = sqlite_path.lstat()
            except OSError as exc:
                raise ValueError("unsafe SQLite database path") from exc
            if is_reparse_link(sqlite_path) or not stat.S_ISREG(initial_info.st_mode):
                raise ValueError("unsafe SQLite database path")
            initial_identity = (initial_info.st_dev, initial_info.st_ino)
            connection_path = sqlite_path
            if wal_info is not None:
                # Python's sqlite wrapper cannot open a pathname relative to
                # an already-validated directory descriptor. Snapshot the
                # validated main/WAL/SHM files through no-follow descriptors,
                # then let SQLite open only that private, identity-stable copy.
                # This prevents a concurrent sidecar replacement from being
                # consumed before the identity check runs.
                snapshot_dir = tempfile.TemporaryDirectory(prefix="pipecat-context-hub-fts-")
                snapshot_root = Path(snapshot_dir.name)
                try:
                    sources = [sqlite_path, wal_path]
                    if shm_info is not None:
                        sources.append(shm_path)
                    for source in sources:
                        source_info = source.lstat()
                        expected = _identity(source_info)
                        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                        try:
                            opened = os.fstat(fd)
                            if expected != _identity(opened):
                                raise ValueError("SQLite path changed while snapshotting")
                            destination = snapshot_root / source.name
                            with destination.open("wb") as output:
                                with os.fdopen(os.dup(fd), "rb") as input_stream:
                                    shutil.copyfileobj(input_stream, output)
                            after = os.fstat(fd)
                            if _identity(after) != expected:
                                raise ValueError("SQLite path changed while snapshotting")
                        finally:
                            os.close(fd)
                except BaseException:
                    snapshot_dir.cleanup()
                    raise
                self._readonly_snapshot_dir = snapshot_dir
                connection_path = snapshot_root / sqlite_path.name
            query = "mode=ro" if wal_info is not None else "mode=ro&immutable=1"
            uri = f"file:{quote(str(connection_path), safe='/')}?{query}"
            try:
                self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            except BaseException:
                if self._readonly_snapshot_dir is not None:
                    self._readonly_snapshot_dir.cleanup()
                    self._readonly_snapshot_dir = None
                raise
        else:
            (
                self._writer_alias_dir,
                self._writer_alias_path,
                self._writer_connection_path,
                self._published_sidecars,
            ) = _writer_alias(sqlite_path)
            try:
                self._conn = sqlite3.connect(
                    str(self._writer_connection_path), check_same_thread=False
                )
            except BaseException:
                self._writer_alias_dir.cleanup()
                self._writer_alias_dir = None
                self._writer_alias_path = None
                self._writer_connection_path = None
                raise
        self._closed = False
        try:
            info = sqlite_path.lstat()
        except OSError:
            self._conn.close()
            if self._readonly_snapshot_dir is not None:
                self._readonly_snapshot_dir.cleanup()
                self._readonly_snapshot_dir = None
            if self._writer_alias_dir is not None:
                self._writer_alias_dir.cleanup()
                self._writer_alias_dir = None
                self._writer_alias_path = None
                self._writer_connection_path = None
            raise
        if is_reparse_link(sqlite_path) or not stat.S_ISREG(info.st_mode):
            self._conn.close()
            if self._readonly_snapshot_dir is not None:
                self._readonly_snapshot_dir.cleanup()
                self._readonly_snapshot_dir = None
            if self._writer_alias_dir is not None:
                self._writer_alias_dir.cleanup()
                self._writer_alias_dir = None
                self._writer_alias_path = None
                self._writer_connection_path = None
            raise ValueError("unsafe SQLite database path")
        identity = (info.st_dev, info.st_ino)
        if initial_identity is not None and identity != initial_identity:
            self._conn.close()
            if self._readonly_snapshot_dir is not None:
                self._readonly_snapshot_dir.cleanup()
                self._readonly_snapshot_dir = None
            if self._writer_alias_dir is not None:
                self._writer_alias_dir.cleanup()
                self._writer_alias_dir = None
                self._writer_alias_path = None
                self._writer_connection_path = None
            raise ValueError("SQLite database path changed while opening")
        if sidecar_identities and any(
            _sidecar_identity(path) != expected for path, expected in sidecar_identities.items()
        ):
            self._conn.close()
            if self._readonly_snapshot_dir is not None:
                self._readonly_snapshot_dir.cleanup()
                self._readonly_snapshot_dir = None
            raise ValueError("SQLite WAL/SHM path changed while opening")
        if not read_only:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()
            alias_path = self._writer_alias_path
            alias_dir = self._writer_alias_dir
            if alias_path is not None and alias_dir is not None:
                for suffix in ("-wal", "-shm"):
                    sidecar_name = f"{alias_path.name}{suffix}"
                    try:
                        alias_sidecar_info = alias_dir.stat(sidecar_name)
                    except FileNotFoundError:
                        continue
                    published = sqlite_path.with_name(sqlite_path.name + suffix)
                    try:
                        existing = alias_dir.stat_parent(published.name)
                    except FileNotFoundError:
                        alias_dir.link_to_parent(sidecar_name, published.name)
                        self._published_sidecars.add(published)
                    else:
                        if not stat.S_ISREG(existing.st_mode):
                            raise ValueError(f"unsafe SQLite {suffix[1:].upper()} path")
                        if (existing.st_dev, existing.st_ino) != (
                            alias_sidecar_info.st_dev,
                            alias_sidecar_info.st_ino,
                        ):
                            raise ValueError(
                                f"SQLite {suffix[1:].upper()} path changed while opening"
                            )
        self._identity = identity
        # ``lock_held`` only describes the lock used while constructing this
        # connection.  The constructor returns after that generation lock has
        # been released, so retaining the flag would let ``close`` unlink
        # published WAL/SHM sidecars without reacquiring it.
        self._lock_held = False
        tracked_sidecars = (
            sqlite_path.with_name(f"{sqlite_path.name}-wal"),
            sqlite_path.with_name(f"{sqlite_path.name}-shm"),
        )
        self._sidecar_identities = {path: _sidecar_identity(path) for path in tracked_sidecars}
        logger.info("FTSIndex initialized at %s", sqlite_path)

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._closed:
                return
            lock_context = None
            locked = self._lock_held
            if not locked:
                lock_context = index_lock(self._sqlite_path.parent, exclusive=True)
                try:
                    lock_context.__enter__()
                    locked = True
                except (OSError, RuntimeError, ValueError):
                    # The connection still must be closed if its parent was
                    # replaced. Without the generation lock, leave published
                    # sidecars in place rather than unlinking a new generation.
                    lock_context = None
            try:
                self._conn.close()
                self._closed = True
                if locked and not self._read_only:
                    for path in set(self._sidecar_identities) | self._published_sidecars:
                        # A second writer owns a hard-linked alias of this
                        # sidecar. Keep the published name in that case.
                        expected = self._sidecar_identities.get(path)
                        with contextlib.suppress(OSError, ValueError):
                            parent_fd = _open_directory_chain(path.parent)
                            try:
                                info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                                if expected == (info.st_dev, info.st_ino) and info.st_nlink <= 2:
                                    os.unlink(path.name, dir_fd=parent_fd)
                            finally:
                                os.close(parent_fd)
                self._published_sidecars.clear()
                if self._readonly_snapshot_dir is not None:
                    self._readonly_snapshot_dir.cleanup()
                    self._readonly_snapshot_dir = None
                if self._writer_alias_dir is not None:
                    self._writer_alias_dir.cleanup()
                    self._writer_alias_dir = None
                    self._writer_alias_path = None
                    self._writer_connection_path = None
            finally:
                if locked and lock_context is not None:
                    lock_context.__exit__(None, None, None)

    def reopen_if_replaced(self) -> bool:
        """Reopen after another process publishes a new SQLite generation."""
        with self._lock:
            try:
                info = self._sqlite_path.lstat()
            except OSError as exc:
                raise ValueError("SQLite database generation is unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError("unsafe SQLite database path")
            identity = (info.st_dev, info.st_ino)
            current_sidecars = {
                self._sqlite_path.with_name(f"{self._sqlite_path.name}-wal"),
                self._sqlite_path.with_name(f"{self._sqlite_path.name}-shm"),
            }
            sidecars_changed = any(
                _sidecar_identity(path) != self._sidecar_identities.get(path)
                for path in current_sidecars
            )
            if identity == self._identity and not sidecars_changed:
                return False
            self.close()
            replacement = FTSIndex(self._sqlite_path, read_only=self._read_only)
            self._conn = replacement._conn
            # A WAL-backed read-only connection may point at a private
            # snapshot.  Transfer ownership of that directory together with
            # the connection; otherwise ``replacement`` is finalized on
            # return and deletes files still in use by ``self._conn``.
            self._readonly_snapshot_dir = replacement._readonly_snapshot_dir
            replacement._readonly_snapshot_dir = None
            self._writer_alias_dir = replacement._writer_alias_dir
            replacement._writer_alias_dir = None
            self._writer_alias_path = replacement._writer_alias_path
            replacement._writer_alias_path = None
            self._writer_connection_path = replacement._writer_connection_path
            replacement._writer_connection_path = None
            self._published_sidecars = replacement._published_sidecars
            replacement._published_sidecars = set()
            self._closed = False
            self._identity = replacement._identity
            self._sidecar_identities = replacement._sidecar_identities
            return True

    def reopen(self) -> None:
        """Reopen even when the database inode is unchanged.

        Refresh publishes records in place, so inode identity alone cannot
        tell a long-lived reader that its native SQLite/FTS state belongs to
        the prior generation.
        """
        with self._lock:
            read_only = self._read_only
            self.close()
            replacement = FTSIndex(self._sqlite_path, read_only=read_only)
            self._conn = replacement._conn
            self._readonly_snapshot_dir = replacement._readonly_snapshot_dir
            replacement._readonly_snapshot_dir = None
            self._writer_alias_dir = replacement._writer_alias_dir
            replacement._writer_alias_dir = None
            self._writer_alias_path = replacement._writer_alias_path
            replacement._writer_alias_path = None
            self._writer_connection_path = replacement._writer_connection_path
            replacement._writer_connection_path = None
            self._published_sidecars = replacement._published_sidecars
            replacement._published_sidecars = set()
            self._closed = False
            self._identity = replacement._identity
            self._sidecar_identities = replacement._sidecar_identities

    def clear(self) -> None:
        """Delete all records from the chunks table (triggers sync FTS)."""
        with self._lock:
            self.reopen_if_replaced()
            self._conn.execute("DELETE FROM chunks")
            self._conn.commit()
            logger.info("FTSIndex cleared")

    def clear_metadata(self) -> None:
        """Delete all persisted index metadata."""
        with self._lock:
            self.reopen_if_replaced()
            self._conn.execute("DELETE FROM index_metadata")
            self._conn.commit()
            logger.info("FTSIndex metadata cleared")

    def reset(self) -> None:
        """Delete all indexed content and cached metadata."""
        self.clear()
        self.clear_metadata()
        logger.info("FTSIndex reset")

    def upsert(self, records: list[ChunkedRecord]) -> int:
        """Upsert records into the FTS index. Returns count written."""
        if not records:
            return 0

        with self._lock:
            self.reopen_if_replaced()
            count = 0
            for record in records:
                meta_json = json.dumps(record.metadata)
                self._conn.execute(
                    """
                    INSERT INTO chunks (chunk_id, content, content_type, source_url,
                                        repo, path, commit_sha, indexed_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        content = excluded.content,
                        content_type = excluded.content_type,
                        source_url = excluded.source_url,
                        repo = excluded.repo,
                        path = excluded.path,
                        commit_sha = excluded.commit_sha,
                        indexed_at = excluded.indexed_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        record.chunk_id,
                        record.content,
                        record.content_type,
                        record.source_url,
                        record.repo,
                        record.path,
                        record.commit_sha,
                        record.indexed_at.isoformat(),
                        meta_json,
                    ),
                )
                count += 1

            self._conn.commit()
            logger.debug("Upserted %d records into FTS index", count)
            return count

    def delete_by_content_type(self, content_type: str) -> int:
        """Delete all records with a given content type. Returns count deleted."""
        with self._lock:
            self.reopen_if_replaced()
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE content_type = ?",
                (content_type,),
            )
            row = cursor.fetchone()
            count: int = row[0] if row else 0

            if count > 0:
                self._conn.execute(
                    "DELETE FROM chunks WHERE content_type = ?",
                    (content_type,),
                )
                self._conn.commit()
                logger.debug(
                    "Deleted %d records from FTS index for content_type=%s", count, content_type
                )

            return count

    def delete_by_repo(self, repo: str) -> int:
        """Delete all records with a given repo. Returns count deleted."""
        with self._lock:
            self.reopen_if_replaced()
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE repo = ?",
                (repo,),
            )
            row = cursor.fetchone()
            count: int = row[0] if row else 0

            if count > 0:
                self._conn.execute(
                    "DELETE FROM chunks WHERE repo = ?",
                    (repo,),
                )
                self._conn.commit()
                logger.debug("Deleted %d records from FTS index for repo=%s", count, repo)

            return count

    def delete_by_source(self, source_url: str) -> int:
        """Delete all records with a given source URL. Returns count deleted."""
        with self._lock:
            self.reopen_if_replaced()
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE source_url = ?",
                (source_url,),
            )
            row = cursor.fetchone()
            count: int = row[0] if row else 0

            if count > 0:
                self._conn.execute(
                    "DELETE FROM chunks WHERE source_url = ?",
                    (source_url,),
                )
                self._conn.commit()
                logger.debug("Deleted %d records from FTS index for source %s", count, source_url)

            return count

    def search(self, query: IndexQuery) -> list[IndexResult]:
        """Search by keyword relevance using FTS5 BM25. Returns ranked results.

        When a ``chunk_id`` filter is present the search becomes a direct
        lookup and the FTS MATCH clause is skipped.
        """
        with self._lock:
            self.reopen_if_replaced()
            # Direct lookup by chunk_id — bypass FTS MATCH
            if "chunk_id" in query.filters:
                return self._get_by_chunk_id(query.filters["chunk_id"])

            # Filter-only lookup (no FTS MATCH) — used by get_doc path lookup
            if query.filter_only:
                return self._filter_only_search(query)

            if not query.query_text.strip():
                return []

            where_parts: list[str] = []
            params: list[Any] = []

            # FTS5 match
            where_parts.append("chunks_fts MATCH ?")
            params.append(self._sanitize_fts_query(query.query_text))

            # Metadata filters applied on the chunks table
            filter_clauses, filter_params = self._build_filter_sql(query.filters)
            where_parts.extend(filter_clauses)
            params.extend(filter_params)

            # `where_parts` only contains static clause templates; user input stays
            # parameterized in `params`, so joining the clauses is safe here.
            sql = "\n".join(
                [
                    "SELECT c.chunk_id, c.content, c.content_type, c.source_url,",
                    "       c.repo, c.path, c.commit_sha, c.indexed_at, c.metadata_json,",
                    "       bm25(chunks_fts) AS rank",
                    "FROM chunks_fts",
                    "JOIN chunks c ON c.rowid = chunks_fts.rowid",
                    "WHERE " + " AND ".join(where_parts),
                    "ORDER BY rank",
                    "LIMIT ?",
                ]
            )
            params.append(query.limit)

            cursor = self._conn.execute(sql, params)
            rows = cursor.fetchall()

        items: list[IndexResult] = []
        for row in rows:
            (
                chunk_id,
                content,
                content_type,
                source_url,
                repo,
                path,
                commit_sha,
                indexed_at_str,
                metadata_json,
                bm25_score,
            ) = row

            extra_meta: dict[str, Any] = json.loads(metadata_json) if metadata_json else {}

            record = ChunkedRecord(
                chunk_id=chunk_id,
                content=content,
                content_type=content_type,
                source_url=source_url,
                repo=repo,
                path=path,
                commit_sha=commit_sha,
                indexed_at=datetime.fromisoformat(indexed_at_str),
                metadata=extra_meta,
            )

            # BM25 returns negative scores (more negative = more relevant)
            # Convert to positive (higher = better)
            score = -float(bm25_score)

            items.append(
                IndexResult(
                    chunk=record,
                    score=score,
                    match_type="keyword",
                )
            )

        return items

    def _get_by_chunk_id(self, chunk_id: str) -> list[IndexResult]:
        """Direct lookup by chunk_id, bypassing FTS MATCH."""
        with self._lock:
            self.reopen_if_replaced()
            cursor = self._conn.execute(
                """
                SELECT chunk_id, content, content_type, source_url,
                       repo, path, commit_sha, indexed_at, metadata_json
                FROM chunks
                WHERE chunk_id = ?
                LIMIT 1
                """,
                (chunk_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return []

        (
            cid,
            content,
            content_type,
            source_url,
            repo,
            path,
            commit_sha,
            indexed_at_str,
            metadata_json,
        ) = row

        extra_meta: dict[str, Any] = json.loads(metadata_json) if metadata_json else {}
        record = ChunkedRecord(
            chunk_id=cid,
            content=content,
            content_type=content_type,
            source_url=source_url,
            repo=repo,
            path=path,
            commit_sha=commit_sha,
            indexed_at=datetime.fromisoformat(indexed_at_str),
            metadata=extra_meta,
        )
        return [IndexResult(chunk=record, score=1.0, match_type="keyword")]

    def _filter_only_search(self, query: IndexQuery) -> list[IndexResult]:
        """Search by metadata filters only, bypassing FTS MATCH.

        Used for direct lookups like get_doc by path where the query text
        is a path string, not a keyword search term.
        """
        filter_clauses, filter_params = self._build_filter_sql(query.filters)
        if not filter_clauses:
            return []

        sql = "\n".join(
            [
                "SELECT c.chunk_id, c.content, c.content_type, c.source_url,",
                "       c.repo, c.path, c.commit_sha, c.indexed_at, c.metadata_json",
                "FROM chunks c",
                "WHERE " + " AND ".join(filter_clauses),
                "ORDER BY c.rowid",
                "LIMIT ?",
            ]
        )
        filter_params.append(query.limit)

        cursor = self._conn.execute(sql, filter_params)
        rows = cursor.fetchall()

        items: list[IndexResult] = []
        for row in rows:
            (
                cid,
                content,
                content_type,
                source_url,
                repo,
                path,
                commit_sha,
                indexed_at_str,
                metadata_json,
            ) = row
            extra_meta: dict[str, Any] = json.loads(metadata_json) if metadata_json else {}
            record = ChunkedRecord(
                chunk_id=cid,
                content=content,
                content_type=content_type,
                source_url=source_url,
                repo=repo,
                path=path,
                commit_sha=commit_sha,
                indexed_at=datetime.fromisoformat(indexed_at_str),
                metadata=extra_meta,
            )
            items.append(IndexResult(chunk=record, score=1.0, match_type="keyword"))
        return items

    def set_metadata(self, key: str, value: str) -> None:
        """Upsert a key-value pair in the index_metadata table."""
        with self._lock:
            self.reopen_if_replaced()
            now = datetime.now().astimezone().isoformat()
            self._conn.execute(
                """
                INSERT INTO index_metadata (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            self._conn.commit()

    def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key, or None if not found."""
        with self._lock:
            self.reopen_if_replaced()
            cursor = self._conn.execute("SELECT value FROM index_metadata WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None

    def delete_metadata(self, key: str) -> None:
        """Remove a metadata key if it exists."""
        with self._lock:
            self.reopen_if_replaced()
            self._conn.execute("DELETE FROM index_metadata WHERE key = ?", (key,))
            self._conn.commit()

    def set_metadata_batch(self, pairs: dict[str, str], *, delete_keys: Iterable[str] = ()) -> None:
        """Upsert and delete metadata keys atomically in a single commit.

        Callers writing several related metadata keys (e.g. a refresh's
        end-of-run metadata pass) should use this instead of individual
        ``set_metadata``/``delete_metadata`` calls, so a reader never
        observes a partially-updated related-key set.
        """
        with self._lock:
            self.reopen_if_replaced()
            now = datetime.now().astimezone().isoformat()
            for key, value in pairs.items():
                self._conn.execute(
                    """
                    INSERT INTO index_metadata (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, now),
                )
            for key in delete_keys:
                self._conn.execute("DELETE FROM index_metadata WHERE key = ?", (key,))
            self._conn.commit()

    def get_all_metadata(self) -> dict[str, str]:
        """Return all index_metadata as a dict."""
        with self._lock:
            self.reopen_if_replaced()
            cursor = self._conn.execute("SELECT key, value FROM index_metadata")
            return dict(cursor.fetchall())

    def get_counts_by_repo(self) -> dict[str, int]:
        """Return record counts grouped by repo. Includes a 'docs' key for doc chunks."""
        with self._lock:
            self.reopen_if_replaced()
            counts: dict[str, int] = {}
            # Doc chunks have no repo — count them separately
            cursor = self._conn.execute("SELECT COUNT(*) FROM chunks WHERE content_type = 'doc'")
            doc_count = cursor.fetchone()[0]
            if doc_count:
                counts["docs.pipecat.ai"] = doc_count
            # Repo-scoped chunks
            cursor = self._conn.execute(
                "SELECT repo, COUNT(*) FROM chunks WHERE repo IS NOT NULL GROUP BY repo"
            )
            counts.update(dict(cursor.fetchall()))
            return counts

    def get_index_stats(self) -> dict[str, Any]:
        """Return record counts by content_type, total count, and distinct commit SHAs."""
        with self._lock:
            self.reopen_if_replaced()
            cursor = self._conn.execute(
                "SELECT content_type, COUNT(*) FROM chunks GROUP BY content_type"
            )
            counts_by_type: dict[str, int] = dict(cursor.fetchall())
            total: int = sum(counts_by_type.values())

            cursor = self._conn.execute(
                "SELECT DISTINCT commit_sha FROM chunks WHERE commit_sha IS NOT NULL"
            )
            commit_shas: list[str] = [r[0] for r in cursor.fetchall()]

            return {
                "counts_by_type": counts_by_type,
                "total": total,
                "commit_shas": commit_shas,
            }

    @staticmethod
    def _sanitize_fts_query(query_text: str) -> str:
        """Sanitize user query for FTS5 MATCH.

        Strips double quotes from tokens before wrapping in double quotes
        to prevent FTS5 syntax injection. Tokens are implicitly AND-ed.
        """
        tokens = query_text.split()
        # Strip quotes from each token to prevent FTS5 syntax breakout,
        # then wrap in double quotes (implicit AND in FTS5).
        return " ".join(
            f'"{token.replace(chr(34), "")}"'
            for token in tokens
            if token.strip() and token.replace('"', "").strip()
        )

    @staticmethod
    def _escape_like(value: str) -> str:
        """Escape LIKE metacharacters so they match literally."""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _build_filter_sql(
        filters: dict[str, Any],
    ) -> tuple[list[str], list[Any]]:
        """Build SQL WHERE clauses for metadata filters.

        Returns (clause_list, param_list) to be appended to the WHERE.
        """
        esc = FTSIndex._escape_like
        clauses: list[str] = []
        params: list[Any] = []

        if "repo" in filters:
            clauses.append("c.repo = ?")
            params.append(filters["repo"])
        if "content_type" in filters:
            clauses.append("c.content_type = ?")
            params.append(filters["content_type"])
        if "path" in filters:
            clauses.append("c.path LIKE ? ESCAPE '\\'")
            params.append(f"{esc(filters['path'])}%")
        if "capability_tags" in filters:
            tags = filters["capability_tags"]
            if isinstance(tags, list):
                for tag in tags:
                    clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
                    params.append(f"%{esc(tag)}%")
            else:
                clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
                params.append(f"%{esc(tags)}%")
        # Metadata JSON filters for fields stored in the JSON blob
        for key in ("foundational_class", "language", "domain", "execution_mode"):
            if key in filters:
                clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
                params.append(f'%"{key}": "{esc(filters[key])}"%')
        # Source API metadata filters (exact match)
        for key in ("chunk_type", "method_name"):
            if key in filters:
                clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
                params.append(f'%"{key}": "{esc(filters[key])}"%')
        # class_name is a prefix filter so "DailyTransport" matches
        # "DailyTransport", "DailyTransportClient", "DailyTransportParams"
        if "class_name" in filters:
            clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
            params.append(f'%"class_name": "{esc(filters["class_name"])}%')
        # module_path is a prefix filter (e.g. "pipecat.services" matches "pipecat.services.tts")
        if "module_path" in filters:
            clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
            params.append(f'%"module_path": "{esc(filters["module_path"])}%')
        if "is_dataclass" in filters:
            val = "true" if filters["is_dataclass"] else "false"
            clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
            params.append(f'%"is_dataclass": {val}%')
        # Call-graph metadata filters — anchor to the JSON array key,
        # quote the value, and close with `]` to prevent matching across
        # field boundaries (e.g. empty "calls": [] followed by "method_name": "push_frame").
        if "yields" in filters:
            clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
            params.append(f'%"yields": [%"{esc(filters["yields"])}"%]%')
        if "calls" in filters:
            clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
            params.append(f'%"calls": [%"{esc(filters["calls"])}"%]%')

        return clauses, params
