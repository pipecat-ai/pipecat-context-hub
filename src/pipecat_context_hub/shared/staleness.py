"""Index-staleness annotation for tool responses.

The local index is a snapshot that silently ages, and the failure mode is
invisible: queries keep succeeding with quietly outdated results — the one
failure a freshness tool shouldn't have. Rather than relying on callers to
poll ``get_hub_status``, both front doors (the MCP server's ``call_tool`` and
the CLI's ``_dispatch``) annotate every tool response with an
``index_staleness`` object once the index is older than a threshold::

    {"hits": [...], "index_staleness": {
        "last_refresh_at": "2026-05-22T...",
        "age_days": 21,
        "hint": "Index is 21 days old; run 'pipecat-context-hub refresh' to update it."
    }}

The field is **absent** when the index is fresh (or its age is unknown), so
the common case carries zero noise. The signal arrives exactly where an agent
is already looking — inside the response it asked for — with the remediation
command attached, mirroring the CLI's exit-2 "run refresh" contract.

``get_hub_status`` is never annotated: it *is* the staleness report.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipecat_context_hub.services.index.store import IndexStore

logger = logging.getLogger(__name__)

# Days after which tool responses start carrying the staleness annotation.
# Override with PIPECAT_HUB_STALE_AFTER_DAYS; <= 0 disables the annotation.
_DEFAULT_STALE_AFTER_DAYS = 7.0
_STALE_AFTER_ENV = "PIPECAT_HUB_STALE_AFTER_DAYS"


def _stale_after_days() -> float:
    """Resolve the staleness threshold, env override first."""
    raw = os.environ.get(_STALE_AFTER_ENV, "").strip()
    if not raw:
        return _DEFAULT_STALE_AFTER_DAYS
    try:
        value = float(raw)
    except ValueError:
        value = None
    # Reject non-numeric AND non-finite (nan/inf): `inf` would make every index
    # look fresh (age < inf is always true) and `nan` would annotate even a
    # fresh one (all nan comparisons are false), silently defeating the warning.
    if value is None or not math.isfinite(value):
        logger.warning(
            "Invalid %s=%r — using default of %s days",
            _STALE_AFTER_ENV,
            raw,
            _DEFAULT_STALE_AFTER_DAYS,
        )
        return _DEFAULT_STALE_AFTER_DAYS
    return value


def staleness_info(index_store: IndexStore) -> dict[str, Any] | None:
    """Return the ``index_staleness`` payload, or None when fresh/unknown.

    Reads ``last_refresh_at`` per call rather than caching, so a long-lived
    ``serve`` reflects a concurrent ``refresh`` immediately. Unknown or
    unparseable timestamps return None — the annotation must never nag when
    it can't substantiate the claim, and never break a response (callers
    additionally guard with try/except).
    """
    threshold = _stale_after_days()
    if threshold <= 0:
        return None

    last_refresh_at = index_store.get_all_metadata().get("last_refresh_at")
    if not last_refresh_at:
        return None
    try:
        refreshed = datetime.fromisoformat(last_refresh_at)
    except ValueError:
        return None
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=UTC)

    age_days = (datetime.now(UTC) - refreshed).total_seconds() / 86400
    if age_days < threshold:
        return None

    return {
        "last_refresh_at": last_refresh_at,
        "age_days": round(age_days, 1),
        "hint": (
            f"Index is {age_days:.0f} days old; run 'pipecat-context-hub refresh' to update it."
        ),
    }


def annotate_response(
    result_json: str,
    index_store: IndexStore,
    *,
    parsed: dict[str, Any] | None = None,
) -> str:
    """Inject ``index_staleness`` into a tool handler's JSON response.

    Returns the input unchanged when the index is fresh, when the payload
    isn't a JSON object, or when anything at all goes wrong — the annotation
    is best-effort and must never cost a response.

    *parsed* lets a caller that already decoded ``result_json`` for its own
    purposes (e.g. inspecting it for a different hint) pass that dict through
    instead of this function re-parsing the same string. Optional and
    keyword-only so every existing caller is unaffected; when omitted this
    parses ``result_json`` itself exactly as before. This function never
    mutates *parsed* — it copies before adding ``index_staleness``, so the
    caller's own dict is safe to inspect or reuse afterward.
    """
    try:
        info = staleness_info(index_store)
        if info is None:
            return result_json
        payload = dict(parsed) if parsed is not None else json.loads(result_json)
        if not isinstance(payload, dict):
            return result_json
        payload["index_staleness"] = info
        return json.dumps(payload)
    except Exception:
        logger.exception("Failed to annotate response with staleness info")
        return result_json
