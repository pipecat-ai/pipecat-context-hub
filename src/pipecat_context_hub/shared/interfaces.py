"""Service interface protocols for the Pipecat Context Hub.

Parallel agents code against these protocols and use mocks in unit tests.
Concrete implementations are wired together during integration (T8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

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

    async def hybrid_search(self, query: IndexQuery) -> tuple[list[IndexResult], list[IndexResult]]:
        """Return vector and keyword results from one generation when supported."""
        ...


class IndexStatusReader(IndexReader, Protocol):
    """Read-only index surface needed by status and one-shot query callers."""

    @property
    def data_dir(self) -> Path:
        """Return the persisted index directory."""
        ...

    def get_all_metadata(self) -> dict[str, str]:
        """Return persisted index metadata without changing it."""
        ...

    def get_index_stats(self) -> dict[str, object]:
        """Return persisted index counts and provenance."""
        ...

    def close(self) -> None:
        """Release read handles."""
        ...


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
