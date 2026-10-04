"""Regression coverage for the portable desktop plugin renderer."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _ROOT / "plugins" / "pipecat-context-hub"
_SERVER_NAME = "pipecat-context-hub-chatgpt-plugin"


@pytest.fixture
def renderer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "chatgpt_plugin_setup", _SOURCE / "scripts" / "prepare_local.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    probe = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(module.subprocess, "run", probe)
    return module


def _copy_source(renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source" / "plugins" / "pipecat-context-hub"
    shutil.copytree(_SOURCE, source)
    monkeypatch.setattr(renderer, "__file__", str(source / "scripts" / "prepare_local.py"))
    return source


def test_render_uses_only_unique_connection_and_preserves_interpreter(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "environment" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    real_python = tmp_path / "base-python"
    real_python.touch()
    executable.symlink_to(real_python)
    monkeypatch.setattr(renderer.sys, "executable", str(executable))
    template_before = (_SOURCE / "mcp.template.json").read_bytes()

    destination = tmp_path / "prepared"
    assert renderer.prepare_local(destination) == destination
    config = json.loads((destination / "mcp.json").read_text())
    assert set(config["mcpServers"]) == {_SERVER_NAME}
    server = config["mcpServers"][_SERVER_NAME]
    assert server == {
        "type": "stdio",
        "command": executable.name,
        "args": ["-P", "-m", "pipecat_context_hub", "serve"],
        "env": {"PATH": str(executable.parent)},
    }
    assert "__HUB_" not in (destination / "mcp.json").read_text()
    assert server["env"]["PATH"] != str(real_python.parent)
    assert renderer.subprocess.run.call_args.args[0] == [
        str(executable),
        "-P",
        "-c",
        "import pipecat_context_hub",
    ]
    assert (_SOURCE / "mcp.template.json").read_bytes() == template_before
    assert (destination / "mcp.template.json").read_bytes() == template_before


def test_render_excludes_evaluation_and_unrelated_files(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _copy_source(renderer, tmp_path, monkeypatch)
    (source / "evaluation.md").write_text("Repository-only report")
    (source / "scratch.txt").write_text("Unrelated local notes")
    destination = renderer.prepare_local(tmp_path / "prepared")
    copied = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
    assert copied == {
        "plugin.json",
        "mcp.template.json",
        "mcp.json",
        "README.md",
        "scripts/prepare_local.py",
        "skills/explore/SKILL.md",
    }
    assert not (_SOURCE / "evaluation.md").exists()
    assert (_ROOT / "docs/evaluations/pipecat-context-hub-plugin.md").is_file()


@pytest.mark.parametrize("mode", ["legacy", "extra"])
def test_render_rejects_legacy_or_additional_server_entries(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    source = _copy_source(renderer, tmp_path, monkeypatch)
    template = source / "mcp.template.json"
    config = json.loads(template.read_text())
    server = config["mcpServers"][_SERVER_NAME]
    if mode == "legacy":
        config["mcpServers"] = {"pipecat-context-hub": server}
    else:
        config["mcpServers"]["pipecat-context-hub"] = server
    template.write_text(json.dumps(config))
    destination = tmp_path / "prepared"
    with pytest.raises(ValueError, match="packaged MCP server identity"):
        renderer.prepare_local(destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("command", "python"),
        ("command", "/absolute/python"),
        ("args", ["-m", "pipecat_context_hub", "serve"]),
        ("env", {}),
        ("env", {"PATH": "__HUB_PYTHON_DIRECTORY__:/usr/bin"}),
        ("env", {"PATH": "__HUB_PYTHON_DIRECTORY__", "PYTHONPATH": "/shadow"}),
        ("cwd", "/shadow"),
        ("type", "http"),
    ],
)
def test_render_rejects_unpinned_or_unsafe_launch(
    renderer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: Any,
) -> None:
    source = _copy_source(renderer, tmp_path, monkeypatch)
    template = source / "mcp.template.json"
    config = json.loads(template.read_text())
    config["mcpServers"][_SERVER_NAME][key] = value
    template.write_text(json.dumps(config))
    destination = tmp_path / "prepared"
    with pytest.raises(ValueError, match="Unexpected MCP launch template"):
        renderer.prepare_local(destination)
    assert not destination.exists()


def test_render_preserves_nonempty_destination(renderer: ModuleType, tmp_path: Path) -> None:
    destination = tmp_path / "prepared"
    destination.mkdir()
    sentinel = destination / "existing.txt"
    sentinel.write_text("Keep this file")
    with pytest.raises(ValueError, match="absent or an empty directory"):
        renderer.prepare_local(destination)
    assert sentinel.read_text() == "Keep this file"
    assert list(destination.iterdir()) == [sentinel]
    renderer.subprocess.run.assert_not_called()


def test_render_refuses_checkout_destination(renderer: ModuleType) -> None:
    destination = _ROOT / "never-create-test-plugin"
    with pytest.raises(ValueError, match="outside the checkout"):
        renderer.prepare_local(destination)
    assert not destination.exists()
    renderer.subprocess.run.assert_not_called()


def test_render_missing_hub_leaves_no_output(renderer: ModuleType, tmp_path: Path) -> None:
    renderer.subprocess.run.return_value = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="Module unavailable"
    )
    destination = tmp_path / "prepared"
    with pytest.raises(ValueError, match="installed Context Hub Python"):
        renderer.prepare_local(destination)
    assert not destination.exists()
