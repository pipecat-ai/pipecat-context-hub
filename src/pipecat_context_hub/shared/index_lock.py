"""Advisory cross-process locks on the index data directory.

Chroma's local HNSW persistence is single-writer. Two ``refresh`` runs on one
data dir can interleave writes to the same segment files, so ``refresh`` takes
an exclusive ``refresh.lock`` for its whole run. ``serve`` holds a shared
``serve.lock`` for its lifetime so a concurrent ``refresh`` can warn that a
live server has the index open (it is not refused: servers stay up for days). Locks are OS-level (released when the process dies, even on SIGKILL),
so a crashed run never leaves a stale lock behind.
"""

from __future__ import annotations

import errno
import logging
import os
import time
from pathlib import Path
from types import TracebackType

logger = logging.getLogger(__name__)

# errno values that mean "someone else holds the lock". Anything else (ENOLCK,
# EOPNOTSUPP on NFS/SMB, EBADF...) means locking itself is unavailable, and a
# best-effort guard must fail open rather than refuse to run.
_CONTENTION = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})

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
        except OSError as exc:
            return _fail_open(exc)
        return True
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except OSError as exc:
        return _fail_open(exc)
    return True


def _fail_open(exc: OSError) -> bool:
    """False for genuine contention; True (lock unavailable, proceed) otherwise."""
    if exc.errno in _CONTENTION or exc.errno == getattr(errno, "EDEADLK", None):
        return False
    logger.debug("File locking unavailable (%s); continuing without a lock", exc)
    return True


def lock_path(data_dir: Path, name: str) -> Path:
    """Where the named lock for ``data_dir`` lives.

    A *sibling* of the data dir (``.<dirname>.<name>``), not a file inside it:
    ``refresh --reset-index`` rmtree's the data dir, which would unlink an
    in-tree lock out from under a running refresh/serve and let the next
    process lock a fresh inode. Resolved first so every spelling of the same
    directory (symlink, ``~``, relative) shares one lock.
    """
    try:
        resolved = data_dir.expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = data_dir.expanduser().absolute()
    if resolved.parent == resolved:  # filesystem root: nowhere to put a sibling
        return resolved / name
    return resolved.parent / f".{resolved.name.lstrip('.')}.{name}"


class IndexLock:
    """Non-blocking advisory lock beside the data dir (see :func:`lock_path`).

    ``exclusive=True`` fails if anyone holds the lock in any mode.
    ``exclusive=False`` (shared) fails only if an exclusive holder exists.
    """

    def __init__(self, data_dir: Path, name: str, *, exclusive: bool) -> None:
        self._path = lock_path(data_dir, name)
        self._exclusive = exclusive
        self._fd: int | None = None

    def acquire(self, *, attempts: int = 1, delay: float = 0.05) -> None:
        """Take the lock, retrying briefly on contention.

        Retries let a starting ``serve`` ride out the instant a ``refresh``
        probe (``serve_is_running``) holds the lock exclusively.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:  # unwritable parent: locking is best-effort
            logger.debug(
                "Cannot create lock file %s (%s); continuing without a lock", self._path, exc
            )
            return
        for attempt in range(max(attempts, 1)):
            if _try_lock(fd, exclusive=self._exclusive):
                self._fd = fd
                return
            if attempt + 1 < attempts:
                time.sleep(delay)
        os.close(fd)
        raise IndexLockedError(f"index data directory is locked by another process: {self._path}")

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
    """True if some process holds the shared ``serve.lock`` for ``data_dir``.

    Probes by attempting an exclusive lock and releasing it immediately; never
    blocks and never creates the data dir.
    """
    if not lock_path(data_dir, SERVE_LOCK).exists():
        return False
    probe = IndexLock(data_dir, SERVE_LOCK, exclusive=True)
    try:
        probe.acquire()
    except IndexLockedError:
        return True
    probe.release()
    return False
