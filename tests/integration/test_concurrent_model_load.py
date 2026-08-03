"""Regression: concurrent first-load of the retrieval models must not crash.

Frozen from a live failure. On the previous sentence-transformers/torch
backend, a multi-concept CLI query (``search-docs "TTS + STT"``) failed 12 out
of 12 runs with the reranker enabled — 10 SIGSEGV/SIGBUS and 2 hangs — on
macOS arm64 / Python 3.14 / torch 2.13.

The mechanism: multi-concept queries fan out one concurrent search per concept,
and the one-shot CLI does no pre-warm, so several ``asyncio.to_thread`` workers
raced to lazily construct the model on first use. ``serve`` was unaffected
because it pre-warms at boot, single-threaded, before any query arrives — which
is why the failure only ever showed up from the CLI front door.

These tests exercise that shape directly: models are constructed unloaded, then
first touched from several threads at once. They are cheap, and they guard the
property rather than the implementation, so they stay meaningful if the backend
is ever swapped again.
"""

from __future__ import annotations

import threading

import pytest

from pipecat_context_hub.services.embedding import EmbeddingService
from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker
from pipecat_context_hub.shared.config import EmbeddingConfig, RerankerConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IndexResult

_THREADS = 8


def _run_concurrently(target, n=_THREADS):
    """Call ``target`` from ``n`` threads at once; re-raise anything it raises."""
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def wrapper() -> None:
        try:
            barrier.wait(timeout=30)  # maximise overlap on the first call
            target()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=wrapper) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    assert not any(t.is_alive() for t in threads), "thread hung during concurrent model load"
    if errors:
        raise errors[0]


class TestConcurrentEmbeddingLoad:
    def test_first_embed_from_many_threads(self):
        """A cold EmbeddingService touched by 8 threads must not crash or hang."""
        service = EmbeddingService(EmbeddingConfig())
        results: list[list[float]] = []
        lock = threading.Lock()

        def embed() -> None:
            vec = service.embed_query("text to speech configuration")
            with lock:
                results.append(vec)

        _run_concurrently(embed)

        assert len(results) == _THREADS
        assert all(len(v) == 384 for v in results)
        # Every thread must observe the same vector — a torn or per-thread model
        # would show up here as divergence.
        first = results[0]
        for other in results[1:]:
            assert other == pytest.approx(first, abs=1e-6)


class TestConcurrentCrossEncoderLoad:
    def test_first_rerank_from_many_threads(self, sample_chunked_record: ChunkedRecord):
        """A cold CrossEncoderReranker scored by 8 threads must not crash or hang."""
        reranker = CrossEncoderReranker(
            model_name=RerankerConfig().cross_encoder_model,
            enabled=True,
        )
        if not CrossEncoderReranker.is_model_cached(reranker._model_name):
            pytest.skip("cross-encoder model not cached; run `refresh` first")

        candidates = [
            IndexResult(chunk=sample_chunked_record, score=0.5, match_type="vector")
        ]
        scored: list[list[IndexResult]] = []
        lock = threading.Lock()

        def rerank() -> None:
            out = reranker._score(list(candidates), "how do I configure text to speech")
            with lock:
                scored.append(out)

        _run_concurrently(rerank)

        assert len(scored) == _THREADS
        assert all(len(s) == len(candidates) for s in scored)
        # Deterministic scoring: concurrent first-load must not perturb results.
        first = scored[0][0].score
        for other in scored[1:]:
            assert other[0].score == pytest.approx(first, abs=1e-6)

    def test_reranker_construction_does_no_model_io(self):
        """Construction stays lazy — pinned because `serve` probes before loading."""
        reranker = CrossEncoderReranker(enabled=True)
        assert reranker.enabled is True
        assert reranker._model is None
