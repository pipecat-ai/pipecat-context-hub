"""Unit tests for the one-shot CLI query subcommands (cli_query.py)."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from pipecat_context_hub.cli import _EXIT_INDEX_UNREADY as _SERVE_EXIT_INDEX_UNREADY
from pipecat_context_hub.cli import main
from pipecat_context_hub.cli_query import _EXIT_BAD_INPUT, _EXIT_INDEX_UNREADY, _TOOL_TO_COMMAND
from pipecat_context_hub.server.main import _BASE_TOOLS, _HUB_STATUS_TOOL
from pipecat_context_hub.services.index import IncompatibleIndexFormatError
from pipecat_context_hub.services.index.errors import RESET_INDEX_REMEDIATION
from pipecat_context_hub.shared.paths import redact_home_in_text as _real_redact_home_in_text
from pipecat_context_hub.shared.support_links import (
    BUG_REPORT_ISSUE_URL,
    RETRIEVAL_QUALITY_ISSUE_URL,
)
from pipecat_context_hub.shared.types import EvidenceReport, SearchDocsOutput

runner = CliRunner()


def _assert_remediation_before_report_hint(stderr: str, remediation_substr: str, url: str) -> None:
    """Pin the 'name the fix before the URL' ordering, not just presence."""
    assert remediation_substr in stderr
    assert url in stderr
    assert stderr.index(remediation_substr) < stderr.index(url)


def _assert_url_is_not_message_leading(stderr: str, url: str) -> None:
    """Weaker, wording-agnostic version of the above: some remediation text
    (of whatever exact phrasing the implementation chose) must precede the
    URL on stderr — the hint must not open with "file a bug", i.e. it must
    not be escalation-first."""
    assert url in stderr
    prefix = stderr[: stderr.index(url)]
    assert prefix.strip() != "", "report-hint URL has no remediation text before it"


def _index_store_mock(total: int) -> MagicMock:
    """A stand-in IndexStore with the methods the query runtime touches."""
    store = MagicMock()
    store.get_index_stats.return_value = {
        "total": total,
        "counts_by_type": {"doc": total, "code": 0, "source": 0},
    }
    store.get_all_metadata.return_value = {
        "last_refresh_at": "2026-06-12T00:00:00+00:00",
        "last_refresh_duration_seconds": "85.8",
    }
    return store


class TestToolCommandParity:
    """Drift guards: the CLI must track the MCP tool surface exactly."""

    def test_every_mcp_tool_has_a_cli_command(self):
        """Adding a tool to server/main.py without a CLI command fails here."""
        tool_names = {name for name, _, _ in _BASE_TOOLS} | {_HUB_STATUS_TOOL[0]}
        assert tool_names == set(_TOOL_TO_COMMAND)

    def test_mapped_commands_are_registered_on_main(self):
        assert set(_TOOL_TO_COMMAND.values()) <= set(main.commands)

    def test_index_unready_exit_code_matches_serve(self):
        """CLI and serve must report 'index unready' with the same exit code."""
        assert _EXIT_INDEX_UNREADY == _SERVE_EXIT_INDEX_UNREADY

    def test_semantic_meta_matches_needs_embeddings_call_sites(self):
        """``_SEMANTIC_META`` must enroll exactly the tools invoked
        with ``needs_embeddings=True`` — nothing more, nothing less.

        The retrieval-quality hint (``_maybe_warn_poor_results``) is gated
        on ``_SEMANTIC_META`` membership, a separate, hand-maintained
        dict from the ``needs_embeddings`` flag that gates the reranker
        warning. A future semantic command that sets
        ``needs_embeddings=True`` but forgets to enroll in
        ``_SEMANTIC_META`` would silently lose the retrieval-quality
        hint with no other test catching it — parses the module's AST for
        every ``_invoke(ctx, "<tool>", ..., needs_embeddings=True)`` call
        site rather than hardcoding the expected tool list, so this stays
        accurate as commands are added or removed.
        """
        import ast
        import inspect

        import pipecat_context_hub.cli_query as cli_query_module
        from pipecat_context_hub.cli_query import _SEMANTIC_META

        source = inspect.getsource(cli_query_module)
        tree = ast.parse(source)

        embeddings_tools: set[str] = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "_invoke":
                continue
            needs_embeddings = next(
                (kw.value for kw in node.keywords if kw.arg == "needs_embeddings"), None
            )
            if not (isinstance(needs_embeddings, ast.Constant) and needs_embeddings.value is True):
                continue
            tool_arg = node.args[1] if len(node.args) > 1 else None
            assert isinstance(tool_arg, ast.Constant) and isinstance(tool_arg.value, str), (
                "expected a string literal as _invoke's tool argument"
            )
            embeddings_tools.add(tool_arg.value)

        assert embeddings_tools, "AST walk found no needs_embeddings=True call sites — broken test"
        assert embeddings_tools == set(_SEMANTIC_META)

    def test_semantic_result_key_values_match_output_model_fields(self):
        """``_SEMANTIC_META``'s *result_key* values must be real fields on
        the corresponding MCP output model.

        The AST test above only pins the dict's keys (tool names); nothing
        previously verified the values (``"hits"``/``"snippets"``) still
        match the actual Pydantic field names. A field rename on any output
        model would silently break empty-results detection — the lookup
        would always return ``None``, so the retrieval-quality hint would
        never fire for that command — with every existing test still green.
        """
        from pydantic import BaseModel

        from pipecat_context_hub.cli_query import _SEMANTIC_META
        from pipecat_context_hub.shared.types import (
            GetCodeSnippetOutput,
            SearchApiOutput,
            SearchDocsOutput,
            SearchExamplesOutput,
        )

        output_models: dict[str, type[BaseModel]] = {
            "search_docs": SearchDocsOutput,
            "search_examples": SearchExamplesOutput,
            "search_api": SearchApiOutput,
            "get_code_snippet": GetCodeSnippetOutput,
        }
        assert set(output_models) == set(_SEMANTIC_META)
        for tool, model in output_models.items():
            assert _SEMANTIC_META[tool].result_key in model.model_fields, (
                f"{tool}: {_SEMANTIC_META[tool].result_key!r} is not a field on {model.__name__}"
            )

    def test_semantic_meta_retry_hints_are_nonempty(self):
        """Every _SEMANTIC_META entry carries a non-empty retry hint —
        the NamedTuple structure guarantees the field exists, but not
        that someone didn't leave it blank."""
        from pipecat_context_hub.cli_query import _SEMANTIC_META

        for tool, meta in _SEMANTIC_META.items():
            assert meta.retry_hint.strip(), f"{tool}: empty retry_hint"


class TestVersionFlag:
    """`--version` reports the package version with zero state."""

    def test_version_flag_prints_server_version(self):
        """No index, no model load: the flag short-circuits before any of that."""
        from pipecat_context_hub.server.main import _SERVER_VERSION

        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0, result.stderr
        # Sourced from importlib.metadata; TestVersionConsistency guarantees it
        # matches _SERVER_VERSION, so a drift here flags a packaging problem.
        assert _SERVER_VERSION in result.stdout
        assert "pipecat-context-hub" in result.stdout


class TestIndexUnready:
    """Empty/unopenable/incompatible index exits 2 with an actionable hint.

    Each of the three paths already names its own remediation before the
    Phase-2 bug-report hint appended to it (empty-index: 'refresh';
    unopenable: '--reset-index'; incompatible-format:
    ``RESET_INDEX_REMEDIATION``, embedded verbatim in the exception message).
    Every test here also pins the co-fire negative: exactly one occurrence
    of the bug-report URL, and no retrieval-quality URL — these exits raise
    before either post-open warning point, so a naive check for
    ``BUG_REPORT_ISSUE_URL`` alone would be ambiguous.
    """

    def test_empty_index_exits_with_refresh_hint(self):
        with patch(
            "pipecat_context_hub.services.index.store.IndexStore",
            return_value=_index_store_mock(total=0),
        ):
            result = runner.invoke(main, ["check-deprecation", "PipelineTask"])
        assert result.exit_code == _EXIT_INDEX_UNREADY
        assert result.stdout == ""
        _assert_remediation_before_report_hint(result.stderr, "refresh", BUG_REPORT_ISSUE_URL)
        assert result.stderr.count(BUG_REPORT_ISSUE_URL) == 1
        assert RETRIEVAL_QUALITY_ISSUE_URL not in result.stderr

    def test_unopenable_index_exits_with_reset_hint(self):
        with patch(
            "pipecat_context_hub.services.index.store.IndexStore",
            side_effect=RuntimeError("corrupt"),
        ):
            result = runner.invoke(main, ["status"])
        assert result.exit_code == _EXIT_INDEX_UNREADY
        assert result.stdout == ""
        _assert_remediation_before_report_hint(result.stderr, "--reset-index", BUG_REPORT_ISSUE_URL)
        assert result.stderr.count(BUG_REPORT_ISSUE_URL) == 1
        assert RETRIEVAL_QUALITY_ISSUE_URL not in result.stderr

    def test_incompatible_index_format_exits_with_bug_report_hint(self, monkeypatch, tmp_path):
        """The previously-untested ``IncompatibleIndexFormatError`` path
        (``cli_query.py:150-157``) — its message embeds the absolute
        ``chroma_path``, so redaction must still hold with the hint appended.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        data_dir = tmp_path / ".pipecat-context-hub"
        chroma_path = data_dir / "chroma"
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                side_effect=IncompatibleIndexFormatError(chroma_path),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(data_dir)}),
        ):
            result = runner.invoke(main, ["status"])
        assert result.exit_code == _EXIT_INDEX_UNREADY
        assert result.stdout == ""
        _assert_remediation_before_report_hint(
            result.stderr, RESET_INDEX_REMEDIATION, BUG_REPORT_ISSUE_URL
        )
        assert result.stderr.count(BUG_REPORT_ISSUE_URL) == 1
        assert RETRIEVAL_QUALITY_ISSUE_URL not in result.stderr
        assert str(chroma_path) not in result.stderr
        assert str(tmp_path) not in result.stderr


class TestDispatch:
    """Happy paths: stdout carries exactly the tool handler's JSON."""

    def test_check_deprecation_clean_symbol(self, tmp_path):
        """Lookup path: no embedding service is constructed."""
        store = _index_store_mock(total=10)
        with (
            patch("pipecat_context_hub.services.index.store.IndexStore", return_value=store),
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                side_effect=AssertionError("lookup commands must not load the embedding model"),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(main, ["check-deprecation", "NotDeprecatedThing"])
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["deprecated"] is False
        store.close.assert_called_once()

    def test_check_deprecation_at_version_forwards_version(self, tmp_path):
        """`--at-version` reaches the handler as the `version` arg; absent otherwise."""
        captured: dict[str, object] = {}

        async def _fake_handler(args, dep_map, fw_version=None):
            captured.clear()
            captured.update(args)
            return json.dumps({"deprecated": False, "status": "current"})

        def _run(extra_args):
            with (
                patch(
                    "pipecat_context_hub.services.index.store.IndexStore",
                    return_value=_index_store_mock(total=10),
                ),
                patch(
                    "pipecat_context_hub.server.tools.check_deprecation.handle_check_deprecation",
                    side_effect=_fake_handler,
                ),
                patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
            ):
                return runner.invoke(main, ["check-deprecation", "PipelineTask", *extra_args])

        result = _run(["--at-version", "2.0.0"])
        assert result.exit_code == 0, result.stderr
        assert captured == {"symbol": "PipelineTask", "version": "2.0.0"}

        result = _run([])
        assert result.exit_code == 0, result.stderr
        assert captured == {"symbol": "PipelineTask"}  # no version key when omitted

    def test_status_reports_index_health(self, tmp_path):
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=42),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(main, ["status"])
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["total_records"] == 42
        assert payload["last_refresh_at"] == "2026-06-12T00:00:00+00:00"

    def test_search_docs_uses_embeddings_and_prints_handler_json(self, tmp_path):
        """Semantic path: embedding service constructed, handler JSON on stdout."""
        retriever = MagicMock()
        retriever.search_docs = AsyncMock(
            return_value=SearchDocsOutput(evidence=EvidenceReport(confidence=1.0))
        )
        embedding = MagicMock()
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                return_value=embedding,
            ) as embedding_cls,
            patch(
                "pipecat_context_hub.services.retrieval.hybrid.HybridRetriever",
                return_value=retriever,
            ) as retriever_cls,
            patch(
                "pipecat_context_hub.services.retrieval.cross_encoder."
                "CrossEncoderReranker.is_model_cached",
                return_value=False,
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(main, ["search-docs", "TTS + STT", "--limit", "5"])
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["hits"] == []
        embedding_cls.assert_called_once()
        # The retriever was built with the constructed embedding service.
        assert retriever_cls.call_args.args[1] is embedding
        # The handler saw the parsed flags.
        sent = retriever.search_docs.call_args.args[0]
        assert sent.query == "TTS + STT"
        assert sent.limit == 5

    def test_get_doc_dispatches_handler_without_embeddings(self, tmp_path):
        """Lookup path: handle_get_doc invoked, no embedding service built."""
        handler = AsyncMock(return_value=json.dumps({"path": "/guides/x", "content": "hi"}))
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                side_effect=AssertionError("lookup commands must not load the embedding model"),
            ),
            patch(
                "pipecat_context_hub.server.tools.get_doc.handle_get_doc",
                handler,
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(main, ["get-doc", "--path", "/guides/x", "--section", "Intro"])
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["path"] == "/guides/x"
        handler.assert_called_once()
        sent = handler.call_args.args[0]
        # None/empty options are dropped before dispatch; --doc-id was absent.
        assert sent["path"] == "/guides/x"
        assert sent["section"] == "Intro"
        assert "doc_id" not in sent

    def test_get_example_dispatches_handler_and_inverts_no_readme(self, tmp_path):
        """Lookup path: --no-readme maps to include_readme=False; no embeddings."""
        handler = AsyncMock(return_value=json.dumps({"example_id": "ex1", "files": []}))
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                side_effect=AssertionError("lookup commands must not load the embedding model"),
            ),
            patch(
                "pipecat_context_hub.server.tools.get_example.handle_get_example",
                handler,
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(main, ["get-example", "ex1", "--no-readme"])
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["example_id"] == "ex1"
        handler.assert_called_once()
        sent = handler.call_args.args[0]
        assert sent["example_id"] == "ex1"
        # --no-readme -> include_readme = not no_readme. Pin the inversion.
        assert sent["include_readme"] is False

    def test_search_examples_uses_embeddings_and_prints_handler_json(self, tmp_path):
        """Semantic path: handle_search_examples invoked, embeddings built."""
        handler = AsyncMock(return_value=json.dumps({"hits": [], "domain": "backend"}))
        embedding = MagicMock()
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                return_value=embedding,
            ) as embedding_cls,
            patch(
                "pipecat_context_hub.services.retrieval.cross_encoder."
                "CrossEncoderReranker.is_model_cached",
                return_value=False,
            ),
            patch(
                "pipecat_context_hub.server.tools.search_examples.handle_search_examples",
                handler,
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(
                main,
                ["search-examples", "idle timeout", "--domain", "backend", "--limit", "3"],
            )
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["domain"] == "backend"
        embedding_cls.assert_called_once()
        handler.assert_called_once()
        sent = handler.call_args.args[0]
        assert sent["query"] == "idle timeout"
        assert sent["domain"] == "backend"
        assert sent["limit"] == 3

    def test_get_code_snippet_uses_embeddings_and_prints_handler_json(self, tmp_path):
        """Semantic path: handle_get_code_snippet invoked, embeddings built."""
        handler = AsyncMock(return_value=json.dumps({"symbol": "DailyTransport.send_dtmf"}))
        embedding = MagicMock()
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                return_value=embedding,
            ) as embedding_cls,
            patch(
                "pipecat_context_hub.services.retrieval.cross_encoder."
                "CrossEncoderReranker.is_model_cached",
                return_value=False,
            ),
            patch(
                "pipecat_context_hub.server.tools.get_code_snippet.handle_get_code_snippet",
                handler,
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(
                main,
                ["get-code-snippet", "--symbol", "DailyTransport.send_dtmf", "--max-lines", "40"],
            )
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["symbol"] == "DailyTransport.send_dtmf"
        embedding_cls.assert_called_once()
        handler.assert_called_once()
        sent = handler.call_args.args[0]
        assert sent["symbol"] == "DailyTransport.send_dtmf"
        assert sent["max_lines"] == 40

    def test_search_api_uses_embeddings_and_prints_handler_json(self, tmp_path):
        """Semantic path: handle_search_api invoked, embeddings built."""
        handler = AsyncMock(return_value=json.dumps({"hits": [], "module": "pipecat.services"}))
        embedding = MagicMock()
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                return_value=embedding,
            ) as embedding_cls,
            patch(
                "pipecat_context_hub.services.retrieval.cross_encoder."
                "CrossEncoderReranker.is_model_cached",
                return_value=False,
            ),
            patch(
                "pipecat_context_hub.server.tools.search_api.handle_search_api",
                handler,
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(
                main,
                ["search-api", "BaseTransport", "--module", "pipecat.services", "--limit", "7"],
            )
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["module"] == "pipecat.services"
        embedding_cls.assert_called_once()
        handler.assert_called_once()
        sent = handler.call_args.args[0]
        assert sent["query"] == "BaseTransport"
        assert sent["module"] == "pipecat.services"
        assert sent["limit"] == 7


# ── Phase 2: reranker not_cached / retrieval-quality report hints ──────────

# (cli_args, handler patch target, results-list key) for each semantic
# command — the ones that pass ``needs_embeddings=True`` and are therefore
# in scope for both the reranker-warning and retrieval-quality hints.
_SEMANTIC_COMMANDS = [
    pytest.param(
        ["search-docs", "TTS"],
        "pipecat_context_hub.server.tools.search_docs.handle_search_docs",
        "hits",
        id="search-docs",
    ),
    pytest.param(
        ["search-examples", "idle timeout"],
        "pipecat_context_hub.server.tools.search_examples.handle_search_examples",
        "hits",
        id="search-examples",
    ),
    pytest.param(
        ["search-api", "BaseTransport"],
        "pipecat_context_hub.server.tools.search_api.handle_search_api",
        "hits",
        id="search-api",
    ),
    pytest.param(
        ["get-code-snippet", "--symbol", "DailyTransport.send_dtmf"],
        "pipecat_context_hub.server.tools.get_code_snippet.handle_get_code_snippet",
        "snippets",
        id="get-code-snippet",
    ),
]

# (cli_args, handler patch target or None, canned handler JSON or None) for
# each lookup command — ``needs_embeddings=False``, so neither hint applies.
# check-deprecation/status use their real handlers against the mocked store
# (mirroring TestDispatch); get-doc/get-example need a patched handler to
# avoid exercising the real retrieval path against a bare MagicMock store.
_LOOKUP_COMMANDS = [
    pytest.param(["check-deprecation", "PipelineTask"], None, None, id="check-deprecation"),
    pytest.param(
        ["get-doc", "--path", "/guides/x"],
        "pipecat_context_hub.server.tools.get_doc.handle_get_doc",
        json.dumps({"path": "/guides/x", "content": "hi"}),
        id="get-doc",
    ),
    pytest.param(
        ["get-example", "ex1"],
        "pipecat_context_hub.server.tools.get_example.handle_get_example",
        json.dumps({"example_id": "ex1", "files": []}),
        id="get-example",
    ),
    pytest.param(["status"], None, None, id="status"),
]


def _healthy_handler_json(results_key: str) -> str:
    """A non-empty, high-confidence response.

    Used to isolate the reranker-warning tests from the retrieval-quality
    hint (and vice versa) — neither condition should fire on this payload.
    """
    return json.dumps(
        {
            results_key: [{"id": "x1"}],
            "evidence": {"low_confidence": False, "confidence": 1.0},
        }
    )


def _run_semantic_command(
    cli_args: list[str],
    handler_target: str,
    handler_json: str,
    tmp_path: Path,
    *,
    is_model_cached: bool,
    disabled_reason_override: str | None = "__unset__",
) -> tuple[Result, MagicMock]:
    """Invoke a semantic command with a healthy handler and a controlled
    reranker cache/disabled-reason state.

    ``disabled_reason_override`` left at its sentinel drives the reranker
    state through the real ``probe_reranker`` (via ``is_model_cached``);
    passing an explicit value (including ``None``) instead patches
    ``probe_reranker`` directly, which is the only way to reach a reason
    (e.g. ``"load_failed"``) it cannot actually return.
    """
    handler = AsyncMock(return_value=handler_json)
    env = {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            )
        )
        stack.enter_context(
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                return_value=MagicMock(),
            )
        )
        if disabled_reason_override != "__unset__":
            stack.enter_context(
                patch(
                    "pipecat_context_hub.shared.reranker.probe_reranker",
                    return_value=(
                        "cross-encoder/ms-marco-MiniLM-L-6-v2",
                        None,
                        disabled_reason_override,
                    ),
                )
            )
        else:
            stack.enter_context(
                patch(
                    "pipecat_context_hub.services.retrieval.cross_encoder."
                    "CrossEncoderReranker.is_model_cached",
                    return_value=is_model_cached,
                )
            )
        stack.enter_context(patch(handler_target, handler))
        redact_spy = stack.enter_context(
            patch(
                "pipecat_context_hub.cli_query.redact_home_in_text",
                side_effect=_real_redact_home_in_text,
            )
        )
        stack.enter_context(patch.dict("os.environ", env))
        result = runner.invoke(main, cli_args)
    return result, redact_spy


class TestRerankerNotCachedWarning:
    """Semantic commands warn (remediation-first) when the reranker model
    isn't cached; lookup commands and non-``not_cached`` reasons never do.
    """

    @pytest.mark.parametrize(("cli_args", "handler_target", "results_key"), _SEMANTIC_COMMANDS)
    def test_fires_for_every_semantic_command(
        self, tmp_path, cli_args, handler_target, results_key
    ):
        """A single-command test would miss a miswired needs_embeddings flag
        on any of the other three — parametrize across all four."""
        result, redact_spy = _run_semantic_command(
            cli_args,
            handler_target,
            _healthy_handler_json(results_key),
            tmp_path,
            is_model_cached=False,
        )
        assert result.exit_code == 0, result.stderr
        _assert_remediation_before_report_hint(result.stderr, "refresh", BUG_REPORT_ISSUE_URL)
        # stdout is untouched: valid JSON, and the bug-report URL never
        # leaks into the payload the handler produced.
        payload = json.loads(result.stdout)
        assert payload[results_key] == [{"id": "x1"}]
        assert BUG_REPORT_ISSUE_URL not in result.stdout
        # A removed redaction wrapper would still pass a naive "no literal
        # home path in the warning" check, so spy on the call instead.
        assert any(BUG_REPORT_ISSUE_URL in str(call.args[0]) for call in redact_spy.call_args_list)

    @pytest.mark.parametrize("disabled_reason", ["config_disabled", None])
    def test_does_not_fire_for_config_disabled_or_enabled(self, tmp_path, disabled_reason):
        result, _ = _run_semantic_command(
            ["search-docs", "TTS"],
            "pipecat_context_hub.server.tools.search_docs.handle_search_docs",
            _healthy_handler_json("hits"),
            tmp_path,
            is_model_cached=True,
            disabled_reason_override=disabled_reason,
        )
        assert result.exit_code == 0, result.stderr
        assert BUG_REPORT_ISSUE_URL not in result.stderr

    def test_unknown_future_reason_is_a_silent_no_op(self, tmp_path):
        """``probe_reranker`` never returns "load_failed" to the CLI today,
        but the gate must not branch on it either — assert directly against
        a forced future/unknown value, which is what actually enforces "no
        load_failed branch" rather than resting on prose."""
        result, _ = _run_semantic_command(
            ["search-docs", "TTS"],
            "pipecat_context_hub.server.tools.search_docs.handle_search_docs",
            _healthy_handler_json("hits"),
            tmp_path,
            is_model_cached=True,
            disabled_reason_override="load_failed",
        )
        assert result.exit_code == 0, result.stderr
        assert BUG_REPORT_ISSUE_URL not in result.stderr

    @pytest.mark.parametrize(("cli_args", "handler_target", "handler_json"), _LOOKUP_COMMANDS)
    def test_lookup_commands_never_warn_even_when_not_cached(
        self, tmp_path, cli_args, handler_target, handler_json
    ):
        """``_query_runtime`` calls ``_resolve_reranker(construct=False)``
        unconditionally, so ``disabled_reason`` is populated even for lookup
        commands that never construct a reranker; a naive implementation
        checking only the status field (not also ``needs_embeddings``) would
        warn on all four."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch(
                    "pipecat_context_hub.services.index.store.IndexStore",
                    return_value=_index_store_mock(total=10),
                )
            )
            stack.enter_context(
                patch(
                    "pipecat_context_hub.services.embedding.EmbeddingService",
                    side_effect=AssertionError("lookup commands must not load the embedding model"),
                )
            )
            stack.enter_context(
                patch(
                    "pipecat_context_hub.services.retrieval.cross_encoder."
                    "CrossEncoderReranker.is_model_cached",
                    return_value=False,
                )
            )
            if handler_target is not None:
                stack.enter_context(patch(handler_target, AsyncMock(return_value=handler_json)))
            stack.enter_context(patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}))
            result = runner.invoke(main, cli_args)
        assert result.exit_code == 0, result.stderr
        assert BUG_REPORT_ISSUE_URL not in result.stderr


class TestRerankerLoadFailedWarning:
    """A *cached* reranker model that fails to load lazily during dispatch
    gets a bug-report warning — the complementary case
    ``_maybe_warn_reranker_not_cached`` cannot see (see its docstring).

    Codex adversarial review: ``_maybe_warn_reranker_not_cached`` only
    inspects the pre-dispatch ``reranker_status`` snapshot, so a model that
    looked cached and enabled at probe time but then failed to load its ONNX
    weights during this exact dispatch degraded silently — the CLI emitted
    no warning at all, and a resulting ``low_confidence`` result was
    mis-routed to the retrieval-quality tracker instead of the bug tracker.
    """

    def _run_with_cross_encoder(
        self, tmp_path, *, cross_encoder_enabled: bool, handler_json: str
    ) -> tuple[Result, MagicMock]:
        mock_cross_encoder = MagicMock()
        mock_cross_encoder.enabled = cross_encoder_enabled
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch(
                    "pipecat_context_hub.services.retrieval.cross_encoder.CrossEncoderReranker",
                    return_value=mock_cross_encoder,
                )
            )
            stack.enter_context(
                patch(
                    "pipecat_context_hub.services.index.store.IndexStore",
                    return_value=_index_store_mock(total=10),
                )
            )
            stack.enter_context(
                patch(
                    "pipecat_context_hub.services.embedding.EmbeddingService",
                    return_value=MagicMock(),
                )
            )
            stack.enter_context(
                patch(
                    "pipecat_context_hub.server.tools.search_docs.handle_search_docs",
                    AsyncMock(return_value=handler_json),
                )
            )
            redact_spy = stack.enter_context(
                patch(
                    "pipecat_context_hub.cli_query.redact_home_in_text",
                    side_effect=_real_redact_home_in_text,
                )
            )
            stack.enter_context(patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}))
            result = runner.invoke(main, ["search-docs", "TTS"])
        return result, redact_spy

    def test_fires_when_cached_model_fails_to_load_at_runtime(self, tmp_path):
        result, redact_spy = self._run_with_cross_encoder(
            tmp_path, cross_encoder_enabled=False, handler_json=_healthy_handler_json("hits")
        )
        assert result.exit_code == 0, result.stderr
        assert "failed to load" in result.stderr
        _assert_remediation_before_report_hint(
            result.stderr, "failed to load", BUG_REPORT_ISSUE_URL
        )
        payload = json.loads(result.stdout)
        assert payload["hits"] == [{"id": "x1"}]
        assert BUG_REPORT_ISSUE_URL not in result.stdout
        assert any(BUG_REPORT_ISSUE_URL in str(call.args[0]) for call in redact_spy.call_args_list)

    def test_does_not_fire_when_cross_encoder_stays_enabled(self, tmp_path):
        result, _ = self._run_with_cross_encoder(
            tmp_path, cross_encoder_enabled=True, handler_json=_healthy_handler_json("hits")
        )
        assert result.exit_code == 0, result.stderr
        assert BUG_REPORT_ISSUE_URL not in result.stderr

    def test_suppresses_low_confidence_retrieval_quality_hint(self, tmp_path):
        """Same mis-routing rule as the ``not_cached`` case: a load failure
        already explains degraded ranking, so it must not also route a
        ``low_confidence`` result to the retrieval-quality tracker."""
        handler_json = json.dumps(
            {"hits": [{"id": "x1"}], "evidence": {"low_confidence": True, "confidence": 0.05}}
        )
        result, _ = self._run_with_cross_encoder(
            tmp_path, cross_encoder_enabled=False, handler_json=handler_json
        )
        assert result.exit_code == 0, result.stderr
        assert "failed to load" in result.stderr
        assert RETRIEVAL_QUALITY_ISSUE_URL not in result.stderr

    def test_does_not_suppress_empty_results_hint(self, tmp_path):
        """A load failure only affects *ranking* — like the ``not_cached``
        case, it cannot itself empty the candidate set, so a genuinely
        empty result is still a retrieval-quality signal in its own right."""
        handler_json = json.dumps(
            {"hits": [], "evidence": {"low_confidence": False, "confidence": 0.9}}
        )
        result, _ = self._run_with_cross_encoder(
            tmp_path, cross_encoder_enabled=False, handler_json=handler_json
        )
        assert result.exit_code == 0, result.stderr
        assert "failed to load" in result.stderr
        assert RETRIEVAL_QUALITY_ISSUE_URL in result.stderr


class TestRetrievalQualityHint:
    """Poor/missing semantic results get a remediation-first stderr nudge
    toward the retrieval-quality issue template; healthy responses and
    malformed handler payloads never do."""

    @pytest.mark.parametrize(("cli_args", "handler_target", "results_key"), _SEMANTIC_COMMANDS)
    def test_low_confidence_triggers_hint(self, tmp_path, cli_args, handler_target, results_key):
        handler_json = json.dumps(
            {
                results_key: [{"id": "x1"}],
                "evidence": {"low_confidence": True, "confidence": 0.1},
            }
        )
        result, _ = _run_semantic_command(
            cli_args, handler_target, handler_json, tmp_path, is_model_cached=True
        )
        assert result.exit_code == 0, result.stderr
        _assert_url_is_not_message_leading(result.stderr, RETRIEVAL_QUALITY_ISSUE_URL)
        assert json.loads(result.stdout)[results_key] == [{"id": "x1"}]

    @pytest.mark.parametrize(("cli_args", "handler_target", "results_key"), _SEMANTIC_COMMANDS)
    def test_empty_results_triggers_hint(self, tmp_path, cli_args, handler_target, results_key):
        handler_json = json.dumps(
            {results_key: [], "evidence": {"low_confidence": False, "confidence": 0.9}}
        )
        result, _ = _run_semantic_command(
            cli_args, handler_target, handler_json, tmp_path, is_model_cached=True
        )
        assert result.exit_code == 0, result.stderr
        _assert_url_is_not_message_leading(result.stderr, RETRIEVAL_QUALITY_ISSUE_URL)

    @pytest.mark.parametrize(("cli_args", "handler_target", "results_key"), _SEMANTIC_COMMANDS)
    def test_healthy_response_does_not_trigger_hint(
        self, tmp_path, cli_args, handler_target, results_key
    ):
        result, _ = _run_semantic_command(
            cli_args,
            handler_target,
            _healthy_handler_json(results_key),
            tmp_path,
            is_model_cached=True,
        )
        assert result.exit_code == 0, result.stderr
        assert RETRIEVAL_QUALITY_ISSUE_URL not in result.stderr

    def test_malformed_response_is_a_silent_no_op(self, tmp_path):
        """A decoded object with none of evidence/hits/snippets — the shape
        of a lightweight handler-mock fixture used elsewhere in this file —
        must not crash the inspection helper or emit a hint."""
        handler_json = json.dumps({"unexpected": "shape"})
        result, _ = _run_semantic_command(
            ["search-docs", "TTS"],
            "pipecat_context_hub.server.tools.search_docs.handle_search_docs",
            handler_json,
            tmp_path,
            is_model_cached=True,
        )
        assert result.exit_code == 0, result.stderr
        assert RETRIEVAL_QUALITY_ISSUE_URL not in result.stderr


class TestRerankerWarningDoesNotCoFireWithValidationError:
    """Regression test for a fix-round finding: the reranker ``not_cached``
    warning used to be emitted from inside ``_query_runtime`` immediately
    after resolving ``reranker_status`` — before dispatch, and therefore
    before input validation. A semantic command with bad arguments (a
    pydantic ``ValidationError``, exit code 1) would still print the
    reranker warning on stderr right next to the unrelated validation
    error, even though the two have nothing to do with each other. The
    warning must now fire only after a successful dispatch.
    """

    def test_validation_error_suppresses_reranker_warning(self, tmp_path):
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch(
                "pipecat_context_hub.services.embedding.EmbeddingService",
                return_value=MagicMock(),
            ),
            patch(
                "pipecat_context_hub.services.retrieval.cross_encoder."
                "CrossEncoderReranker.is_model_cached",
                return_value=False,
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            # get-code-snippet with no lookup mode (--symbol/--intent/--path)
            # fails GetCodeSnippetInput's model validator before any handler
            # runs — a pure input-validation failure, unrelated to reranking.
            result = runner.invoke(main, ["get-code-snippet"])
        assert result.exit_code == _EXIT_BAD_INPUT
        assert "Error:" in result.stderr
        assert BUG_REPORT_ISSUE_URL not in result.stderr
        assert "reranking disabled" not in result.stderr
        assert result.stdout == ""


class TestRerankerWarningSuppressesRetrievalQualityHint:
    """Regression test for a fix-round finding: when the reranker
    ``not_cached`` warning fires, the retrieval-quality hint must not also
    fire *for the low_confidence signal* on the same invocation — an
    uncached reranker is a known, already-explained cause of degraded
    ranking with its own remediation (``refresh``), so pairing it with a
    second nudge toward the retrieval-quality tracker would mis-route a
    known install/config state into the wrong issue tracker. This uses a
    non-empty result list specifically to isolate the ``low_confidence``
    half of the gate from the ``empty_results`` half, which is covered
    separately below (an uncached reranker cannot itself empty the result
    set, so that half must never be suppressed).
    """

    def test_not_cached_and_low_confidence_only_emits_reranker_warning(self, tmp_path):
        handler_json = json.dumps(
            {"hits": [{"id": "x1"}], "evidence": {"low_confidence": True, "confidence": 0.05}}
        )
        result, _ = _run_semantic_command(
            ["search-docs", "TTS"],
            "pipecat_context_hub.server.tools.search_docs.handle_search_docs",
            handler_json,
            tmp_path,
            is_model_cached=False,
        )
        assert result.exit_code == 0, result.stderr
        assert "reranking disabled" in result.stderr
        assert BUG_REPORT_ISSUE_URL in result.stderr
        assert RETRIEVAL_QUALITY_ISSUE_URL not in result.stderr


class TestRerankerWarningDoesNotSuppressEmptyResultsHint:
    """Regression test: an uncached reranker only affects *ranking* — it
    can never empty the candidate set RRF already produced. The
    ``reranker_uncached`` gate in ``_maybe_warn_poor_results`` used to
    suppress both the ``low_confidence`` and ``empty_results`` halves of
    the retrieval-quality hint together, which meant a cold-cache operator
    whose query genuinely matched nothing saw only the reranker warning and
    no signal that their query itself found no hits. Both warnings must
    fire together in that case.
    """

    def test_empty_results_and_not_cached_emits_both_warnings(self, tmp_path):
        handler_json = json.dumps(
            {"hits": [], "evidence": {"low_confidence": False, "confidence": 0.9}}
        )
        result, _ = _run_semantic_command(
            ["search-docs", "TTS"],
            "pipecat_context_hub.server.tools.search_docs.handle_search_docs",
            handler_json,
            tmp_path,
            is_model_cached=False,
        )
        assert result.exit_code == 0, result.stderr
        assert "reranking disabled" in result.stderr
        assert BUG_REPORT_ISSUE_URL in result.stderr
        assert RETRIEVAL_QUALITY_ISSUE_URL in result.stderr


class TestQuietOutput:
    """Query commands keep captured output lean — it lands in agent context."""

    def test_default_log_level_downgrades_to_warning(self, tmp_path):
        import logging

        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            logging.getLogger().setLevel(logging.INFO)
            result = runner.invoke(main, ["check-deprecation", "X"])
            assert result.exit_code == 0, result.stderr
            assert logging.getLogger().level == logging.WARNING

    def test_explicit_log_level_is_honored(self, tmp_path):
        import logging

        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            logging.getLogger().setLevel(logging.INFO)
            result = runner.invoke(main, ["--log-level", "INFO", "check-deprecation", "X"])
            assert result.exit_code == 0, result.stderr
            assert logging.getLogger().level == logging.INFO

    def test_model_loading_noise_env_defaults_applied(self, tmp_path):
        import os

        env = {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            # patch.dict restores os.environ wholesale on exit, including
            # keys the command sets itself.
            patch.dict("os.environ", env, clear=False),
        ):
            for var in (
                "HF_HUB_OFFLINE",
                "HF_HUB_DISABLE_PROGRESS_BARS",
            ):
                os.environ.pop(var, None)
            result = runner.invoke(main, ["check-deprecation", "X"])
            assert result.exit_code == 0, result.stderr
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"

    def test_explicit_env_wins_over_defaults(self, tmp_path):
        import os

        env = {"PIPECAT_HUB_DATA_DIR": str(tmp_path), "HF_HUB_OFFLINE": "0"}
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch.dict("os.environ", env, clear=False),
        ):
            result = runner.invoke(main, ["check-deprecation", "X"])
            assert result.exit_code == 0, result.stderr
            assert os.environ["HF_HUB_OFFLINE"] == "0"


class TestHomeRedaction:
    """Stderr error paths must not leak the absolute home dir (R6).

    Each test patches ``Path.home()`` to a tmp dir AND points the index
    ``data_dir`` *under* that home (via ``PIPECAT_HUB_DATA_DIR``). Without
    both, ``redact_home`` is a no-op and the assertions pass vacuously — the
    review explicitly warns about this trap.
    """

    def test_open_failure_redacts_data_dir_and_embedded_exc_path(self, monkeypatch, tmp_path):
        """:162 branch — a generic open failure whose own message embeds an
        absolute path under home. BOTH the interpolated ``data_dir`` token and
        the path inside ``{exc}`` must be redacted; a token-only fix leaks the
        embedded path (the crux of R6)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        data_dir = tmp_path / ".pipecat-context-hub"
        # An absolute path under home, embedded inside the exception message —
        # this is what a FileNotFoundError carrying …/chroma.sqlite3 looks like.
        embedded = data_dir / "chroma.sqlite3"
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                side_effect=FileNotFoundError(f"unable to open database file: {embedded}"),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(data_dir)}),
        ):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == _EXIT_INDEX_UNREADY
        # Redaction happened (tilde present) and nothing leaks the literal home.
        assert "~/" in result.stderr or "~" + os.sep in result.stderr
        assert str(tmp_path) not in result.stderr
        # The data_dir token is redacted.
        assert str(data_dir) not in result.stderr
        # The crux: the path embedded inside {exc} is redacted too.
        assert str(embedded) not in result.stderr

    def test_empty_index_redacts_data_dir(self, monkeypatch, tmp_path):
        """:171 branch — empty index (total=0). The interpolated ``data_dir``
        in the 'is empty' message must be home-redacted."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        data_dir = tmp_path / ".pipecat-context-hub"
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=0),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(data_dir)}),
        ):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == _EXIT_INDEX_UNREADY
        assert "is empty" in result.stderr
        assert "~/" in result.stderr or "~" + os.sep in result.stderr
        assert str(tmp_path) not in result.stderr
        assert str(data_dir) not in result.stderr


class TestBadInput:
    """Validation failures exit 1 with a message, not a traceback."""

    def test_get_doc_requires_id_or_path(self, tmp_path):
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_index_store_mock(total=10),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(main, ["get-doc"])
        assert result.exit_code == 1
        assert "doc_id or path" in result.stderr
        assert result.stdout == ""
