"""Runtime tracking helpers shared across server layers.

Separate from ``shared/types.py`` (Pydantic data contracts) because
these are stateful runtime objects, not schema definitions.
"""

from __future__ import annotations

import time


class IdleTracker:
    """Tracks the time since the last MCP tool dispatch.

    Used by ``server/main.py`` (the ``call_tool`` dispatcher, producer)
    and ``server/transport.py`` (the idle watchdog, consumer).

    Single-event-loop semantics: ``touch()`` and ``seconds_since_last()``
    are called from the same asyncio loop, so no lock is needed; float
    read/write is atomic under the GIL. ``time.monotonic`` is used so
    wall-clock changes can't trigger spurious idle fires.
    """

    def __init__(self) -> None:
        self._last = time.monotonic()

    def touch(self) -> None:
        self._last = time.monotonic()

    def seconds_since_last(self) -> float:
        return time.monotonic() - self._last
