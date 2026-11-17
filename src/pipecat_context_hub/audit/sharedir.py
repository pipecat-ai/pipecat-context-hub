"""The share/ directory on disk: what extract, repack, gist, ingest and discard do to it.

Roles, each in one place: the reader (:func:`read_share`), the no-follow writer (:class:`SafeDir`),
the send-side verdict (:func:`check_share`, with :func:`expected_report`, the one recipe for the
report a bundle's rows render to), the output directory's lifecycle (what ``extract`` may write
into, :func:`refuse_extract_target` and :func:`refuse_unsafe_outputs`; what ``discard`` deletes,
:func:`generated_paths` and :func:`discard`), the tarball (:func:`tar_bytes`,
:func:`write_tarball`), the gist plan (:func:`gist_plan`) and the private staging copy ``gh`` posts
from (:func:`staged`). Every path inside an output directory is spelled through
:data:`SHARE_DIR` and :data:`LOCAL_OUTPUTS`.

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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from pipecat_context_hub.audit import bundle as _bundle
from pipecat_context_hub.audit.bundle import (
    BUNDLE_FILES,
    Bundle,
    BundleError,
    decode_manifest,
    has_content,
    jsonl_rows,
    load_files,
    omitted_when_posting,
)
from pipecat_context_hub.audit.egress import ShareLeakError, verify_files
from pipecat_context_hub.audit.report import render_report
from pipecat_context_hub.shared.paths import is_inside

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

    def names(self) -> list[str]:
        """The names in this directory (none is followed or opened)."""
        return sorted(os.listdir(self.path if self._fd is None else self._fd))

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


STALE_REPORT = (
    "report.md does not match calls.jsonl and manifest.json (share/ was edited without "
    "`audit repack`); run `audit repack`"
)


def expected_report(files: Mapping[str, bytes]) -> bytes:
    """``report.md`` as a function of the bundle's own ``manifest.json`` and ``calls.jsonl`` bytes:
    the one recipe, used by ``extract`` and ``repack`` to write the report and by
    :func:`check_share` to check it, so the two cannot drift. Raises ``BundleError`` on a manifest
    or row the bundle contract rejects."""
    return render_report(
        decode_manifest(files.get("manifest.json")), jsonl_rows(files, "calls.jsonl")
    ).encode()


def check_share(files: Mapping[str, bytes]) -> Bundle:
    """The one send-side gate on a bundle's bytes: the leak check (``ShareLeakError`` naming each
    file and rule, never the text), the whole-bundle validator ``ingest`` runs, then ``report.md``
    against the report its rows and manifest render to, byte for byte (``BundleError`` when they
    differ): ``report.md`` is a function of ``calls.jsonl`` and ``manifest.json``, so a row edited
    without ``repack`` can never be posted beside a report that still shows the old text.

    ``ingest`` does not run this last step: it merges rows and never ``report.md``, and a bundle
    from an older renderer is still a valid bundle.
    """
    leaks = verify_files(files)
    if leaks:
        raise ShareLeakError("; ".join(leaks))
    bundle = load_files(files)
    try:
        expected = expected_report(files)
    except (ValueError, TypeError, ArithmeticError, RecursionError):
        raise BundleError("report.md cannot be rendered from calls.jsonl") from None
    if files["report.md"] != expected:
        raise BundleError(STALE_REPORT)
    return bundle


# ---- The output directory's lifecycle: what `extract` may write into, what `repack` rewrites and
# what `discard` deletes. Every check goes through `SafeDir.kind` (a link is never followed), the
# same rule the reader and the writer use.
LOCAL_OUTPUTS = (
    "summary.json",
    "sessions.jsonl",
    "calls.jsonl",
    "share.tar.gz",
    "share.tar.gz.sha256",
)
SHARE_DIR = "share"


def _is_reg(mode: int | None) -> bool:
    return mode is not None and stat.S_ISREG(mode)


def _open_existing(path: Path) -> SafeDir | None:
    try:
        return SafeDir.open(path)
    except FileNotFoundError:
        return None


def refuse_unsafe_outputs(out: Path) -> None:
    """Refuse an output directory whose share/ or any file this tool writes is not a plain one.

    A friendly early refusal with the names, before any work. It is not what keeps a write from
    following a link: every write goes through :class:`SafeDir` (no-follow directory, a new file
    renamed over the target), which also covers a link planted after this check.
    """
    d = _open_existing(out)
    if d is None:
        return
    with d:
        mode = d.kind(SHARE_DIR)
        if mode is not None and not stat.S_ISDIR(mode):
            raise UnsafePathError(
                f"refusing to write: {SHARE_DIR}/ is a symlink or not a directory"
            )
        bad = [n for n in LOCAL_OUTPUTS if d.kind(n) is not None and not _is_reg(d.kind(n))]
        if mode is not None:
            with d.child(SHARE_DIR) as share:
                bad += [
                    n
                    for n in BUNDLE_FILES
                    if share.kind(n) is not None and not _is_reg(share.kind(n))
                ]
    if bad:
        raise UnsafePathError(f"refusing to write: {bad} already exist as a symlink or non-file")


def refuse_extract_target(out: Path, inputs: Sequence[Path]) -> None:
    """Refuse an ``extract --out`` that could overwrite or delete something that is not ours.

    The invariant: ``extract`` writes only into a directory that is missing, empty or holds only
    what a previous ``extract`` wrote (:data:`LOCAL_OUTPUTS` and share/ with the bundle files), and
    that is neither an input directory nor inside or around one: a transcript named
    ``sessions.jsonl`` in the input would otherwise be replaced by the output's.

    "Is, inside or around" is decided by the project's one filesystem-identity predicate
    (``shared.paths.is_inside``: resolved paths compared lexically, then the device and inode of the
    path and each existing ancestor), never by spelling alone, so a case-flipped ``--out`` on a
    case-insensitive volume (the macOS default), a symlinked alias and ``..`` all name the
    directory they reach.
    """
    target = out.resolve()
    for root in inputs:
        src = root.resolve()
        if is_inside(target, src) or is_inside(src, target):
            raise ValueError(
                f"refusing to write: --out {out.name or out} is, contains or is inside the input "
                f"directory {root.name or root}; use a separate, empty directory"
            )
    d = _open_existing(out)
    if d is None:
        return
    with d:
        foreign = [n for n in d.names() if n not in LOCAL_OUTPUTS and n != SHARE_DIR]
        if stat.S_ISDIR(d.kind(SHARE_DIR) or 0):
            with d.child(SHARE_DIR) as share:
                foreign += [f"{SHARE_DIR}/{n}" for n in share.names() if n not in BUNDLE_FILES]
    if foreign:
        shown = ", ".join(foreign[:3]) + (", ..." if len(foreign) > 3 else "")
        raise ValueError(
            f"refusing to write: --out {out.name or out} holds files `audit extract` did not write "
            f"({len(foreign)}: {shown}); use a new or empty directory"
        )


def generated_paths(out_dir: Path) -> list[Path]:
    """Exactly the files ``extract`` writes into ``out_dir`` that are there (nothing else is ever
    listed).

    Refuses a directory that does not look like extract output, and symlinks, so a typo'd path
    cannot delete someone's files. ``extract`` writes nothing until its share bundle has passed
    every check, so a failed extract leaves nothing behind for this to miss.
    """
    not_ours = ValueError(f"{out_dir.name or out_dir} does not look like `audit extract` output")
    d = _open_existing(out_dir)
    if d is None:
        raise not_ours
    with d:
        share_mode = d.kind(SHARE_DIR)
        if d.kind("summary.json") is None or share_mode is None:
            raise not_ours
        if not stat.S_ISDIR(share_mode):  # its files would be deleted in the link's target
            raise UnsafePathError(f"refusing to delete: {SHARE_DIR}/ is a symlink")
        with d.child(SHARE_DIR) as share:
            if share.kind("manifest.json") is None:
                raise not_ours
            found = [(d, out_dir / n, n) for n in LOCAL_OUTPUTS if d.kind(n) is not None]
            found += [
                (share, out_dir / SHARE_DIR / n, n)
                for n in BUNDLE_FILES
                if share.kind(n) is not None
            ]
            if not all(_is_reg(where.kind(n)) for where, _, n in found):
                raise UnsafePathError("refusing to delete: output contains a symlink or non-file")
    return [p for _, p, _ in found]


def discard(out_dir: Path) -> int:
    """Delete what :func:`generated_paths` lists, through no-follow directory handles (a link
    swapped in after the listing is refused, never followed); returns how many files went."""
    listed = generated_paths(out_dir)
    with SafeDir.open(out_dir) as out, out.child(SHARE_DIR) as share:
        for p in listed:
            (share if p.parent == out_dir / SHARE_DIR else out).unlink(p.name)
    for d in (out_dir / SHARE_DIR, out_dir):
        try:
            d.rmdir()  # only if now empty: anything else of yours stays
        except OSError:
            pass
    return len(listed)


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
    share = out_dir / SHARE_DIR
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
