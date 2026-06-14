"""check_deprecation MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.services.ingest.deprecation_map import status_for
from pipecat_context_hub.shared.types import CheckDeprecationInput, CheckDeprecationOutput


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
