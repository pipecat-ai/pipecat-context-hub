"""Handle-relative, read-only SQLite opens for persisted index probes."""

from __future__ import annotations

import contextlib
import errno
import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path


def _open_directory_chain(path: Path) -> int:
    """Open every directory component without following reparse points."""
    if os.open not in getattr(os, "supports_dir_fd", set()):
        raise OSError(errno.ENOTSUP, "secure SQLite opens are unavailable")
    # ``tempfile`` paths on macOS may be spelled through the system ``/var``
    # symlink.  Resolve that trusted spelling before the descriptor walk;
    # callers have already rejected reparse points in the published index.
    absolute = path.resolve(strict=True)
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


@contextlib.contextmanager
def connect_readonly(path: Path, *, immutable: bool) -> Iterator[sqlite3.Connection]:
    """Connect SQLite through an already-open, no-follow file descriptor.

    SQLite's URI pathname is ``/dev/fd/N`` rather than the published path, so
    replacing a directory component or the leaf after validation cannot make
    the probe follow a different database.  Platforms without descriptor-
    relative opens fail closed instead of falling back to a pathname open.
    """
    parent_fd = _open_directory_chain(path.parent)
    fd = -1
    conn: sqlite3.Connection | None = None
    try:
        fd = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.ELOOP, "SQLite database is not a regular file")
        query = "mode=ro" + ("&immutable=1" if immutable else "")
        conn = sqlite3.connect(f"file:/dev/fd/{fd}?{query}", uri=True)
        yield conn
    finally:
        if conn is not None:
            conn.close()
        if fd != -1:
            os.close(fd)
        os.close(parent_fd)
