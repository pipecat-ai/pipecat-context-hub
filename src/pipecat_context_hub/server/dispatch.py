"""Shared typed tool registry used by the MCP and CLI front doors."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from typing import Any

from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.types import (
    CheckDeprecationInput,
    GetCodeSnippetInput,
    GetDocInput,
    GetExampleInput,
    GetHubStatusInput,
    SearchApiInput,
    SearchDocsInput,
    SearchExamplesInput,
)

ToolHandler = Callable[[dict[str, Any], Retriever], Coroutine[Any, Any, str]]


@dataclass(frozen=True)
class ToolDefinition:
    """The parts of a tool that are shared by every front door."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


async def _handle_check_deprecation_registry(
    arguments: dict[str, Any], retriever: Retriever
) -> str:
    """Registry adapter for the deprecation handler's front-door context.

    MCP and CLI add their indexed-version resolution around this handler, but
    the registry still exposes a callable for parity and discovery.
    """
    from pipecat_context_hub.server.tools import check_deprecation

    dep_map = getattr(retriever, "deprecation_map", None)
    return await check_deprecation.handle_check_deprecation(arguments, dep_map)


async def _handle_search_docs(arguments: dict[str, Any], retriever: Retriever) -> str:
    from pipecat_context_hub.server.tools import search_docs

    return await search_docs.handle_search_docs(arguments, retriever)


async def _handle_get_doc(arguments: dict[str, Any], retriever: Retriever) -> str:
    from pipecat_context_hub.server.tools import get_doc

    return await get_doc.handle_get_doc(arguments, retriever)


async def _handle_search_examples(arguments: dict[str, Any], retriever: Retriever) -> str:
    from pipecat_context_hub.server.tools import search_examples

    return await search_examples.handle_search_examples(arguments, retriever)


async def _handle_get_example(arguments: dict[str, Any], retriever: Retriever) -> str:
    from pipecat_context_hub.server.tools import get_example

    return await get_example.handle_get_example(arguments, retriever)


async def _handle_get_code_snippet(arguments: dict[str, Any], retriever: Retriever) -> str:
    from pipecat_context_hub.server.tools import get_code_snippet

    return await get_code_snippet.handle_get_code_snippet(arguments, retriever)


async def _handle_search_api(arguments: dict[str, Any], retriever: Retriever) -> str:
    from pipecat_context_hub.server.tools import search_api

    return await search_api.handle_search_api(arguments, retriever)


async def _handle_hub_status_front_door_only(
    _arguments: dict[str, Any], _retriever: Retriever
) -> str:
    raise RuntimeError("get_hub_status requires an index store")


_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        "search_docs",
        "Search Pipecat documentation for conceptual questions, guides, configuration, and API "
        "references. Use for 'how do I...?' questions. Returns ranked doc hits with evidence. "
        "Use `area` to narrow by docs path prefix (e.g. 'guides', 'server/services'). "
        "For multiple topics, use ` + ` or ` & ` delimiters (e.g. 'TTS + STT').",
        SearchDocsInput.model_json_schema(),
        _handle_search_docs,
    ),
    ToolDefinition(
        "get_doc",
        "Retrieve a specific Pipecat documentation page by chunk ID or path. "
        "Use `doc_id` (from a search_docs result) or `path` (e.g. '/guides/learn/transports') for direct lookup. "
        "Use `section` to extract a specific heading; falls back to full document if not found.",
        GetDocInput.model_json_schema(),
        _handle_get_doc,
    ),
    ToolDefinition(
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
        _handle_search_examples,
    ),
    ToolDefinition(
        "get_example",
        "Retrieve full source files for a specific Pipecat example. "
        "Use after search_examples to get complete runnable code.",
        GetExampleInput.model_json_schema(),
        _handle_get_example,
    ),
    ToolDefinition(
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
        _handle_get_code_snippet,
    ),
    ToolDefinition(
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
        _handle_search_api,
    ),
    ToolDefinition(
        "check_deprecation",
        "Check if a pipecat module path, class, or import is deprecated. "
        "Use when you see pipecat imports to verify they are current. "
        "Returns replacement path if deprecated. "
        "E.g., check_deprecation(symbol='pipecat.services.grok.llm') → deprecated, use pipecat.services.xai.llm.",
        CheckDeprecationInput.model_json_schema(),
        _handle_check_deprecation_registry,
    ),
)

HUB_STATUS_TOOL = ToolDefinition(
    "get_hub_status",
    "Get index health: last refresh time, record counts by type, indexed pipecat version, "
    "and commit SHAs. Use to check if the index is fresh before answering questions.",
    GetHubStatusInput.model_json_schema(),
    _handle_hub_status_front_door_only,
)


def iter_tool_definitions(*, include_hub_status: bool = False) -> Iterator[ToolDefinition]:
    yield from _TOOL_DEFINITIONS
    if include_hub_status:
        yield HUB_STATUS_TOOL


def get_tool_handler(name: str) -> ToolHandler | None:
    return next(
        (definition.handler for definition in _TOOL_DEFINITIONS if definition.name == name), None
    )


# Compatibility views for callers/tests that inspect the registration surface.
BASE_TOOLS = [(d.name, d.description, d.input_schema) for d in _TOOL_DEFINITIONS]
HUB_STATUS_TOOL_TUPLE = (
    HUB_STATUS_TOOL.name,
    HUB_STATUS_TOOL.description,
    HUB_STATUS_TOOL.input_schema,
)
TOOL_REGISTRY = _TOOL_DEFINITIONS
