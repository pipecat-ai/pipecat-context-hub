#!/usr/bin/env python3
"""Reproducible compatibility probe for the MCP 2.x low-level API."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Any

import anyio
from pydantic import BaseModel

_LOCKED_VERSION = "2.2.0"
_LOCKED_SOURCE_SHA256 = "77060e4d52aab6750fda88f679970788133e5ddbb94ab78a1a177de0d07575e0"
_UTF8_TEXT = "é 東京"


class _RequiredArguments(BaseModel):
    count: int


def _package_identity() -> dict[str, Any]:
    distribution = importlib.metadata.distribution("mcp")
    import mcp

    package_root = Path(mcp.__file__).parent
    digest = hashlib.sha256()
    files = sorted(package_root.rglob("*.py"))
    for path in files:
        digest.update(path.relative_to(package_root).as_posix().encode())
        digest.update(path.read_bytes())
    return {
        "version": distribution.version,
        "distribution": str(distribution.locate_file("")),
        "package_root": str(package_root),
        "source_files": len(files),
        "source_sha256": digest.hexdigest(),
    }


def _wire_dump(message: Any) -> dict[str, Any]:
    model = getattr(message, "message", message)
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _wire_line(message: Any) -> str:
    return json.dumps(_wire_dump(message), ensure_ascii=False, separators=(",", ":"))


def _request(
    jsonrpc: Any, request_id: int, method: str, params: dict[str, Any] | None = None
) -> Any:
    return jsonrpc.JSONRPCRequest(jsonrpc="2.0", id=request_id, method=method, params=params)


def _notification(jsonrpc: Any, method: str) -> Any:
    return jsonrpc.JSONRPCNotification(jsonrpc="2.0", method=method)


def _build_server() -> Any:
    from mcp import types
    from mcp.server.lowlevel import Server

    tools = [
        types.Tool(
            name="echo",
            description="Echo a UTF-8 string.",
            inputSchema={"type": "object", "properties": {"text": {"type": "string"}}},
        ),
        types.Tool(
            name="validate",
            description="Require an integer.",
            inputSchema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
        ),
        types.Tool(name="generic_error", inputSchema={"type": "object"}),
    ]

    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        arguments = params.arguments or {}
        if params.name == "echo":
            return types.CallToolResult(
                content=[types.TextContent(text=str(arguments.get("text", "")))]
            )
        if params.name == "validate":
            parsed = _RequiredArguments.model_validate(arguments)
            return types.CallToolResult(content=[types.TextContent(text=str(parsed.count))])
        if params.name == "generic_error":
            raise RuntimeError("registered generic probe failure")
        raise ValueError(f"Unknown tool: {params.name}")

    async def on_ping(_ctx: Any, _params: Any) -> types.EmptyResult:
        return types.EmptyResult()

    return Server(
        "mcp-2x-probe",
        version="probe",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_ping=on_ping,
    )


async def _memory_round_trip() -> dict[str, Any]:
    """Run the real low-level dispatcher over an in-memory stream pair."""
    from mcp.shared.message import SessionMessage
    from mcp_types import jsonrpc

    server = _build_server()
    client_send, server_receive = anyio.create_memory_object_stream[Any](20)
    server_send, client_receive = anyio.create_memory_object_stream[Any](20)
    frames: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        frames.append(_wire_dump(message))
        await client_send.send(SessionMessage(message))

    async def receive_response(request_id: int) -> dict[str, Any]:
        while True:
            response = await client_receive.receive()
            frames.append(_wire_dump(response))
            if frames[-1].get("id") == request_id:
                return frames[-1]

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            server.run, server_receive, server_send, server.create_initialization_options()
        )
        await send(
            _request(
                jsonrpc,
                1,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "probe"},
                },
            )
        )
        initialize = await receive_response(1)
        await send(_notification(jsonrpc, "notifications/initialized"))
        await send(_request(jsonrpc, 2, "tools/list"))
        tools = await receive_response(2)
        await send(
            _request(jsonrpc, 3, "tools/call", {"name": "echo", "arguments": {"text": _UTF8_TEXT}})
        )
        echo = await receive_response(3)
        await send(_request(jsonrpc, 4, "ping"))
        ping = await receive_response(4)
        errors: dict[str, dict[str, Any]] = {}
        for request_id, label, name, arguments in (
            (100, "unknown_tool", "missing", {}),
            (101, "generic_exception", "generic_error", {}),
            (102, "validation_error", "validate", {"count": "not-an-int"}),
        ):
            await send(
                _request(jsonrpc, request_id, "tools/call", {"name": name, "arguments": arguments})
            )
            errors[label] = await receive_response(request_id)
        await client_send.aclose()
        await server_send.aclose()

    assert initialize["result"]["serverInfo"]["name"] == "mcp-2x-probe"
    assert tools["result"]["tools"][0]["inputSchema"]["properties"]["text"]["type"] == "string"
    assert echo["result"]["content"][0]["text"] == _UTF8_TEXT
    assert ping["result"] == {}
    assert errors["unknown_tool"] == {
        "jsonrpc": "2.0",
        "id": 100,
        "error": {"code": 0, "message": "Unknown tool: missing"},
    }
    assert errors["generic_exception"] == {
        "jsonrpc": "2.0",
        "id": 101,
        "error": {"code": 0, "message": "registered generic probe failure"},
    }
    assert errors["validation_error"]["error"] == {
        "code": -32602,
        "message": "Invalid request parameters",
        "data": "",
    }
    return {
        "frames": frames,
        "initialize": initialize,
        "tools": tools,
        "echo": echo,
        "ping": ping,
        "errors": errors,
    }


async def _stdio_round_trip() -> dict[str, Any]:
    """Run real stdio with caller-owned explicit UTF-8 streams."""
    from mcp.server.stdio import stdio_server
    from mcp_types import jsonrpc

    server = _build_server()
    incoming_read_fd, incoming_write_fd = os.pipe()
    incoming = io.TextIOWrapper(
        os.fdopen(incoming_read_fd, "rb", buffering=0), encoding="utf-8", newline="\n"
    )
    outgoing_buffer = io.BytesIO()
    outgoing = io.TextIOWrapper(outgoing_buffer, encoding="utf-8", newline="\n")
    stdin_before, stdout_before = __import__("sys").stdin, __import__("sys").stdout
    requests = [
        _request(
            jsonrpc,
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "probe"},
            },
        ),
        _notification(jsonrpc, "notifications/initialized"),
        _request(jsonrpc, 3, "tools/call", {"name": "echo", "arguments": {"text": _UTF8_TEXT}}),
    ]
    async_in = anyio.wrap_file(incoming)
    async_out = anyio.wrap_file(outgoing)
    async with stdio_server(stdin=async_in, stdout=async_out) as (read_stream, write_stream):
        writer_done = threading.Event()

        def write_requests() -> None:
            with os.fdopen(incoming_write_fd, "wb", buffering=0) as writer:
                writer.write(
                    ("\n".join(_wire_line(request) for request in requests) + "\n").encode("utf-8")
                )
                writer.flush()
                writer_done.wait()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server.run, read_stream, write_stream, server.create_initialization_options()
            )
            task_group.start_soon(anyio.to_thread.run_sync, write_requests)
            for _ in range(500):
                await anyio.sleep(0.01)
                if (
                    b'"id":3' in outgoing_buffer.getvalue()
                    and _UTF8_TEXT.encode("utf-8") in outgoing_buffer.getvalue()
                ):
                    break
            else:
                raise AssertionError("stdio echo response was not observed")
            writer_done.set()
    outgoing.flush()
    raw_output = outgoing_buffer.getvalue()
    lines = [line for line in raw_output.decode("utf-8").splitlines() if line]
    frames = [json.loads(line) for line in lines]
    assert frames[0]["result"]["serverInfo"]["name"] == "mcp-2x-probe"
    assert any(
        frame.get("id") == 3
        and frame.get("result", {}).get("content", [{}])[0].get("text") == _UTF8_TEXT
        for frame in frames
    ), frames
    assert all("\ufffd" not in line for line in lines)
    result = {
        "frames": frames,
        "raw_utf8": raw_output.decode("utf-8"),
        "encoding": {"stdin": incoming.encoding, "stdout": outgoing.encoding},
        "utf8_bytes": _UTF8_TEXT.encode("utf-8").hex(),
        "caller_owns_wrappers_after_context": not incoming.closed and not outgoing.closed,
        "stdio_objects_unchanged": __import__("sys").stdin is stdin_before
        and __import__("sys").stdout is stdout_before,
    }
    assert result["caller_owns_wrappers_after_context"]
    assert result["stdio_objects_unchanged"]
    incoming.close()
    outgoing.close()
    return result


async def _probe_once() -> dict[str, Any]:
    from mcp.server.lowlevel import Server
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    package = _package_identity()
    assert Version(package["version"]) in SpecifierSet(">=2.0,<3.0")
    if package["version"] == _LOCKED_VERSION:
        assert package["source_sha256"] == _LOCKED_SOURCE_SHA256
        assert importlib.metadata.version("mcp-types") == package["version"]
    return {
        "package": package,
        "api": {
            "server_constructor": str(inspect.signature(Server)),
            "run": str(inspect.signature(Server.run)),
            "initialization_options": str(inspect.signature(Server.create_initialization_options)),
        },
        "tool_constructor": "mcp.types.Tool(name=..., inputSchema=...) accepted",
        "memory": await _memory_round_trip(),
        "stdio": await _stdio_round_trip(),
        "watchdog": {
            "probe_scope": "SDK transport lifecycle only; hub watchdog remains test_serve_lifetime.py",
            "graceful_unwind": "server.run returned after explicit stdio EOF",
        },
    }


def _matrix_command(script: Path, requirement: str) -> list[str]:
    return [
        "uv",
        "run",
        "--isolated",
        "--no-project",
        "--with",
        requirement,
        "--with",
        "anyio",
        "--with",
        "pydantic",
        "--with",
        "packaging",
        "python",
        str(script),
        "--single",
    ]


def _run_matrix(script: Path) -> int:
    specs = [("floor_2x", "mcp==2.0.*"), ("newest_2x", "mcp>=2,<3")]
    print(json.dumps({"matrix": "locked", "result": anyio.run(_probe_once)}, ensure_ascii=False))
    for label, requirement in specs:
        command = _matrix_command(script, requirement)
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        print(json.dumps({"matrix": label, "returncode": completed.returncode}))
        if completed.stdout:
            print(completed.stdout.rstrip())
        if completed.stderr:
            print(json.dumps({"matrix": label, "stderr": completed.stderr[-2000:]}))
        if completed.returncode:
            return completed.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--single", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.matrix:
        return _run_matrix(Path(__file__).resolve())
    print(json.dumps(anyio.run(_probe_once), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
