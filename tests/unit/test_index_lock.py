"""Advisory index locks: refresh excludes refresh; serve is detectable."""

from __future__ import annotations

import errno
import logging
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from pipecat_context_hub import cli
from pipecat_context_hub.shared.index_lock import (
    REFRESH_LOCK,
    SERVE_LOCK,
    IndexLock,
    IndexLockedError,
    serve_is_running,
)


def test_exclusive_blocks_exclusive(tmp_path: Path) -> None:
    with IndexLock(tmp_path, REFRESH_LOCK, exclusive=True):
        with pytest.raises(IndexLockedError):
            IndexLock(tmp_path, REFRESH_LOCK, exclusive=True).acquire()


def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    with IndexLock(tmp_path, REFRESH_LOCK, exclusive=True):
        pass
    with IndexLock(tmp_path, REFRESH_LOCK, exclusive=True):
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="msvcrt has no shared lock mode")
def test_shared_locks_coexist_but_block_exclusive(tmp_path: Path) -> None:
    a = IndexLock(tmp_path, SERVE_LOCK, exclusive=False)
    b = IndexLock(tmp_path, SERVE_LOCK, exclusive=False)
    a.acquire()
    b.acquire()
    with pytest.raises(IndexLockedError):
        IndexLock(tmp_path, SERVE_LOCK, exclusive=True).acquire()
    a.release()
    b.release()


def test_serve_is_running_reflects_shared_lock(tmp_path: Path) -> None:
    assert serve_is_running(tmp_path) is False  # no lock file yet; creates nothing
    assert not (tmp_path / SERVE_LOCK).exists()
    serve = IndexLock(tmp_path, SERVE_LOCK, exclusive=False)
    serve.acquire()
    assert serve_is_running(tmp_path) is True
    serve.release()
    assert serve_is_running(tmp_path) is False


def test_serve_lock_does_not_block_refresh_lock(tmp_path: Path) -> None:
    serve = IndexLock(tmp_path, SERVE_LOCK, exclusive=False)
    serve.acquire()
    with IndexLock(tmp_path, REFRESH_LOCK, exclusive=True):
        pass
    serve.release()


class TestRefreshGuards:
    def test_second_refresh_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("PIPECAT_HUB_MIN_FREE_GB", "0")
        with IndexLock(tmp_path, REFRESH_LOCK, exclusive=True):
            result = CliRunner().invoke(cli.main, ["refresh"])
        assert result.exit_code != 0
        assert "already running" in result.output

    def test_low_disk_refuses_before_any_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "hub"
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
        monkeypatch.setenv("PIPECAT_HUB_MIN_FREE_GB", str(10**9))  # ~1 EB: never satisfiable
        result = CliRunner().invoke(cli.main, ["refresh"])
        assert result.exit_code != 0
        assert "GB free" in result.output
        assert not data_dir.exists()

    def test_reset_index_is_not_refused_for_low_disk_before_the_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--reset-index` is the recovery for a disk-filling index: the delete
        must run before the free-space check, which then sees the freed space."""
        data_dir = tmp_path / "hub"
        data_dir.mkdir()
        stale = data_dir / "stale.bin"
        stale.write_bytes(b"x")
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
        monkeypatch.setenv("PIPECAT_HUB_MIN_FREE_GB", str(10**9))  # never satisfiable
        result = CliRunner().invoke(cli.main, ["refresh", "--reset-index"])
        assert result.exit_code != 0
        assert "GB free" in result.output  # still checked, but after the delete
        assert not stale.exists()

    def test_lock_released_when_command_ends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("PIPECAT_HUB_MIN_FREE_GB", str(10**9))
        CliRunner().invoke(cli.main, ["refresh"])  # exits at the disk check
        with IndexLock(tmp_path, REFRESH_LOCK, exclusive=True):
            pass

    def test_warns_when_serve_is_live(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        serve = IndexLock(tmp_path, SERVE_LOCK, exclusive=False)
        serve.acquire()
        logger = logging.getLogger("test")
        with caplog.at_level(logging.WARNING, logger="test"):
            with click.Context(click.Command("x")):
                cli._acquire_refresh_lock_or_exit(tmp_path, logger).release()
        serve.release()
        assert "`serve` process has this index open" in caplog.text


class TestMinFreeBytes:
    def test_default(self) -> None:
        assert cli._min_free_bytes({}) == 5 * 1024**3

    @pytest.mark.parametrize("raw", ["", "  ", "abc", "-1", "nan", "inf"])
    def test_invalid_falls_back_to_default(self, raw: str) -> None:
        assert cli._min_free_bytes({"PIPECAT_HUB_MIN_FREE_GB": raw}) == 5 * 1024**3

    def test_overflowing_value_falls_back_to_default(self) -> None:
        # Finite as a float, but `gb * 1024**3` overflows to inf.
        assert cli._min_free_bytes({"PIPECAT_HUB_MIN_FREE_GB": "1e300"}) == 5 * 1024**3

    def test_zero_disables(self, tmp_path: Path) -> None:
        assert cli._min_free_bytes({"PIPECAT_HUB_MIN_FREE_GB": "0"}) == 0
        cli._require_free_disk(tmp_path, min_free=0)


@pytest.mark.skipif(sys.platform == "win32", reason="patches fcntl.flock")
def test_unsupported_locking_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ENOLCK / EOPNOTSUPP (some NFS/SMB mounts) must not read as 'locked'."""
    import fcntl

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", boom)
    IndexLock(tmp_path, REFRESH_LOCK, exclusive=True).acquire()
