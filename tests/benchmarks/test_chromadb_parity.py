"""ChromaDB migration parity harness — same query, same result across versions.

Phase 3 of the chromadb 0.6 -> 1.x migration: prove that bumping the chromadb
pin does not silently change retrieval results or the surfaced similarity score
(`score = 1 - distance/2`, consumed by the reranker's RRF merge — see risk #7).

Two layers are compared:

* **Layer A (raw chroma boundary):** top-K chunk IDs + cosine distances straight
  from `IndexStore.vector_search`. Distance is recovered exactly from the
  IndexResult score as ``2 * (1 - score)``.
* **Layer B (integration boundary):** `HybridRetriever.search_docs` hits — the
  doc IDs and surfaced similarity scores after RRF merge + cross-encoder rerank.

Workflow:

1. On chromadb 0.6 (v0.0.20), capture the reference against the live index::

     uv run python -m tests.benchmarks.test_chromadb_parity capture \
       /tmp/chroma-v0.0.20-parity-results.json

   (Run against the default ~/.pipecat-context-hub data dir, which holds BOTH
   the chroma vector index and the SQLite FTS index Layer B needs. Read-only —
   safe alongside a running `serve`.)

2. On the 1.x branch, after re-indexing the same corpus, run the comparison::

     PIPECAT_HUB_PARITY_REFERENCE=/tmp/chroma-v0.0.20-parity-results.json \
       uv run pytest tests/benchmarks/test_chromadb_parity.py -m benchmark -v -s

The pytest SKIPS when ``PIPECAT_HUB_PARITY_REFERENCE`` is unset (opt-in, like the
other benchmarks) but FAILS loudly when the env var points at a missing or
malformed reference file — a missing 0.6 reference is a Phase 3 blocker, not a
silent pass.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pipecat_context_hub.services.embedding import EmbeddingService
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever
from pipecat_context_hub.shared.config import EmbeddingConfig, StorageConfig
from pipecat_context_hub.shared.types import IndexQuery, SearchDocsInput

_REFERENCE_ENV = "PIPECAT_HUB_PARITY_REFERENCE"
_SCHEMA_VERSION = 1

# Number of top results captured/compared per query at each layer.
_TOP_K = 10

# Fixed query set committed to the repo so 0.6 and 1.x are scored on identical
# inputs. Spread across doc / example / API-style intents to exercise different
# regions of the embedding space.
QUERY_SET: tuple[str, ...] = (
    "how to configure text-to-speech in a pipecat pipeline",
    "speech-to-text with Deepgram",
    "function calling with an LLM service",
    "WebRTC transport with Daily",
    "handle user interruptions in a voice bot",
    "ElevenLabs voice synthesis setup",
    "OpenAI LLM service streaming",
    "custom frame processor subclass",
    "idle timeout and disconnect handling",
    "RTVI client server messaging",
    "Cartesia TTS configuration",
    "deploy a bot to serverless infrastructure",
    "pipeline runner and task lifecycle",
    "aggregating sentences from streaming text",
    "audio resampling and frame formats",
)

# --- Phase 3 pass thresholds (from the dev plan) ----------------------------
_LAYER_A_TOPK_JACCARD_MIN = 0.90
_LAYER_A_TOP1_MATCH_RATE_MIN = 0.95
_LAYER_A_TOP1_DISTANCE_TOL = 1e-6
_LAYER_B_SCORE_TOL = 0.01
_LAYER_B_TOPK_JACCARD_MIN = 0.85


@dataclass(frozen=True)
class _Stack:
    store: IndexStore
    embedding: EmbeddingService
    retriever: HybridRetriever


def _open_stack(data_dir: Path | None = None) -> _Stack:
    """Open the retrieval stack against *data_dir* (default ~/.pipecat-context-hub)."""
    storage = StorageConfig(data_dir=data_dir) if data_dir is not None else StorageConfig()
    store = IndexStore(storage)
    embedding = EmbeddingService(EmbeddingConfig())
    retriever = HybridRetriever(store, embedding)
    return _Stack(store=store, embedding=embedding, retriever=retriever)


async def _capture_query(stack: _Stack, query: str) -> dict[str, Any]:
    """Capture Layer A (raw chroma) and Layer B (HybridRetriever) for one query."""
    embedding = stack.embedding.embed_query(query)

    # Layer A — raw chroma boundary. No filters: pure vector ranking.
    index_query = IndexQuery(query_text=query, query_embedding=embedding, limit=_TOP_K)
    vector_results = await stack.store.vector_search(index_query)
    layer_a = [
        {"id": r.chunk.chunk_id, "distance": 2.0 * (1.0 - r.score)} for r in vector_results[:_TOP_K]
    ]

    # Layer B — integration boundary (RRF merge + cross-encoder rerank).
    docs_out = await stack.retriever.search_docs(SearchDocsInput(query=query, limit=_TOP_K))
    layer_b = [{"id": hit.doc_id, "score": hit.score} for hit in docs_out.hits[:_TOP_K]]

    return {"query": query, "layer_a": layer_a, "layer_b": layer_b}


async def _capture_all(stack: _Stack) -> dict[str, Any]:
    record_count = stack.store.get_index_stats().get("total")
    queries = [await _capture_query(stack, q) for q in QUERY_SET]
    return {
        "schema_version": _SCHEMA_VERSION,
        "top_k": _TOP_K,
        "record_count": record_count,
        "queries": queries,
    }


def capture_reference(out_path: Path, data_dir: Path | None = None) -> dict[str, Any]:
    """Capture the parity reference for the current index and write it to *out_path*."""
    stack = _open_stack(data_dir)
    try:
        report = asyncio.run(_capture_all(stack))
    finally:
        stack.store.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


# --- comparison helpers -----------------------------------------------------


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 1.0


def _ids(layer: list[dict[str, Any]]) -> list[str]:
    return [row["id"] for row in layer]


# --- pytest -----------------------------------------------------------------


def _load_reference() -> dict[str, Any]:
    ref_path = os.environ.get(_REFERENCE_ENV, "").strip()
    if not ref_path:
        pytest.skip(
            f"Set {_REFERENCE_ENV}=<0.6 reference json> to run the parity benchmark.",
            allow_module_level=True,
        )
    path = Path(ref_path)
    if not path.is_file():
        pytest.fail(
            f"{_REFERENCE_ENV} points at a missing reference file: {path}. "
            "Capture it on chromadb 0.6 first (see module docstring)."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        pytest.fail(f"Parity reference at {path} is unreadable/malformed: {exc}")
    if data.get("schema_version") != _SCHEMA_VERSION:
        pytest.fail(
            f"Parity reference schema_version={data.get('schema_version')} "
            f"!= expected {_SCHEMA_VERSION}"
        )
    return data


@pytest.mark.benchmark
class TestChromaParity:
    @pytest.fixture(scope="class")
    def reference(self) -> dict[str, Any]:
        return _load_reference()

    @pytest.fixture(scope="class")
    def current(self, reference: dict[str, Any]) -> dict[str, Any]:
        stack = _open_stack()
        try:
            return asyncio.run(_capture_all(stack))
        finally:
            stack.store.close()

    def test_layer_a_raw_chroma_parity(
        self, reference: dict[str, Any], current: dict[str, Any]
    ) -> None:
        ref_by_q = {q["query"]: q for q in reference["queries"]}
        jaccards: list[float] = []
        top1_matches = 0
        top1_distance_violations: list[str] = []

        for cur in current["queries"]:
            ref = ref_by_q.get(cur["query"])
            assert ref is not None, f"query missing from reference: {cur['query']}"

            ref_ids, cur_ids = _ids(ref["layer_a"]), _ids(cur["layer_a"])
            jaccards.append(_jaccard(ref_ids, cur_ids))

            if ref_ids and cur_ids and ref_ids[0] == cur_ids[0]:
                top1_matches += 1
                ref_d = ref["layer_a"][0]["distance"]
                cur_d = cur["layer_a"][0]["distance"]
                if abs(ref_d - cur_d) > _LAYER_A_TOP1_DISTANCE_TOL:
                    top1_distance_violations.append(
                        f"{cur['query']!r}: |{ref_d:.8f}-{cur_d:.8f}|={abs(ref_d - cur_d):.2e}"
                    )

        n = len(current["queries"])
        mean_jaccard = sum(jaccards) / len(jaccards)
        top1_rate = top1_matches / n

        print(
            f"\nLayer A: mean top-{_TOP_K} Jaccard={mean_jaccard:.3f} "
            f"top-1 match rate={top1_rate:.3f} ({top1_matches}/{n})"
        )
        assert mean_jaccard >= _LAYER_A_TOPK_JACCARD_MIN, (
            f"Layer A top-K Jaccard {mean_jaccard:.3f} < {_LAYER_A_TOPK_JACCARD_MIN}"
        )
        assert top1_rate >= _LAYER_A_TOP1_MATCH_RATE_MIN, (
            f"Layer A top-1 match rate {top1_rate:.3f} < {_LAYER_A_TOP1_MATCH_RATE_MIN}"
        )
        assert not top1_distance_violations, (
            "Layer A top-1 distance drift > "
            f"{_LAYER_A_TOP1_DISTANCE_TOL:.0e}: {top1_distance_violations}"
        )

    def test_layer_b_integration_parity(
        self, reference: dict[str, Any], current: dict[str, Any]
    ) -> None:
        ref_by_q = {q["query"]: q for q in reference["queries"]}
        jaccards: list[float] = []
        score_violations: list[str] = []

        for cur in current["queries"]:
            ref = ref_by_q.get(cur["query"])
            assert ref is not None, f"query missing from reference: {cur['query']}"

            ref_ids, cur_ids = _ids(ref["layer_b"]), _ids(cur["layer_b"])
            jaccards.append(_jaccard(ref_ids, cur_ids))

            ref_score = {row["id"]: row["score"] for row in ref["layer_b"]}
            for row in cur["layer_b"]:
                if row["id"] in ref_score:
                    delta = abs(ref_score[row["id"]] - row["score"])
                    if delta > _LAYER_B_SCORE_TOL:
                        score_violations.append(f"{cur['query']!r}/{row['id']}: Δscore={delta:.4f}")

        mean_jaccard = sum(jaccards) / len(jaccards)
        print(f"\nLayer B: mean top-{_TOP_K} Jaccard={mean_jaccard:.3f}")
        assert mean_jaccard >= _LAYER_B_TOPK_JACCARD_MIN, (
            f"Layer B reranker top-K Jaccard {mean_jaccard:.3f} < {_LAYER_B_TOPK_JACCARD_MIN}"
        )
        assert not score_violations, (
            f"Layer B surfaced-score drift > {_LAYER_B_SCORE_TOL}: {score_violations}"
        )


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "capture":
        print(
            "usage: python -m tests.benchmarks.test_chromadb_parity capture <out.json> [data_dir]",
            file=sys.stderr,
        )
        return 2
    out_path = Path(argv[1])
    data_dir = Path(argv[2]) if len(argv) > 2 else None
    report = capture_reference(out_path, data_dir)
    print(
        f"Captured parity reference: {len(report['queries'])} queries, "
        f"record_count={report['record_count']}, top_k={report['top_k']} -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
