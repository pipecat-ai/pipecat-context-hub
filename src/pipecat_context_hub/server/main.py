"""MCP server entry point — tool registration and request dispatch."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from pydantic import ValidationError

from pipecat_context_hub.server.tools.check_deprecation import (
    handle_check_deprecation,
    resolve_framework_version,
)
from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status
from pipecat_context_hub.server.dispatch import (
    BASE_TOOLS,
    HUB_STATUS_TOOL_TUPLE,
    get_tool_handler,
    iter_tool_definitions,
)
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.staleness import annotate_response
from pipecat_context_hub.shared.support_links import (
    BUG_REPORT_ISSUE_URL,
    RETRIEVAL_QUALITY_ISSUE_URL,
)
from pipecat_context_hub.shared.tracking import IdleTracker
from pipecat_context_hub.shared.types import RerankerStatus

logger = logging.getLogger(__name__)

# Kept in sync with pyproject.toml [project].version by
# tests/unit/test_server.py::TestVersionConsistency. If this is ever replaced
# with a runtime lookup, the PyPI distribution name is "pipecat-ai-context-hub"
# (not "pipecat-context-hub", which is only the command / server name) —
# importlib.metadata.version() must use the former.
_SERVER_VERSION = "0.7.0"

# Tool name → (description, input schema, handler)
_BASE_TOOLS = BASE_TOOLS
_HUB_STATUS_TOOL = HUB_STATUS_TOOL_TUPLE


_UNSUBSTITUTED_PLACEHOLDER = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


def _assert_no_unsubstituted_placeholder(text: str) -> None:
    """Raise if ``text`` still contains an unsubstituted ``{PLACEHOLDER}``.

    ``_SERVER_INSTRUCTIONS`` is built via ``.replace("{URL_CONST}", ...)``
    rather than an f-string or ``.format()`` — a deliberate choice (``d97e23d``)
    so a future stray brace in the prose (e.g. a JSON example) can't crash
    import. That tradeoff means a renamed/typo'd ``RETRIEVAL_QUALITY_ISSUE_URL``
    or ``BUG_REPORT_ISSUE_URL`` constant would make the substitution a silent
    no-op instead, shipping literal ``{PLACEHOLDER}`` text to MCP clients with
    no error. This guard closes that gap.

    The check matches ``{UPPER_SNAKE_CASE}`` specifically — the naming
    convention every ``.replace()`` target in this module follows — rather
    than any literal brace, so a future JSON example in the prose (e.g.
    ``{"query": "TTS"}``) does not itself trip this guard; only an actual
    unresolved placeholder token does.

    Raise rather than ``assert`` so the guard survives ``python -O`` (which
    strips asserts) — see transport.py's ``_INTERMEDIATE_LAUNCHERS`` guard
    for the same convention: fail loudly at import/CI instead.
    """
    if _UNSUBSTITUTED_PLACEHOLDER.search(text):
        raise ValueError(
            "_SERVER_INSTRUCTIONS still contains an unsubstituted {PLACEHOLDER} — "
            "a renamed/typo'd RETRIEVAL_QUALITY_ISSUE_URL or BUG_REPORT_ISSUE_URL "
            "constant would otherwise ship literal placeholder text to MCP clients "
            "with no error. Check the .replace() chain above against the two "
            "shared/support_links.py constants."
        )


_SERVER_INSTRUCTIONS = """\
You are using the Pipecat Context Hub — a retrieval server for Pipecat \
framework documentation, code examples, and API source.

**Always use these tools for Pipecat questions instead of reading .venv or \
source files directly.**

Tool selection guide:
- "How do I ...?" / conceptual questions → search_docs
- "Show me an example of ..." / working code → search_examples, then get_example
- Class constructors, method signatures, frame types → search_api
- Specific code span or symbol → get_code_snippet
- Retrieve a specific doc page → get_doc
- Import deprecation check → check_deprecation
- Index health, freshness, version info → get_hub_status

**Important:** When you see pipecat imports in user code (e.g. \
``from pipecat.services.grok import ...``), use ``check_deprecation`` \
to verify the import path is not deprecated before recommending it.

**Version-aware results:** If the user mentions their pipecat version \
(e.g. in pyproject.toml or requirements.txt), pass it as \
``pipecat_version`` to ``search_examples``, ``search_api``, and \
``get_code_snippet``. This scores results for compatibility and \
annotates them with ``version_compatibility``. Use \
``version_filter="compatible_only"`` to exclude results requiring a \
newer version than theirs.

Multi-concept queries: use ` + ` or ` & ` to search for multiple concepts \
at once (e.g. "idle timeout + function calling + Gemini"). Each concept is \
searched independently and results are interleaved for balanced coverage.

When suggesting commands for Pipecat projects, always use `uv` as the \
package manager:
- Install dependencies: `uv sync` (not `pip install`)
- Run scripts: `uv run python bot.py` (not `python bot.py`)
- Add packages: `uv add <package>` (not `pip install <package>`)
- Run tools: `uv run pytest`, `uv run mypy`, etc.

Pipecat examples use `uv` and include a `pyproject.toml`. Do not suggest \
`pip`, `venv`, or `conda` unless the user explicitly requests them.

**When results are poor or missing:** If a search returns ``low_confidence: true``, \
zero hits, or the user says the results are wrong, try these steps before giving up:
1. Remove filters and increase ``limit`` to 20 — check if the content exists but \
was filtered out.
2. Try ``get_doc(path="...")`` or ``search_api(query="SYMBOL")`` for direct lookup \
— the content may be indexed under a different name.
3. Try 2-3 rephrased queries or multi-concept queries (`` + `` syntax).
4. Check ``get_hub_status`` — the index may be stale or missing content types.

If none of these work, suggest the user file a retrieval quality issue at \
{RETRIEVAL_QUALITY_ISSUE_URL} \
— the issue template includes a diagnostic prompt that you can run to generate \
a structured report for the maintainers.

**When the hub itself is degraded after initialization:** If ``get_hub_status`` reports \
``reranker_disabled_reason`` of ``not_cached`` or ``load_failed``, the \
hub is running in a degraded mode.

If the reason is ``not_cached``, suggest the user run \
``pipecat-context-hub refresh`` first — this downloads the reranker model \
and is the most common fix. This ``pipecat-context-hub`` MCP server process \
already resolved its reranker state at startup and does not re-check the \
model cache while running, so ``refresh`` alone will not change what this \
connection reports: the fix requires actually terminating and restarting \
the underlying ``pipecat-context-hub`` server process — a client-side \
"reconnect" that reuses the same running process will not help, since \
this state was resolved once at that process's startup, not per \
connection. After the process has genuinely restarted, re-run \
``get_hub_status`` on the new connection to confirm the fix — \
re-checking ``get_hub_status`` on the current connection, or on a \
reconnect that did not restart the process, will still show \
``not_cached`` even after a successful ``refresh``. If \
restarting doesn't resolve it, or the reason is \
``load_failed``, share the full ``get_hub_status`` response and any \
``pipecat-context-hub`` startup log lines (look for \
``Reranker disabled at startup`` and the ``pipecat-context-hub vX.Y.Z \
starting`` banner) with the user and suggest they file a bug report at \
{BUG_REPORT_ISSUE_URL} \
so the maintainers can diagnose from the trace alone.

If the MCP connection fails at boot with a non-zero exit code, the failure \
happened before MCP initialization, so ``get_hub_status`` is unavailable. \
Follow the remediation in the startup stderr first, then reconnect. Empty \
indexes prescribe ``pipecat-context-hub refresh``; unreadable or incompatible \
indexes prescribe ``pipecat-context-hub refresh --force --reset-index``. Only \
request ``get_hub_status`` after successful initialization. If the prescribed \
remediation does not resolve the boot failure, share the startup stderr with \
the user and suggest they file a bug report at {BUG_REPORT_ISSUE_URL}.

A ``reranker_disabled_reason`` of ``config_disabled`` is a supported \
operator choice (``PIPECAT_HUB_RERANKER_ENABLED=0``), not a degraded state \
— do not treat it as an incident or route it into the bug-report flow.\
""".replace("{RETRIEVAL_QUALITY_ISSUE_URL}", RETRIEVAL_QUALITY_ISSUE_URL).replace(
    "{BUG_REPORT_ISSUE_URL}", BUG_REPORT_ISSUE_URL
)
_assert_no_unsubstituted_placeholder(_SERVER_INSTRUCTIONS)


def create_server(
    retriever: Retriever,
    index_store: IndexStore | None = None,
    reranker_status_provider: Callable[[], RerankerStatus] | None = None,
    idle_tracker: IdleTracker | None = None,
) -> Server:
    """Create and configure the MCP server with all tool handlers.

    When *index_store* is provided the ``get_hub_status`` tool is registered;
    otherwise it is omitted so clients never discover an unusable tool.

    *reranker_status_provider* is a zero-arg callable returning the
    current reranker state. Evaluated on every ``get_hub_status`` call so
    post-startup availability changes (e.g. first-query load failures)
    are reflected. When omitted, reranking is reported as disabled.
    """
    tool_registry = tuple(iter_tool_definitions(include_hub_status=index_store is not None))

    async def list_tools(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        # Count capability-refresh requests as activity too — some clients
        # keep the session alive by polling tools/list without ever
        # dispatching a tool call. Reaping those as idle would be a false
        # positive.
        if idle_tracker is not None:
            idle_tracker.touch()
        return types.ListToolsResult(tools=[
            types.Tool(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
            )
            for definition in tool_registry
        ])

    def _annotate(result_json: str) -> str:
        """Attach the index_staleness footer when the index is old.

        Skipped for get_hub_status (it *is* the staleness report) by virtue
        of that branch returning before this is called, and a no-op when no
        index_store was provided. Best-effort: annotate_response never
        raises.
        """
        if index_store is None:
            return result_json
        return annotate_response(result_json, index_store)

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        # Mark the call in-flight so the idle watchdog treats the whole
        # dispatch (including slow first-call lazy loads in
        # EmbeddingService / the cross-encoder) as active. `begin()`
        # also touches the clock; `end()` resets it again at completion
        # so the idle window starts from "request finished", not
        # "request dispatched".
        if idle_tracker is not None:
            idle_tracker.begin()
        try:
            name = params.name
            args = params.arguments or {}

            # get_hub_status has a different dispatch signature (needs index_store)
            if name == "get_hub_status" and index_store is not None:
                status = reranker_status_provider() if reranker_status_provider else None
                result_json = await handle_get_hub_status(args, index_store, status)
                return types.CallToolResult(content=[types.TextContent(type="text", text=result_json)])

            # check_deprecation dispatches via retriever.deprecation_map, with the
            # indexed framework version as the default for version-relative status.
            if name == "check_deprecation":
                dep_map = getattr(retriever, "deprecation_map", None)
                fw_version = resolve_framework_version(index_store, dep_map)
                result_json = await handle_check_deprecation(args, dep_map, fw_version)
                return types.CallToolResult(content=[types.TextContent(type="text", text=_annotate(result_json))])

            handler = get_tool_handler(name)
            if handler is None:
                raise ValueError(f"Unknown tool: {name}")

            result_json = await handler(args, retriever)
            return types.CallToolResult(content=[types.TextContent(type="text", text=_annotate(result_json))])
        except ValidationError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))],
                is_error=True,
            )
        finally:
            if idle_tracker is not None:
                idle_tracker.end()

    if idle_tracker is not None:
        _tracker = idle_tracker

        async def ping_with_idle_touch(_ctx: Any, _params: types.RequestParams | None) -> types.EmptyResult:
            _tracker.touch()
            return types.EmptyResult()

        return Server(
            name="pipecat-context-hub",
            version=_SERVER_VERSION,
            instructions=_SERVER_INSTRUCTIONS,
            on_list_tools=list_tools,
            on_call_tool=call_tool,
            on_ping=ping_with_idle_touch,
        )

    return Server(
        name="pipecat-context-hub",
        version=_SERVER_VERSION,
        instructions=_SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
