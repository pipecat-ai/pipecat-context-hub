# Vector Backend Selection: ChromaDB

**Decision:** Use ChromaDB as the local vector index backend for v0.

**Date:** 2026-02-18

## Options Considered

| Backend | Local-first | Python-native | Persistence | Embedding support | Maturity |
|---|---|---|---|---|---|
| **ChromaDB** | Yes | Yes | Yes (local dir) | Built-in sentence-transformers | High |
| LanceDB | Yes | Yes | Yes (Lance format) | Via external models | Medium |
| sqlite-vec | Yes | Yes (C ext) | Yes (SQLite) | No (BYO vectors) | Early |
| FAISS | Yes | Yes (C++ binding) | Manual save/load | No (BYO vectors) | High |
| Qdrant (local) | Yes | Yes (Rust binary) | Yes | No (BYO vectors) | High |

## Rationale

1. **Zero-config local persistence.** ChromaDB persists to a local directory with no external process required. Aligns with the "local-first hackathon" v0 requirement.

2. **Built-in embedding support.** ChromaDB integrates with `sentence-transformers` out of the box, so ingestion can embed and store in one call. No separate embedding pipeline needed in v0.

3. **Metadata filtering.** ChromaDB supports `where` clauses on metadata fields (`repo`, `content_type`, `path`), which maps directly to the `IndexQuery.filters` contract.

4. **Python-native.** No Rust/C++ compilation step. `pip install chromadb` works on macOS/Linux without system dependencies.

5. **Ecosystem maturity.** Large community, well-documented, stable API.

## Trade-offs

- **Heavier dependency** than sqlite-vec (~200MB with sentence-transformers). Acceptable for a dev tool, not ideal for ultra-lightweight distribution.
- **Not as fast as FAISS** for large-scale vector search. Not a concern at v0 scale (tens of thousands of chunks, not millions).
- **Coupling risk.** ChromaDB's internal storage format is opaque. Mitigated by coding against `IndexWriter`/`IndexReader` protocols so the backend can be swapped in v1.

## v1 Considerations

If v1 needs lighter packaging or higher throughput, consider:
- **sqlite-vec** for a single-file solution that shares the SQLite FTS5 database.
- **LanceDB** for columnar storage with better large-scale performance.

The `IndexWriter`/`IndexReader` protocol abstraction makes this swap straightforward.
