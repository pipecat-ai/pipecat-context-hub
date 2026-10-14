"""Handle-relative, read-only SQLite opens for persisted index probes."""

from __future__ import annotations

import contextlib
import errno
import os
import sqlite3
import stat
import tempfile
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


def _link_open_fd(fd: int, target: Path) -> None:
    """Hard-link an open descriptor without reopening its pathname."""
    last_error: OSError | None = None
    for prefix in ("/proc/self/fd", "/dev/fd"):
        try:
            os.link(f"{prefix}/{fd}", target)
            return
        except OSError as exc:
            last_error = exc
    raise OSError(errno.ENOTSUP, "cannot preserve SQLite sidecar visibility") from last_error


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
    sidecar_fds: list[int] = []
    temporary: tempfile.TemporaryDirectory[str] | None = None
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
        if immutable:
            query = "mode=ro&immutable=1"
            conn = sqlite3.connect(f"file:/dev/fd/{fd}?{query}", uri=True)
        else:
            # SQLite derives WAL/SHM names from the URI pathname.  A
            # descriptor URI would therefore look for ``/dev/fd/N-wal`` and
            # silently read a stale main database.  Bind the already-open
            # database and existing sidecars into a private directory so the
            # real WAL remains visible while the descriptor identity is still
            # authoritative.  If this cannot be done, fail closed before any
            # native index code can run.
            temporary = tempfile.TemporaryDirectory(prefix="pipecat-context-hub-sqlite-")
            snapshot = Path(temporary.name) / path.name
            try:
                _link_open_fd(fd, snapshot)
                for suffix in ("-wal", "-shm"):
                    sidecar = path.with_name(path.name + suffix)
                    try:
                        sidecar_fd = os.open(
                            sidecar.name,
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=parent_fd,
                        )
                    except FileNotFoundError:
                        continue
                    sidecar_info = os.fstat(sidecar_fd)
                    if not stat.S_ISREG(sidecar_info.st_mode):
                        os.close(sidecar_fd)
                        raise OSError(errno.ELOOP, "SQLite sidecar is not a regular file")
                    sidecar_fds.append(sidecar_fd)
                    _link_open_fd(sidecar_fd, snapshot.with_name(path.name + suffix))
                conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
            except OSError:
                # macOS does not permit hard-linking /dev/fd descriptors. The
                # validated real path retains WAL visibility there; bind it to
                # the opened identities before and after SQLite connects.
                for sidecar_fd in sidecar_fds:
                    os.close(sidecar_fd)
                sidecar_fds.clear()
                temporary.cleanup()
                temporary = None
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise OSError(errno.ELOOP, "SQLite database changed during open")
        yield conn
    finally:
        if conn is not None:
            conn.close()
        if fd != -1:
            os.close(fd)
        for sidecar_fd in sidecar_fds:
            os.close(sidecar_fd)
        if temporary is not None:
            temporary.cleanup()
        os.close(parent_fd)
