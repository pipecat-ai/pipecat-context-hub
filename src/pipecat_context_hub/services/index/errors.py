"""Typed errors for the index service layer."""

from __future__ import annotations

from pathlib import Path

# Shared operator-facing remediation. Phase 6 of the chromadb 1.x migration
# asserts this literal command string appears in serve/refresh error output, so
# keep the exact ``refresh --force --reset-index`` substring intact.
RESET_INDEX_REMEDIATION = (
    "Rebuild the local index with: uv run pipecat-context-hub refresh --force --reset-index"
)


class IncompatibleIndexFormatError(RuntimeError):
    """A persisted Chroma directory uses an on-disk format 1.x cannot open.

    chromadb 1.x is not backward-compatible with the pre-1.0 (chromadb 0.6-era)
    on-disk format. Opening such a directory with ``PersistentClient`` raises an
    opaque ``InternalError`` (and may rewrite state), so the index layer probes
    the SQLite schema first and raises this typed error with a clear upgrade
    path instead.
    """

    def __init__(
        self, chroma_path: Path | str, detected_sysdb_migration: int | None = None
    ) -> None:
        self.chroma_path = Path(chroma_path)
        self.detected_sysdb_migration = detected_sysdb_migration
        detail = (
            f" (found pre-1.0 sysdb schema migration {detected_sysdb_migration}; "
            "chromadb 1.x requires migration 10+)"
            if detected_sysdb_migration is not None
            else ""
        )
        super().__init__(
            f"Incompatible ChromaDB index format at {self.chroma_path}: this index was "
            f"written by chromadb 0.6 (pre-1.0 on-disk format) and cannot be opened by "
            f"chromadb 1.x{detail}. {RESET_INDEX_REMEDIATION}"
        )
