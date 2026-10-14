"""Read-only rejection of oversized Chroma HNSW link-list files.

Chroma 1.5.x can SIGSEGV while loading a corrupt persisted graph. This is
a bounded size check for the observed sparse-file corruption, not a complete
graph validator. It must run before constructing the native client.
"""

from __future__ import annotations

import sqlite3
import struct
import os
import stat
import errno
from pathlib import Path
from uuid import UUID

from pipecat_context_hub.services.index.errors import IncompatibleIndexFormatError
from pipecat_context_hub.services.index.safe_sqlite import connect_readonly
from pipecat_context_hub.shared.paths import is_reparse_link

# Chroma's persisted HNSW v1 header (64-bit, little-endian), written by
# chroma-core/hnswlib's hnswalg.h. See upstream corruption report #7510:
# https://github.com/chroma-core/chroma/issues/7510
_HEADER = struct.Struct("<i6Qii3QdQ")
_SEGMENT_TYPE = "urn:chroma:segment/vector/hnsw-local-persisted"
_MAX_CAPACITY = 100_000_000
_MAX_LEVEL = 64
_MAX_M = 1024


def _open_segment_file_without_dir_fd(
    root: Path, segment_name: str, filename: str
) -> tuple[int, os.stat_result]:
    """Reject platforms without handle-relative, no-reparse opens."""
    raise OSError(errno.ENOTSUP, "secure persisted HNSW opens are unavailable")


def _open_segment_file(
    root: Path,
    segment_name: str,
    filename: str,
    *,
    expected_root: tuple[int, int] | None = None,
    expected_segment: tuple[int, int] | None = None,
) -> tuple[int, os.stat_result]:
    """Open an active segment file without following a swapped path component."""
    if os.name == "nt":
        return _open_segment_file_without_dir_fd(root, segment_name, filename)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, directory_flags)
    except NotImplementedError:
        return _open_segment_file_without_dir_fd(root, segment_name, filename)
    try:
        root_info = os.fstat(root_fd)
        if expected_root is not None and (root_info.st_dev, root_info.st_ino) != expected_root:
            raise OSError(errno.ELOOP, "persisted HNSW root changed")
        try:
            segment_fd = os.open(
                segment_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
        except NotImplementedError:
            return _open_segment_file_without_dir_fd(root, segment_name, filename)
        try:
            segment_info = os.fstat(segment_fd)
            if (
                expected_segment is not None
                and (segment_info.st_dev, segment_info.st_ino) != expected_segment
            ):
                raise OSError(errno.ELOOP, "persisted HNSW segment changed")
            fd = os.open(filename, flags, dir_fd=segment_fd)
        finally:
            os.close(segment_fd)
    finally:
        os.close(root_fd)
    info = os.fstat(fd)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise OSError("persisted HNSW path is not a regular file")
    return fd, info


def validate_persisted_hnsw(
    chroma_path: Path, collection_name: str, *, allow_wal: bool = False
) -> None:
    """Check active collection segments without opening Chroma or unpickling.

    SQLite is opened read-only/immutable, so this probe creates no WAL/SHM
    files. Orphan segment directories are ignored. New indexes have no
    persisted graph yet. Unrecognized SQLite schemas are left to Chroma's
    existing format checks.
    """
    try:
        root_info = chroma_path.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            return
    except OSError:
        return
    db_path = chroma_path / "chroma.sqlite3"
    try:
        db_info = db_path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise IncompatibleIndexFormatError(
            chroma_path, reason="unable to inspect chroma.sqlite3; refusing native loading"
        ) from exc
    if is_reparse_link(db_path) or not stat.S_ISREG(db_info.st_mode):
        raise IncompatibleIndexFormatError(chroma_path, reason="unsafe chroma.sqlite3 path")

    # An uncoordinated caller must not validate the main database while
    # ignoring a WAL.  The index open path passes ``allow_wal`` only while it
    # holds the shared/exclusive index lock, allowing SQLite's read-only
    # connection to see the same committed generation as Chroma.
    wal_path = db_path.with_name(f"{db_path.name}-wal")
    try:
        wal_info = wal_path.lstat()
    except FileNotFoundError:
        wal_info = None
    except OSError as exc:
        raise IncompatibleIndexFormatError(
            chroma_path, reason="unable to inspect chroma.sqlite3-wal; refusing native loading"
        ) from exc
    if wal_info is not None:
        if is_reparse_link(wal_path) or not stat.S_ISREG(wal_info.st_mode):
            reason = "unsafe chroma.sqlite3-wal path"
        elif not allow_wal:
            reason = (
                "chroma.sqlite3-WAL is present; uncheckpointed Chroma state cannot be "
                "validated safely"
            )
        else:
            reason = ""
        if reason:
            raise IncompatibleIndexFormatError(chroma_path, reason=reason)

    shm_path = db_path.with_name(f"{db_path.name}-shm")
    try:
        shm_info = shm_path.lstat()
    except FileNotFoundError:
        shm_info = None
    except OSError as exc:
        raise IncompatibleIndexFormatError(
            chroma_path, reason="unable to inspect chroma.sqlite3-shm; refusing native loading"
        ) from exc
    if shm_info is not None and (is_reparse_link(shm_path) or not stat.S_ISREG(shm_info.st_mode)):
        raise IncompatibleIndexFormatError(chroma_path, reason="unsafe chroma.sqlite3-shm path")

    try:
        with connect_readonly(db_path, immutable=not allow_wal) as conn:
            segments = conn.execute(
                "SELECT s.id FROM segments s JOIN collections c ON s.collection=c.id "
                "WHERE s.type=? AND c.name=?",
                (_SEGMENT_TYPE, collection_name),
            ).fetchall()
    except OSError as exc:
        raise IncompatibleIndexFormatError(
            chroma_path, reason="unsafe chroma.sqlite3 path; refusing native loading"
        ) from exc
    except sqlite3.Error as exc:
        if allow_wal and wal_info is not None:
            raise IncompatibleIndexFormatError(
                chroma_path,
                reason="unable to validate WAL-backed SQLite state; refusing native loading",
            ) from exc
        return

    # Without the coordination lock a WAL can arrive after the first check;
    # do not accept that stale probe.  Under the lock, a writer cannot change
    # the generation, so a pre-existing WAL is valid and remains visible.
    try:
        final_wal_info = wal_path.lstat() if wal_path.exists() or wal_path.is_symlink() else None
        if final_wal_info is not None and (
            is_reparse_link(wal_path) or not stat.S_ISREG(final_wal_info.st_mode)
        ):
            raise IncompatibleIndexFormatError(chroma_path, reason="unsafe chroma.sqlite3-wal path")
        if not allow_wal and final_wal_info is not None:
            raise IncompatibleIndexFormatError(
                chroma_path,
                reason=(
                    "chroma.sqlite3-wal appeared during validation; uncheckpointed Chroma "
                    "state cannot be validated safely"
                ),
            )
        final_shm_info = shm_path.lstat() if shm_path.exists() or shm_path.is_symlink() else None
        if final_shm_info is not None and (
            is_reparse_link(shm_path) or not stat.S_ISREG(final_shm_info.st_mode)
        ):
            raise IncompatibleIndexFormatError(chroma_path, reason="unsafe chroma.sqlite3-shm path")
    except OSError as exc:
        raise IncompatibleIndexFormatError(
            chroma_path, reason="unable to recheck chroma.sqlite3-wal; refusing native loading"
        ) from exc

    for (segment_id,) in segments:
        # Persisted metadata must never turn this probe into a path traversal.
        try:
            segment_name = str(UUID(segment_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise IncompatibleIndexFormatError(
                chroma_path, reason="invalid persisted HNSW segment identifier"
            ) from exc
        segment_path = chroma_path / segment_name
        links_path = segment_path / "link_lists.bin"
        try:
            segment_info = segment_path.lstat()
            if is_reparse_link(segment_path) or not stat.S_ISDIR(segment_info.st_mode):
                raise OSError(errno.ELOOP, "unsafe persisted HNSW segment")
            expected_segment = (segment_info.st_dev, segment_info.st_ino)
            links_fd, links_info = _open_segment_file(
                chroma_path,
                segment_name,
                "link_lists.bin",
                expected_root=(root_info.st_dev, root_info.st_ino),
                expected_segment=expected_segment,
            )
        except OSError as exc:
            if exc.errno == getattr(os, "ENOENT", 2):
                continue
            if exc.errno in {
                getattr(os, "ELOOP", 62),
                getattr(os, "ENOTDIR", 20),
                getattr(errno, "ENOTSUP", 95),
            }:
                reason = f"unsafe persisted HNSW links in segment {segment_name}"
            else:
                reason = f"unreadable persisted HNSW links in segment {segment_name}"
            raise IncompatibleIndexFormatError(chroma_path, reason=reason) from exc
        try:
            try:
                header_fd, _header_info = _open_segment_file(
                    chroma_path,
                    segment_name,
                    "header.bin",
                    expected_root=(root_info.st_dev, root_info.st_ino),
                    expected_segment=expected_segment,
                )
            except OSError as exc:
                if exc.errno in {getattr(os, "ELOOP", 62), getattr(os, "ENOTDIR", 20)}:
                    reason = f"unsafe persisted HNSW header in segment {segment_name}"
                else:
                    reason = f"unreadable persisted HNSW header in segment {segment_name}"
                raise IncompatibleIndexFormatError(chroma_path, reason=reason) from exc
            try:
                try:
                    header = os.read(header_fd, _HEADER.size + 1)
                except OSError as exc:
                    raise IncompatibleIndexFormatError(
                        chroma_path,
                        reason=f"unreadable persisted HNSW header in segment {segment_name}",
                    ) from exc
            finally:
                os.close(header_fd)
            if len(header) != _HEADER.size:
                raise IncompatibleIndexFormatError(
                    chroma_path,
                    reason=f"unreadable persisted HNSW header in segment {segment_name}",
                )
            fields = _HEADER.unpack(header)
            version, capacity, count, max_level, max_m = (
                fields[0],
                fields[2],
                fields[3],
                fields[7],
                fields[9],
            )
            if (
                version != 1
                or capacity < count
                or capacity > _MAX_CAPACITY
                or count > _MAX_CAPACITY
                or max_level < -1
                or max_level > _MAX_LEVEL
                or max_m == 0
                or max_m > _MAX_M
            ):
                raise IncompatibleIndexFormatError(
                    chroma_path,
                    reason=f"unsupported persisted HNSW header in segment {segment_name}",
                )
            # Each slot stores a uint32 length and at most max_level upper levels,
            # each containing a uint32 neighbor count plus maxM uint32 neighbors.
            # Use capacity (rather than count) and one extra level conservatively,
            # allowing retained storage without accepting the observed runaway file.
            upper_bound = capacity * (4 + (max_m * 4 + 4) * (max(max_level, 0) + 1))
            # Keep the descriptor as the authority across a path swap.  The
            # pathname stat is only a compatibility probe for tests and older
            # callers; its inode must still match the already-open descriptor.
            try:
                observed = links_path.stat()
            except OSError as exc:
                raise IncompatibleIndexFormatError(
                    chroma_path, reason=f"unreadable persisted HNSW links in segment {segment_name}"
                ) from exc
            if (observed.st_dev, observed.st_ino) != (links_info.st_dev, links_info.st_ino):
                raise IncompatibleIndexFormatError(
                    chroma_path,
                    reason=(
                        f"unsafe persisted HNSW links in segment {segment_name}: "
                        "link_lists.bin was replaced during validation"
                    ),
                )
            size = observed.st_size
            if size > upper_bound:
                raise IncompatibleIndexFormatError(
                    chroma_path,
                    reason=(
                        f"corrupt persisted HNSW segment {segment_name}: link_lists.bin has "
                        f"{size} bytes, exceeding the header-derived capacity bound "
                        f"of {upper_bound} bytes; refusing native Chroma loading"
                    ),
                )
        finally:
            os.close(links_fd)
