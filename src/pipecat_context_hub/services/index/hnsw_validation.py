"""Read-only rejection of oversized Chroma HNSW link-list files.

Chroma 1.5.x can SIGSEGV while loading a corrupt persisted graph. This is
a bounded size check for the observed sparse-file corruption, not a complete
graph validator. It must run before constructing the native client.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from uuid import UUID

from pipecat_context_hub.services.index.errors import IncompatibleIndexFormatError

# Chroma's persisted HNSW v1 header (64-bit, little-endian), written by
# chroma-core/hnswlib's hnswalg.h. See upstream corruption report #7510:
# https://github.com/chroma-core/chroma/issues/7510
_HEADER = struct.Struct("<i6Qii3QdQ")
_SEGMENT_TYPE = "urn:chroma:segment/vector/hnsw-local-persisted"


def validate_persisted_hnsw(chroma_path: Path, collection_name: str) -> None:
    """Check active collection segments without opening Chroma or unpickling.

    SQLite is opened read-only/immutable, so this probe creates no WAL/SHM
    files. Orphan segment directories are ignored. New indexes have no
    persisted graph yet. Unrecognized SQLite schemas are left to Chroma's
    existing format checks.
    """
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
        if not links_path.is_file():
            continue
        header_path = segment_path / "header.bin"
        try:
            with header_path.open("rb") as stream:
                header = stream.read(_HEADER.size + 1)
        except OSError as exc:
            raise IncompatibleIndexFormatError(
                chroma_path, reason=f"unreadable persisted HNSW header in segment {segment_name}"
            ) from exc
        if len(header) != _HEADER.size:
            raise IncompatibleIndexFormatError(
                chroma_path, reason=f"unreadable persisted HNSW header in segment {segment_name}"
            )
        fields = _HEADER.unpack(header)
        version, capacity, count, max_level, max_m = (
            fields[0],
            fields[2],
            fields[3],
            fields[7],
            fields[9],
        )
        if version != 1 or capacity < count or max_level < -1 or max_m == 0:
            raise IncompatibleIndexFormatError(
                chroma_path, reason=f"unsupported persisted HNSW header in segment {segment_name}"
            )
        # Each slot stores a uint32 length and at most max_level upper levels,
        # each containing a uint32 neighbor count plus maxM uint32 neighbors.
        # Use capacity (rather than count) and one extra level conservatively,
        # allowing retained storage without accepting the observed runaway file.
        upper_bound = capacity * (4 + (max_m * 4 + 4) * (max(max_level, 0) + 1))
        size = links_path.stat().st_size
        if size > upper_bound:
            raise IncompatibleIndexFormatError(
                chroma_path,
                reason=(
                    f"corrupt persisted HNSW segment {segment_name}: link_lists.bin has "
                    f"{size} bytes, exceeding the header-derived capacity bound "
                    f"of {upper_bound} bytes; refusing native Chroma loading"
                ),
            )
