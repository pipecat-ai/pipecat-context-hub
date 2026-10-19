"""Advisory cross-process locks on the index data directory.

Chroma's local HNSW persistence is single-writer. Two ``refresh`` runs on one
data dir can interleave writes to the same segment files, so ``refresh`` takes
an exclusive ``refresh.lock`` for its whole run. ``serve`` holds a shared
``serve.lock`` for its lifetime so a concurrent ``refresh`` can warn that a
live server has the index open (it is not refused: servers stay up for days).
Readers hold ``refresh.lock`` shared while they open the index
(:func:`refresh_excluded`), so an open never overlaps a refresh. Locks are
OS-level (released when the process dies, even on SIGKILL), so a crashed run
never leaves a stale lock behind.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

from pipecat_context_hub.shared.interfaces import IndexUnavailableError

logger = logging.getLogger(__name__)

# errno values that mean "someone else holds the lock". Anything else (ENOLCK,
# EOPNOTSUPP on NFS/SMB, EBADF...) means locking itself is unavailable, and a
# best-effort guard must fail open rather than refuse to run.
_CONTENTION = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})

REFRESH_LOCK = "refresh.lock"
SERVE_LOCK = "serve.lock"

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# The refresh lock file's size steps through 1.._GENERATION_CYCLE, one step per
# exclusive acquire (see refresh_generation()); bounded so it never grows.
_GENERATION_CYCLE = 4096


class IndexLockedError(RuntimeError):
    """The data dir is locked by another process in an incompatible mode."""


def _unusable_lock_file(fd: int, path: Path) -> str | None:
    """Why the opened lock file must not be used, or None if it is fine.

    The lock may live in a shared directory, and an exclusive acquire writes
    to it (size step and mtime, see :func:`refresh_generation`), so it must be
    our own regular file with no other name: a planted hard link to a file we
    own would pass the owner check and get truncated. Ownership is POSIX only.
    Without ``O_NOFOLLOW`` (Windows) the open may have followed a symlink
    planted after :meth:`IndexLock.acquire` checked, so *path* must now be a
    non-link naming the very file *fd* opened: a path check alone would miss a
    link swapped in for the open and back out before the check.
    """
    try:
        st = os.fstat(fd)
    except OSError:
        st = None
    if not _O_NOFOLLOW:
        try:
            lst = os.lstat(path)
        except OSError:
            return "could not be checked"
        if stat.S_ISLNK(lst.st_mode):
            return "is a symbolic link"
        if st is None or (lst.st_dev, lst.st_ino) != (st.st_dev, st.st_ino):
            return "changed while it was being opened"
    if st is None:
        return None
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if st.st_nlink != 1:
        return "has other hard links"
    getuid = getattr(os, "getuid", None)
    if getuid is not None and st.st_uid != getuid():
        return "belongs to another user"
    return None


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
            return _fail_open(exc, exclusive=exclusive)
        return True
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except OSError as exc:
        return _fail_open(exc, exclusive=exclusive)
    return True


def _fail_open(exc: OSError, *, exclusive: bool) -> bool:
    """False for genuine contention; True (lock unavailable, proceed) otherwise.

    Proceeding unlocked is only worth a warning for an exclusive (writer)
    lock: a reader has nothing to protect that a writer's own warning does not
    already report, and readers run on every query.
    """
    if exc.errno in _CONTENTION or exc.errno == getattr(errno, "EDEADLK", None):
        return False
    log = logger.warning if exclusive else logger.debug
    log("File locking unavailable (%s); continuing without a lock", exc)
    return True


def lock_path(data_dir: Path, name: str) -> Path:
    """Where the named lock for ``data_dir`` lives.

    A *sibling* of the data dir (``<dirname>.<name>``, hidden when the data
    dir is), not a file inside it: ``refresh --reset-index`` rmtree's the data
    dir, which would unlink an in-tree lock out from under a running
    refresh/serve and let the next process lock a fresh inode. Resolved first
    so every spelling of the same directory (symlink, ``~``, relative) shares
    one lock, and named from the full dirname so distinct sibling dirs
    (``x`` and ``.x``) never share one.
    """
    try:
        resolved = data_dir.expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = data_dir.expanduser().absolute()
    if resolved.parent == resolved:  # filesystem root: nowhere to put a sibling
        return resolved / name
    return resolved.parent / f"{resolved.name}.{name}"


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
            # The lock lives beside the data dir, possibly in a shared
            # directory, so a planted symlink must not redirect it: refuse one
            # (with a warning), and where O_NOFOLLOW exists also refuse one
            # swapped in after this check. Without it (Windows) the open
            # follows links; the post-open identity check below catches that
            # (after the open may have created an empty file at a dangling
            # target: an accepted residual, see the dev plan).
            if self._path.is_symlink():
                logger.warning(
                    "Lock file %s is a symbolic link; continuing without a lock", self._path
                )
                return
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW, 0o600)
        except OSError as exc:  # unwritable parent / symlink: locking is best-effort
            # A writer (exclusive) running unprotected is worth a warning, as
            # for the symlink / not-a-regular-file cases below (e.g. a data dir
            # whose parent is read-only, such as a container volume mounted at
            # /data); readers stay quiet, since they run on every query.
            log = logger.warning if self._exclusive else logger.debug
            log("Cannot create lock file %s (%s); continuing without a lock", self._path, exc)
            return
        reason = _unusable_lock_file(fd, self._path)
        if reason is not None:
            os.close(fd)
            logger.warning("Lock file %s %s; continuing without a lock", self._path, reason)
            return
        for attempt in range(max(attempts, 1)):
            if _try_lock(fd, exclusive=self._exclusive):
                self._fd = fd
                if self._exclusive:
                    # Mark the file so readers can tell a lock was taken (and so
                    # a refresh happened) even if they never observed it held.
                    # See refresh_generation(). The mtime alone can repeat on a
                    # coarse-timestamp filesystem (1-2 s on HFS+, FAT, some
                    # network mounts), so also step the size, which every
                    # exclusive acquire changes.
                    with contextlib.suppress(OSError):
                        os.ftruncate(fd, os.fstat(fd).st_size % _GENERATION_CYCLE + 1)
                    with contextlib.suppress(OSError, NotImplementedError):
                        os.utime(fd if os.utime in os.supports_fd else self._path)
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


REFRESH_IN_PROGRESS_MESSAGE = (
    "The Pipecat context index is being rebuilt by a running `refresh`, and reading it "
    "mid-rebuild is unsafe. Retry in 60-120 seconds."
)


class RefreshInProgressError(IndexUnavailableError):
    """A ``refresh`` is rewriting the index; the caller should retry later."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or REFRESH_IN_PROGRESS_MESSAGE)


# Reader probes retry briefly: on Windows (no shared lock mode) two concurrent
# readers can collide and would otherwise report a refresh that is not running.
_READER_ATTEMPTS = 3
_READER_DELAY = 0.02


@contextlib.contextmanager
def refresh_excluded(data_dir: Path) -> Iterator[None]:
    """Hold off any ``refresh`` while the body opens the index.

    Takes a shared lock on ``refresh.lock`` for the duration of the block, so
    a refresh can neither be running when the body starts nor start while it
    runs (``refresh`` retries its exclusive lock for a while to ride this out).
    A probe-then-open would leave a window in which a refresh starts between
    the probe and the open; opening mid-write is the corruption trigger
    (2026-10-15 spikes). Raises :class:`RefreshInProgressError` if a refresh
    holds the lock. Unlike :func:`refresh_in_progress` this creates the lock
    file if needed: a lock nobody can hold excludes nothing.
    """
    lock = IndexLock(data_dir, REFRESH_LOCK, exclusive=False)
    try:
        lock.acquire(attempts=_READER_ATTEMPTS, delay=_READER_DELAY)
    except IndexLockedError as exc:
        raise RefreshInProgressError() from exc
    try:
        yield
    finally:
        lock.release()


def refresh_in_progress(data_dir: Path) -> bool:
    """True if a ``refresh`` currently holds its exclusive lock for ``data_dir``.

    Probes with a non-blocking shared lock, so it never waits for a refresh and
    never creates the lock file. Readers (``serve``, the one-shot CLI) use it
    to avoid opening or querying an index that a refresh is rewriting. Retries
    briefly: on Windows (no shared lock mode) two concurrent probes can collide
    and would otherwise report a refresh that is not running.
    """
    if not lock_path(data_dir, REFRESH_LOCK).exists():
        return False
    probe = IndexLock(data_dir, REFRESH_LOCK, exclusive=False)
    try:
        probe.acquire(attempts=_READER_ATTEMPTS, delay=_READER_DELAY)
    except IndexLockedError:
        return True
    probe.release()
    return False


def refresh_generation(data_dir: Path) -> tuple[int, int, int] | None:
    """Identity of the most recent refresh start, or None if none ever ran.

    ``refresh`` touches its lock file and steps its size on acquire, so this
    changes whenever a refresh has begun since it was last read, even one that
    started and finished between two reader calls (which
    ``refresh_in_progress`` cannot see) or within one filesystem timestamp
    tick. A reader that holds the index open compares it on each call.
    """
    try:
        st = lock_path(data_dir, REFRESH_LOCK).stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)
