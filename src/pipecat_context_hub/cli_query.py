"""Query subcommands — the MCP tools as one-shot shell commands.

The MCP server (``serve``) is the warm, session-long front door to the local
index. These subcommands are the second front door: the same index, the same
retrieval stack, and the same tool handlers, invoked once per process with the
handler's JSON printed to stdout. They exist so a coding agent (or a script,
or CI) can query the hub with nothing configured but a shell::

    pipecat-context-hub check-deprecation PipelineTask
    pipecat-context-hub search-api "WebSocketTransport + BaseTransport"

Design notes:

- Output contract: stdout carries exactly the tool handler's JSON — the same
  payload an MCP client receives — so callers can pipe it to a parser. Logs
  and error messages go to stderr. Exit codes: 0 on success, 1 on invalid
  input, ``_EXIT_INDEX_UNREADY`` (2) when the local index is missing, empty,
  or unreadable.
- Quiet by default: query commands downgrade logging to WARNING (explicit
  ``--log-level`` wins) and silence third-party model-loading chatter, since
  the captured stderr of a one-shot command lands in an agent's context.
  ``HF_HUB_OFFLINE=1`` additionally skips huggingface_hub's per-load network
  revalidation of already-cached models (see ``_quiet_model_loading``).
- The embedding model (and cross-encoder reranker, when enabled and cached)
  load only for the semantic commands (``search-docs``, ``search-examples``,
  ``search-api``, ``get-code-snippet``). The lookup commands
  (``check-deprecation``, ``get-doc``, ``get-example``, ``status``) skip
  them, keeping the hottest agent path — deprecation checks — cheap.
- Heavy imports are deferred into the runtime helper so ``--help`` stays
  instant, mirroring how ``serve``/``refresh`` defer their imports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import click

from pipecat_context_hub.shared.model_loading import quiet_model_loading
from pipecat_context_hub.shared.paths import redact_home_in_text

if TYPE_CHECKING:
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker
    from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever
    from pipecat_context_hub.shared.config import HubConfig
    from pipecat_context_hub.shared.types import RerankerStatus

# Exit code for a missing/empty/unreadable local index. Keep in sync with
# ``cli._EXIT_INDEX_UNREADY`` (enforced by tests/unit/test_cli_query.py).
_EXIT_INDEX_UNREADY = 2
_EXIT_BAD_INPUT = 1

# MCP tool name -> CLI command name. The drift guard in
# tests/unit/test_cli_query.py asserts this covers every registered MCP tool,
# so adding a tool to server/main.py without a CLI command fails the suite.
_TOOL_TO_COMMAND = {
    "search_docs": "search-docs",
    "get_doc": "get-doc",
    "search_examples": "search-examples",
    "get_example": "get-example",
    "get_code_snippet": "get-code-snippet",
    "search_api": "search-api",
    "check_deprecation": "check-deprecation",
    "get_hub_status": "status",
}


@dataclass
class _QueryRuntime:
    """Everything a one-shot tool dispatch needs, built by ``_query_runtime``."""

    retriever: HybridRetriever
    index_store: IndexStore
    reranker_status: RerankerStatus


def _quiet_query_logging(ctx: click.Context) -> None:
    """Default one-shot query commands to WARNING-level logging.

    Stdout is the data; stderr should carry only problems. The INFO chatter
    that's useful in a long-lived ``serve`` (index init, model loads, httpx
    requests) is pure context noise for an agent capturing a one-shot
    command's output. An explicit ``--log-level`` always wins — only the
    click *default* is downgraded.
    """
    parent = ctx.parent
    if parent is None:
        return
    if parent.get_parameter_source("log_level") == click.core.ParameterSource.DEFAULT:
        logging.getLogger().setLevel(logging.WARNING)


# Quiet/offline model-loading env defaults — shared with `serve`, which pays
# the same HF revalidation cost at boot. Kept under the local name so the
# `_invoke` call site reads as part of this module's quieting story.
_quiet_model_loading = quiet_model_loading


def _resolve_reranker(
    config: HubConfig, *, construct: bool
) -> tuple[CrossEncoderReranker | None, RerankerStatus]:
    """Resolve the cross-encoder reranker the way ``serve`` does at boot.

    With ``construct=False`` this only *probes* (config + HF cache check) and
    reports the status a query run would see, without paying the model load —
    used by ``status``, which must stay cheap.
    """
    from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker
    from pipecat_context_hub.shared.config import _RERANKER_MODEL_ENV
    from pipecat_context_hub.shared.types import RerankerStatus

    active_model = config.reranker.effective_model
    requested = os.environ.get(_RERANKER_MODEL_ENV, "").strip() or (
        config.reranker.cross_encoder_model
    )
    if not config.reranker.effective_enabled:
        return None, RerankerStatus(
            enabled=False, configured_model=requested, disabled_reason="config_disabled"
        )
    if not CrossEncoderReranker.is_model_cached(active_model):
        return None, RerankerStatus(
            enabled=False, configured_model=requested, disabled_reason="not_cached"
        )
    status = RerankerStatus(enabled=True, model=active_model, configured_model=requested)
    if not construct:
        return None, status
    cross_encoder = CrossEncoderReranker(
        model_name=active_model, top_n=config.reranker.top_n, enabled=True
    )
    return cross_encoder, status


@contextlib.contextmanager
def _query_runtime(config: HubConfig, *, needs_embeddings: bool) -> Iterator[_QueryRuntime]:
    """Open the index and build a retriever for a single tool dispatch.

    Mirrors the ``serve`` startup wiring (index store -> embedding ->
    reranker -> retriever -> deprecation map) minus the prewarm — for a
    one-shot process the query *is* the first query, so prewarming buys
    nothing. Failure modes exit with an actionable message on stderr rather
    than a traceback, because the primary caller is a coding agent that will
    read stderr and run the suggested command.
    """
    from pipecat_context_hub.services.index import IncompatibleIndexFormatError
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever

    try:
        index_store = IndexStore(config.storage)
        stats = index_store.get_index_stats()
    except IncompatibleIndexFormatError as exc:
        # exc.__str__ embeds the absolute chroma_path, so redact the whole
        # composed message (not just a data_dir token, which is absent here).
        click.echo(redact_home_in_text(f"Error: {exc}"), err=True)
        raise SystemExit(_EXIT_INDEX_UNREADY) from exc
    except Exception as exc:
        # Both the data_dir token and the {exc} rendering (e.g. a
        # FileNotFoundError carrying .../chroma.sqlite3) can leak an absolute
        # path, so redact the full formatted string, not a single argument.
        click.echo(
            redact_home_in_text(
                f"Error: failed to open index at {config.storage.data_dir}: {exc}\n"
                "Run 'pipecat-context-hub refresh --force --reset-index' to rebuild."
            ),
            err=True,
        )
        raise SystemExit(_EXIT_INDEX_UNREADY) from exc

    try:
        if stats.get("total", 0) == 0:
            click.echo(
                redact_home_in_text(
                    f"Error: the index at {config.storage.data_dir} is empty.\n"
                    "Run 'pipecat-context-hub refresh' first to build it "
                    "(the first run downloads models and indexes sources — allow a few minutes)."
                ),
                err=True,
            )
            raise SystemExit(_EXIT_INDEX_UNREADY)

        embedding_svc = None
        cross_encoder = None
        if needs_embeddings:
            from pipecat_context_hub.services.embedding import EmbeddingService

            embedding_svc = EmbeddingService(config.embedding)
            cross_encoder, reranker_status = _resolve_reranker(config, construct=True)
        else:
            _, reranker_status = _resolve_reranker(config, construct=False)

        retriever = HybridRetriever(index_store, embedding_svc, cross_encoder=cross_encoder)

        from pipecat_context_hub.services.ingest.deprecation_map import DeprecationMap

        retriever.deprecation_map = DeprecationMap.load(
            config.storage.data_dir / "deprecation_map.json"
        )

        yield _QueryRuntime(
            retriever=retriever, index_store=index_store, reranker_status=reranker_status
        )
    finally:
        index_store.close()


def _dispatch(tool: str, args: dict[str, Any], runtime: _QueryRuntime) -> str:
    """Dispatch one tool call through the same handlers the MCP server uses.

    Mirrors ``server.main.create_server``'s ``call_tool`` dispatch (the two
    special signatures, then the uniform ``handler(args, retriever)`` map) so
    CLI and MCP results cannot diverge.
    """
    from pipecat_context_hub.server.tools.check_deprecation import handle_check_deprecation
    from pipecat_context_hub.server.tools.get_code_snippet import handle_get_code_snippet
    from pipecat_context_hub.server.tools.get_doc import handle_get_doc
    from pipecat_context_hub.server.tools.get_example import handle_get_example
    from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status
    from pipecat_context_hub.server.tools.search_api import handle_search_api
    from pipecat_context_hub.server.tools.search_docs import handle_search_docs
    from pipecat_context_hub.server.tools.search_examples import handle_search_examples

    if tool == "get_hub_status":
        return asyncio.run(
            handle_get_hub_status(args, runtime.index_store, runtime.reranker_status)
        )
    if tool == "check_deprecation":
        dep_map = getattr(runtime.retriever, "deprecation_map", None)
        return asyncio.run(handle_check_deprecation(args, dep_map))

    handler_map: dict[str, Any] = {
        "search_docs": handle_search_docs,
        "get_doc": handle_get_doc,
        "search_examples": handle_search_examples,
        "get_example": handle_get_example,
        "get_code_snippet": handle_get_code_snippet,
        "search_api": handle_search_api,
    }
    result: str = asyncio.run(handler_map[tool](args, runtime.retriever))
    return result


def _invoke(ctx: click.Context, tool: str, args: dict[str, Any], *, needs_embeddings: bool) -> None:
    """Run one tool call end to end: open runtime, dispatch, print JSON.

    Pydantic/ValueError validation failures (e.g. ``get-doc`` with neither
    ``--doc-id`` nor ``--path``) become a one-line stderr message and exit
    code 1 — not a traceback.
    """
    from pydantic import ValidationError

    _quiet_query_logging(ctx)
    _quiet_model_loading()

    config: HubConfig = ctx.obj["config"]
    # Drop None/empty values so handler-side pydantic models see absent
    # fields, exactly as an MCP client omitting them.
    cleaned = {k: v for k, v in args.items() if v is not None and v != () and v != []}
    with _query_runtime(config, needs_embeddings=needs_embeddings) as runtime:
        try:
            result = _dispatch(tool, cleaned, runtime)
        except (ValidationError, ValueError) as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(_EXIT_BAD_INPUT) from exc
    click.echo(result)


# ── Commands ─────────────────────────────────────────────────────────────────
# Option sets mirror the tool input models in shared/types.py; constraints that
# pydantic enforces (e.g. limit 1-50) are duplicated as click ranges for
# better error messages at the shell boundary.


@click.command("search-docs")
@click.argument("query")
@click.option("--area", default=None, help="Filter by docs area (path prefix).")
@click.option("--limit", default=10, show_default=True, type=click.IntRange(1, 50))
@click.pass_context
def search_docs_command(ctx: click.Context, query: str, area: str | None, limit: int) -> None:
    """Semantic search over the indexed Pipecat docs.

    For multiple concepts, join them with ' + ' (e.g. "TTS + STT") rather
    than stuffing one natural-language query.
    """
    _invoke(
        ctx,
        "search_docs",
        {"query": query, "area": area, "limit": limit},
        needs_embeddings=True,
    )


@click.command("get-doc")
@click.option("--doc-id", default=None, help="Chunk ID from a search-docs hit.")
@click.option("--path", default=None, help="Docs path, e.g. '/guides/telephony/overview'.")
@click.option("--section", default=None, help="Return only this section heading.")
@click.pass_context
def get_doc_command(
    ctx: click.Context, doc_id: str | None, path: str | None, section: str | None
) -> None:
    """Fetch a full docs page (or one section) by ID or path."""
    _invoke(
        ctx,
        "get_doc",
        {"doc_id": doc_id, "path": path, "section": section},
        needs_embeddings=False,
    )


@click.command("search-examples")
@click.argument("query")
@click.option("--repo", default=None, help="Filter by repo slug.")
@click.option("--language", default=None, help="e.g. 'python' or 'typescript'.")
@click.option("--domain", default=None, help="e.g. 'backend' or 'frontend'.")
@click.option("--tag", "tags", multiple=True, help="Capability tag (repeatable).")
@click.option("--foundational-class", default=None, help="Filter by foundational class.")
@click.option("--execution-mode", default=None)
@click.option("--pipecat-version", default=None, help="Score results for this pipecat-ai version.")
@click.option(
    "--compatible-only",
    is_flag=True,
    help="Exclude results requiring a newer version (needs --pipecat-version).",
)
@click.option("--limit", default=10, show_default=True, type=click.IntRange(1, 50))
@click.pass_context
def search_examples_command(
    ctx: click.Context,
    query: str,
    repo: str | None,
    language: str | None,
    domain: str | None,
    tags: tuple[str, ...],
    foundational_class: str | None,
    execution_mode: str | None,
    pipecat_version: str | None,
    compatible_only: bool,
    limit: int,
) -> None:
    """Find working Pipecat examples for a capability.

    For multiple concepts, join them with ' + '
    (e.g. "idle timeout + function calling").
    """
    _invoke(
        ctx,
        "search_examples",
        {
            "query": query,
            "repo": repo,
            "language": language,
            "domain": domain,
            "tags": list(tags) or None,
            "foundational_class": foundational_class,
            "execution_mode": execution_mode,
            "pipecat_version": pipecat_version,
            "version_filter": "compatible_only" if compatible_only else None,
            "limit": limit,
        },
        needs_embeddings=True,
    )


@click.command("get-example")
@click.argument("example_id")
@click.option("--no-readme", is_flag=True, help="Skip the example's README content.")
@click.pass_context
def get_example_command(ctx: click.Context, example_id: str, no_readme: bool) -> None:
    """Fetch the full source files of an example (use after search-examples)."""
    _invoke(
        ctx,
        "get_example",
        {"example_id": example_id, "include_readme": not no_readme},
        needs_embeddings=False,
    )


@click.command("get-code-snippet")
@click.option("--symbol", default=None, help="Symbol name, e.g. 'DailyTransport.send_dtmf'.")
@click.option("--intent", default=None, help="Intent description (searches example code).")
@click.option("--path", default=None, help="File path (with --line-start for range lookup).")
@click.option("--line-start", default=None, type=int)
@click.option("--line-end", default=None, type=int)
@click.option("--module", default=None, help="Module path prefix filter (symbol mode).")
@click.option("--class-name", default=None, help="Class name prefix filter (symbol mode).")
@click.option(
    "--content-type",
    default=None,
    type=click.Choice(["code", "source"]),
    help="'source' = framework code, 'code' = examples.",
)
@click.option("--pipecat-version", default=None, help="Score results for this pipecat-ai version.")
@click.option("--max-lines", default=100, show_default=True, type=click.IntRange(1, 500))
@click.pass_context
def get_code_snippet_command(
    ctx: click.Context,
    symbol: str | None,
    intent: str | None,
    path: str | None,
    line_start: int | None,
    line_end: int | None,
    module: str | None,
    class_name: str | None,
    content_type: str | None,
    pipecat_version: str | None,
    max_lines: int,
) -> None:
    """Get a targeted code snippet by symbol, intent, or path + line range."""
    _invoke(
        ctx,
        "get_code_snippet",
        {
            "symbol": symbol,
            "intent": intent,
            "path": path,
            "line_start": line_start,
            "line_end": line_end,
            "module": module,
            "class_name": class_name,
            "content_type": content_type,
            "pipecat_version": pipecat_version,
            "max_lines": max_lines,
        },
        needs_embeddings=True,
    )


@click.command("search-api")
@click.argument("query")
@click.option("--module", default=None, help="Module path prefix, e.g. 'pipecat.services'.")
@click.option("--class-name", default=None, help="Class name prefix, e.g. 'DailyTransport'.")
@click.option(
    "--chunk-type",
    default=None,
    type=click.Choice(
        ["module_overview", "class_overview", "method", "function", "type_definition"]
    ),
)
@click.option("--is-dataclass", is_flag=True, help="Only dataclass types.")
@click.option("--yields", default=None, help="Methods yielding a frame type.")
@click.option("--calls", default=None, help="Methods calling a specific method.")
@click.option("--pipecat-version", default=None, help="Score results for this pipecat-ai version.")
@click.option(
    "--compatible-only",
    is_flag=True,
    help="Exclude results requiring a newer version (needs --pipecat-version).",
)
@click.option("--limit", default=10, show_default=True, type=click.IntRange(1, 50))
@click.pass_context
def search_api_command(
    ctx: click.Context,
    query: str,
    module: str | None,
    class_name: str | None,
    chunk_type: str | None,
    is_dataclass: bool,
    yields: str | None,
    calls: str | None,
    pipecat_version: str | None,
    compatible_only: bool,
    limit: int,
) -> None:
    """Search framework internals: classes, signatures, frames, inheritance.

    For multiple concepts, join them with ' + '
    (e.g. "BaseTransport + WebSocketTransport").
    """
    _invoke(
        ctx,
        "search_api",
        {
            "query": query,
            "module": module,
            "class_name": class_name,
            "chunk_type": chunk_type,
            "is_dataclass": is_dataclass or None,
            "yields": yields,
            "calls": calls,
            "pipecat_version": pipecat_version,
            "version_filter": "compatible_only" if compatible_only else None,
            "limit": limit,
        },
        needs_embeddings=True,
    )


@click.command("check-deprecation")
@click.argument("symbol")
@click.pass_context
def check_deprecation_command(ctx: click.Context, symbol: str) -> None:
    """Check whether a module path, class, or method is deprecated.

    The cheapest and most important call: run it on any Pipecat API you are
    about to write from memory (e.g. 'PipelineTask').
    """
    _invoke(ctx, "check_deprecation", {"symbol": symbol}, needs_embeddings=False)


@click.command("status")
@click.pass_context
def status_command(ctx: click.Context) -> None:
    """Index health: freshness, record counts, reranker state.

    Check ``last_refresh_at`` — if it is stale (or predates a pipecat-ai
    upgrade), run 'pipecat-context-hub refresh'.
    """
    _invoke(ctx, "get_hub_status", {}, needs_embeddings=False)


_COMMANDS: tuple[click.Command, ...] = (
    search_docs_command,
    get_doc_command,
    search_examples_command,
    get_example_command,
    get_code_snippet_command,
    search_api_command,
    check_deprecation_command,
    status_command,
)


def register_query_commands(group: click.Group) -> None:
    """Attach the query subcommands to the main CLI group."""
    for command in _COMMANDS:
        group.add_command(command)
