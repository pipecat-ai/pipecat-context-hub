"""Render a portable local plugin copy using the installed Hub interpreter."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_MCP_SERVER_NAME = "pipecat-context-hub-chatgpt-plugin"


def prepare_local(destination: Path) -> Path:
    """Copy only package resources and pin the safe installed launch command."""
    source = Path(__file__).resolve().parents[1]
    checkout = next((p for p in source.parents if (p / ".git").exists()), source)
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise ValueError("Destination must not be a symlink")
    destination = destination.resolve()
    if destination == checkout or checkout in destination.parents:
        raise ValueError("Destination must be outside the checkout")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Destination must be absent or an empty directory")

    # Do not resolve executable symlinks: that can escape a virtual environment.
    interpreter = Path(sys.executable).absolute()
    executable = str(interpreter)
    probe = subprocess.run(  # nosec B603 - fixed interpreter/argv, no shell
        [executable, "-P", "-c", "import pipecat_context_hub"],
        cwd=tempfile.gettempdir(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if probe.returncode:
        raise ValueError("Run the renderer with the installed Context Hub Python")

    files = [source / name for name in ("plugin.json", "mcp.template.json", "README.md")]
    files.append(source / "scripts" / "prepare_local.py")
    files.extend(
        source / "assets" / name
        for name in (
            "cat-mark.svg",
            "cat-mark-dark.svg",
            "context-hub-logo.svg",
            "context-hub-logo-dark.svg",
        )
    )
    files.extend(sorted((source / "skills").glob("*/SKILL.md")))
    for path in files:
        if not path.is_file() or any(
            part.is_symlink() for part in (path, *path.parents) if part != source.parent
        ):
            raise ValueError(f"Package resource must be a regular file: {path.name}")
    config = json.loads((source / "mcp.template.json").read_text())
    servers = config["mcpServers"]
    if set(servers) != {PLUGIN_MCP_SERVER_NAME}:
        raise ValueError("Unexpected packaged MCP server identity")
    server = servers[PLUGIN_MCP_SERVER_NAME]
    if server != {
        "type": "stdio",
        "command": "__HUB_PYTHON_NAME__",
        "args": ["-P", "-m", "pipecat_context_hub", "serve"],
        "env": {"PATH": "__HUB_PYTHON_DIRECTORY__"},
    }:
        raise ValueError("Unexpected MCP launch template")
    server["command"] = interpreter.name
    server["env"]["PATH"] = str(interpreter.parent)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hub-plugin-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "package"
        staged.mkdir()
        for path in files:
            target = staged / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        (staged / "mcp.json").write_text(json.dumps(config, indent=2) + "\n")
        if destination.exists():
            destination.rmdir()  # Refuses if anything appeared since preflight.
        staged.rename(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        destination = prepare_local(args.destination)
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(f"Prepared {destination}; no client registration or index refresh performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
