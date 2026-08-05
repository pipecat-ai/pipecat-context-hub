"""MCP server entry point — tool registration and request dispatch."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server

from pipecat_context_hub.server.tools.check_deprecation import (
    handle_check_deprecation,
    resolve_framework_version,
)
from pipecat_context_hub.server.tools.get_code_snippet import handle_get_code_snippet
from pipecat_context_hub.server.tools.get_doc import handle_get_doc
from pipecat_context_hub.server.tools.get_example import handle_get_example
from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status
from pipecat_context_hub.server.tools.search_api import handle_search_api
from pipecat_context_hub.server.tools.search_docs import handle_search_docs
from pipecat_context_hub.server.tools.search_examples import handle_search_examples
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.staleness import annotate_response
from pipecat_context_hub.shared.support_links import (
    BUG_REPORT_ISSUE_URL,
    RETRIEVAL_QUALITY_ISSUE_URL,
)
from pipecat_context_hub.shared.tracking import IdleTracker
from pipecat_context_hub.shared.types import (
    CheckDeprecationInput,
    GetCodeSnippetInput,
    GetDocInput,
    GetExampleInput,
    GetHubStatusInput,
    RerankerStatus,
    SearchApiInput,
    SearchDocsInput,
    SearchExamplesInput,
)

logger = logging.getLogger(__name__)

# Kept in sync with pyproject.toml [project].version by
# tests/unit/test_server.py::TestVersionConsistency. If this is ever replaced
# with a runtime lookup, the PyPI distribution name is "pipecat-ai-context-hub"
# (not "pipecat-context-hub", which is only the command / server name) —
# importlib.metadata.version() must use the former.
_SERVER_VERSION = "0.5.0"

# Tool name → (description, input schema, handler)
_BASE_TOOLS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "search_docs",
        "Search Pipecat documentation for conceptual questions, guides, configuration, and API "
        "references. Use for 'how do I...?' questions. Returns ranked doc hits with evidence. "
        "Use `area` to narrow by docs path prefix (e.g. 'guides', 'server/services'). "
        "For multiple topics, use ` + ` or ` & ` delimiters (e.g. 'TTS + STT').",
        SearchDocsInput.model_json_schema(),
    ),
    (
        "get_doc",
        "Retrieve a specific Pipecat documentation page by chunk ID or path. "
        "Use `doc_id` (from a search_docs result) or `path` (e.g. '/guides/learn/transports') for direct lookup. "
        "Use `section` to extract a specific heading; falls back to full document if not found.",
        GetDocInput.model_json_schema(),
    ),
    (
        "search_examples",
        "Find working Pipecat code examples by task, modality, or component. "
        "Use when the user needs runnable code patterns. "
        "Filter by `repo`, `tags` (capability tags), `foundational_class`, `language`, `domain` "
        "(backend/frontend/config/infra), or `execution_mode`. "
        "Pass `pipecat_version` (e.g. '0.0.95') to score results for compatibility "
        "and annotate with `version_compatibility`. Use `version_filter='compatible_only'` "
        "to exclude results requiring a newer version. "
        "For multiple topics, use ` + ` or ` & ` delimiters (e.g. 'idle timeout + function calling').",
        SearchExamplesInput.model_json_schema(),
    ),
    (
        "get_example",
        "Retrieve full source files for a specific Pipecat example. "
        "Use after search_examples to get complete runnable code.",
        GetExampleInput.model_json_schema(),
    ),
    (
        "get_code_snippet",
        "Get a targeted code snippet by symbol name, intent, or file path + line range. "
        "Symbol lookups search framework source (class/method definitions); "
        "intent lookups search example code. "
        "Use `module` to scope symbol lookups (e.g. module='pipecat.runner.daily' with symbol='configure'). "
        "Use `class_name` to scope to a specific class (prefix match, e.g. 'DailyTransport' matches DailyTransportClient). "
        "Use `content_type='source'` with intent to search framework code instead of examples. "
        "Pass `pipecat_version` (e.g. '0.0.95') to score results for compatibility. "
        "For multiple topics, use ` + ` or ` & ` delimiters.",
        GetCodeSnippetInput.model_json_schema(),
    ),
    (
        "search_api",
        "Search Pipecat framework internals — class definitions, method signatures, constructors, "
        "base classes, and frame types. Use when you need implementation details, type information, "
        "or inheritance hierarchies. "
        "Filter by `module` (path prefix, e.g. 'pipecat.services'), `class_name` (prefix match, e.g. 'DailyTransport' matches DailyTransportClient), "
        "`chunk_type` ('module_overview', 'class_overview', 'method', 'function', 'type_definition'), or `is_dataclass`. "
        "Pass `pipecat_version` (e.g. '0.0.95') to score results for compatibility. "
        "Use `version_filter='compatible_only'` to exclude results requiring a newer version. "
        "For multiple topics, use ` + ` or ` & ` delimiters (e.g. 'BaseTransport + WebSocketTransport').",
        SearchApiInput.model_json_schema(),
    ),
    (
        "check_deprecation",
        "Check if a pipecat module path, class, or import is deprecated. "
        "Use when you see pipecat imports to verify they are current. "
        "Returns replacement path if deprecated. "
        "E.g., check_deprecation(symbol='pipecat.services.grok.llm') → deprecated, use pipecat.services.xai.llm.",
        CheckDeprecationInput.model_json_schema(),
    ),
]

_HUB_STATUS_TOOL: tuple[str, str, dict[str, Any]] = (
    "get_hub_status",
    "Get index health: last refresh time, record counts by type, indexed pipecat version, "
    "and commit SHAs. Use to check if the index is fresh before answering questions.",
    GetHubStatusInput.model_json_schema(),
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
connection reports: after it completes, the user must restart or reconnect \
this MCP server, then re-run ``get_hub_status`` on the new connection to \
confirm the fix — re-checking ``get_hub_status`` on the current connection \
will still show ``not_cached`` even after a successful ``refresh``. If \
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
    # Build the tool list — only include get_hub_status when store is available
    tool_registry = list(_BASE_TOOLS)
    if index_store is not None:
        tool_registry.append(_HUB_STATUS_TOOL)

    server = Server(
        name="pipecat-context-hub",
        version=_SERVER_VERSION,
        instructions=_SERVER_INSTRUCTIONS,
    )

    # MCP's low-level Server routes `ping` requests via its built-in
    # handler (`types.PingRequest -> _ping_handler`), bypassing our
    # list/call decorators. Clients that keep an otherwise idle
    # session alive via periodic pings would otherwise still be
    # reaped by the idle watchdog after `idle_timeout_secs`. Wrap
    # the built-in ping handler so it counts as activity.
    if idle_tracker is not None:
        _builtin_ping = server.request_handlers.get(types.PingRequest)
        if _builtin_ping is not None:
            # Bind a local so the closure captures a non-Optional
            # reference (avoids mypy narrowing issues and a bandit
            # B101 `assert`).
            _tracker = idle_tracker

            async def _ping_with_idle_touch(request: types.PingRequest) -> types.ServerResult:
                _tracker.touch()
                return await _builtin_ping(request)

            server.request_handlers[types.PingRequest] = _ping_with_idle_touch

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        # Count capability-refresh requests as activity too — some clients
        # keep the session alive by polling tools/list without ever
        # dispatching a tool call. Reaping those as idle would be a false
        # positive.
        if idle_tracker is not None:
            idle_tracker.touch()
        return [
            types.Tool(
                name=name,
                description=description,
                inputSchema=schema,
            )
            for name, description, schema in tool_registry
        ]

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

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        # Mark the call in-flight so the idle watchdog treats the whole
        # dispatch (including slow first-call lazy loads in
        # EmbeddingService / the cross-encoder) as active. `begin()`
        # also touches the clock; `end()` resets it again at completion
        # so the idle window starts from "request finished", not
        # "request dispatched".
        if idle_tracker is not None:
            idle_tracker.begin()
        try:
            args = arguments or {}

            # get_hub_status has a different dispatch signature (needs index_store)
            if name == "get_hub_status" and index_store is not None:
                status = reranker_status_provider() if reranker_status_provider else None
                result_json = await handle_get_hub_status(args, index_store, status)
                return [types.TextContent(type="text", text=result_json)]

            # check_deprecation dispatches via retriever.deprecation_map, with the
            # indexed framework version as the default for version-relative status.
            if name == "check_deprecation":
                dep_map = getattr(retriever, "deprecation_map", None)
                fw_version = resolve_framework_version(index_store)
                result_json = await handle_check_deprecation(args, dep_map, fw_version)
                return [types.TextContent(type="text", text=_annotate(result_json))]

            handler_map: dict[str, Any] = {
                "search_docs": handle_search_docs,
                "get_doc": handle_get_doc,
                "search_examples": handle_search_examples,
                "get_example": handle_get_example,
                "get_code_snippet": handle_get_code_snippet,
                "search_api": handle_search_api,
            }
            handler = handler_map.get(name)
            if handler is None:
                raise ValueError(f"Unknown tool: {name}")

            result_json = await handler(args, retriever)
            return [types.TextContent(type="text", text=_annotate(result_json))]
        finally:
            if idle_tracker is not None:
                idle_tracker.end()

    return server
