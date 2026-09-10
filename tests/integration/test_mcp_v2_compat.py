"""Permanent wire-level compatibility coverage for the MCP 2.x server."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pipecat_context_hub.services.embedding import EmbeddingIndexWriter, EmbeddingService
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.shared.config import EmbeddingConfig, StorageConfig
from pipecat_context_hub.shared.types import ChunkedRecord

REPO_ROOT = Path(__file__).resolve().parents[2]

# Deliberately maintained independently of ``server.dispatch``.  This is the
# client-facing compatibility oracle: a registry refactor must not make this
# test tautological by changing both the producer and expected value together.
EXPECTED_TOOL_SCHEMAS = {
    "search_docs": {
        "title": "SearchDocsInput",
        "required": ["query"],
        "properties": {"area", "limit", "query"},
    },
    "get_doc": {
        "title": "GetDocInput",
        "required": [],
        "properties": {"doc_id", "path", "section"},
    },
    "search_examples": {
        "title": "SearchExamplesInput",
        "required": ["query"],
        "properties": {
            "domain",
            "execution_mode",
            "foundational_class",
            "language",
            "limit",
            "pipecat_version",
            "query",
            "repo",
            "tags",
            "version_filter",
        },
    },
    "get_example": {
        "title": "GetExampleInput",
        "required": ["example_id"],
        "properties": {"example_id", "include_readme"},
    },
    "get_code_snippet": {
        "title": "GetCodeSnippetInput",
        "required": [],
        "properties": {
            "class_name",
            "content_type",
            "intent",
            "line_end",
            "line_start",
            "max_lines",
            "module",
            "path",
            "pipecat_version",
            "symbol",
        },
    },
    "search_api": {
        "title": "SearchApiInput",
        "required": ["query"],
        "properties": {
            "calls",
            "chunk_type",
            "class_name",
            "is_dataclass",
            "limit",
            "module",
            "pipecat_version",
            "query",
            "version_filter",
            "yields",
        },
    },
    "check_deprecation": {
        "title": "CheckDeprecationInput",
        "required": ["symbol"],
        "properties": {"symbol", "version"},
    },
    "get_hub_status": {"title": "GetHubStatusInput", "required": [], "properties": set()},
}

GENERIC_ERROR_SERVER = textwrap.dedent(
    """
    import asyncio
    from mcp import types
    from mcp.server.lowlevel import Server
    from pipecat_context_hub.server.transport import run_stdio

    async def list_tools(_ctx, _params):
        return types.ListToolsResult(tools=[types.Tool(
            name="explode", description="registered exception probe",
            inputSchema={"type": "object"},
        ), types.Tool(
            name="unicode", description="registered UTF-8 probe",
            inputSchema={"type": "object"},
        )])

    async def call_tool(_ctx, params):
        if params.name == "unicode":
            return types.CallToolResult(content=[types.TextContent(
                type="text", text="東京 é",
            )])
        raise RuntimeError("registered generic handler boom")

    asyncio.run(run_stdio(Server(
        name="wire-generic-error", version="test",
        on_list_tools=list_tools, on_call_tool=call_tool,
    )))
    """
)

NO_STORE_SERVER = textwrap.dedent(
    """
    import asyncio
    from unittest.mock import MagicMock
    from pipecat_context_hub.server.main import create_server
    from pipecat_context_hub.server.transport import run_stdio

    asyncio.run(run_stdio(create_server(MagicMock())))
    """
)


@pytest.fixture(scope="session")
def seeded_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create the smallest real index that lets ``serve`` reach MCP."""
    home = tmp_path_factory.mktemp("mcp_v2_home")
    data_dir = home / ".pipecat-context-hub"
    store = IndexStore(StorageConfig(data_dir=data_dir))
    try:
        record = ChunkedRecord(
            chunk_id="mcp-v2-seed",
            content="seed record for MCP 2.x wire compatibility tests",
            content_type="doc",
            source_url="https://docs.pipecat.ai/seed",
            path="/seed",
            indexed_at=datetime.now(tz=timezone.utc),
            metadata={"title": "MCP compatibility seed"},
        )
        writer = EmbeddingIndexWriter(store, EmbeddingService(EmbeddingConfig()))
        asyncio.run(writer.upsert([record]))
    finally:
        store.close()
    return home


class JsonRpcProcess:
    """Small pipe client so this test exercises the real stdio transport."""

    def __init__(self, home: Path, *, script: str | None = None) -> None:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "PIPECAT_HUB_WARMUP": "0",
                "PIPECAT_HUB_RERANKER_ENABLED": "0",
                "PIPECAT_HUB_PARENT_WATCH_INTERVAL": "3600",
            }
        )
        command = (
            [sys.executable, "-c", script]
            if script is not None
            else [sys.executable, "-m", "pipecat_context_hub.cli", "serve"]
        )
        self.process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._next_id = 1
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_lines, daemon=True)
        self._reader.start()
        self._last_raw_line: bytes | None = None

    def _read_lines(self) -> None:
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            self._lines.put(line or None)
            if not line:
                return

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        return self._read_response(request_id)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def _write(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def _read_response(self, request_id: int) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + 45
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out waiting for JSON-RPC id {request_id}: {self._stderr_tail()}"
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise AssertionError(
                    f"timed out waiting for JSON-RPC id {request_id}: {self._stderr_tail()}"
                ) from exc
            if not line:
                raise AssertionError(
                    f"serve closed stdout before JSON-RPC id {request_id}: {self._stderr_tail()}"
                )
            self._last_raw_line = line
            message = json.loads(line.decode("utf-8"))
            if message.get("id") == request_id:
                return message

    def _stderr_tail(self) -> str:
        if self.process.stderr is None:
            return ""
        return self.process.stderr.read1(4000).decode("utf-8", errors="replace")


def _result_text(response: dict[str, Any]) -> str:
    result = response.get("result")
    assert isinstance(result, dict), response
    content = result.get("content")
    assert isinstance(content, list) and content, response
    text = content[0].get("text")
    assert isinstance(text, str), response
    return text


def _call(client: JsonRpcProcess, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.request("tools/call", {"name": name, "arguments": arguments})
    assert "error" not in response, response
    return response


def test_real_mcp_v2_stdio_contract(seeded_home: Path) -> None:
    """Pin the real initialize/list/call/ping wire contract under MCP 2.x."""
    client = JsonRpcProcess(seeded_home)
    try:
        initialized = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "wire-regression", "version": "1"},
            },
        )
        assert initialized["result"]["serverInfo"]["name"] == "pipecat-context-hub"
        assert initialized["result"]["capabilities"]["tools"] == {"listChanged": False}
        client.notify("notifications/initialized")

        listed = client.request("tools/list", {})
        tools = listed["result"]["tools"]
        assert {tool["name"] for tool in tools} == set(EXPECTED_TOOL_SCHEMAS)
        for tool in tools:
            oracle = EXPECTED_TOOL_SCHEMAS[tool["name"]]
            assert tool["inputSchema"]["title"] == oracle["title"]
            assert tool["inputSchema"].get("required", []) == oracle["required"]
            assert tool["inputSchema"]["type"] == "object"
            assert set(tool["inputSchema"]["properties"]) == oracle["properties"]

        ping = client.request("ping", {})
        assert ping["result"] == {}

        calls = {
            "get_doc": {"path": "/seed"},
            "check_deprecation": {"symbol": "Pipeline"},
            "get_hub_status": {},
        }
        for name, arguments in calls.items():
            response = _call(client, name, arguments)
            payload = json.loads(_result_text(response))
            assert isinstance(payload, dict), (name, response)

        # These handlers are reached through the same real MCP dispatch path;
        # invalid arguments keep the wire test independent of a downloaded
        # embedding model and still pin the 2.x ValidationError result shape.
        for name in (
            "search_docs",
            "search_examples",
            "get_example",
            "get_code_snippet",
            "search_api",
        ):
            invalid_dispatch = _call(client, name, {})
            assert invalid_dispatch["result"]["isError"] is True
            assert invalid_dispatch["result"]["content"][0]["type"] == "text"

        # ensure_ascii=False is intentional: this is a UTF-8 wire assertion.
        unicode_response = _call(client, "check_deprecation", {"symbol": "東京 é"})
        assert json.loads(_result_text(unicode_response))["deprecated"] is False

        invalid = _call(client, "get_doc", {})
        assert invalid["result"]["isError"] is True
        assert "Either doc_id or path must be provided" in _result_text(invalid)

        unknown = client.request("tools/call", {"name": "definitely_unknown_tool", "arguments": {}})
        assert "error" in unknown
        assert "Unknown tool: definitely_unknown_tool" in unknown["error"]["message"]
        assert unknown["error"]["code"] == 0  # locked mcp==2.2.0 canary
    finally:
        client.close()


def test_registered_generic_handler_exception_is_reachable_over_wire(seeded_home: Path) -> None:
    """A known tool raising a generic exception remains a wire-level error."""
    client = JsonRpcProcess(seeded_home, script=GENERIC_ERROR_SERVER)
    try:
        client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        client.notify("notifications/initialized")
        response = client.request("tools/call", {"name": "explode", "arguments": {}})
        assert response["error"]["message"] == "registered generic handler boom"
        assert response["error"]["code"] == 0  # locked mcp==2.2.0 canary
        unicode_response = client.request("tools/call", {"name": "unicode", "arguments": {}})
        assert _result_text(unicode_response) == "東京 é"
        assert client._last_raw_line is not None
        assert "東京 é".encode("utf-8") in client._last_raw_line
        assert b"\\u6771" not in client._last_raw_line
    finally:
        client.close()


def test_server_without_store_omits_hub_status_tool_over_wire(seeded_home: Path) -> None:
    """The no-store branch is verified through the real MCP stdio dispatch."""
    client = JsonRpcProcess(seeded_home, script=NO_STORE_SERVER)
    try:
        client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        client.notify("notifications/initialized")
        listed = client.request("tools/list", {})
        assert "get_hub_status" not in {tool["name"] for tool in listed["result"]["tools"]}
    finally:
        client.close()
