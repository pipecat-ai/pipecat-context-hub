"""Advisory cross-process locks on the index data directory.

Chroma's local HNSW persistence is single-writer. Two ``refresh`` runs on one
data dir can interleave writes to the same segment files, so ``refresh`` takes
an exclusive ``refresh.lock`` for its whole run. ``serve`` holds a shared
``serve.lock`` for its lifetime so a concurrent ``refresh`` can warn that a
live server has the index open (it is not refused: servers stay up for days). Locks are OS-level (released when the process dies, even on SIGKILL),
so a crashed run never leaves a stale lock behind.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

REFRESH_LOCK = "refresh.lock"
SERVE_LOCK = "serve.lock"


class IndexLockedError(RuntimeError):
    """The data dir is locked by another process in an incompatible mode."""


def _try_lock(fd: int, *, exclusive: bool) -> bool:
    """Take a non-blocking lock on ``fd``; return False if it is held."""
    try:
        import fcntl
    except ImportError:  # Windows: no shared mode; msvcrt locks are exclusive.
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


class IndexLock:
    """Non-blocking advisory lock on ``<data_dir>/<name>``.

    ``exclusive=True`` fails if anyone holds the lock in any mode.
    ``exclusive=False`` (shared) fails only if an exclusive holder exists.
    """

    def __init__(self, data_dir: Path, name: str, *, exclusive: bool) -> None:
        self._path = data_dir / name
        self._exclusive = exclusive
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        if not _try_lock(fd, exclusive=self._exclusive):
            os.close(fd)
            raise IndexLockedError(
                f"index data directory is locked by another process: {self._path}"
            )
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the flock
            self._fd = None

    def __enter__(self) -> IndexLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def serve_is_running(data_dir: Path) -> bool:
    """True if some process holds the shared ``serve.lock`` on ``data_dir``.

    Probes by attempting an exclusive lock and releasing it immediately; never
    blocks and never creates the data dir.
    """
    if not (data_dir / SERVE_LOCK).exists():
        return False
    probe = IndexLock(data_dir, SERVE_LOCK, exclusive=True)
    try:
        probe.acquire()
    except IndexLockedError:
        return True
    probe.release()
    return False
