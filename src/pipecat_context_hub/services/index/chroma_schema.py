"""Read-only access to Chroma's private on-disk schema.

Shared by the HNSW graph check (``hnsw_validation``) and the vector index's
format probe and orphan-segment cleanup (``vector``). Nothing here opens a
Chroma client.
"""

from __future__ import annotations

import functools
import importlib.metadata
import sqlite3
from pathlib import Path
from typing import Any

# chromadb (major, minor) releases whose on-disk HNSW layout and ``segments``
# table this package was verified against (tests/unit/test_hnsw_validation.py
# persists a real graph with the installed chromadb). pyproject allows any
# 1.x, so on an unverified release a header or structure mismatch is logged
# and skipped rather than fail-closing every healthy index; the size bound
# still applies to a header that parses as version 1.
VERIFIED_CHROMADB_RELEASES = frozenset({(1, 5)})


@functools.lru_cache(maxsize=1)
def chromadb_format_verified() -> bool:
    """True when the installed chromadb is a release this package was verified on."""
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


def installed_chromadb_version() -> str:
    """The installed chromadb version string, for log messages."""
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

    A rollback journal beside the database means a writer died mid-transaction
    (Chroma runs in ``delete`` journal mode). ``immutable=1`` would ignore it
    and read uncommitted pages, and a read-only connection cannot roll it back,
    so the read then opens read-write (never creating the database): SQLite
    rolls the journal back first, exactly as Chroma's own open is about to.
    """
    db_path = chroma_path / "chroma.sqlite3"
    if not db_path.is_file():
        return None
    if Path(f"{db_path}-journal").exists():
        mode = "?mode=rw"
    else:
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
