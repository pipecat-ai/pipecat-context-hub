"""Pipecat Context Hub — local-first MCP server for Pipecat docs and examples."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pipecat-ai-context-hub")
except PackageNotFoundError:
    # Running from a source tree with no installed distribution. Deliberately
    # not a version-shaped string: "0.0.0" would read as a real release to
    # anything comparing versions, and silently comparing wrong is worse than
    # failing to compare at all.
    __version__ = "unknown"

__all__ = ["__version__"]
