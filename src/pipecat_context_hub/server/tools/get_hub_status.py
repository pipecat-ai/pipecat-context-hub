"""get_hub_status MCP tool handler."""

from __future__ import annotations

import logging
import asyncio
from collections.abc import Callable
from typing import Any, TypeVar, cast

from pipecat_context_hub.shared.interfaces import IndexStatusReader
from pipecat_context_hub.shared.types import HubStatusOutput, RerankerStatus

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _parse(value: str | None, cast: Callable[[str], _T]) -> _T | None:
    """Convert a metadata string, or None when it is absent or malformed.

    Status is what an operator reaches for when something looks wrong, so a
    single unparseable value must not be what stops them seeing the rest.
    """
    if not value:
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        logger.warning("Ignoring unparseable index metadata value: %r", value)
        return None


async def handle_get_hub_status(
    arguments: dict[str, Any],
    index_store: IndexStatusReader,
    reranker_status: RerankerStatus | None = None,
) -> str:
    """Return index health metadata: freshness, record counts, commit SHAs.

    *reranker_status* reflects live runtime state (not configured intent),
    built by the CLI after the reranker is constructed or skipped. When
    omitted, reranker fields report as disabled.
    """
    # Import here to use the same version string as the server.
    from pipecat_context_hub.server.main import _SERVER_VERSION

    # Both accessors synchronously acquire the generation lock.  A refresh
    # may hold it for minutes, so never make an MCP event-loop task wait on
    # either call directly.
    stats_raw, metadata_raw = await asyncio.gather(
        asyncio.to_thread(index_store.get_index_stats),
        asyncio.to_thread(index_store.get_all_metadata),
    )
    stats = cast(dict[str, Any], stats_raw)
    metadata = metadata_raw

    duration = _parse(metadata.get("last_refresh_duration_seconds"), float)
    commits_ahead = _parse(metadata.get("indexed_framework_commits_ahead"), int)

    if reranker_status is None:
        # Caller didn't wire a provider — we don't actually know why
        # reranking is off, so leave disabled_reason unset.
        reranker_status = RerankerStatus(enabled=False)

    output = HubStatusOutput(
        server_version=_SERVER_VERSION,
        last_refresh_at=metadata.get("last_refresh_at"),
        last_refresh_duration_seconds=duration,
        total_records=stats["total"],
        counts_by_type=stats["counts_by_type"],
        commit_shas=stats.get("commit_shas", []),
        index_path=str(index_store.data_dir),
        framework_version=metadata.get("framework_version"),
        indexed_framework_version=metadata.get("indexed_framework_version"),
        indexed_framework_commits_ahead=commits_ahead,
        reranker_enabled=reranker_status.enabled,
        reranker_model=reranker_status.model,
        reranker_configured_model=reranker_status.configured_model,
        reranker_disabled_reason=reranker_status.disabled_reason,
    )
    return output.model_dump_json()
