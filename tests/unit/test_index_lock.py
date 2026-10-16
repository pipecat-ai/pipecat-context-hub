"""Advisory index locks: refresh excludes refresh; serve is detectable."""

from __future__ import annotations

import errno
import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

from pipecat_context_hub import cli
from pipecat_context_hub.shared.index_lock import (
    REFRESH_IN_PROGRESS_MESSAGE,
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
    with IndexLock(tmp_path, REFRESH_LOCK, exclusive=True), pytest.raises(IndexLockedError):
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
        monkeypatch.setattr(cli, "_REFRESH_LOCK_ATTEMPTS", 2)
        monkeypatch.setattr(cli, "_REFRESH_LOCK_RETRY_DELAY", 0.01)
        with IndexLock(tmp_path, REFRESH_LOCK, exclusive=True):
            result = CliRunner().invoke(cli.main, ["refresh"])
        # Retry-later, like the query commands (cli.py:168): not 1 (bad input).
        assert result.exit_code == 75
        assert "already running" in result.output

    def test_reader_hold_is_not_a_running_refresh(self, tmp_path: Path) -> None:
        """Regression (index_lock.py:175): readers hold refresh.lock shared for
        an instant (probe) or an index open; a refresh starting then must wait
        it out, not report "already running"."""
        import threading

        reader = IndexLock(tmp_path, REFRESH_LOCK, exclusive=False)
        reader.acquire()
        threading.Timer(0.2, reader.release).start()
        with click.Context(click.Command("x")):
            cli._acquire_refresh_lock_or_exit(tmp_path, logging.getLogger("test")).release()

    @pytest.mark.skipif(sys.platform == "win32", reason="msvcrt has no shared lock mode")
    def test_readers_outlasting_the_wait_are_not_reported_as_a_refresh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Regression (cli.py:213): if readers' shared holds outlast the retry
        budget, the error must not claim another refresh is running."""
        monkeypatch.setattr(cli, "_REFRESH_LOCK_ATTEMPTS", 2)
        monkeypatch.setattr(cli, "_REFRESH_LOCK_RETRY_DELAY", 0.01)
        with IndexLock(tmp_path, REFRESH_LOCK, exclusive=False):
            with click.Context(click.Command("x")):
                with pytest.raises(click.ClickException) as exc_info:
                    cli._acquire_refresh_lock_or_exit(tmp_path, logging.getLogger("test"))
        assert "already running" not in exc_info.value.message
        assert "reading the index" in exc_info.value.message
        assert exc_info.value.exit_code == 75

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


def test_writer_warns_when_it_cannot_create_its_lock_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A refresh that proceeds unprotected must say so (e.g. a read-only /data
    volume); readers, which run on every query, stay quiet."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    data_dir = blocker / "hub"  # the lock's parent is a file: it can never be created
    with caplog.at_level(logging.WARNING, logger="pipecat_context_hub.shared.index_lock"):
        IndexLock(data_dir, REFRESH_LOCK, exclusive=False).acquire()
        assert "continuing without a lock" not in caplog.text
        IndexLock(data_dir, REFRESH_LOCK, exclusive=True).acquire()
    assert "continuing without a lock" in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="patches fcntl.flock")
def test_writer_warns_when_locking_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import fcntl

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", boom)
    with caplog.at_level(logging.WARNING, logger="pipecat_context_hub.shared.index_lock"):
        IndexLock(tmp_path, REFRESH_LOCK, exclusive=False).acquire()
        assert "continuing without a lock" not in caplog.text
        IndexLock(tmp_path, REFRESH_LOCK, exclusive=True).acquire()
    assert "File locking unavailable" in caplog.text


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
    with IndexLock(real, REFRESH_LOCK, exclusive=True), pytest.raises(IndexLockedError):
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


def _simulate_refresh(data_dir: Path) -> None:
    """A refresh that started and finished (bumps the lock-file generation)."""
    import os

    path = lock_path(data_dir, REFRESH_LOCK)
    if path.exists():
        os.utime(path, ns=(1, 1))  # an old generation, whatever the mtime granularity
    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        pass


class TestReopeningIndexStore:
    @staticmethod
    def _store(data_dir: Path, opener: Any) -> Any:
        from pipecat_context_hub.services.index.reopening import ReopeningIndexStore

        return ReopeningIndexStore(data_dir, opener)

    def test_opens_lazily_and_delegates(self, tmp_path: Path) -> None:
        real = MagicMock()
        real.get_all_metadata.return_value = {"k": "v"}
        opener = MagicMock(return_value=real)
        store = self._store(tmp_path / "hub", opener)
        assert store.data_dir == tmp_path / "hub"  # never opens the index
        opener.assert_not_called()
        assert store.get_all_metadata() == {"k": "v"}
        opener.assert_called_once()

    def test_reopens_after_a_refresh_no_call_overlapped(self, tmp_path: Path) -> None:
        """Regression (cli.py:928): the generation is the one current *at open*,
        so a refresh that starts right after the open, with no tool call during
        it, still makes the next call reopen."""
        data_dir = tmp_path / "hub"
        first, second = MagicMock(), MagicMock()
        opener = MagicMock(side_effect=[first, second])
        store = self._store(data_dir, opener)
        store.open_now()
        _simulate_refresh(data_dir)
        assert store.unavailable_reason() is None
        first.close.assert_called_once()
        assert opener.call_count == 2
        store.get_index_stats()
        second.get_index_stats.assert_called_once()
        assert opener.call_count == 2  # reopened exactly once

    def test_never_opens_while_a_refresh_runs(self, tmp_path: Path) -> None:
        """Regression (cli.py:938): a call already past the gate when a refresh
        starts must not reopen the index mid-write, and the refresh must still
        be noticed afterwards."""
        from pipecat_context_hub.shared.index_lock import RefreshInProgressError

        data_dir = tmp_path / "hub"
        first, second = MagicMock(), MagicMock()
        opener = MagicMock(side_effect=[first, second])
        store = self._store(data_dir, opener)
        store.open_now()
        with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
            assert store.unavailable_reason() == REFRESH_IN_PROGRESS_MESSAGE
            with pytest.raises(RefreshInProgressError):
                store.get_all_metadata()  # the in-flight call
            assert opener.call_count == 1
        assert store.unavailable_reason() is None
        assert opener.call_count == 2
        first.close.assert_called_once()

    def test_open_holds_off_a_refresh(self, tmp_path: Path) -> None:
        """Regression (index_lock.py:180): the open itself runs under the
        refresh lock, so a refresh cannot start between probe and open."""
        data_dir = tmp_path / "hub"
        seen: list[bool] = []

        def opener() -> MagicMock:
            try:
                IndexLock(data_dir, REFRESH_LOCK, exclusive=True).acquire()
            except IndexLockedError:
                seen.append(True)
            else:
                seen.append(False)
            return MagicMock()

        self._store(data_dir, opener).open_now()
        assert seen == [True]

    def test_stale_store_is_closed_before_the_next_opens(self, tmp_path: Path) -> None:
        """Regression (reopening.py:42, reopening.py:150): a store made stale by
        a refresh is not closed under a running call, and no replacement opens
        until it is closed (calls meanwhile get the reopen-pending message)."""
        import asyncio

        from pipecat_context_hub.services.index.reopening import REOPEN_PENDING_MESSAGE

        data_dir = tmp_path / "hub"
        first, second = MagicMock(), MagicMock()
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow_search(_query: object) -> list[object]:
            started.set()
            await finish.wait()
            first.close.assert_not_called()
            return []

        first.vector_search = slow_search
        opener = MagicMock(side_effect=[first, second])
        store = self._store(data_dir, opener)

        async def scenario() -> None:
            call = asyncio.create_task(store.vector_search(MagicMock()))
            await started.wait()
            _simulate_refresh(data_dir)
            assert store.unavailable_reason() == REOPEN_PENDING_MESSAGE
            assert opener.call_count == 1  # nothing opened beside the stale store
            first.close.assert_not_called()
            finish.set()
            await call
            first.close.assert_called_once()
            assert store.unavailable_reason() is None  # now reopens
            assert opener.call_count == 2

        asyncio.run(scenario())

    def test_reopen_after_a_refresh_does_not_reuse_the_stale_chroma_system(
        self, tmp_path: Path
    ) -> None:
        """Regression (reopening.py:150), against real Chroma: chromadb shares
        one System per path among open clients, so a store reopened while the
        stale one was still in use served the stale graph. A refresh in another
        process adds a closer match; after the reopen, search must see it."""
        import asyncio
        import subprocess
        import textwrap

        from pipecat_context_hub.services.index.reopening import REOPEN_PENDING_MESSAGE
        from pipecat_context_hub.services.index.store import IndexStore
        from pipecat_context_hub.shared.config import StorageConfig
        from pipecat_context_hub.shared.types import IndexQuery
        from tests.unit.test_index_store import _make_record

        data_dir = tmp_path / "hub"
        storage = StorageConfig(data_dir=data_dir)
        seed = IndexStore(storage, create=True)
        asyncio.run(seed.upsert([_make_record("old", embedding=[1.0, 0.0])]))
        seed.close()

        store = self._store(data_dir, lambda: IndexStore(storage))
        query = IndexQuery(query_text="x", query_embedding=[0.0, 1.0], limit=1)
        in_flight = store._acquire()  # a call still running when the refresh lands
        assert [h.chunk.chunk_id for h in asyncio.run(in_flight.store.vector_search(query))] == [
            "old"
        ]
        writer = textwrap.dedent(
            f"""
            from pathlib import Path
            from pipecat_context_hub.services.index.vector import VectorIndex
            from tests.unit.test_index_store import _make_record
            index = VectorIndex(Path({str(storage.chroma_path)!r}), create=True)
            index.upsert([_make_record("new", embedding=[0.0, 1.0])])
            index.close()
            """
        )
        subprocess.run([sys.executable, "-c", writer], check=True, cwd=Path.cwd())
        _simulate_refresh(data_dir)
        try:
            assert store.unavailable_reason() == REOPEN_PENDING_MESSAGE
            store._release(in_flight)
            assert store.unavailable_reason() is None
            hits = asyncio.run(store.vector_search(query))
            assert [h.chunk.chunk_id for h in hits] == ["new"]
        finally:
            store.close()

    def test_reopen_failure_is_the_tool_answer(self, tmp_path: Path) -> None:
        """Regression (reopening.py:46): a reopen goes through the same
        readiness checks, and its failure becomes the gate's message."""
        from pipecat_context_hub.shared.interfaces import IndexUnavailableError

        data_dir = tmp_path / "hub"
        opener = MagicMock(side_effect=[MagicMock(), IndexUnavailableError("index is empty")])
        store = self._store(data_dir, opener)
        store.open_now()
        _simulate_refresh(data_dir)
        assert store.unavailable_reason() == "index is empty"

    def test_open_listeners_run_on_every_reopen(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "hub"
        store = self._store(data_dir, MagicMock(side_effect=[MagicMock(), MagicMock()]))
        store.open_now()
        listener = MagicMock()
        store.add_open_listener(listener)
        _simulate_refresh(data_dir)
        store.open_now()
        listener.assert_called_once()

    def test_close_does_not_open_the_index(self, tmp_path: Path) -> None:
        opener = MagicMock()
        self._store(tmp_path, opener).close()
        opener.assert_not_called()


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


def test_refresh_generation_changes_within_one_timestamp_tick(tmp_path: Path) -> None:
    """Regression (index_lock.py:148): the generation was (inode, mtime), so two
    refresh starts inside one coarse filesystem timestamp tick (1-2 s on
    HFS+/FAT/network mounts) read as the same generation and serve kept its
    stale handle. Simulate the coarse tick by pinning the mtime."""
    import os

    data_dir = tmp_path / "hub"
    path = lock_path(data_dir, REFRESH_LOCK)
    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        pass
    os.utime(path, ns=(1, 1))
    before = refresh_generation(data_dir)
    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        pass
    os.utime(path, ns=(1, 1))  # the same tick as before
    assert refresh_generation(data_dir) != before


def test_hidden_and_plain_sibling_dirs_get_distinct_locks(tmp_path: Path) -> None:
    """Regression (index_lock.py:103): the dirname's leading dots were
    stripped, so ``x`` and ``.x`` shared one lock (and one generation)."""
    assert lock_path(tmp_path / "x", REFRESH_LOCK) != lock_path(tmp_path / ".x", REFRESH_LOCK)
    assert lock_path(tmp_path / ".pipecat-context-hub", REFRESH_LOCK) == (
        tmp_path / ".pipecat-context-hub.refresh.lock"
    )  # the default data dir's lock name is unchanged


def test_shared_probe_does_not_bump_the_generation(tmp_path: Path) -> None:
    import os

    data_dir = tmp_path / "hub"
    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        pass
    os.utime(lock_path(data_dir, REFRESH_LOCK), ns=(1, 1))
    before = refresh_generation(data_dir)
    assert refresh_in_progress(data_dir) is False
    assert refresh_generation(data_dir) == before


def test_refresh_excluded_blocks_a_refresh_and_refuses_during_one(tmp_path: Path) -> None:
    from pipecat_context_hub.shared.index_lock import RefreshInProgressError, refresh_excluded

    data_dir = tmp_path / "hub"
    with refresh_excluded(data_dir):  # creates the lock file so it can be held
        with pytest.raises(IndexLockedError):
            IndexLock(data_dir, REFRESH_LOCK, exclusive=True).acquire()
    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        with pytest.raises(RefreshInProgressError, match="Retry in 60-120 seconds"):
            with refresh_excluded(data_dir):
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_lock_file_symlink_is_not_followed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (index_lock.py:102): a lock name planted as a symlink in a
    shared parent must not redirect the open or the mtime bump. Regression
    (docs/README.md): with O_NOFOLLOW the refusal was logged only at DEBUG,
    so the documented "continuing without a lock" warning never appeared."""
    import os

    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    os.utime(victim, ns=(1, 1))
    data_dir = tmp_path / "hub"
    lock_path(data_dir, REFRESH_LOCK).symlink_to(victim)
    lock = IndexLock(data_dir, REFRESH_LOCK, exclusive=True)
    with caplog.at_level(logging.WARNING, logger="pipecat_context_hub.shared.index_lock"):
        lock.acquire()  # best-effort: proceeds without a lock
    lock.release()
    assert "continuing without a lock" in caplog.text
    assert victim.stat().st_mtime_ns == 1
    assert victim.read_text() == "keep"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("dangling", [False, True])
def test_lock_file_symlink_is_refused_without_o_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dangling: bool
) -> None:
    """Regression (index_lock.py:141): without O_NOFOLLOW (Windows) the open
    followed a planted lock symlink, and the exclusive acquire truncated the
    target (or created a dangling one). Emulated by zeroing the flag."""
    from pipecat_context_hub.shared import index_lock

    monkeypatch.setattr(index_lock, "_O_NOFOLLOW", 0)
    victim = tmp_path / "victim.txt"
    if not dangling:
        victim.write_text("keep")
        os.utime(victim, ns=(1, 1))
    data_dir = tmp_path / "hub"
    lock_path(data_dir, REFRESH_LOCK).symlink_to(victim)
    lock = IndexLock(data_dir, REFRESH_LOCK, exclusive=True)
    lock.acquire()  # best-effort: proceeds without a lock
    assert lock._fd is None
    lock.release()
    if dangling:
        assert not victim.exists()
    else:
        assert victim.stat().st_mtime_ns == 1
        assert victim.read_text() == "keep"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_lock_file_symlink_planted_after_the_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without O_NOFOLLOW, a symlink swapped in between the pre-open check and
    the open is caught by the post-open re-check before anything is written."""
    from pipecat_context_hub.shared import index_lock

    monkeypatch.setattr(index_lock, "_O_NOFOLLOW", 0)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)  # the race
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    data_dir = tmp_path / "hub"
    lock_path(data_dir, REFRESH_LOCK).symlink_to(victim)
    lock = IndexLock(data_dir, REFRESH_LOCK, exclusive=True)
    lock.acquire()
    assert lock._fd is None
    assert victim.read_text() == "keep"


def test_lock_file_swapped_back_after_the_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (index_lock.py:59, index_lock.py:161): without O_NOFOLLOW the
    post-open check only lstat'ed the path, so a link swapped in for the open
    and swapped back to a regular file before the check passed it, and the
    exclusive acquire then truncated the file the open followed. The path must
    name the very file the descriptor opened."""
    from pipecat_context_hub.shared import index_lock

    monkeypatch.setattr(index_lock, "_O_NOFOLLOW", 0)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    data_dir = tmp_path / "hub"
    path = lock_path(data_dir, REFRESH_LOCK)
    path.write_text("")  # what the path names when it is checked
    real_open = os.open

    def _open_followed(target: Any, flags: int, mode: int = 0o777) -> int:
        # The open followed a link that has since been swapped back.
        return real_open(victim if Path(target) == path else target, flags, mode)

    monkeypatch.setattr(os, "open", _open_followed)
    lock = IndexLock(data_dir, REFRESH_LOCK, exclusive=True)
    lock.acquire()
    assert lock._fd is None
    assert victim.read_text() == "keep"


@pytest.mark.parametrize("exclusive", [True, False])
def test_lock_file_hard_link_is_not_written(tmp_path: Path, exclusive: bool) -> None:
    """Regression (index_lock.py:137): a lock name planted as a hard link to a
    file we own passed O_NOFOLLOW and the owner check, and the exclusive
    acquire then truncated it (size step) and bumped its mtime."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep this content")
    os.utime(victim, ns=(1, 1))
    data_dir = tmp_path / "hub"
    os.link(victim, lock_path(data_dir, REFRESH_LOCK))
    lock = IndexLock(data_dir, REFRESH_LOCK, exclusive=exclusive)
    lock.acquire()  # best-effort: proceeds without a lock
    assert lock._fd is None
    lock.release()
    assert victim.stat().st_mtime_ns == 1
    assert victim.read_text() == "keep this content"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX only")
def test_lock_file_owned_by_another_user_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    lock = IndexLock(tmp_path / "hub", REFRESH_LOCK, exclusive=True)
    lock.acquire()
    assert lock._fd is None


@pytest.mark.parametrize("script", ["extract_dashboard", "extract_embeddings"])
def test_dashboard_scripts_open_through_the_reader_path(
    script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (reopening.py:47): the dashboard scripts opened Chroma with a
    bare PersistentClient, skipping refresh exclusion and the open-time checks.
    They must refuse while a refresh holds the lock, and open via IndexStore
    (through the shared reopening.open_index_for_export)."""
    from tests.unit.test_config import _REPO_ROOT, _import_script

    module = _import_script(_REPO_ROOT / "dashboard" / "scripts" / f"{script}.py", script)
    data_dir = tmp_path / "hub"
    monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
    opened = MagicMock(side_effect=RuntimeError("opened"))
    monkeypatch.setattr("pipecat_context_hub.services.index.store.IndexStore", opened)

    with IndexLock(data_dir, REFRESH_LOCK, exclusive=True):
        with pytest.raises(SystemExit, match="Retry in 60-120 seconds"):
            module.main()
    opened.assert_not_called()

    # Any open failure becomes the operator message, not a traceback.
    with pytest.raises(SystemExit, match="opened"):
        module.main()
    opened.assert_called_once()


@pytest.mark.parametrize("script", ["extract_dashboard", "extract_embeddings"])
def test_dashboard_scripts_report_an_incompatible_index_without_a_traceback(
    script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (reopening.py:96): the dashboard reader open let
    IncompatibleIndexFormatError (a FatalIndexError, not IndexUnavailableError)
    escape, so the dashboard scripts crashed with a traceback instead of
    printing the reset remediation that serve and the CLI give."""
    import sqlite3

    from pipecat_context_hub.services.index.fts import FTSIndex
    from pipecat_context_hub.shared.config import StorageConfig
    from tests.unit.test_config import _REPO_ROOT, _import_script

    module = _import_script(_REPO_ROOT / "dashboard" / "scripts" / f"{script}.py", script)
    data_dir = tmp_path / "hub"
    monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
    chroma_path = StorageConfig(data_dir=data_dir).chroma_path
    chroma_path.mkdir(parents=True)
    with sqlite3.connect(chroma_path / "chroma.sqlite3") as conn:  # a chromadb 0.6 sysdb
        conn.execute("CREATE TABLE migrations (dir TEXT, version INTEGER)")
        conn.execute("INSERT INTO migrations VALUES ('sysdb', 1)")
    conn.close()
    FTSIndex(StorageConfig(data_dir=data_dir).sqlite_path, create=True).close()

    with pytest.raises(SystemExit, match="--reset-index"):
        module.main()


@pytest.mark.parametrize("script", ["extract_dashboard", "extract_embeddings"])
def test_dashboard_scripts_refuse_a_missing_index_without_creating_one(
    script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (extract_embeddings.py:37): on a missing index the dashboard
    scripts created chroma.sqlite3 and an empty collection, then carried on
    with 0 records (UMAP crashed on the empty array). They must exit with the
    `refresh` hint and create nothing under the data dir."""
    from tests.unit.test_config import _REPO_ROOT, _import_script

    module = _import_script(_REPO_ROOT / "dashboard" / "scripts" / f"{script}.py", script)
    data_dir = tmp_path / "hub"
    monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))

    with pytest.raises(SystemExit, match="refresh"):
        module.main()
    assert not data_dir.exists()

    # A built but empty index gets the same message as serve and the CLI.
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.shared.config import StorageConfig

    IndexStore(StorageConfig(data_dir=data_dir), create=True).close()
    with pytest.raises(SystemExit, match=r"is empty \(0 records\)\. Run 'uv run"):
        module.main()


@pytest.mark.parametrize("script", ["extract_dashboard", "extract_embeddings"])
def test_dashboard_scripts_refuse_a_lost_vector_half(
    script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (cli.py:123): an empty ``latest`` beside a populated keyword
    index (what a refused refresh used to leave) was opened as a valid, empty
    index. Every reader, the dashboard included, must give the --force remedy."""
    from pipecat_context_hub.shared.config import StorageConfig
    from tests.unit.test_config import _REPO_ROOT, _import_script
    from tests.unit.test_index_store import _lose_vector_half

    module = _import_script(_REPO_ROOT / "dashboard" / "scripts" / f"{script}.py", script)
    data_dir = tmp_path / "hub"
    monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(data_dir))
    _lose_vector_half(StorageConfig(data_dir=data_dir), empty_latest=True)

    with pytest.raises(SystemExit, match="refresh --force"):
        module.main()
