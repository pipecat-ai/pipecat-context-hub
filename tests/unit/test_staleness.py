"""Unit tests for the index-staleness annotation (shared/staleness.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pipecat_context_hub.cli import main
from pipecat_context_hub.shared.staleness import annotate_response, staleness_info

runner = CliRunner()


def _store_refreshed_days_ago(days: float) -> MagicMock:
    store = MagicMock()
    refreshed = datetime.now(UTC) - timedelta(days=days)
    store.get_all_metadata.return_value = {"last_refresh_at": refreshed.isoformat()}
    store.get_index_stats.return_value = {"total": 10, "counts_by_type": {"doc": 10}}
    return store


class TestStalenessInfo:
    def test_fresh_index_returns_none(self):
        assert staleness_info(_store_refreshed_days_ago(1)) is None

    def test_stale_index_returns_payload_with_hint(self):
        info = staleness_info(_store_refreshed_days_ago(21))
        assert info is not None
        assert info["age_days"] >= 20
        assert "refresh" in info["hint"]

    def test_missing_timestamp_returns_none(self):
        store = MagicMock()
        store.get_all_metadata.return_value = {}
        assert staleness_info(store) is None

    def test_unparseable_timestamp_returns_none(self):
        store = MagicMock()
        store.get_all_metadata.return_value = {"last_refresh_at": "not-a-date"}
        assert staleness_info(store) is None

    def test_env_threshold_override(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "30")
        assert staleness_info(_store_refreshed_days_ago(21)) is None

    def test_env_zero_disables(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "0")
        assert staleness_info(_store_refreshed_days_ago(365)) is None

    def test_env_negative_disables(self, monkeypatch):
        # Negative thresholds disable the annotation like 0 (the `<= 0` guard).
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "-5")
        assert staleness_info(_store_refreshed_days_ago(365)) is None

    def test_env_inf_falls_back_to_default(self, monkeypatch):
        # `inf` would make every index look fresh (age < inf is always true),
        # silently defeating the warning — it must fall back to the 7-day
        # default, so a 365-day index still annotates and a fresh one stays quiet.
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "inf")
        assert staleness_info(_store_refreshed_days_ago(365)) is not None
        assert staleness_info(_store_refreshed_days_ago(1)) is None

    def test_env_nan_falls_back_to_default(self, monkeypatch):
        # `nan` makes all comparisons false (would annotate even a fresh index)
        # — it must fall back to the default, so a fresh index stays quiet and a
        # stale one still annotates.
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "nan")
        assert staleness_info(_store_refreshed_days_ago(1)) is None
        assert staleness_info(_store_refreshed_days_ago(365)) is not None

    def test_env_non_numeric_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_STALE_AFTER_DAYS", "soon")
        assert staleness_info(_store_refreshed_days_ago(1)) is None
        assert staleness_info(_store_refreshed_days_ago(365)) is not None


class TestAnnotateResponse:
    def test_stale_injects_field(self):
        out = annotate_response('{"hits": []}', _store_refreshed_days_ago(21))
        payload = json.loads(out)
        assert payload["hits"] == []
        assert payload["index_staleness"]["age_days"] >= 20

    def test_fresh_returns_input_unchanged(self):
        raw = '{"hits": []}'
        assert annotate_response(raw, _store_refreshed_days_ago(1)) is raw

    def test_non_object_payload_unchanged(self):
        raw = "[1, 2, 3]"
        assert annotate_response(raw, _store_refreshed_days_ago(21)) == raw

    def test_store_errors_never_break_the_response(self):
        store = MagicMock()
        store.get_all_metadata.side_effect = RuntimeError("boom")
        raw = '{"hits": []}'
        assert annotate_response(raw, store) == raw

    def test_parsed_payload_is_reused_without_reparsing(self):
        """``parsed=`` lets a caller that already decoded ``result_json``

        (e.g. the CLI's retrieval-quality hint inspection) skip a second
        ``json.loads`` of the same string. Patch ``json.loads`` to raise if
        called at all — the only way this test passes is if ``annotate_response``
        never re-parses ``raw`` when a pre-decoded payload is supplied.
        """
        raw = '{"hits": []}'
        parsed: dict[str, object] = {"hits": []}
        with patch(
            "pipecat_context_hub.shared.staleness.json.loads",
            side_effect=AssertionError("must not re-parse when parsed= is supplied"),
        ):
            out = annotate_response(raw, _store_refreshed_days_ago(21), parsed=parsed)
        payload = json.loads(out)
        assert payload["hits"] == []
        assert payload["index_staleness"]["age_days"] >= 20

    def test_parsed_payload_omitted_still_parses_result_json(self):
        """Backward-compat: no ``parsed=`` argument means the old behavior —

        parse ``result_json`` itself — is unchanged for every existing caller.
        """
        out = annotate_response('{"hits": []}', _store_refreshed_days_ago(21))
        payload = json.loads(out)
        assert payload["hits"] == []
        assert payload["index_staleness"]["age_days"] >= 20


class TestBothFrontDoors:
    """The footer reaches responses on the CLI and MCP dispatch paths alike."""

    def test_cli_response_carries_footer_when_stale(self, tmp_path):
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_store_refreshed_days_ago(21),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(main, ["check-deprecation", "SomeSymbol"])
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        assert "refresh" in payload["index_staleness"]["hint"]

    def test_cli_status_is_never_annotated(self, tmp_path):
        with (
            patch(
                "pipecat_context_hub.services.index.store.IndexStore",
                return_value=_store_refreshed_days_ago(21),
            ),
            patch.dict("os.environ", {"PIPECAT_HUB_DATA_DIR": str(tmp_path)}),
        ):
            result = runner.invoke(main, ["status"])
        assert result.exit_code == 0, result.stderr
        assert "index_staleness" not in json.loads(result.stdout)

    def test_mcp_response_carries_footer_when_stale(self):
        import asyncio

        from pipecat_context_hub.server import main as server_main

        store = _store_refreshed_days_ago(21)
        retriever = MagicMock()
        retriever.deprecation_map = None

        async def run() -> str:
            server = server_main.create_server(retriever, store)
            # call_tool is registered via decorator; reach it through the
            # server's request handler for CallToolRequest.
            from mcp import types

            req = types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(
                    name="check_deprecation", arguments={"symbol": "X"}
                ),
            )
            result = await server.request_handlers[types.CallToolRequest](req)
            content = result.root.content  # type: ignore[union-attr]
            block = content[0]
            assert isinstance(block, types.TextContent)
            return block.text

        text = asyncio.run(run())
        payload = json.loads(text)
        assert "refresh" in payload["index_staleness"]["hint"]
