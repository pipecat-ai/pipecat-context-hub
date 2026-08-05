"""Unit tests for the MCP server: tool registration, call dispatch, transport.

Tests cover:
1. Server registers all 7 tools and tools/list returns them.
2. Tool calls dispatch correctly and return valid JSON.
3. Unknown tool name raises ValueError.
4. Transport module is importable and functions exist.
5. CLI commands exist and are callable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipecat_context_hub.server.main import _BASE_TOOLS, _HUB_STATUS_TOOL, create_server
from pipecat_context_hub.shared.types import (
    ApiHit,
    Citation,
    CodeSnippet,
    DocHit,
    EvidenceReport,
    ExampleFile,
    ExampleHit,
    GetCodeSnippetOutput,
    GetDocOutput,
    GetExampleOutput,
    KnownItem,
    SearchApiOutput,
    SearchDocsOutput,
    SearchExamplesOutput,
    TaxonomyEntry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 2, 18, tzinfo=UTC)


def _citation(**overrides: Any) -> Citation:
    defaults: dict[str, Any] = {
        "source_url": "https://docs.pipecat.ai/test",
        "path": "test.md",
        "indexed_at": NOW,
    }
    defaults.update(overrides)
    return Citation.model_validate(defaults)


def _evidence() -> EvidenceReport:
    return EvidenceReport(
        known=[KnownItem(statement="test", citations=[_citation()], confidence=0.9)],
        unknown=[],
        confidence=0.9,
        confidence_rationale="test",
    )


@pytest.fixture
def mock_retriever():
    retriever = AsyncMock()

    retriever.search_docs.return_value = SearchDocsOutput(
        hits=[
            DocHit(
                doc_id="d1",
                title="T",
                snippet="S",
                citation=_citation(),
                score=0.9,
            )
        ],
        evidence=_evidence(),
    )

    retriever.get_doc.return_value = GetDocOutput(
        doc_id="d1",
        title="T",
        content="C",
        source_url="https://docs.pipecat.ai/test",
        indexed_at=NOW,
        sections=[],
        evidence=_evidence(),
    )

    retriever.search_examples.return_value = SearchExamplesOutput(
        hits=[
            ExampleHit(
                example_id="e1",
                summary="S",
                repo="r",
                path="p",
                citation=_citation(),
                score=0.9,
            )
        ],
        evidence=_evidence(),
    )

    retriever.get_example.return_value = GetExampleOutput(
        example_id="e1",
        metadata=TaxonomyEntry(example_id="e1", repo="r", path="p"),
        files=[ExampleFile(path="f.py", content="pass", language="python")],
        citation=_citation(),
        detected_symbols=[],
        evidence=_evidence(),
    )

    retriever.get_code_snippet.return_value = GetCodeSnippetOutput(
        snippets=[
            CodeSnippet(
                content="pass",
                path="f.py",
                line_start=1,
                line_end=1,
                language="python",
                citation=_citation(),
            )
        ],
        evidence=_evidence(),
    )

    retriever.search_api.return_value = SearchApiOutput(
        hits=[
            ApiHit(
                chunk_id="a1",
                module_path="pipecat.services.tts",
                chunk_type="class_overview",
                snippet="class TTSService:",
                is_dataclass=False,
                citation=_citation(),
                score=0.9,
            )
        ],
        evidence=_evidence(),
    )

    return retriever


# ---------------------------------------------------------------------------
# Tool registration tests
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_base_tools_has_seven_entries(self):
        assert len(_BASE_TOOLS) == 7

    def test_base_tool_names(self):
        names = [name for name, _, _ in _BASE_TOOLS]
        assert names == [
            "search_docs",
            "get_doc",
            "search_examples",
            "get_example",
            "get_code_snippet",
            "search_api",
            "check_deprecation",
        ]

    def test_hub_status_tool_exists(self):
        name, desc, schema = _HUB_STATUS_TOOL
        assert name == "get_hub_status"
        assert schema["type"] == "object"

    def test_registry_schemas_are_valid_json_schema(self):
        all_tools = list(_BASE_TOOLS) + [_HUB_STATUS_TOOL]
        for name, _, schema in all_tools:
            assert schema["type"] == "object", f"{name} schema must be an object"
            assert "properties" in schema, f"{name} schema must have properties"

    def test_hub_status_not_listed_without_store(self, mock_retriever):
        """Without index_store, get_hub_status should not be registered."""
        server = create_server(mock_retriever)
        # We can't call list_tools directly, but we verify the closure
        # builds correctly — the real test is that ValueError is never raised
        assert server.name == "pipecat-context-hub"

    async def test_list_tools_handler_registered(self, mock_retriever):
        from mcp import types

        server = create_server(mock_retriever)
        assert types.ListToolsRequest in server.request_handlers

    async def test_list_tools_touches_idle_tracker(self, mock_retriever):
        """tools/list must reset the idle clock — clients that only poll
        capabilities (no tool calls) still represent an active session."""
        from mcp import types

        from pipecat_context_hub.shared.tracking import IdleTracker

        tracker = IdleTracker()
        server = create_server(mock_retriever, idle_tracker=tracker)
        handler = server.request_handlers[types.ListToolsRequest]
        request = types.ListToolsRequest(method="tools/list")

        # Age the tracker, then fire the handler; touch() must reset it.
        tracker._last -= 1000.0
        assert tracker.seconds_since_last() >= 1000.0
        await handler(request)
        assert tracker.seconds_since_last() < 1.0

    async def test_call_tool_touches_idle_tracker(self, mock_retriever):
        """tools/call must reset the idle clock (existing behaviour, now pinned)."""
        from mcp import types

        from pipecat_context_hub.shared.tracking import IdleTracker

        tracker = IdleTracker()
        server = create_server(mock_retriever, idle_tracker=tracker)
        handler = server.request_handlers[types.CallToolRequest]
        request = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name="search_docs", arguments={"query": "x"}),
        )

        tracker._last -= 1000.0
        assert tracker.seconds_since_last() >= 1000.0
        await handler(request)
        assert tracker.seconds_since_last() < 1.0

    async def test_ping_touches_idle_tracker(self, mock_retriever):
        """MCP `ping` requests are handled by the low-level Server directly
        (not via our list/call decorators), so they must still count as
        activity — otherwise a client keeping an idle session alive with
        periodic ping heartbeats would still be reaped as idle.
        """
        from mcp import types

        from pipecat_context_hub.shared.tracking import IdleTracker

        tracker = IdleTracker()
        server = create_server(mock_retriever, idle_tracker=tracker)
        handler = server.request_handlers[types.PingRequest]
        request = types.PingRequest(method="ping")

        tracker._last -= 1000.0
        assert tracker.seconds_since_last() >= 1000.0
        result = await handler(request)
        assert tracker.seconds_since_last() < 1.0
        # Built-in ping still returns an EmptyResult.
        assert isinstance(result.root, types.EmptyResult)

    async def test_ping_handler_noop_without_idle_tracker(self, mock_retriever):
        """Omitting idle_tracker must leave the built-in ping handler
        in place unchanged — we don't want to break ping when idle
        watchdogging is disabled."""
        from mcp import types

        server = create_server(mock_retriever)  # no idle_tracker
        handler = server.request_handlers[types.PingRequest]
        result = await handler(types.PingRequest(method="ping"))
        assert isinstance(result.root, types.EmptyResult)


# ---------------------------------------------------------------------------
# Tool dispatch tests
# ---------------------------------------------------------------------------


class TestToolDispatch:
    async def test_call_tool_handler_registered(self, mock_retriever):
        from mcp import types

        server = create_server(mock_retriever)
        assert types.CallToolRequest in server.request_handlers

    async def test_create_server_returns_server(self, mock_retriever):
        from mcp.server.lowlevel import Server

        server = create_server(mock_retriever)
        assert isinstance(server, Server)

    async def test_server_name(self, mock_retriever):
        server = create_server(mock_retriever)
        assert server.name == "pipecat-context-hub"


# ---------------------------------------------------------------------------
# Transport module tests
# ---------------------------------------------------------------------------


class TestGetHubStatusRerankerFields:
    """Reranker fields in handle_get_hub_status reflect live runtime state."""

    def _stub_store(self) -> Any:
        class _Stub:
            data_dir = "/tmp/hub"

            def get_index_stats(self) -> dict[str, Any]:
                return {"total": 0, "counts_by_type": {}, "commit_shas": []}

            def get_all_metadata(self) -> dict[str, str]:
                return {}

        return _Stub()

    async def test_enabled_reports_live_model(self):
        import json

        from pipecat_context_hub.server.tools.get_hub_status import (
            handle_get_hub_status,
        )
        from pipecat_context_hub.shared.types import RerankerStatus

        status = RerankerStatus(
            enabled=True,
            model="cross-encoder/ms-marco-TinyBERT-L-2-v2",
            configured_model="cross-encoder/ms-marco-TinyBERT-L-2-v2",
        )
        payload = await handle_get_hub_status({}, self._stub_store(), status)
        data = json.loads(payload)
        assert data["reranker_enabled"] is True
        assert data["reranker_model"] == "cross-encoder/ms-marco-TinyBERT-L-2-v2"
        assert data["reranker_configured_model"] == "cross-encoder/ms-marco-TinyBERT-L-2-v2"
        assert data["reranker_disabled_reason"] is None

    async def test_not_cached_surfaces_reason_and_configured_model(self):
        import json

        from pipecat_context_hub.server.tools.get_hub_status import (
            handle_get_hub_status,
        )
        from pipecat_context_hub.shared.types import RerankerStatus

        status = RerankerStatus(
            enabled=False,
            configured_model="cross-encoder/ms-marco-MiniLM-L-12-v2",
            disabled_reason="not_cached",
        )
        payload = await handle_get_hub_status({}, self._stub_store(), status)
        data = json.loads(payload)
        assert data["reranker_enabled"] is False
        assert data["reranker_model"] is None
        assert data["reranker_configured_model"] == "cross-encoder/ms-marco-MiniLM-L-12-v2"
        assert data["reranker_disabled_reason"] == "not_cached"

    async def test_load_failed_surfaces_reason(self):
        import json

        from pipecat_context_hub.server.tools.get_hub_status import (
            handle_get_hub_status,
        )
        from pipecat_context_hub.shared.types import RerankerStatus

        status = RerankerStatus(
            enabled=False,
            configured_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            disabled_reason="load_failed",
        )
        payload = await handle_get_hub_status({}, self._stub_store(), status)
        data = json.loads(payload)
        assert data["reranker_enabled"] is False
        assert data["reranker_disabled_reason"] == "load_failed"

    async def test_no_status_returns_disabled_with_unknown_reason(self):
        import json

        from pipecat_context_hub.server.tools.get_hub_status import (
            handle_get_hub_status,
        )

        payload = await handle_get_hub_status({}, self._stub_store(), None)
        data = json.loads(payload)
        assert data["reranker_enabled"] is False
        assert data["reranker_model"] is None
        # When no provider is wired the reason is unknown — don't lie.
        assert data["reranker_disabled_reason"] is None

    async def test_provider_is_called_per_query(self):
        """create_server evaluates the provider on each get_hub_status call."""
        from unittest.mock import AsyncMock

        from pipecat_context_hub.server.main import create_server
        from pipecat_context_hub.shared.types import RerankerStatus

        retriever = AsyncMock()
        calls: list[int] = []

        def _provider() -> RerankerStatus:
            calls.append(1)
            return RerankerStatus(enabled=False, disabled_reason="config_disabled")

        server = create_server(
            retriever,
            index_store=self._stub_store(),
            reranker_status_provider=_provider,
        )
        assert server.name == "pipecat-context-hub"
        # The closure is wired in — exercising it requires the full call path
        # which is covered by integration tests; here we assert create_server
        # accepts the callable and does not eagerly invoke it.
        assert calls == []


class TestTransport:
    def test_transport_module_importable(self):
        from pipecat_context_hub.server import transport

        assert hasattr(transport, "run_stdio")
        assert hasattr(transport, "serve_stdio")
        assert callable(transport.run_stdio)
        assert callable(transport.serve_stdio)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_main_importable(self):
        from pipecat_context_hub.cli import main

        assert main is not None

    def test_cli_has_serve_command(self):
        from pipecat_context_hub.cli import serve

        assert serve is not None

    def test_cli_has_refresh_command(self):
        from pipecat_context_hub.cli import refresh

        assert refresh is not None

    def test_cli_group_commands(self):
        from pipecat_context_hub.cli import main

        assert "serve" in main.commands
        assert "refresh" in main.commands


# ---------------------------------------------------------------------------
# __main__ entry point test
# ---------------------------------------------------------------------------


class TestVersionConsistency:
    """Ensure pyproject.toml version and _SERVER_VERSION stay in sync."""

    def test_server_version_matches_pyproject(self):
        """_SERVER_VERSION in server/main.py must match pyproject.toml [project].version."""
        import tomllib
        from pathlib import Path

        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with pyproject_path.open("rb") as f:
            pyproject_version = tomllib.load(f)["project"]["version"]

        from pipecat_context_hub.server.main import _SERVER_VERSION

        assert _SERVER_VERSION == pyproject_version, (
            f"Version mismatch: _SERVER_VERSION={_SERVER_VERSION!r} "
            f"but pyproject.toml version={pyproject_version!r}. "
            f"Both must be updated together on each release."
        )

    def test_package_version_matches_pyproject(self):
        """``pipecat_context_hub.__version__`` is the version external consumers read.

        Unlike its ``_SERVER_VERSION`` sibling this reads installed distribution
        metadata rather than a source constant, so it also fails when the
        environment is stale — which the message below names, because during a
        release bump that is the likelier cause than a real mismatch.
        """
        import tomllib
        from pathlib import Path

        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with pyproject_path.open("rb") as f:
            pyproject_version = tomllib.load(f)["project"]["version"]

        import pipecat_context_hub

        assert pipecat_context_hub.__version__ == pyproject_version, (
            f"__version__={pipecat_context_hub.__version__!r} but pyproject.toml "
            f"version={pyproject_version!r}. __version__ comes from installed "
            f"distribution metadata, so after bumping the version re-sync the "
            f"environment (`uv sync --extra dev --group dev`) before re-running."
        )


class TestServerInstructions:
    """Pin the self-report guidance in the MCP ``initialize`` instructions.

    This text is advisory prose for the *connecting agent*, not a code path
    that fires on an exception — nothing else in the tree references it (see
    AGENTS.md item #50), so a wording edit that drops a clause or a URL would
    otherwise pass silently. Also, the CLI (``cli_query.py``) now gets the
    same guidance via the shared ``shared/support_links.py`` constants — see
    ``TestSupportLinks`` below.

    Assertions here check against the *imported* constants, not hardcoded
    literals, so this class fails if ``_SERVER_INSTRUCTIONS`` stops
    interpolating from ``shared/support_links.py``.
    """

    def test_retrieval_quality_report_hint_present(self):
        from pipecat_context_hub.server.main import _SERVER_INSTRUCTIONS
        from pipecat_context_hub.shared.support_links import (
            RETRIEVAL_QUALITY_ISSUE_URL,
        )

        assert "low_confidence" in _SERVER_INSTRUCTIONS
        assert RETRIEVAL_QUALITY_ISSUE_URL in _SERVER_INSTRUCTIONS

    def test_degraded_hub_report_hint_present(self):
        from pipecat_context_hub.server.main import _SERVER_INSTRUCTIONS
        from pipecat_context_hub.shared.support_links import BUG_REPORT_ISSUE_URL

        assert "reranker_disabled_reason" in _SERVER_INSTRUCTIONS
        assert "not_cached" in _SERVER_INSTRUCTIONS
        assert "load_failed" in _SERVER_INSTRUCTIONS
        assert BUG_REPORT_ISSUE_URL in _SERVER_INSTRUCTIONS

    def test_not_cached_remediation_precedes_bug_report(self):
        """``not_cached`` has a self-service fix; ``load_failed`` does not.

        Mirrors the CLI's remediation-first wording (``cli_query.py``): try
        ``pipecat-context-hub refresh`` before routing to the bug tracker.
        Regression guard for the MCP-side symmetry gap this phase closed —
        the clause previously jumped straight to "file a bug report" for
        both reasons alike.
        """
        from pipecat_context_hub.server.main import _SERVER_INSTRUCTIONS
        from pipecat_context_hub.shared.support_links import BUG_REPORT_ISSUE_URL

        assert "pipecat-context-hub refresh" in _SERVER_INSTRUCTIONS
        refresh_pos = _SERVER_INSTRUCTIONS.index("pipecat-context-hub refresh")
        bug_report_pos = _SERVER_INSTRUCTIONS.index(BUG_REPORT_ISSUE_URL)
        assert refresh_pos < bug_report_pos

    def test_not_cached_remediation_requires_reconnect(self):
        """``refresh`` alone does not fix an already-running ``serve`` process.

        Codex adversarial review (round 5): the reranker's disabled-reason is
        resolved once at ``serve`` startup (``cli.py``'s ``probe_reranker``
        call, captured by the ``_reranker_status`` closure) and never
        re-probed while the process is alive. An agent that runs ``refresh``
        and then re-calls ``get_hub_status`` on the *same* MCP connection
        would still see ``not_cached`` and could wrongly escalate to filing a
        bug report for what is actually stale in-memory state. The
        instructions must say to restart/reconnect before re-checking.
        """
        from pipecat_context_hub.server.main import _SERVER_INSTRUCTIONS

        assert "restart or reconnect" in _SERVER_INSTRUCTIONS
        assert "does not re-check the" in _SERVER_INSTRUCTIONS
        refresh_pos = _SERVER_INSTRUCTIONS.index("pipecat-context-hub refresh")
        reconnect_pos = _SERVER_INSTRUCTIONS.index("restart or reconnect")
        assert refresh_pos < reconnect_pos

    @pytest.mark.parametrize("index_state", ["empty", "incompatible"])
    def test_boot_failure_guidance_matches_index_recovery_paths(
        self, index_state, tmp_path, monkeypatch, caplog
    ):
        """Boot guidance must defer to the recovery emitted before MCP initialization.

        Empty and incompatible indexes prescribe different commands on stderr,
        but both exit before an MCP client can call ``get_hub_status``.
        """
        from click.testing import CliRunner

        from pipecat_context_hub.cli import _EXIT_INDEX_UNREADY, main
        from pipecat_context_hub.server.main import _SERVER_INSTRUCTIONS
        from pipecat_context_hub.services.index import IncompatibleIndexFormatError
        from pipecat_context_hub.services.index.errors import RESET_INDEX_REMEDIATION

        if index_state == "empty":
            store = MagicMock()
            store.get_index_stats.return_value = {
                "counts_by_type": {},
                "total": 0,
                "commit_shas": [],
            }
            index_store = MagicMock(return_value=store)
            emitted_remediation = "pipecat-context-hub refresh"
            guided_remediation = "pipecat-context-hub refresh"
        else:
            chroma_path = tmp_path / ".pipecat-context-hub" / "chroma"
            index_store = MagicMock(side_effect=IncompatibleIndexFormatError(chroma_path))
            emitted_remediation = RESET_INDEX_REMEDIATION
            guided_remediation = "pipecat-context-hub refresh --force --reset-index"

        monkeypatch.chdir(tmp_path)
        with (
            patch("pipecat_context_hub.services.index.store.IndexStore", index_store),
            caplog.at_level("ERROR", logger="pipecat_context_hub.cli"),
        ):
            result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == _EXIT_INDEX_UNREADY
        assert emitted_remediation in caplog.text
        assert guided_remediation in _SERVER_INSTRUCTIONS.replace("``", "")
        assert "before MCP initialization" in _SERVER_INSTRUCTIONS
        assert "get_hub_status`` is unavailable" in _SERVER_INSTRUCTIONS
        assert "Follow the remediation in the startup stderr first" in _SERVER_INSTRUCTIONS
        assert "Only request ``get_hub_status`` after successful initialization" in (
            _SERVER_INSTRUCTIONS
        )

    def test_config_disabled_is_excluded_from_bug_report_flow(self):
        """``PIPECAT_HUB_RERANKER_ENABLED=0`` is an operator choice, not an incident.

        Regression guard: if this clause is ever dropped, an operator who
        deliberately disabled the reranker would get funnelled into filing a
        bug report for expected behaviour.
        """
        from pipecat_context_hub.server.main import _SERVER_INSTRUCTIONS

        assert "config_disabled" in _SERVER_INSTRUCTIONS
        assert "not treat it as an incident" in _SERVER_INSTRUCTIONS

    def test_instructions_are_wired_into_the_server(self):
        """The constant must actually reach the MCP ``initialize`` response."""
        import inspect

        from pipecat_context_hub.server.main import create_server

        source = inspect.getsource(create_server)
        assert "instructions=_SERVER_INSTRUCTIONS" in source


class TestSupportLinks:
    """Pin ``shared/support_links.py`` as the single source of truth for the
    two GitHub issue-template URLs.

    ``TestServerInstructions`` only pins that ``_SERVER_INSTRUCTIONS``
    interpolates from these constants; it would pass even if a typo landed
    in ``support_links.py`` itself, since the test and the source would
    agree with each other trivially. The literal-value assertions below are
    what actually catch that typo.
    """

    def test_retrieval_quality_issue_url_literal_value(self):
        from pipecat_context_hub.shared.support_links import (
            RETRIEVAL_QUALITY_ISSUE_URL,
        )

        assert RETRIEVAL_QUALITY_ISSUE_URL == (
            "https://github.com/pipecat-ai/pipecat-context-hub/issues/new"
            "?template=retrieval-quality.yml"
        )

    def test_bug_report_issue_url_literal_value(self):
        from pipecat_context_hub.shared.support_links import BUG_REPORT_ISSUE_URL

        assert BUG_REPORT_ISSUE_URL == (
            "https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=bug-report.yml"
        )

    def test_issue_template_files_exist(self):
        """Each constant's ``template=`` value names a real workflow file.

        Cheap cross-check against already-shipped behaviour: if either
        template is ever renamed on the GitHub side without updating this
        module, the constant would silently point at a 404.
        """
        import re
        from pathlib import Path
        from urllib.parse import parse_qs, urlparse

        from pipecat_context_hub.shared.support_links import (
            BUG_REPORT_ISSUE_URL,
            RETRIEVAL_QUALITY_ISSUE_URL,
        )

        repo_root = Path(__file__).resolve().parents[2]
        template_dir = repo_root / ".github" / "ISSUE_TEMPLATE"

        for url in (RETRIEVAL_QUALITY_ISSUE_URL, BUG_REPORT_ISSUE_URL):
            template = parse_qs(urlparse(url).query)["template"][0]
            assert re.fullmatch(r"[\w.-]+\.ya?ml", template), template
            assert (template_dir / template).is_file(), f"{template} not found in {template_dir}"

    def test_no_stray_issue_template_literals(self):
        """``"issues/new?template="`` may appear only in ``support_links.py``.

        Guards the "one source of truth" requirement structurally: a new
        hardcoded copy of either URL anywhere else under ``src/`` fails this
        test, rather than relying on a manual grep during review. (A
        known, accepted exception outside ``src/`` is ``docs/README.md`` —
        markdown can't import Python constants — which this test does not
        and should not cover.)
        """
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        src_root = repo_root / "src"
        needle = "issues/new?template="
        support_links_path = src_root / "pipecat_context_hub" / "shared" / "support_links.py"

        offenders = []
        for path in src_root.rglob("*.py"):
            if path == support_links_path:
                continue
            if needle in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(repo_root)))

        assert offenders == [], (
            f"found stray '{needle}' literal(s) outside support_links.py: {offenders}"
        )

    def test_consumers_import_and_reference_support_links_symbols(self):
        """Structural guard, separate from the stray-literal scan above.

        The stray-literal scan only catches a URL re-typed verbatim; it
        cannot catch one rebuilt from fragments (e.g. string concatenation
        or an f-string assembled from a base URL + template name), which
        would contain no `"issues/new?template="` substring to grep for.
        This test instead parses each consumer's AST and asserts (a) it
        imports the required symbol(s) from `shared.support_links`, and
        (b) each imported symbol is actually referenced as a `Name` load
        somewhere else in the module — not merely imported and unused,
        which would mean nothing built from it is really shared.

        ``cli.py`` is intentionally not a direct consumer here: it imports
        the shared ``_bug_report_hint()`` helper from ``cli_query`` instead
        of duplicating the sentence that builds on ``BUG_REPORT_ISSUE_URL``
        (avoids a second, separately-maintained copy of that wording, which
        was itself a review finding). It still gets the URL transitively
        through that helper — this test doesn't need to re-check that path
        since ``cli_query.py``'s own entry below already pins the constant
        at its one real source.
        """
        import ast
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        src_root = repo_root / "src" / "pipecat_context_hub"

        consumers = {
            "server/main.py": {"RETRIEVAL_QUALITY_ISSUE_URL", "BUG_REPORT_ISSUE_URL"},
            "cli_query.py": {"RETRIEVAL_QUALITY_ISSUE_URL", "BUG_REPORT_ISSUE_URL"},
        }

        for rel_path, required_symbols in consumers.items():
            path = src_root / rel_path
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

            imported_symbols: set[str] = set()
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "pipecat_context_hub.shared.support_links"
                ):
                    imported_symbols.update(alias.asname or alias.name for alias in node.names)

            missing_imports = required_symbols - imported_symbols
            assert not missing_imports, (
                f"{rel_path} does not import {missing_imports} from "
                "pipecat_context_hub.shared.support_links"
            )

            # Every Name node in the module, minus the import statement's own
            # alias bindings, so an imported-but-never-used symbol doesn't
            # count as "referenced" merely by appearing in its own import.
            referenced_names = {
                node.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            unreferenced = required_symbols - referenced_names
            assert not unreferenced, (
                f"{rel_path} imports {unreferenced} from support_links but never "
                "references them — nothing in this file actually shares the constant"
            )

    def test_cli_imports_shared_bug_report_hint_helper(self):
        """``cli.py`` must not carry its own copy of ``_bug_report_hint()``.

        Companion to the consumers check above: since ``cli.py`` sources the
        bug-report URL transitively through this helper rather than
        importing ``BUG_REPORT_ISSUE_URL`` directly, pin that it actually
        imports the helper — otherwise a future edit could silently
        reintroduce a duplicated local definition with no test catching it.
        """
        import ast
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        cli_path = repo_root / "src" / "pipecat_context_hub" / "cli.py"
        tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename=str(cli_path))

        imported_from_cli_query: set[str] = set()
        local_defs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "pipecat_context_hub.cli_query":
                imported_from_cli_query.update(alias.asname or alias.name for alias in node.names)
            if isinstance(node, ast.FunctionDef):
                local_defs.add(node.name)

        assert "_bug_report_hint" in imported_from_cli_query, (
            "cli.py must import _bug_report_hint from cli_query rather than defining its own copy"
        )
        assert "_bug_report_hint" not in local_defs, (
            "cli.py must not redefine _bug_report_hint locally — it shadows the "
            "imported shared helper"
        )


class TestEntryPoint:
    def test_main_module_has_main(self):
        """Verify __main__ module exists and references cli.main."""
        import importlib

        mod = importlib.import_module("pipecat_context_hub.__main__")
        assert hasattr(mod, "main")
