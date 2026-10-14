"""Small cross-platform advisory lock for the published index directory."""

from __future__ import annotations

import contextlib
import asyncio
import contextvars
import os
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pipecat_context_hub.shared.paths import is_reparse_link


_held = threading.local()
_generation_token: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "pipecat_hub_generation_lock", default=None
)
_generation_leases: dict[tuple[Path, object], bool] = {}
_generation_leases_guard = threading.Lock()
# Keep a holder count, not just one identity.  Multiple worker threads in the
# same process can hold the same sibling inode concurrently; dropping the
# entry when the first one exits would let a replacement inode split the
# coordination domain while another descriptor is still locked.
_active_lock_identities: dict[Path, tuple[tuple[int, int], int]] = {}
_active_lock_identities_guard = threading.Lock()


def _lock_path(data_dir: Path) -> Path:
    data_dir = data_dir.absolute()
    return data_dir.parent / f".{data_dir.name}.index.lock"


@contextlib.contextmanager
def generation_lock(data_dir: Path) -> Iterator[None]:
    """Hold one exclusive generation lock across an async refresh.

    ``asyncio.to_thread`` copies context variables, so backend operations made
    by a refresh can reuse this lease without trying to acquire the same OS
    lock from a second worker-thread descriptor. Other processes still block on
    the descriptor held by the outer context.
    """
    # Refresh may be the first operation for a nested custom path. Resolve the
    # spelling before creating its missing parents so the lock itself is still
    # beside the canonical generation and never follows a link at the target.
    data_dir = data_dir.expanduser().resolve(strict=False)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(data_dir)
    token = object()
    with index_lock(data_dir, exclusive=True):
        marker = _generation_token.set(token)
        with _generation_leases_guard:
            _generation_leases[(lock_path, token)] = True
        try:
            yield
        finally:
            with _generation_leases_guard:
                _generation_leases.pop((lock_path, token), None)
            _generation_token.reset(marker)


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
    lock_path = _lock_path(data_dir)
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
    token = _generation_token.get()
    if token is not None:
        with _generation_leases_guard:
            lease_exclusive = _generation_leases.get((lock_path, token))
        if lease_exclusive:
            yield
            return
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

    # Read-only callers must not create a coordination inode. Legacy indexes
    # may predate the sibling lock, so coordinate through the existing index
    # directory itself until a writer publishes the stable sibling lock. This
    # keeps the read-only contract (no new files) while preventing a writer
    # from creating the lock and replacing the generation mid-snapshot.
    try:
        lock_info = lock_path.lstat()
    except FileNotFoundError:
        lock_info = None
    if lock_info is not None:
        if stat.S_ISLNK(lock_info.st_mode) or not stat.S_ISREG(lock_info.st_mode):
            raise ValueError("Unsafe index lock path")
        lock_identity = (lock_info.st_dev, lock_info.st_ino)
    else:
        lock_identity = None
    # A lock inode replacement while another owner is active splits the
    # coordination domain: a contender must fail closed rather than acquire a
    # fresh sibling inode while the original descriptor is still held.
    with _active_lock_identities_guard:
        active_entry = _active_lock_identities.get(lock_path)
        active_identity = active_entry[0] if active_entry is not None else None
    if active_identity is not None and lock_identity != active_identity:
        raise ValueError("Unsafe index lock path")
    # A sibling lock inode can be unlinked and recreated by another process;
    # POSIX flock would then treat the replacement as an unrelated lock. Hold
    # an exclusive descriptor lock on the stable parent directory for the
    # complete sibling-lock lifetime so no contender can pass the path check
    # while an older inode is still held. This intentionally serializes index
    # operations across processes; correctness is more important than reader
    # parallelism for the small local metadata/index critical sections.
    parent_guard_fd = -1

    def release_parent_guard() -> None:
        """Release the parent guard at most once."""
        nonlocal parent_guard_fd
        if parent_guard_fd == -1:
            return
        fd = parent_guard_fd
        parent_guard_fd = -1
        with contextlib.suppress(OSError):
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    if os.name != "nt":
        try:
            parent_guard_fd = os.open(
                data_dir.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            import fcntl

            fcntl.flock(parent_guard_fd, fcntl.LOCK_EX)
            current_parent = data_dir.parent.lstat()
            if (current_parent.st_dev, current_parent.st_ino) != (
                lock_parent_info.st_dev,
                lock_parent_info.st_ino,
            ):
                raise ValueError("Unsafe index lock path")
        except (OSError, RuntimeError, ValueError) as exc:
            release_parent_guard()
            if isinstance(exc, ValueError):
                raise
            raise ValueError("Unsafe index lock path") from exc
    legacy_fd = -1

    def release_legacy_fd() -> None:
        """Release the legacy directory/file lock at most once."""
        nonlocal legacy_fd
        if legacy_fd == -1:
            return
        fd = legacy_fd
        legacy_fd = -1
        try:
            if os.name == "nt":
                msvcrt = __import__("msvcrt")
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    if lock_info is None:
        try:
            if os.name == "nt":
                # CRT byte-range locks cannot target a directory. A legacy
                # populated index does have a stable metadata database, so
                # lock its first byte without creating any read-side file.
                legacy_target = data_dir / "metadata.db"
                if legacy_target.is_file() and not is_reparse_link(legacy_target):
                    legacy_fd = os.open(legacy_target, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                    msvcrt: Any = __import__("msvcrt")
                    mode = (
                        getattr(msvcrt, "LK_NBLCK", msvcrt.LK_LOCK)
                        if create
                        else getattr(msvcrt, "LK_NBRLCK", msvcrt.LK_RLCK)
                    )
                    while True:
                        try:
                            msvcrt.locking(legacy_fd, mode, 1)
                            break
                        except OSError:
                            time.sleep(1)
                elif not create:
                    raise ValueError("legacy index has no coordination lock")
            else:
                if data_dir.exists():
                    legacy_fd = os.open(
                        data_dir,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                    )
                else:
                    # A generation lock also coordinates creation of a fresh
                    # index; there is no directory to lock until the writer
                    # creates it under this parent-level lock.
                    legacy_fd = -1
                if legacy_fd == -1:
                    if not create:
                        raise ValueError("legacy index has no coordination lock")
                else:
                    import fcntl

                    fcntl.flock(legacy_fd, fcntl.LOCK_EX if create else fcntl.LOCK_SH)
        except (OSError, RuntimeError, ValueError) as exc:
            release_legacy_fd()
            release_parent_guard()
            if isinstance(exc, ValueError) and str(exc) == "legacy index has no coordination lock":
                raise
            raise ValueError("Unsafe index lock path") from exc
        if not create:
            # A writer may have won the legacy-to-sibling transition while we
            # waited for the shared directory lock.  Do not return holding
            # the old inode: writers now coordinate through the sibling lock.
            # The writer cannot create that sibling while this shared lock is
            # held, so this recheck closes the transition race.
            try:
                sibling_info = lock_path.lstat()
            except FileNotFoundError:
                sibling_info = None
            except OSError as exc:
                release_legacy_fd()
                release_parent_guard()
                raise ValueError("Unsafe index lock path") from exc
            if sibling_info is not None and (
                stat.S_ISLNK(sibling_info.st_mode) or not stat.S_ISREG(sibling_info.st_mode)
            ):
                release_legacy_fd()
                release_parent_guard()
                raise ValueError("Unsafe index lock path")
            sibling_now_exists = sibling_info is not None
            if sibling_now_exists:
                release_legacy_fd()
                release_parent_guard()
                with index_lock(data_dir, exclusive=False, create=False):
                    yield
                return
            # The legacy directory/file lock remains held through the read,
            # so a writer cannot publish the sibling lock until this context
            # exits. The parent guard is only needed for the transition
            # recheck above; keeping it while yielding would deadlock a
            # recursive sibling-lock acquisition in the transition case.
            release_parent_guard()
            try:
                yield
            finally:
                release_legacy_fd()
            return
    flags = (os.O_CREAT if create else 0) | (os.O_RDWR if exclusive else os.O_RDONLY)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        fd = os.open(lock_path, flags, 0o600)
        opened_info = os.fstat(fd)
        opened_identity = (opened_info.st_dev, opened_info.st_ino)
        if lock_identity is not None and opened_identity != lock_identity:
            raise ValueError("Unsafe index lock path")
        with _active_lock_identities_guard:
            active_entry = _active_lock_identities.get(lock_path)
            active_identity = active_entry[0] if active_entry is not None else None
        if active_identity is not None and opened_identity != active_identity:
            raise ValueError("Unsafe index lock path")
        if parent_info is not None:
            current_info = data_dir.lstat()
            if (current_info.st_dev, current_info.st_ino) != (
                parent_info.st_dev,
                parent_info.st_ino,
            ):
                raise ValueError("Unsafe index lock path")
    except (OSError, RuntimeError, ValueError) as exc:
        if fd != -1:
            os.close(fd)
        release_parent_guard()
        release_legacy_fd()
        if isinstance(exc, ValueError) and str(exc) == "Unsafe index lock path":
            raise
        raise ValueError("Unsafe index lock path") from exc
    held[lock_path] = (fd, exclusive, 1, owner)
    with _active_lock_identities_guard:
        active_entry = _active_lock_identities.get(lock_path)
        if active_entry is not None and active_entry[0] != opened_identity:
            # A contender may have observed the old inode before another
            # holder entered.  Never overwrite the active coordination
            # identity with a replacement inode.
            held.pop(lock_path, None)
            os.close(fd)
            release_parent_guard()
            raise ValueError("Unsafe index lock path")
        _active_lock_identities[lock_path] = (
            opened_identity,
            1 if active_entry is None else active_entry[1] + 1,
        )
    try:
        if os.name == "nt":
            msvcrt = __import__("msvcrt")

            # Lock one byte at the start of the file.  The descriptor is kept
            # open for the whole context, so the lock cannot be released early.
            # CRT's LK_LOCK/LK_RLCK silently give up after ten one-second
            # retries. Use the non-blocking variants in an unbounded loop so
            # Windows readers and writers wait for long refreshes just like
            # the POSIX flock path.
            mode = (
                getattr(msvcrt, "LK_NBLCK", msvcrt.LK_LOCK)
                if exclusive
                else getattr(msvcrt, "LK_NBRLCK", msvcrt.LK_RLCK)
            )
            while True:
                try:
                    msvcrt.locking(fd, mode, 1)
                    break
                except OSError:
                    time.sleep(1)
            try:
                current_lock = lock_path.lstat()
                if (current_lock.st_dev, current_lock.st_ino) != opened_identity:
                    raise ValueError("Unsafe index lock path")
                yield
            finally:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                current_lock = lock_path.lstat()
                if (current_lock.st_dev, current_lock.st_ino) != opened_identity:
                    raise ValueError("Unsafe index lock path")
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        held_entry = held.get(lock_path)
        if held_entry is not None:
            if held_entry[0] == fd and held_entry[3] == owner:
                held.pop(lock_path, None)
        with _active_lock_identities_guard:
            active_entry = _active_lock_identities.get(lock_path)
            if active_entry is not None and active_entry[0] == opened_identity:
                if active_entry[1] <= 1:
                    _active_lock_identities.pop(lock_path, None)
                else:
                    _active_lock_identities[lock_path] = (opened_identity, active_entry[1] - 1)
        os.close(fd)
        release_parent_guard()
        release_legacy_fd()
