"""Prepare a portable skills-only plugin copy without installing its runtime."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import stat
import tempfile
from pathlib import Path


def prepare_local(destination: Path) -> Path:
    """Copy only skills-only package resources into a fresh destination."""
    source = Path(__file__).resolve().parents[1]
    checkout = next(
        (p for p in source.parents if (p / ".git").is_file() or (p / ".git").is_dir()),
        source,
    )
    destination = destination.expanduser().absolute()
    if _is_reparse_point(destination):
        raise ValueError("Destination must not be a symlink")
    _validate_destination_parents(destination)
    try:
        destination = destination.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("Destination path is invalid") from exc
    if destination == checkout or checkout in destination.parents:
        raise ValueError("Destination must be outside the checkout")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Destination must be absent or an empty directory")

    files = [source / name for name in ("plugin.json", "README.md", "PRIVACY.md", "LICENSE")]
    files.extend(
        source / "scripts" / name for name in ("prepare_local.py", "safe_cloud_metadata.py")
    )
    files.extend(
        source / "assets" / name
        for name in (
            "cat-mark.svg",
            "cat-mark-dark.svg",
            "context-hub-logo.svg",
            "context-hub-logo-dark.svg",
        )
    )
    files.extend(
        source / "skills" / name / "SKILL.md" for name in ("build", "deploy", "explore", "setup")
    )
    for path in files:
        if not path.is_file() or any(
            _is_reparse_point(part) for part in (path, *path.parents) if part != source.parent
        ):
            raise ValueError(f"Package resource must be a regular file: {path.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hub-plugin-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "package"
        staged.mkdir()
        for path in files:
            target = staged / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_resource(path, target)
        if destination.exists():
            destination.rmdir()  # Refuses if anything appeared since preflight.
        _rename_no_symlink(staged, destination)
    return destination


def _validate_destination_parents(destination: Path) -> None:
    """Reject symlinked or looping parents before any directory is created.

    ``Path.resolve(strict=False)`` is not a reliable loop detector on every
    supported Python/filesystem combination.  In particular, a self-referential
    parent can survive resolution and fail later in ``mkdir`` with an exception
    that includes the caller's absolute path.  Walking existing parents with
    ``is_symlink`` keeps the failure deterministic and path-free.
    """
    try:
        current = destination.parent
        while current != current.parent:
            if _is_reparse_point(current):
                raise ValueError("Destination path is invalid")
            current = current.parent
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "Destination path is invalid":
            raise
        raise ValueError("Destination path is invalid") from exc


def _copy_resource(source: Path, target: Path) -> None:
    """Copy a resource from an already-validated path without following links."""
    if os.open not in getattr(os, "supports_dir_fd", set()):
        _copy_resource_path_fallback(source, target)
        return
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    before = source.lstat()
    if _is_reparse_point(source) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Package resource must be a regular file: {source.name}")
    # Probe the spelling once with ``O_NOFOLLOW`` as well.  Besides preserving
    # the preflight-to-open identity check for a replaced leaf, the actual
    # copy below still uses descriptor-relative opens for every parent.
    probe_fd = os.open(source, flags)
    os.close(probe_fd)
    source_parent_fd = _open_directory_chain(source.parent)
    target_parent_fd = _open_directory_chain(target.parent)
    fd = -1
    output_fd = -1
    try:
        fd = os.open(source.name, flags, dir_fd=source_parent_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_dev,
            before.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"Package resource changed during copy: {source.name}")
        target_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        output_fd = os.open(target.name, target_flags, 0o600, dir_fd=target_parent_fd)
        with os.fdopen(fd, "rb") as stream, os.fdopen(output_fd, "wb") as output:
            fd = -1
            output_fd = -1
            shutil.copyfileobj(stream, output)
    finally:
        if fd != -1:
            os.close(fd)
        if output_fd != -1:
            os.close(output_fd)
        os.close(source_parent_fd)
        os.close(target_parent_fd)


def _open_directory_chain(path: Path) -> int:
    """Open every directory component without following symlinks."""
    if os.open not in getattr(os, "supports_dir_fd", set()):
        raise ValueError("Secure directory descriptors are unavailable")
    path = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _copy_resource_path_fallback(source: Path, target: Path) -> None:
    """Copy safely enough on Windows, where descriptor-relative APIs are absent.

    The destination is a private staging tree.  Validate every existing source
    parent and compare the source identity before/after the copy so a replaced
    resource fails closed.  Windows has no portable ``O_NOFOLLOW`` equivalent
    exposed by Python, hence the explicit post-copy identity check.
    """
    source = source.absolute()
    target = target.absolute()
    _validate_destination_parents(source)
    before = source.lstat()
    if _is_reparse_point(source) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Package resource must be a regular file: {source.name}")
    # Keep a narrow open-time identity probe even on Windows.  It catches the
    # common replacement race before the path-based copy begins; the final
    # lstat below covers replacements during the copy itself.
    probe_fd = os.open(source, os.O_RDONLY)
    os.close(probe_fd)
    if _is_reparse_point(source):
        raise ValueError(f"Package resource changed during copy: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    try:
        after = source.lstat()
        if _is_reparse_point(source) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise ValueError(f"Package resource changed during copy: {source.name}")
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()
        raise


def _rename_no_symlink(source: Path, destination: Path) -> None:
    """Rename within securely opened parent directories where supported."""
    source_parent_fd = _open_directory_chain(source.parent)
    destination_parent_fd = _open_directory_chain(destination.parent)
    try:
        os.rename(
            source.name,
            destination.name,
            src_dir_fd=source_parent_fd,
            dst_dir_fd=destination_parent_fd,
        )
    finally:
        os.close(source_parent_fd)
        os.close(destination_parent_fd)


def _is_reparse_point(path: Path) -> bool:
    """Return true for symlinks and Windows junction/mount-point reparse tags."""
    try:
        if path.is_symlink():
            return True
    except (OSError, RuntimeError, ValueError):
        return False

    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        try:
            return bool(isjunction(path))
        except (OSError, RuntimeError, ValueError):
            return False

    mount_point_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    if mount_point_tag is None:
        return False
    try:
        return getattr(os.lstat(path), "st_reparse_tag", None) == mount_point_tag
    except (OSError, RuntimeError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        destination = prepare_local(args.destination)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(
        f"Prepared skills-only plugin at {destination}; no runtime installation or index refresh performed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
