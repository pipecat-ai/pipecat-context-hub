"""An IndexStore handle that can be dropped and reopened after a refresh.

``serve`` holds one index handle for days. A ``refresh`` rewrites the Chroma
files underneath it, so the long-lived handle then serves a stale in-memory
graph, and (per the 2026-10-15 spikes) opening an index mid-write is a likely
trigger for the graph corruption in chroma#7510. This proxy lets ``serve``
(a) start without opening the index while a refresh is running and (b) discard
its handle once a refresh has happened, reopening on the next call.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class ReopeningIndexStore:
    """Lazily opened, replaceable stand-in for ``IndexStore``.

    Attribute access is delegated to the current store, opening it on demand.
    ``mark_stale()`` schedules the current store to be closed and replaced on
    the next access; it never touches the store itself, so it is safe to call
    from a watcher thread while a refresh is writing.
    """

    def __init__(self, factory: Callable[[], Any], initial: Any | None = None) -> None:
        self._factory = factory
        self._real = initial
        self._stale = False
        self._lock = threading.RLock()

    def mark_stale(self) -> None:
        with self._lock:
            self._stale = True

    def _current(self) -> Any:
        with self._lock:
            if self._real is not None and self._stale:
                real, self._real = self._real, None
                try:
                    real.close()
                finally:
                    self._stale = False
            if self._real is None:
                self._real = self._factory()
                self._stale = False
            return self._real

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not found normally. Never delegate private or
        # dunder lookups: they must not open the index as a side effect.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._current(), name)

    def close(self) -> None:
        with self._lock:
            real, self._real = self._real, None
            if real is not None:
                real.close()
