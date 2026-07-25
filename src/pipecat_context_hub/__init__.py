"""Pipecat Context Hub — local-first MCP server for Pipecat docs and examples."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pipecat-ai-context-hub")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

__all__ = ["__version__"]
