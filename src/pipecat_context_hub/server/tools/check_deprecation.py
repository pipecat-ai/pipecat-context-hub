"""check_deprecation MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.services.ingest.deprecation_map import status_for
from pipecat_context_hub.shared.types import CheckDeprecationInput, CheckDeprecationOutput
from pipecat_context_hub.shared.versioning import is_latest_sentinel


def resolve_framework_version(index_store: Any, deprecation_map: Any = None) -> str | None:
    """The indexed pipecat version, the default ``version`` for ``check_deprecation``.

    Shared by the MCP server and the one-shot CLI so both resolve the default the
    same way. Uses ``indexed_framework_version`` only when
    ``indexed_framework_commits_ahead`` is exactly zero: an unpinned default-branch
    refresh records the nearest release as a floor, not the exact version of the
    indexed code. For a floor (or incomplete provenance), returning ``None`` lets
    the deprecation handler preserve the registry entry's intrinsic status instead
    of evaluating it against a potentially older release. Falls back to
    ``framework_version`` only when no indexed revision is recorded — and never
    to the ``latest`` sentinel, which metadata contract v2 permits that key to
    hold: it is a pin, not a version, so it names no release to evaluate against.

    When ``deprecation_map`` is supplied, also cross-checks the metadata's
    ``deprecation_map_commit_sha`` stamp against the map's own
    ``pipecat_commit_sha``. A refresh crash between publishing the deprecation
    map and writing the provenance metadata can leave the two describing
    different revisions; when that divergence is detectable, this falls back to
    ``None`` rather than asserting version-exactness against a map that may not
    match. Missing/None on either side skips the check (fail open, matching the
    existing tolerance for missing provenance elsewhere).
    """
    if index_store is None:
        return None
    metadata = index_store.get_all_metadata()
    indexed = metadata.get("indexed_framework_version")
    if indexed is not None:
        try:
            commits_ahead = int(str(metadata.get("indexed_framework_commits_ahead")))
        except (TypeError, ValueError):
            commits_ahead = None
        if commits_ahead == 0:
            stamped_sha = metadata.get("deprecation_map_commit_sha")
            loaded_sha = getattr(deprecation_map, "pipecat_commit_sha", None) or None
            if stamped_sha and loaded_sha and stamped_sha != loaded_sha:
                # The on-disk map and the indexed-version stamp were committed
                # in different runs (crash/failure between the two writes) —
                # don't assert version-exactness against a map that may not
                # match. Callers fall back to the entry's intrinsic status.
                return None
            return str(indexed)
        return None
    version = metadata.get("framework_version")
    if version is None or is_latest_sentinel(str(version)):
        return None
    return str(version)


async def handle_check_deprecation(
    arguments: dict[str, Any],
    deprecation_map: Any,
    framework_version: str | None = None,
) -> str:
    """Check whether a symbol is deprecated or removed in the pipecat framework.

    Args:
        arguments: Raw tool arguments from MCP call.
        deprecation_map: A ``DeprecationMap`` instance (from the retriever).
        framework_version: The indexed pipecat version, used as the default when the
            caller doesn't pass ``version`` — so the answer reflects the indexed data.
    """
    inp = CheckDeprecationInput.model_validate(arguments)

    if deprecation_map is None:
        output = CheckDeprecationOutput(
            deprecated=False,
            status="current",
            note="Deprecation map not available. Run `refresh` to build it.",
        )
        return output.model_dump_json()

    entry = deprecation_map.check(inp.symbol)
    if entry is None:
        return CheckDeprecationOutput(deprecated=False, status="current").model_dump_json()

    target_version = inp.version or framework_version
    status = status_for(entry, target_version)

    if status == "current":
        # Known to the registry but not deprecated yet at the evaluated version.
        at = f" at {target_version}" if target_version else ""
        suffix = f" (deprecated in {entry.deprecated_in})." if entry.deprecated_in else "."
        output = CheckDeprecationOutput(
            deprecated=False,
            status="current",
            deprecated_in=entry.deprecated_in,
            note=f"`{inp.symbol}` is not deprecated{at}{suffix}",
        )
        return output.model_dump_json()

    note: str | None
    if status == "removed":
        replacement = f" Use `{entry.new_path}` instead." if entry.new_path else " No replacement."
        note = f"`{entry.old_path}` was removed in {entry.removed_in}.{replacement}"
    else:
        note = entry.note or None

    output = CheckDeprecationOutput(
        deprecated=True,
        status=status,
        replacement=entry.new_path,
        deprecated_in=entry.deprecated_in,
        removed_in=entry.removed_in,
        announced_removed_in=entry.announced_removed_in,
        note=note,
        kind=entry.kind,
        relation=entry.relation,
        location=entry.location,
    )
    return output.model_dump_json()
