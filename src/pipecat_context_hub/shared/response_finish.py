"""The one post-handler step both front doors run on a tool's JSON response.

MCP's ``call_tool`` (``server/main.py``) and the CLI's ``_invoke``
(``cli_query.py``) both call :func:`finish_response`, so the two give identical
JSON by construction. A new post-handler step belongs here, not in either door.

``shared/`` imports nothing from ``services/`` at runtime; the index store is
typed under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from typing import TYPE_CHECKING, Any

from pipecat_context_hub.shared.corpus_hint import attach_corpus_hint, corpus_hint
from pipecat_context_hub.shared.staleness import annotate_response

if TYPE_CHECKING:
    from pipecat_context_hub.shared.interfaces import ServingIndex

# The status tool *is* the staleness report and never hints.
_UNFINISHED_TOOLS = frozenset({"get_hub_status"})


def finish_response(
    index_store: ServingIndex,
    *,
    tool: str,
    args: Mapping[str, Any],
    result_json: str,
    configured_repos: Set[str],
    tainted_repos: Set[str],
) -> tuple[str, dict[str, Any] | None]:
    """The finished JSON and the corpus hint for one tool call.

    Computes the corpus hint, adds the staleness footer (``annotate_response``),
    then attaches the hint. ``get_hub_status`` comes back untouched. The hint
    is returned too, so the CLI can let its stderr poor-results hint yield to
    it. Never raises (neither step does).
    """
    if tool in _UNFINISHED_TOOLS:
        return result_json, None
    hint = corpus_hint(
        index_store,
        tool=tool,
        args=args,
        configured_repos=configured_repos,
        tainted_repos=tainted_repos,
    )
    return attach_corpus_hint(annotate_response(result_json, index_store), hint), hint
