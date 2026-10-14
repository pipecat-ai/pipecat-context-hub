"""Small cross-platform advisory lock for the published index directory."""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any


@contextlib.contextmanager
def index_lock(data_dir: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold the index lock while a backend is opened or its files are copied.

    POSIX uses ``flock``; Windows uses the CRT byte-range lock.  The lock file
    is deliberately outside Chroma so a read-only snapshot can coordinate with
    a writer before opening any native Chroma state.
    """
    lock_path = data_dir / ".index.lock"
    try:
        parent_info = data_dir.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise OSError("index lock parent is not a regular directory")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Unsafe index lock path") from exc
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(lock_path, flags, 0o600)
        current_info = data_dir.lstat()
        if (current_info.st_dev, current_info.st_ino) != (
            parent_info.st_dev,
            parent_info.st_ino,
        ):
            raise ValueError("Unsafe index lock path")
    except (OSError, RuntimeError, ValueError) as exc:
        if fd != -1:
            os.close(fd)
        if isinstance(exc, ValueError) and str(exc) == "Unsafe index lock path":
            raise
        raise ValueError("Unsafe index lock path") from exc
    try:
        if os.name == "nt":
            msvcrt: Any = __import__("msvcrt")

            # Lock one byte at the start of the file.  The descriptor is kept
            # open for the whole context, so the lock cannot be released early.
            mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_RLCK
            msvcrt.locking(fd, mode, 1)
            try:
                yield
            finally:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
