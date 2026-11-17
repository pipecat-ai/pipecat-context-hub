"""The share/ directory on disk: one reader, one writer that never follows a link, one verdict.

Reader. :func:`read_share` is the only code that reads a share/ directory (or a cloned gist) into a
bundle's bytes map; ``gist``, ``repack`` and ``ingest`` all call it, so one rule set decides what a
directory holds. The directory itself must not be a symlink. Each bundle file that is present must
be a regular file reached without following a link: a symlink, FIFO or directory at a bundle name
is refused, never skipped. A file is at most ``bundle.MAX_FILE_BYTES`` and all of them together at
most ``bundle.MAX_BUNDLE_BYTES``. An absent file is simply absent: whether it may be is the bundle
contract's call (``bundle.load_files``). Other files in the directory are ignored.

Writer. :class:`SafeDir` holds a directory opened without following a link (``O_NOFOLLOW``), and
writes every file as a fresh temporary file created inside it with ``O_CREAT | O_EXCL |
O_NOFOLLOW`` and then renamed over the target: a symlink planted at a target, before or after any
check, is replaced, never written through, and one planted at the directory is refused when the
directory is opened. ``extract``, ``repack`` and ``discard`` go through it.

Windows has neither ``dir_fd`` nor ``O_NOFOLLOW``. There the same refusals run on paths (``lstat``
before each open), which leaves the usual window between the check and the use.

Verdict. :func:`check_share` is the one send-side gate (the leak check, then the whole-bundle
validator), run on the bytes before anything is written or posted: by ``extract`` on what it is
about to write, by ``repack`` on what it is about to rewrite, and by :func:`gist_plan` on what it
is about to post.
"""

from __future__ import annotations

import contextlib
import errno
import gzip
import hashlib
import io
import os
import secrets
import stat
import tarfile
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from pipecat_context_hub.audit import bundle as _bundle
from pipecat_context_hub.audit.bundle import (
    BUNDLE_FILES,
    Bundle,
    BundleError,
    has_content,
    load_files,
    omitted_when_posting,
)
from pipecat_context_hub.audit.egress import ShareLeakError, verify_files

# Descriptor-relative, no-follow I/O. Off on a platform without it (Windows): see the docstring.
FD_SAFE = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and {os.open, os.mkdir, os.rename, os.unlink, os.stat} <= os.supports_dir_fd
)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # a FIFO at a bundle name must not block the open
# What open(2) reports for a final component that is a symlink under O_NOFOLLOW (ELOOP; EMLINK on
# FreeBSD) or not a directory under O_DIRECTORY.
_LINK_ERRNOS = {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}


class UnsafePathError(ValueError):
    """A path this tool would read, write or delete is a symlink or not a plain file/directory."""


def _not_plain(path: Path, what: str) -> UnsafePathError:
    kind = "directory" if what == "/" else "regular file"
    return UnsafePathError(f"refusing: {path.name or path}{what} is a symlink or not a {kind}")


class SafeDir:
    """A directory reached without following a link; files are opened relative to it."""

    def __init__(self, path: Path, fd: int | None) -> None:
        self.path = path
        self._fd = fd  # None on a platform without dir_fd: the path-based fallback

    @classmethod
    def open(cls, path: Path, *, create: bool = False) -> SafeDir:
        """``path``, whose last component must not be a symlink; created first when ``create``."""
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileExistsError):
                os.mkdir(path)
        return cls._open_at(None, str(path), path)

    def child(self, name: str, *, create: bool = False) -> SafeDir:
        """The sub-directory ``name``, never through a link; created first when ``create``."""
        if create:
            with contextlib.suppress(FileExistsError):
                if self._fd is None:
                    os.mkdir(self.path / name)
                else:
                    os.mkdir(name, dir_fd=self._fd)
        return self._open_at(self._fd, name, self.path / name)

    @classmethod
    def _open_at(cls, dir_fd: int | None, name: str, path: Path) -> SafeDir:
        if not FD_SAFE:
            if not stat.S_ISDIR(os.lstat(path).st_mode):  # FileNotFoundError propagates
                raise _not_plain(path, "/")
            return cls(path, None)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in _LINK_ERRNOS:
                raise _not_plain(path, "/") from None
            raise
        return cls(path, fd)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> SafeDir:
        return self

    def __exit__(
        self, _t: type[BaseException] | None, _e: BaseException | None, _tb: TracebackType | None
    ) -> None:
        self.close()

    def kind(self, name: str) -> int | None:
        """``st_mode`` of ``name`` itself (a link is not followed), or None when it is absent."""
        try:
            if self._fd is None:
                return os.lstat(self.path / name).st_mode
            return os.stat(name, dir_fd=self._fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return None

    def read(self, name: str) -> bytes | None:
        """The bytes of the regular file ``name`` (at most ``MAX_FILE_BYTES``), or None when it is
        absent."""
        limit = _bundle.MAX_FILE_BYTES  # read at call time: one limit, adjustable in tests
        try:
            if self._fd is None:
                if not stat.S_ISREG(os.lstat(self.path / name).st_mode):
                    raise BundleError(f"{name} is a symlink or not a regular file")
                fd = os.open(self.path / name, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            else:
                flags = os.O_RDONLY | os.O_NOFOLLOW | _NONBLOCK | _CLOEXEC
                fd = os.open(name, flags, dir_fd=self._fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in _LINK_ERRNOS:
                raise BundleError(f"{name} is a symlink or not a regular file") from None
            raise
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise BundleError(f"{name} is a symlink or not a regular file")
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise BundleError(f"{name} too large (limit {limit} bytes)")
        return data

    def read_bundle(self) -> dict[str, bytes]:
        """The bundle files present here, under the reader's rules (see the module docstring)."""
        out: dict[str, bytes] = {}
        total = 0
        for name in BUNDLE_FILES:
            data = self.read(name)
            if data is None:
                continue
            total += len(data)
            if total > _bundle.MAX_BUNDLE_BYTES:
                raise BundleError(f"bundle larger than {_bundle.MAX_BUNDLE_BYTES} bytes")
            out[name] = data
        return out

    def write(self, name: str, data: bytes) -> None:
        """Replace ``name`` with ``data``: a new file created here, renamed over the target."""
        tmp = f".{name}.{secrets.token_hex(8)}.tmp"
        if self._fd is None:
            target = self.path / name
            mode = self.kind(name)
            if mode is not None and not stat.S_ISREG(mode):
                raise _not_plain(target, "")
            fd = os.open(
                self.path / tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            )
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(self.path / tmp, target)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(self.path / tmp)
                raise
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC
        fd = os.open(tmp, flags, 0o644, dir_fd=self._fd)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            # rename(2) replaces the target's directory entry: a symlink there is replaced, never
            # followed.
            os.rename(tmp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=self._fd)
            raise

    def unlink(self, name: str) -> None:
        """Delete the regular file ``name`` (a link or anything else is refused)."""
        mode = self.kind(name)
        if mode is None:
            return
        if not stat.S_ISREG(mode):
            raise _not_plain(self.path / name, "")
        if self._fd is None:
            os.unlink(self.path / name)
        else:
            os.unlink(name, dir_fd=self._fd)


def read_share(path: Path) -> dict[str, bytes]:
    """The one reader of a share/ directory or a cloned gist (see the module docstring)."""
    try:
        with SafeDir.open(path) as d:
            return d.read_bundle()
    except FileNotFoundError:
        raise BundleError(f"{path.name or path}/ does not exist") from None


def check_share(files: Mapping[str, bytes]) -> Bundle:
    """The one send-side gate on a bundle's bytes: the leak check (``ShareLeakError`` naming each
    file and rule, never the text), then the whole-bundle validator ``ingest`` runs."""
    leaks = verify_files(files)
    if leaks:
        raise ShareLeakError("; ".join(leaks))
    return load_files(files)


def tar_bytes(files: Mapping[str, bytes]) -> bytes:
    """A deterministic ``share.tar.gz`` of ``files`` (flat names, no owners, fixed mtime), built
    from the bytes the checks saw, never re-read from disk."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="share.tar.gz", fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tf:
            for name in BUNDLE_FILES:
                if name not in files:
                    continue
                info = tarfile.TarInfo(name)
                info.size = len(files[name])
                info.mode = 0o644
                info.mtime = 0
                tf.addfile(info, io.BytesIO(files[name]))
    return buf.getvalue()


def write_tarball(out: SafeDir, files: Mapping[str, bytes]) -> tuple[Path, str]:
    """Write ``share.tar.gz`` of ``files`` and its ``.sha256`` into ``out``; (path, sha256)."""
    data = tar_bytes(files)
    digest = hashlib.sha256(data).hexdigest()
    out.write("share.tar.gz", data)
    out.write("share.tar.gz.sha256", f"{digest}  share.tar.gz\n".encode())
    return out.path / "share.tar.gz", digest


@dataclass
class GistPlan:
    post: dict[str, bytes]  # what a gist would carry, in bundle order
    omitted: list[str]  # empty JSONL files a gist cannot hold


def gist_plan(out_dir: Path, *, allow_empty: bool) -> GistPlan:
    """What ``audit gist`` would post from ``out_dir/share/``, or ``ValueError`` saying why not.

    Read once (:func:`read_share`), gated once (:func:`check_share`); the plan carries the bytes
    that were checked, so what is posted is what was checked.
    """
    share = out_dir / "share"
    try:
        files = read_share(share)
    except BundleError as exc:
        raise ValueError(f"cannot read {share.name}/: {exc}") from None
    try:
        bundle = check_share(files)
    except ShareLeakError as exc:
        raise ValueError(
            f"{share.name}/ failed the leak check ({exc}): remove what it names, then run "
            "`audit repack`"
        ) from None
    except BundleError as exc:
        raise ValueError(
            f"{share.name}/ is not a valid bundle ({exc}); fix it, then run `audit repack`"
        ) from None
    if not allow_empty and not has_content(bundle.manifest["counts"]):
        raise ValueError(
            "this bundle has no Context Hub calls and no Pipecat work without it, so a gist would "
            "carry nothing useful (only session counters and timestamps); pass --allow-empty to "
            "post it anyway"
        )
    post = {n: d for n, d in files.items() if not omitted_when_posting(n, d)}
    return GistPlan(post, [n for n in files if n not in post])


@contextlib.contextmanager
def staged(files: Mapping[str, bytes]) -> Iterator[list[Path]]:
    """``files`` written into a fresh private temporary directory; yields their absolute paths.

    ``gh`` reads what it posts from these paths, so a file swapped in share/ after the checks is
    never what gets posted.
    """
    with tempfile.TemporaryDirectory(prefix="hub-audit-gist-") as tmp:
        paths = []
        for name, data in files.items():
            p = Path(tmp).resolve() / name
            p.write_bytes(data)
            paths.append(p)
        yield paths
