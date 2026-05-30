"""Direct chromadb engine parity: same vectors into 0.6 and 1.x, compare results.

Phase 3 of the chromadb 1.x migration. Isolates risk #7 (does 1.x change cosine
distance / score semantics?) from all ingestion/repo-discovery variance by
replaying byte-identical (id, embedding) pairs into both engines and querying
with identical query vectors. The rigorous complement to the full-stack
``test_chromadb_parity.py`` harness — that one needs a reproducible corpus, which
repo discovery (GitHub API) makes flaky run-to-run; this one does not.

MANUAL two-venv tool (NOT a pytest — needs both a 0.6 and a 1.x environment):

    # in the 0.6 venv, against any populated 0.6 index:
    uv run python tests/benchmarks/engine_parity_check.py capture-06 <data_dir>/chroma
    # in the 1.x venv:
    uv run python tests/benchmarks/engine_parity_check.py compare-1x

Result (chromadb 0.6.3 vs 1.5.9, 39,786 identical vectors, 15 queries, 2026-05-30):
    top-1 ID match rate          : 15/15 = 1.000
    mean top-20 Jaccard          : 0.987   (tail reorder = HNSW approximation)
    max top-1 distance delta     : 4.17e-07
    max common-result dist delta : 4.17e-07
    VERDICT: PASS

=> 1.x preserves cosine distance semantics to ~4e-7, so the surfaced
   ``1 - distance/2`` similarity score is unchanged. Layer B (HybridRetriever)
   parity follows because the SQLite FTS index and cross-encoder reranker are
   chromadb-independent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import chromadb
import numpy as np

CORPUS_NPZ = Path("/tmp/engine-corpus.npz")
QUERIES_NPZ = Path("/tmp/engine-queries.npz")
RESULTS_06 = Path("/tmp/engine-0.6-results.json")
TOP_K = 20

QUERY_SET = [
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
]


def _batched_get_all(col):
    ids: list[str] = []
    embs: list[list[float]] = []
    total = col.count()
    for off in range(0, total, 5000):
        got = col.get(limit=5000, offset=off, include=["embeddings"])
        ids.extend(got["ids"])
        embs.extend(got["embeddings"])
    return ids, np.asarray(embs, dtype=np.float32)


def capture_06(chroma_path: str) -> None:
    from pipecat_context_hub.services.embedding import EmbeddingService
    from pipecat_context_hub.shared.config import EmbeddingConfig

    client = chromadb.PersistentClient(path=chroma_path)
    col = client.get_collection("latest")
    ids, embs = _batched_get_all(col)
    print(f"0.6 corpus: {len(ids)} records, emb shape {embs.shape}")

    svc = EmbeddingService(EmbeddingConfig())
    q = np.asarray([svc.embed_query(t) for t in QUERY_SET], dtype=np.float32)

    results = []
    for i, text in enumerate(QUERY_SET):
        r = col.query(query_embeddings=[q[i].tolist()], n_results=TOP_K, include=["distances"])
        dists = r["distances"]
        assert dists is not None  # we requested distances above
        results.append({"query": text, "ids": r["ids"][0], "distances": dists[0]})

    np.savez(CORPUS_NPZ, ids=np.asarray(ids), embeddings=embs)
    np.savez(QUERIES_NPZ, embeddings=q)
    RESULTS_06.write_text(json.dumps(results))
    print(f"wrote {CORPUS_NPZ}, {QUERIES_NPZ}, {RESULTS_06}")


def compare_1x() -> None:
    # Inputs are self-generated /tmp dumps from the capture step (trusted);
    # string ids load without pickle, so allow_pickle stays off.
    corpus = np.load(CORPUS_NPZ)
    ids = [str(x) for x in corpus["ids"]]
    embs = corpus["embeddings"].astype(np.float32)
    q = np.load(QUERIES_NPZ)["embeddings"].astype(np.float32)
    ref = json.loads(RESULTS_06.read_text())
    print(f"1.x replay: {len(ids)} records, emb shape {embs.shape}")

    import tempfile

    client = chromadb.PersistentClient(path=tempfile.mkdtemp())
    col = client.get_or_create_collection("replay", metadata={"hnsw:space": "cosine"})
    for off in range(0, len(ids), 5000):
        col.add(ids=ids[off : off + 5000], embeddings=embs[off : off + 5000].tolist())
    print(f"1.x collection built: {col.count()} records")

    top1_match = 0
    jaccards: list[float] = []
    max_common_dist_delta = 0.0
    max_top1_dist_delta = 0.0
    for i, ref_q in enumerate(ref):
        r = col.query(query_embeddings=[q[i].tolist()], n_results=TOP_K, include=["distances"])
        dists = r["distances"]
        assert dists is not None  # we requested distances above
        cur_ids, cur_dist = r["ids"][0], dists[0]
        ref_ids, ref_dist = ref_q["ids"], ref_q["distances"]

        if ref_ids and cur_ids and ref_ids[0] == cur_ids[0]:
            top1_match += 1
            max_top1_dist_delta = max(max_top1_dist_delta, abs(ref_dist[0] - cur_dist[0]))

        sa, sb = set(ref_ids), set(cur_ids)
        jaccards.append(len(sa & sb) / len(sa | sb) if (sa | sb) else 1.0)

        # Distance-value parity for IDs present in BOTH result sets.
        ref_map = dict(zip(ref_ids, ref_dist))
        for cid, cd in zip(cur_ids, cur_dist):
            if cid in ref_map:
                max_common_dist_delta = max(max_common_dist_delta, abs(ref_map[cid] - cd))

    n = len(ref)
    print("\n=== ENGINE PARITY (0.6 vs 1.x, identical vectors) ===")
    print(f"top-1 ID match rate : {top1_match}/{n} = {top1_match / n:.3f}")
    print(f"mean top-{TOP_K} Jaccard : {sum(jaccards) / len(jaccards):.3f}")
    print(f"max top-1 distance delta      : {max_top1_dist_delta:.2e}")
    print(f"max common-result dist delta  : {max_common_dist_delta:.2e}")
    ok = (
        top1_match / n >= 0.95
        and sum(jaccards) / len(jaccards) >= 0.90
        and max_common_dist_delta < 1e-5
    )
    print("VERDICT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "capture-06":
        capture_06(sys.argv[2])
    elif mode == "compare-1x":
        compare_1x()
    else:
        print("usage: engine_parity.py capture-06 <chroma_path> | compare-1x", file=sys.stderr)
        sys.exit(2)
