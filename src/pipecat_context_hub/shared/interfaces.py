"""Service interface protocols for the Pipecat Context Hub.

Parallel agents code against these protocols and use mocks in unit tests.
Concrete implementations are wired together during integration (T8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pipecat_context_hub.shared.types import (
    ChunkedRecord,
    GetCodeSnippetInput,
    GetCodeSnippetOutput,
    GetDocInput,
    GetDocOutput,
    GetExampleInput,
    GetExampleOutput,
    IndexQuery,
    IndexResult,
    IngestResult,
    SearchApiInput,
    SearchApiOutput,
    SearchDocsInput,
    SearchDocsOutput,
    SearchExamplesInput,
    SearchExamplesOutput,
)

# ---------------------------------------------------------------------------
# Index service protocols
# ---------------------------------------------------------------------------

# Synthetic key ``get_counts_by_repo`` adds for crawled doc chunks (they have
# no repo). Not a repo: readers that list repos must skip it.
DOCS_COUNT_KEY = "docs.pipecat.ai"


class IndexWriter(Protocol):
    """Writes chunked records into the index store."""

    async def upsert(self, records: list[ChunkedRecord]) -> int:
        """Insert or update records. Returns count of records written."""
        ...

    async def delete_by_source(self, source_url: str) -> int:
        """Delete all records from a given source. Returns count deleted."""
        ...


class IndexReader(Protocol):
    """Reads from the index store."""

    async def vector_search(self, query: IndexQuery) -> list[IndexResult]:
        """Return results ranked by embedding similarity."""
        ...

    async def keyword_search(self, query: IndexQuery) -> list[IndexResult]:
        """Return results ranked by FTS5 keyword relevance."""
        ...


class ServingIndex(IndexReader, Protocol):
    """The read surface ``serve`` exposes to tool handlers.

    Satisfied by ``IndexStore`` and by ``serve``'s reopening handle.
    """

    @property
    def data_dir(self) -> Path:
        """Path to the index data directory."""
        ...

    def get_index_stats(self) -> dict[str, Any]:
        """Return index statistics (counts by type, total, commit SHAs)."""
        ...

    def get_all_metadata(self) -> dict[str, str]:
        """Return all persistent index metadata as a dict."""
        ...

    def get_counts_by_repo(self) -> dict[str, int]:
        """Return the number of indexed records per repo.

        Keys are each repo's configured slug verbatim (not casefolded), plus the
        synthetic :data:`DOCS_COUNT_KEY` for doc chunks when any exist; that key
        is not a repo.
        """
        ...


class FatalIndexError(RuntimeError):
    """The on-disk index is unsafe to keep using (incompatible or corrupt).

    Raised by an ``IndexWriter`` (or on open). Ingesters must re-raise it
    rather than record it as one more per-source error, so a refresh stops
    instead of writing every remaining source into a damaged index.
    """


class IndexUnavailableError(RuntimeError):
    """The index cannot serve this call right now; ``str(exc)`` says why.

    The message is meant for the calling agent (e.g. "retry in 60-120
    seconds" while a refresh rewrites the index, or how to rebuild an empty
    or unreadable index).
    """


# ---------------------------------------------------------------------------
# Retrieval service protocol
# ---------------------------------------------------------------------------


class Retriever(Protocol):
    """High-level retrieval interface consumed by MCP tool handlers."""

    async def search_docs(self, input: SearchDocsInput) -> SearchDocsOutput: ...

    async def get_doc(self, input: GetDocInput) -> GetDocOutput: ...

    async def search_examples(self, input: SearchExamplesInput) -> SearchExamplesOutput: ...

    async def get_example(self, input: GetExampleInput) -> GetExampleOutput: ...

    async def get_code_snippet(self, input: GetCodeSnippetInput) -> GetCodeSnippetOutput: ...

    async def search_api(self, input: SearchApiInput) -> SearchApiOutput: ...


# ---------------------------------------------------------------------------
# Ingestion service protocol
# ---------------------------------------------------------------------------


class Ingester(Protocol):
    """Ingestion interface for crawling a source and writing to the index."""

    async def ingest(self) -> IngestResult:
        """Run a full ingestion pass."""
        ...
