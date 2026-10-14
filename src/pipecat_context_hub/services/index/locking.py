"""Small cross-platform advisory lock for the published index directory."""

from __future__ import annotations

import contextlib
import asyncio
import os
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any


_held = threading.local()


def _lock_owner() -> tuple[str, int]:
    """Identify the current synchronous owner without inheriting task state."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        return ("task", id(task))
    return ("thread", threading.get_ident())


def _held_locks() -> dict[Path, tuple[int, bool, int, tuple[str, int]]]:
    locks = getattr(_held, "locks", None)
    if locks is None:
        locks = {}
        _held.locks = locks
    return locks


@contextlib.contextmanager
def index_lock(data_dir: Path, *, exclusive: bool, create: bool = True) -> Iterator[None]:
    """Hold the index lock while a backend is opened or its files are copied.

    POSIX uses ``flock``; Windows uses the CRT byte-range lock.  The lock file
    is deliberately outside Chroma so a read-only snapshot can coordinate with
    a writer before opening any native Chroma state.
    """
    # Keep coordination beside, rather than inside, the published index. A
    # reset removes the index directory, and deleting an open lock file would
    # let a new generation create a second inode that no longer coordinates
    # with readers holding the old one.
    data_dir = data_dir.absolute()
    lock_path = data_dir.parent / f".{data_dir.name}.index.lock"
    try:
        lock_parent_info = data_dir.parent.lstat()
        if stat.S_ISLNK(lock_parent_info.st_mode) or not stat.S_ISDIR(lock_parent_info.st_mode):
            raise OSError("index lock parent is not a regular directory")
        parent_info = data_dir.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise OSError("index lock target is not a regular directory")
    except FileNotFoundError as exc:
        # The deletion path locks before removing the data directory. It still
        # has a regular parent, so an absent target is safe and coordinated.
        if data_dir.exists() or not data_dir.parent.exists():
            raise ValueError("Unsafe index lock path") from exc
        parent_info = None
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Unsafe index lock path") from exc
    lock_path = lock_path.absolute()
    held = _held_locks()
    owner = _lock_owner()
    existing = held.get(lock_path)
    if existing is not None:
        fd, held_exclusive, depth, held_owner = existing
        if held_owner != owner:
            # A thread-local entry inherited by another coroutine must never
            # be mistaken for reentrancy. It gets its own OS lock instead.
            existing = None
        else:
            if exclusive and not held_exclusive:
                raise RuntimeError("cannot upgrade a shared index lock")
            held[lock_path] = (fd, held_exclusive, depth + 1, held_owner)
            try:
                yield
            finally:
                current = held[lock_path]
                if current[2] == 1:
                    del held[lock_path]
                else:
                    held[lock_path] = (current[0], current[1], current[2] - 1, current[3])
            return

    # A reader must create the stable coordination inode when opening a
    # legacy index that predates locking. This mutates only the parent-side
    # lock namespace, never the published index directory itself.
    flags = (os.O_CREAT | os.O_RDWR) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(lock_path, flags, 0o600)
        if parent_info is not None:
            current_info = data_dir.lstat()
            if (current_info.st_dev, current_info.st_ino) != (
                parent_info.st_dev,
                parent_info.st_ino,
            ):
                raise ValueError("Unsafe index lock path")
    except FileNotFoundError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        if fd != -1:
            os.close(fd)
        if isinstance(exc, ValueError) and str(exc) == "Unsafe index lock path":
            raise
        raise ValueError("Unsafe index lock path") from exc
    held[lock_path] = (fd, exclusive, 1, owner)
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
        held_entry = held.get(lock_path)
        if held_entry is not None:
            if held_entry[0] == fd and held_entry[3] == owner:
                held.pop(lock_path, None)
        os.close(fd)
