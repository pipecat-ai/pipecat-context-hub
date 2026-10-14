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
    """Windows-compatible path open with the same component safety checks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    segment_path = root / segment_name
    file_path = segment_path / filename
    root_info = root.lstat()
    segment_info = segment_path.lstat()
    file_info = file_path.lstat()
    if (
        is_reparse_link(root)
        or not stat.S_ISDIR(root_info.st_mode)
        or is_reparse_link(segment_path)
        or not stat.S_ISDIR(segment_info.st_mode)
        or is_reparse_link(file_path)
        or not stat.S_ISREG(file_info.st_mode)
    ):
        raise OSError(errno.ELOOP, "unsafe persisted HNSW path")
    fd = os.open(file_path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.ELOOP, "persisted HNSW path is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def _open_segment_file(root: Path, segment_name: str, filename: str) -> tuple[int, os.stat_result]:
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
        try:
            segment_fd = os.open(
                segment_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
        except NotImplementedError:
            return _open_segment_file_without_dir_fd(root, segment_name, filename)
        try:
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


def validate_persisted_hnsw(chroma_path: Path, collection_name: str) -> None:
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
    if not db_path.is_file():
        return
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)
        segments = conn.execute(
            "SELECT s.id FROM segments s JOIN collections c ON s.collection=c.id "
            "WHERE s.type=? AND c.name=?",
            (_SEGMENT_TYPE, collection_name),
        ).fetchall()
    except sqlite3.Error:
        return
    finally:
        if conn is not None:
            conn.close()

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
            links_fd, links_info = _open_segment_file(chroma_path, segment_name, "link_lists.bin")
        except OSError as exc:
            if exc.errno == getattr(os, "ENOENT", 2):
                continue
            if exc.errno in {getattr(os, "ELOOP", 62), getattr(os, "ENOTDIR", 20)}:
                reason = f"unsafe persisted HNSW links in segment {segment_name}"
            else:
                reason = f"unreadable persisted HNSW links in segment {segment_name}"
            raise IncompatibleIndexFormatError(chroma_path, reason=reason) from exc
        try:
            try:
                header_fd, _header_info = _open_segment_file(
                    chroma_path, segment_name, "header.bin"
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
            size = (
                observed.st_size
                if (observed.st_dev, observed.st_ino)
                == (
                    links_info.st_dev,
                    links_info.st_ino,
                )
                else links_info.st_size
            )
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
