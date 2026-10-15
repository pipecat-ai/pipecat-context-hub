"""An index handle for ``serve`` that is dropped and reopened after a refresh.

``serve`` holds one index handle for days. A ``refresh`` rewrites the Chroma
files underneath it, so the long-lived handle then serves a stale in-memory
graph, and (per the 2026-10-15 spikes) opening an index mid-write is a likely
trigger for the graph corruption in chroma#7510. This handle lets ``serve``
(a) start without opening the index while a refresh is running, (b) notice
any refresh since it opened (the refresh lock file's generation) and reopen,
and (c) never open while a refresh runs: every open happens under
:func:`refresh_excluded`.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pipecat_context_hub.shared.index_lock import (
    REFRESH_IN_PROGRESS_MESSAGE,
    refresh_excluded,
    refresh_generation,
    refresh_in_progress,
)
from pipecat_context_hub.shared.interfaces import IndexUnavailableError
from pipecat_context_hub.shared.paths import redact_home, redact_home_in_text
from pipecat_context_hub.shared.support_links import bug_report_hint
from pipecat_context_hub.shared.types import IndexQuery, IndexResult

if TYPE_CHECKING:
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.services.index.vector import VectorIndex
    from pipecat_context_hub.shared.config import StorageConfig

logger = logging.getLogger(__name__)

REOPEN_PENDING_MESSAGE = (
    "The Pipecat context index was just rebuilt by a `refresh`, and the server is "
    "switching to it once a call still using the old one finishes. Retry in a few seconds."
)


class _Closable(Protocol):
    def close(self) -> None: ...


_IndexT = TypeVar("_IndexT", bound=_Closable)


def open_ready_index_store(storage: StorageConfig) -> IndexStore:
    """Open the index for reading and check it can answer queries.

    The one reader open path: ``serve``'s first open and every reopen, and
    each one-shot CLI query. The caller holds :func:`refresh_excluded`
    (``serve`` and the CLI hold it around more than this open).
    Raises ``IndexUnavailableError`` (after closing whatever came up) when the
    index is missing, incompatible, unreadable, mid-compaction, or empty; its message is
    the home-redacted operator guidance, ending with the bug-report hint.
    """
    from pipecat_context_hub.services.index.store import IndexStore as _IndexStore

    return _open_ready(
        storage,
        lambda: _IndexStore(storage, create=False),  # a reader never creates the index
        lambda store: int(store.get_index_stats().get("total", 0)),
    )


def open_ready_vector_index(storage: StorageConfig) -> VectorIndex:
    """Open only the vector index for a bulk read (the dashboard scripts).

    The same reader open as :func:`open_ready_index_store`, minus the keyword
    index, with one difference: it takes :func:`refresh_excluded` itself (its
    callers are standalone scripts that hold nothing else), so a running
    refresh raises ``RefreshInProgressError``.
    """
    from pipecat_context_hub.services.index.vector import VectorIndex as _VectorIndex

    with refresh_excluded(storage.data_dir):
        return _open_ready(
            storage,
            lambda: _VectorIndex(storage.chroma_path, create=False),
            lambda index: index.count(),
        )


def _open_ready(
    storage: StorageConfig,
    open_index: Callable[[], _IndexT],
    count: Callable[[_IndexT], int],
) -> _IndexT:
    """Open with *open_index*, refuse an empty index, and turn every failure
    into ``IndexUnavailableError`` with the operator guidance."""
    from pipecat_context_hub.services.index import IncompatibleIndexFormatError

    index: _IndexT | None = None
    try:
        index = open_index()
        total = count(index)
    except Exception as exc:
        # IndexStore.__init__ opens two backends (Chroma + SQLite) without
        # rolling back on partial failure; close() whatever did come up.
        if index is not None:
            with contextlib.suppress(Exception):
                index.close()
        if isinstance(exc, (IncompatibleIndexFormatError, IndexUnavailableError)):
            # Typed errors carry their own remediation (an incompatible or
            # corrupt graph: reset; an interrupted compaction: refresh).
            message = redact_home_in_text(str(exc))
        else:
            message = (
                f"Failed to open index at {redact_home(storage.data_dir)}: "
                f"{redact_home_in_text(str(exc))}. "
                "Run 'uv run pipecat-context-hub refresh --force --reset-index' to rebuild."
            )
        raise IndexUnavailableError(f"{message} {bug_report_hint()}") from exc

    if total == 0:
        index.close()
        raise IndexUnavailableError(f"{_empty_index_message(storage)} {bug_report_hint()}")
    return index


def _empty_index_message(storage: StorageConfig) -> str:
    return (
        f"Index at {redact_home(storage.data_dir)} is empty (0 records). Run 'uv run "
        "pipecat-context-hub refresh' to build it (the first run downloads models and "
        "indexes sources; allow a few minutes)."
    )


@dataclass
class _Handle:
    store: IndexStore
    # refresh_generation() when this store was opened; any later refresh
    # start changes it.
    generation: tuple[int, int, int] | None
    users: int = 0
    retired: bool = False
    closed: bool = False


class ReopeningIndexStore:
    """Lazily opened, replaceable read handle satisfying ``ServingIndex``.

    *opener* builds a ready ``IndexStore`` or raises ``IndexUnavailableError``
    with the message for the caller; it runs for the initial open and every
    reopen, so a reopened index gets the same readiness checks as startup.

    Each read checks the store out for the duration of the call. There is only
    ever one store: one made stale by a refresh is closed once no call is still
    using it, and only then is the next one opened (until then calls get
    ``REOPEN_PENDING_MESSAGE``). Opening it earlier would not help: Chroma
    hands every client on a path the same native System, so the new client
    would serve the stale graph.
    """

    def __init__(self, data_dir: Path, opener: Callable[[], IndexStore]) -> None:
        self._data_dir = data_dir
        self._opener = opener
        self._handle: _Handle | None = None
        self._open_listeners: list[Callable[[], None]] = []
        self._lock = threading.RLock()

    @property
    def data_dir(self) -> Path:
        """The index data directory. Never opens the index."""
        return self._data_dir

    def add_open_listener(self, listener: Callable[[], None]) -> None:
        """Run *listener* after every later (re)open, still under the refresh hold.

        For state that a refresh rewrites alongside the index (the
        deprecation map), so it is reloaded together with the index.
        """
        self._open_listeners.append(listener)

    def open_now(self) -> None:
        """Open the index if it is not open (or a refresh made it stale).

        Raises ``IndexUnavailableError`` (``RefreshInProgressError`` while a
        refresh runs) with the caller-facing reason.
        """
        self._release(self._acquire())

    def unavailable_reason(self) -> str | None:
        """Gate run before every tool call: a message to answer with, or None.

        While a refresh runs this answers "retry in 60-120 seconds" without
        touching the index. Otherwise it (re)opens the index if needed, so a
        refresh that started and finished between two calls is still noticed.
        """
        if refresh_in_progress(self._data_dir):
            return REFRESH_IN_PROGRESS_MESSAGE
        try:
            self.open_now()
        except IndexUnavailableError as exc:
            return str(exc)
        return None

    # -- ServingIndex ------------------------------------------------------

    async def vector_search(self, query: IndexQuery) -> list[IndexResult]:
        handle = self._acquire()
        try:
            return await handle.store.vector_search(query)
        finally:
            self._release(handle)

    async def keyword_search(self, query: IndexQuery) -> list[IndexResult]:
        handle = self._acquire()
        try:
            return await handle.store.keyword_search(query)
        finally:
            self._release(handle)

    def get_index_stats(self) -> dict[str, Any]:
        with self._using() as store:
            return store.get_index_stats()

    def get_all_metadata(self) -> dict[str, str]:
        with self._using() as store:
            return store.get_all_metadata()

    def close(self) -> None:
        """Close the current store now (shutdown), even if a call is using it."""
        with self._lock:
            handle, self._handle = self._handle, None
            if handle is not None and not handle.closed:
                handle.retired = True
                handle.closed = True
                handle.store.close()

    # -- lifecycle ---------------------------------------------------------

    @contextmanager
    def _using(self) -> Iterator[IndexStore]:
        handle = self._acquire()
        try:
            yield handle.store
        finally:
            self._release(handle)

    def _acquire(self) -> _Handle:
        with self._lock:
            handle = self._handle
            if (
                handle is not None
                and not handle.retired
                and refresh_generation(self._data_dir) != handle.generation
            ):
                handle.retired = True
            if handle is not None and handle.retired:
                if handle.users:
                    # Chroma shares one native System per path among all open
                    # clients, so a client opened now would reuse the stale
                    # graph this handle loaded. Reopen only once it is closed.
                    raise IndexUnavailableError(REOPEN_PENDING_MESSAGE)
                self._close_quietly(handle)
                self._handle = handle = None
            if handle is None:
                handle = self._open()
                self._handle = handle
            handle.users += 1
            return handle

    def _release(self, handle: _Handle) -> None:
        with self._lock:
            handle.users -= 1
            if handle.retired and handle.users == 0:
                self._close_quietly(handle)
                if self._handle is handle:
                    self._handle = None

    def _open(self) -> _Handle:
        # The hold keeps a refresh from starting until the open (and the
        # listeners) finished, and raises RefreshInProgressError if one runs.
        with refresh_excluded(self._data_dir):
            generation = refresh_generation(self._data_dir)
            store = self._opener()
            for listener in self._open_listeners:
                try:
                    listener()
                except Exception:
                    logger.warning("Index reopen listener failed", exc_info=True)
        return _Handle(store=store, generation=generation)

    @staticmethod
    def _close_quietly(handle: _Handle) -> None:
        if handle.closed:
            return
        handle.closed = True
        try:
            handle.store.close()
        except Exception:
            logger.warning("Failed to close a replaced index store", exc_info=True)
