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

    Two cases raise this:

    * **Pre-1.0 directory** — the pre-open sysdb-migration probe detects a
      chromadb 0.6 on-disk format (``detected_sysdb_migration`` set).
    * **Poisoned collection config** — a 1.x directory whose persisted
      collection configuration an older chromadb (0.6) wrote into, leaving a
      ``config_json_str`` that 1.x cannot parse (``KeyError '_type'``). The
      migration probe can't see this (the ``migrations`` table is still 1.x;
      the damage is in the ``collections`` row), so ``_open_client`` catches
      the parse failure and raises this with a custom ``reason``.
    """

    def __init__(
        self,
        chroma_path: Path | str,
        detected_sysdb_migration: int | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        self.chroma_path = Path(chroma_path)
        self.detected_sysdb_migration = detected_sysdb_migration
        if reason is not None:
            body = reason
        else:
            detail = (
                f" (found pre-1.0 sysdb schema migration {detected_sysdb_migration}; "
                "chromadb 1.x requires migration 10+)"
                if detected_sysdb_migration is not None
                else ""
            )
            body = (
                "this index was written by chromadb 0.6 (pre-1.0 on-disk format) and "
                f"cannot be opened by chromadb 1.x{detail}"
            )
        super().__init__(
            f"Incompatible ChromaDB index format at {self.chroma_path}: {body}. "
            f"{RESET_INDEX_REMEDIATION}"
        )
