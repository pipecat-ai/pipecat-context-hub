"""Typed errors for the index service layer."""

from __future__ import annotations

from pathlib import Path

from pipecat_context_hub.shared.interfaces import FatalIndexError, IndexUnavailableError

# Shared operator-facing remediation. Phase 6 of the chromadb 1.x migration
# asserts this literal command string appears in serve/refresh error output, so
# keep the exact ``refresh --force --reset-index`` substring intact.
RESET_INDEX_REMEDIATION = (
    "Rebuild the local index with: uv run pipecat-context-hub refresh --force --reset-index"
)


class IncompatibleIndexFormatError(FatalIndexError):
    """A persisted Chroma directory uses an on-disk format 1.x cannot open.

    chromadb 1.x is not backward-compatible with the pre-1.0 (chromadb 0.6-era)
    on-disk format. Opening such a directory with ``PersistentClient`` raises an
    opaque ``InternalError`` (and may rewrite state), so the index layer probes
    the SQLite schema first and raises this typed error with a clear upgrade
    path instead.

    Three cases raise this:

    * **Pre-1.0 directory** — the pre-open sysdb-migration probe detects a
      chromadb 0.6 on-disk format (``detected_sysdb_migration`` set).
    * **Poisoned collection config** — a 1.x directory whose persisted
      collection configuration an older chromadb (0.6) wrote into, leaving a
      ``config_json_str`` that 1.x cannot parse (``KeyError '_type'``). The
      migration probe can't see this (the ``migrations`` table is still 1.x;
      the damage is in the ``collections`` row), so ``_open_client`` catches
      the parse failure and raises this with a custom ``reason``.
    * **Corrupt persisted HNSW graph** — raised as the
      :class:`CorruptHnswGraphError` subclass, so callers can tell it from an
      upgrade problem (see that class).
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


class CorruptHnswGraphError(IncompatibleIndexFormatError):
    """A persisted HNSW graph is oversized or structurally damaged.

    Raised by the read-only check in ``hnsw_validation``: before the native
    client loads the graph (Chroma 1.5.x may otherwise SIGSEGV, or grow
    ``link_lists.bin`` without bound), and after each write while ``refresh``
    runs. The check does not repair the graph.
    """


class CompactionSwapError(FatalIndexError):
    """``compact()`` failed mid-swap and left no ``latest`` collection.

    The index is left as an interrupted compaction would leave it (no
    ``latest``; the live records under the backup name), so readers get
    :class:`InterruptedCompactionError` until ``refresh`` restores it. Fatal so
    that ``refresh`` exits non-zero instead of reporting a valid index.
    """

    def __init__(self, chroma_path: Path | str) -> None:
        self.chroma_path = Path(chroma_path)
        super().__init__(
            f"Vector index compaction at {self.chroma_path} failed mid-swap and could not "
            "restore the original collection; the index is unreadable until it is restored. "
            "Run 'uv run pipecat-context-hub refresh' to restore it (no rebuild needed)."
        )


class CompactionRestoreError(FatalIndexError):
    """``refresh`` found an interrupted compaction and could not restore it.

    Raised by the open (``VectorIndex(..., recover=True)``), not by
    ``compact()``: the restore rename failed and ``latest`` is still missing.
    Running ``refresh`` again would retry the same rename, so the remedy is a
    rebuild.
    """

    def __init__(self, chroma_path: Path | str) -> None:
        self.chroma_path = Path(chroma_path)
        super().__init__(
            f"The vector index at {self.chroma_path} was left mid-swap by an interrupted "
            "`refresh --force`, and restoring its collection failed. "
            f"{RESET_INDEX_REMEDIATION}"
        )


class InterruptedCompactionError(IndexUnavailableError):
    """A ``refresh --force`` compaction died between its two renames.

    The live records sit under the backup collection name. Only ``refresh``
    (which holds the exclusive refresh lock) restores them; a reader that
    finds this state gets this error instead of writing to the index.
    """

    def __init__(self, chroma_path: Path | str) -> None:
        self.chroma_path = Path(chroma_path)
        super().__init__(
            f"The vector index at {self.chroma_path} was left mid-swap by an interrupted "
            "`refresh --force`. Run 'uv run pipecat-context-hub refresh' to restore it "
            "(no rebuild needed)."
        )


class IndexNotBuiltError(IndexUnavailableError):
    """A reader found nothing indexed: neither half exists, or it has 0 records."""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        super().__init__(
            f"Index at {self.data_dir} is empty (0 records). Run 'uv run "
            "pipecat-context-hub refresh' to build it (the first run downloads models "
            "and indexes sources; allow a few minutes)."
        )


class MissingVectorCollectionError(IndexUnavailableError):
    """The vector half is missing or empty but must not be (re)created here.

    Raised by ``VectorIndex`` when its database or ``latest`` collection is
    missing and the open may not create one, and by ``IndexStore`` when
    ``latest`` is empty while the keyword half has records. ``refresh``
    without ``--force`` gets it too (exit 2): it would skip every unchanged
    source (their hashes live in the keyword index) and leave vector search
    silently empty. Nothing is created before it is raised.
    """

    def __init__(self, chroma_path: Path | str) -> None:
        self.chroma_path = Path(chroma_path)
        super().__init__(
            f"The vector index at {self.chroma_path} is missing or empty (its 'latest' "
            "collection was lost), so vector search would come back empty. Run 'uv run "
            "pipecat-context-hub refresh --force' to rebuild it."
        )


class MissingKeywordIndexError(IndexUnavailableError):
    """A reader found no initialized keyword (SQLite FTS) database to open.

    Readers never create one: an empty keyword index beside a populated
    vector index would make keyword search silently return nothing.
    """

    def __init__(self, sqlite_path: Path | str) -> None:
        self.sqlite_path = Path(sqlite_path)
        super().__init__(
            f"The keyword index at {self.sqlite_path} is missing or incomplete. Run 'uv run "
            "pipecat-context-hub refresh --force' to rebuild it."
        )


class CorruptKeywordIndexError(IndexUnavailableError):
    """The keyword (SQLite FTS) database exists but cannot be read.

    Not a database, malformed, or with a schema the index cannot use or
    complete (``FTSIndex`` raises it from its open). A failure of the moment
    (busy, locked, I/O, full disk, permissions) is not this: it propagates
    unchanged, since a rebuild would not fix it.
    """

    def __init__(self, sqlite_path: Path | str, detail: str) -> None:
        self.sqlite_path = Path(sqlite_path)
        super().__init__(
            f"The keyword index at {self.sqlite_path} is unreadable ({detail}). "
            f"{RESET_INDEX_REMEDIATION}"
        )
