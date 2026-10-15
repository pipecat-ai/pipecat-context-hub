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
    lock_path,
    refresh_generation,
    refresh_in_progress,
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
    assert not lock_path(tmp_path, SERVE_LOCK).exists()
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


def test_lock_survives_deleting_the_data_dir(tmp_path: Path) -> None:
    """`--reset-index` rmtree's the data dir; the lock must not live inside it."""
    import shutil

    data_dir = tmp_path / "hub"
    data_dir.mkdir()
    held = IndexLock(data_dir, REFRESH_LOCK, exclusive=True)
    held.acquire()
    shutil.rmtree(data_dir)
    data_dir.mkdir()
    with pytest.raises(IndexLockedError):
        IndexLock(data_dir, REFRESH_LOCK, exclusive=True).acquire()
    held.release()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_every_spelling_of_a_data_dir_shares_one_lock(tmp_path: Path) -> None:
    real = tmp_path / "hub"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    assert lock_path(real, REFRESH_LOCK) == lock_path(alias, REFRESH_LOCK)
    with IndexLock(real, REFRESH_LOCK, exclusive=True):
        with pytest.raises(IndexLockedError):
            IndexLock(alias, REFRESH_LOCK, exclusive=True).acquire()


def test_unwritable_lock_location_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*_a: object, **_k: object) -> int:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr("os.open", deny)
    IndexLock(tmp_path / "hub", REFRESH_LOCK, exclusive=True).acquire()


def test_refresh_in_progress_reflects_the_refresh_lock(tmp_path: Path) -> None:
    data_dir = tmp_path / "hub"
    assert refresh_in_progress(data_dir) is False
    assert not lock_path(data_dir, REFRESH_LOCK).exists()  # probing creates nothing
    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        assert refresh_in_progress(data_dir) is True
    assert refresh_in_progress(data_dir) is False


def test_a_running_serve_is_not_a_running_refresh(tmp_path: Path) -> None:
    data_dir = tmp_path / "hub"
    serve = IndexLock(data_dir, SERVE_LOCK, exclusive=False)
    serve.acquire()
    assert refresh_in_progress(data_dir) is False
    serve.release()


class TestReopeningIndexStore:
    def test_opens_lazily_and_delegates(self) -> None:
        from unittest.mock import MagicMock

        from pipecat_context_hub.services.index.reopening import ReopeningIndexStore

        real = MagicMock()
        real.get_metadata.return_value = "v"
        factory = MagicMock(return_value=real)
        store = ReopeningIndexStore(factory)
        factory.assert_not_called()
        assert store.get_metadata("k") == "v"
        factory.assert_called_once()

    def test_mark_stale_closes_and_reopens_on_next_use(self) -> None:
        from unittest.mock import MagicMock

        from pipecat_context_hub.services.index.reopening import ReopeningIndexStore

        first, second = MagicMock(), MagicMock()
        factory = MagicMock(side_effect=[second])
        store = ReopeningIndexStore(factory, initial=first)
        store.mark_stale()
        first.close.assert_not_called()  # marking never touches the live handle
        store.get_index_stats()
        first.close.assert_called_once()
        second.get_index_stats.assert_called_once()
        store.get_index_stats()
        factory.assert_called_once()  # reopened exactly once

    def test_close_does_not_open_the_index(self) -> None:
        from unittest.mock import MagicMock

        from pipecat_context_hub.services.index.reopening import ReopeningIndexStore

        factory = MagicMock()
        ReopeningIndexStore(factory).close()
        factory.assert_not_called()


def test_refresh_generation_changes_on_every_refresh_start(tmp_path: Path) -> None:
    """A refresh that begins and ends between two reader calls must still be noticed."""
    import os

    data_dir = tmp_path / "hub"
    assert refresh_generation(data_dir) is None
    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        first = refresh_generation(data_dir)
    assert first is not None
    path = lock_path(data_dir, REFRESH_LOCK)
    os.utime(path, ns=(1, 1))  # an old generation, regardless of filesystem timestamp granularity
    old = refresh_generation(data_dir)
    assert old != first
    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        pass  # not running now, but it ran
    assert refresh_generation(data_dir) not in (None, old)


def test_shared_probe_does_not_bump_the_generation(tmp_path: Path) -> None:
    import os

    data_dir = tmp_path / "hub"
    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        pass
    os.utime(lock_path(data_dir, REFRESH_LOCK), ns=(1, 1))
    before = refresh_generation(data_dir)
    assert refresh_in_progress(data_dir) is False
    assert refresh_generation(data_dir) == before
