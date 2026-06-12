"""Unit tests for the one-shot CLI query subcommands (cli_query.py)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from pipecat_context_hub.cli import _EXIT_INDEX_UNREADY as _SERVE_EXIT_INDEX_UNREADY
from pipecat_context_hub.cli import main
from pipecat_context_hub.cli_query import _EXIT_INDEX_UNREADY, _TOOL_TO_COMMAND
from pipecat_context_hub.server.main import _BASE_TOOLS, _HUB_STATUS_TOOL
from pipecat_context_hub.shared.types import EvidenceReport, SearchDocsOutput

runner = CliRunner()


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


class TestIndexUnready:
    """Empty/unopenable index exits 2 with an actionable refresh hint."""

    def test_empty_index_exits_with_refresh_hint(self):
        with patch(
            "pipecat_context_hub.services.index.store.IndexStore",
            return_value=_index_store_mock(total=0),
        ):
            result = runner.invoke(main, ["check-deprecation", "PipelineTask"])
        assert result.exit_code == _EXIT_INDEX_UNREADY
        assert "refresh" in result.stderr
        assert result.stdout == ""

    def test_unopenable_index_exits_with_reset_hint(self):
        with patch(
            "pipecat_context_hub.services.index.store.IndexStore",
            side_effect=RuntimeError("corrupt"),
        ):
            result = runner.invoke(main, ["status"])
        assert result.exit_code == _EXIT_INDEX_UNREADY
        assert "--reset-index" in result.stderr


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
