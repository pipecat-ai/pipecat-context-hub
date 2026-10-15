"""Read-only rejection of oversized Chroma HNSW link-list files.

Chroma 1.5.x can SIGSEGV while loading a corrupt persisted graph. This is
a bounded size check for the observed sparse-file corruption, not a complete
graph validator. It must run before constructing the native client.
"""

from __future__ import annotations

import functools
import importlib.metadata
import logging
import os
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from pipecat_context_hub.services.index.errors import CorruptHnswGraphError

logger = logging.getLogger(__name__)

# Chroma's persisted HNSW v1 header (64-bit, little-endian), written by
# chroma-core/hnswlib's hnswalg.h. See upstream corruption report #7510:
# https://github.com/chroma-core/chroma/issues/7510
_HEADER = struct.Struct("<i6Qii3QdQ")
_SEGMENT_TYPE = "urn:chroma:segment/vector/hnsw-local-persisted"
_U32 = struct.Struct("<I")

# chromadb (major, minor) releases whose on-disk HNSW layout and ``segments``
# table this module was verified against (tests/unit/test_hnsw_validation.py
# persists a real graph with the installed chromadb). pyproject allows any
# 1.x, so on an unverified release a header or structure mismatch is logged
# and skipped rather than fail-closing every healthy index; the size bound
# still applies to a header that parses as version 1.
VERIFIED_CHROMADB_RELEASES = frozenset({(1, 5)})

# Absolute ceiling on link_lists.bin, independent of the header. A healthy
# record averages ~10 bytes (45k records -> well under 1 MB), so 256 MiB is
# tens of millions of records: far beyond this corpus. It bounds the memory and
# time of the structural walk even when a damaged header yields a huge bound.
MAX_LINK_LISTS_BYTES = 256 * 1024**2


@functools.lru_cache(maxsize=1)
def chromadb_format_verified() -> bool:
    """True when the installed chromadb is a release this module was verified on."""
    try:
        version = importlib.metadata.version("chromadb")
    except importlib.metadata.PackageNotFoundError:
        return False
    parts = version.split(".")
    try:
        release = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        return False
    return release in VERIFIED_CHROMADB_RELEASES


_unverified_warned = False


def _unverified_problem(reason: str) -> None:
    """Log (once per process) a check result we cannot trust on this chromadb."""
    global _unverified_warned
    if not _unverified_warned:
        _unverified_warned = True
        logger.warning(
            "Skipping the HNSW graph check: chromadb %s is not a release it was "
            "verified against, and it reported: %s",
            _installed_chromadb_version(),
            reason,
        )


def _installed_chromadb_version() -> str:
    try:
        return importlib.metadata.version("chromadb")
    except importlib.metadata.PackageNotFoundError:
        return "(not installed)"


def query_chroma_sqlite(
    chroma_path: Path, sql: str, params: tuple[object, ...] = (), *, immutable: bool
) -> list[tuple[Any, ...]] | None:
    """Rows from Chroma's private ``chroma.sqlite3``; None if absent or unreadable.

    The one place that reads Chroma's schema behind its back. Always
    read-only, and never creates the database. ``immutable=True`` also skips
    SQLite's locking and journal handling, so it creates no -wal/-shm files
    (legacy 0.6 directories included): for probes that run before a Chroma
    client opens, when no writer can be running (readers open under
    ``refresh_excluded``; ``refresh``, the only writer, holds the lock
    exclusively). ``immutable=False`` takes SQLite's shared lock for a
    consistent snapshot: required before deleting anything based on it.
    """
    db_path = chroma_path / "chroma.sqlite3"
    if not db_path.is_file():
        return None
    mode = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    try:
        conn = sqlite3.connect(db_path.absolute().as_uri() + mode, uri=True)
    except sqlite3.Error:
        return None
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def validate_persisted_hnsw(chroma_path: Path, collection_name: str) -> None:
    """Check active collection segments without opening Chroma or unpickling.

    SQLite is opened read-only/immutable, so this probe creates no WAL/SHM
    files. Orphan segment directories are ignored. New indexes have no
    persisted graph yet. Unrecognized SQLite schemas are left to Chroma's
    existing format checks.
    """
    segments = query_chroma_sqlite(
        chroma_path,
        "SELECT s.id FROM segments s JOIN collections c ON s.collection=c.id "
        "WHERE s.type=? AND c.name=?",
        (_SEGMENT_TYPE, collection_name),
        immutable=True,
    )
    if segments is None:
        return

    for (segment_id,) in segments:
        # Persisted metadata must never turn this probe into a path traversal.
        try:
            segment_name = str(UUID(segment_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CorruptHnswGraphError(
                chroma_path, reason="invalid persisted HNSW segment identifier"
            ) from exc
        check_hnsw_segment_dir(chroma_path, chroma_path / segment_name)


def check_hnsw_segment_dir(chroma_path: Path, segment_path: Path) -> None:
    """Reject one persisted HNSW segment whose link-list file is oversized or damaged.

    Reads ``header.bin`` and at most a bounded prefix of ``link_lists.bin``.
    A segment with no link-list file yet (never persisted) passes.
    """
    segment_name = segment_path.name
    links_path = segment_path / "link_lists.bin"
    if not links_path.is_file():
        return
    header_path = segment_path / "header.bin"
    try:
        with header_path.open("rb") as stream:
            header = stream.read(_HEADER.size + 1)
    except OSError as exc:
        raise CorruptHnswGraphError(
            chroma_path, reason=f"unreadable persisted HNSW header in segment {segment_name}"
        ) from exc
    if len(header) != _HEADER.size:
        _header_problem(chroma_path, f"unreadable persisted HNSW header in segment {segment_name}")
        return
    fields = _HEADER.unpack(header)
    version, capacity, count, max_level, max_m = (
        fields[0],
        fields[2],
        fields[3],
        fields[7],
        fields[9],
    )
    if version != 1 or capacity < count or max_level < -1 or max_m == 0:
        _header_problem(chroma_path, f"unsupported persisted HNSW header in segment {segment_name}")
        return
    # Each slot stores a uint32 length and at most max_level upper levels,
    # each containing a uint32 neighbor count plus maxM uint32 neighbors.
    # Use capacity (rather than count) and one extra level conservatively,
    # allowing retained storage without accepting the observed runaway file.
    upper_bound = capacity * (4 + (max_m * 4 + 4) * (max(max_level, 0) + 1))
    size = links_path.stat().st_size
    _check_size(chroma_path, segment_name, size, upper_bound)
    _walk_link_lists(chroma_path, segment_name, links_path, count, max_level, max_m, upper_bound)


def _header_problem(chroma_path: Path, reason: str) -> None:
    """A header this module cannot read: fatal on a verified chromadb, else warn."""
    if chromadb_format_verified():
        raise CorruptHnswGraphError(chroma_path, reason=reason)
    _unverified_problem(reason)


def _check_size(chroma_path: Path, segment_name: str, size: int, upper_bound: int) -> None:
    """Reject a link-list file above the header bound or the absolute ceiling."""
    if size > upper_bound:
        raise CorruptHnswGraphError(
            chroma_path,
            reason=(
                f"corrupt persisted HNSW segment {segment_name}: link_lists.bin has "
                f"{size} bytes, exceeding the header-derived capacity bound "
                f"of {upper_bound} bytes"
            ),
        )
    if size > MAX_LINK_LISTS_BYTES:
        raise CorruptHnswGraphError(
            chroma_path,
            reason=(
                f"corrupt persisted HNSW segment {segment_name}: link_lists.bin has "
                f"{size} bytes, exceeding the {MAX_LINK_LISTS_BYTES}-byte ceiling"
            ),
        )


def _walk_link_lists(
    chroma_path: Path,
    segment_name: str,
    links_path: Path,
    count: int,
    max_level: int,
    max_m: int,
    upper_bound: int,
) -> None:
    """Structurally validate a within-bound ``link_lists.bin``.

    The size bound alone misses the dangerous case: a graph left inconsistent
    by two racing writers has the right size and header, but its next load or
    persist either SIGSEGVs or grows the file without bound (spike, 2026-10-15;
    upstream chroma#7510 — unvalidated ``linkListSize`` on load). The file is a
    plain sequence of ``count`` records, each a uint32 byte length (0 = no
    upper levels) followed by that many bytes made of ``max_m*4+4``-byte level
    blocks (uint32 neighbour count, then neighbour ids).

    The file is opened once and its size re-checked on that descriptor, then
    at most that many bytes are read: a file that grows after the check above
    (the runaway this guards against) can never be read to EOF.
    """
    stride = max_m * 4 + 4
    try:
        with links_path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            _check_size(chroma_path, segment_name, size, upper_bound)
            data = stream.read(size)
    except OSError as exc:
        raise CorruptHnswGraphError(
            chroma_path, reason=f"unreadable persisted HNSW link lists in segment {segment_name}"
        ) from exc
    problem = _first_link_list_problem(data, count, max_level, max_m, stride)
    if problem is None:
        return
    reason = f"corrupt persisted HNSW segment {segment_name}: link_lists.bin {problem}"
    if not chromadb_format_verified():
        _unverified_problem(reason)
        return
    raise CorruptHnswGraphError(chroma_path, reason=reason)


def _first_link_list_problem(
    data: bytes, count: int, max_level: int, max_m: int, stride: int
) -> str | None:
    size = len(data)
    if count * 4 > size:  # every record is at least its 4-byte length
        return f"is truncated: {size} bytes cannot hold {count} elements"
    pos = 0
    for element in range(count):
        if pos + 4 > size:
            return f"is truncated at element {element} of {count}"
        (length,) = _U32.unpack_from(data, pos)
        pos += 4
        if length == 0:
            continue
        if length % stride or length // stride > max_level:
            return (
                f"element {element} has link size {length}, not a multiple of the "
                f"{stride}-byte level block within {max_level} levels"
            )
        if pos + length > size:
            return f"element {element} runs past the end of the file"
        for level in range(length // stride):
            base = pos + level * stride
            (neighbours,) = _U32.unpack_from(data, base)
            neighbours &= 0xFFFF
            if neighbours > max_m:
                return f"element {element} level {level + 1} lists {neighbours} neighbours (max {max_m})"
            for (neighbour,) in struct.iter_unpack(
                "<I", data[base + 4 : base + 4 + 4 * neighbours]
            ):
                if neighbour >= count:
                    return f"element {element} references neighbour {neighbour} >= {count}"
        pos += length
    if pos != size:
        return f"has {size - pos} unexpected trailing bytes after {count} elements"
    return None


class HnswWriteGuard:
    """Detect a runaway link-list file *while* this process is writing.

    ``validate_persisted_hnsw`` only runs when an index is opened, so a graph
    that goes bad mid-refresh keeps growing until the disk is full. This guard
    re-runs the same bounded size check on every segment directory touched
    since the guard was created. Directories whose ``link_lists.bin`` was last
    written earlier (old orphan segments) are ignored: they are not this run's
    problem and must not block a rebuild.
    """

    # Filesystem mtime granularity slack, so a file written in the same tick
    # the guard was created is still considered ours.
    _MTIME_SLACK_SECONDS = 2.0

    def __init__(self, chroma_path: Path) -> None:
        self._chroma_path = chroma_path
        self._since = time.time() - self._MTIME_SLACK_SECONDS

    def check(self) -> None:
        try:
            entries = list(self._chroma_path.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                UUID(entry.name)
                links = entry / "link_lists.bin"
                if not entry.is_dir() or links.stat().st_mtime < self._since:
                    continue
            except (ValueError, OSError):
                continue
            check_hnsw_segment_dir(self._chroma_path, entry)
