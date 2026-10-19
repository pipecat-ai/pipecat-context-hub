"""Shared fixtures for the config.toml trust seam (``read_trusted_config``).

``TRUST_REFUSAL_CASES`` is the ONE tuple of situations the 2b trust predicates
refuse: for each, ``read_trusted_config(path)`` raises ``ConfigNotTrusted``.
The reader-vs-seam equivalence test (``test_env_loading.py``) and the writer
tests (phase 2c) both parametrize over it, so the writer and the reader are
proven to refuse the same inputs.

It deliberately excludes the outcomes that are NOT trust refusals: invalid TOML
(``tomllib.TOMLDecodeError``, not wrapped), a missing file (``None``) and an
unresolvable path (never reaches the seam). Those are added by the tests that
need them.

This is a shared helper module with no tests of its own. Each case is a
builder ``(tmp_path, monkeypatch) -> Path`` that arranges the filesystem (or
patches the OS probes) and returns the config path to hand to the seam.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_VALID_TOML = 'PIPECAT_HUB_STALE_AFTER_DAYS = "42"\n'

ConfigBuilder = Callable[[Path, pytest.MonkeyPatch], Path]

_posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission/ownership semantics")


def _good_file(directory: Path, mode: int = 0o600) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.toml"
    path.write_text(_VALID_TOML)
    path.chmod(mode)
    return path


def _non_regular(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cfg" / "config.toml"
    path.mkdir(parents=True)  # a directory where the file should be
    return path


def _world_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _good_file(tmp_path / "cfg", 0o666)


def _foreign_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _good_file(tmp_path / "cfg")
    real_fstat = os.fstat

    class _ForeignStat:
        def __init__(self, st: os.stat_result) -> None:
            self._st = st

        def __getattr__(self, name: str) -> object:
            return getattr(self._st, name)

        st_uid = os.getuid() + 12345 if hasattr(os, "getuid") else 12345

    def _fstat(fd: int, *args: Any, **kwargs: Any) -> Any:
        return _ForeignStat(real_fstat(fd, *args, **kwargs))

    monkeypatch.setattr(os, "fstat", _fstat)
    return path


def _foreign_owned_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _good_file(tmp_path / "theirs")
    real_stat = os.stat
    marker = str(path.parent)
    foreign_uid = os.getuid() + 12345

    class _ForeignDir:
        def __init__(self, st: os.stat_result) -> None:
            self._st = st

        def __getattr__(self, name: str) -> object:
            return getattr(self._st, name)

        st_uid = foreign_uid

    def _stat(p: Any, *args: Any, **kwargs: Any) -> Any:
        st = real_stat(p, *args, **kwargs)
        return _ForeignDir(st) if str(p) == marker else st

    monkeypatch.setattr(os, "stat", _stat)
    return path


def _mid_read_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The open succeeds; the read fails (EIO)."""
    path = _good_file(tmp_path / "cfg")
    real_fdopen = os.fdopen

    class _FailingRead:
        def __init__(self, f: Any) -> None:
            self._f = f

        def __enter__(self) -> _FailingRead:
            return self

        def __exit__(self, *exc: object) -> None:
            self._f.close()

        def read(self, *args: Any) -> bytes:
            raise OSError(errno.EIO, "Input/output error")

    def _fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        return _FailingRead(real_fdopen(fd, *args, **kwargs))

    monkeypatch.setattr(os, "fdopen", _fdopen)
    return path


def _untrusted_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _good_file(tmp_path / "loose-dir")
    path.parent.chmod(0o777)  # world-writable, no sticky bit
    return path


def _foreign_symlink_hop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    real = _good_file(tmp_path / "real")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link = link_dir / "config.toml"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/permissions")
    real_lstat = os.lstat
    foreign_uid = os.getuid() + 12345

    class _ForeignLink:
        def __init__(self, st: os.stat_result) -> None:
            self._st = st

        def __getattr__(self, name: str) -> object:
            return getattr(self._st, name)

        st_uid = foreign_uid

    def _lstat(p: Any, *args: Any, **kwargs: Any) -> Any:
        st = real_lstat(p, *args, **kwargs)
        if stat.S_ISLNK(st.st_mode):
            return _ForeignLink(st)
        return st

    monkeypatch.setattr(os, "lstat", _lstat)
    return link


def _unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if os.getuid() == 0:
        pytest.skip("root bypasses file permission bits")
    return _good_file(tmp_path / "cfg", 0o000)


def _undecodable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _good_file(tmp_path / "cfg")
    path.write_bytes(b'PIPECAT_HUB_STALE_AFTER_DAYS = "\xff\xfe\x80"\n')
    path.chmod(0o600)
    return path


def _embedded_nul(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return tmp_path / "cfg" / "con\x00fig.toml"


TRUST_REFUSAL_CASES: tuple[object, ...] = (
    pytest.param(_non_regular, id="non-regular"),
    pytest.param(_world_writable, id="world-writable", marks=_posix_only),
    pytest.param(_foreign_owned, id="foreign-owned", marks=_posix_only),
    pytest.param(_untrusted_parent, id="untrusted-parent-dir", marks=_posix_only),
    pytest.param(_foreign_owned_parent, id="foreign-owned-parent-dir", marks=_posix_only),
    pytest.param(_foreign_symlink_hop, id="foreign-symlink-hop", marks=_posix_only),
    pytest.param(_unreadable, id="unreadable", marks=_posix_only),
    pytest.param(_undecodable, id="undecodable"),
    pytest.param(_mid_read_oserror, id="mid-read-oserror"),
    pytest.param(_embedded_nul, id="embedded-nul-path"),
)
