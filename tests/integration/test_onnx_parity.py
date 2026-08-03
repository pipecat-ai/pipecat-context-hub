"""Parity guard: the ONNX backend must reproduce the sentence-transformers vectors.

The hub replaced ``sentence-transformers``/``torch`` with direct ONNX Runtime
inference against the same published weights. That swap is only safe because an
ONNX export is a format change rather than a math change — which is what lets
existing indexes keep working without a reindex.

``tests/fixtures/onnx_parity/reference_vectors.json`` holds outputs captured
from the old backend (sentence-transformers 5.3.0 / torch 2.13.0). These tests
re-run the same inputs through the current backend and assert the results still
match, so a future model, tokenizer, or onnxruntime bump cannot silently shift
the embedding space out from under an index that was built against it.

The fixture is committed precisely so this runs without torch installed.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from pipecat_context_hub.services.embedding import EmbeddingService
from pipecat_context_hub.services.onnx_backend import OnnxCrossEncoder

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "onnx_parity" / "reference_vectors.json"
)

# Tolerances. The measured elementwise difference between backends is ~2.5e-07
# (float32 rounding), so these sit an order of magnitude above the observed
# noise floor while still failing loudly on a real change in behaviour.
_MIN_COSINE = 0.999999
_MAX_ELEMENTWISE_DIFF = 1e-5
_MAX_LOGIT_DIFF = 1e-3


@pytest.fixture(scope="module")
def reference() -> dict[str, Any]:
    with _FIXTURE.open(encoding="utf-8") as handle:
        return json.load(handle)


class TestEmbeddingParity:
    def test_vectors_match_the_pre_onnx_backend(self, reference: dict[str, Any]):
        expected = np.asarray(reference["embeddings"], dtype=np.float64)
        actual = np.asarray(EmbeddingService().embed_texts(reference["texts"]), dtype=np.float64)

        assert actual.shape == expected.shape

        cosine = (expected * actual).sum(axis=1) / (
            np.linalg.norm(expected, axis=1) * np.linalg.norm(actual, axis=1)
        )
        worst = int(np.argmin(cosine))
        assert cosine.min() >= _MIN_COSINE, (
            f"embedding drift vs the sentence-transformers reference: "
            f"cosine {cosine.min():.9f} on text {worst!r} "
            f"({reference['texts'][worst][:60]!r}). Existing indexes were built "
            f"with the reference vectors — a drift this large means queries and "
            f"stored documents no longer share an embedding space."
        )
        assert np.abs(expected - actual).max() < _MAX_ELEMENTWISE_DIFF

    def test_vectors_are_l2_normalised(self, reference: dict[str, Any]):
        actual = np.asarray(EmbeddingService().embed_texts(reference["texts"]), dtype=np.float64)
        norms = np.linalg.norm(actual, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5), f"expected unit vectors, got {norms}"

    def test_batching_does_not_change_results(self, reference: dict[str, Any]):
        """Internal batching must not alter output — the batch boundary is at 32."""
        texts = reference["texts"] * 6  # 48 texts: crosses the batch boundary
        service = EmbeddingService()
        batched = np.asarray(service.embed_texts(texts), dtype=np.float64)
        one_at_a_time = np.asarray([service.embed_query(text) for text in texts], dtype=np.float64)
        assert np.abs(batched - one_at_a_time).max() < _MAX_ELEMENTWISE_DIFF


class TestCrossEncoderParity:
    def test_logits_match_the_pre_onnx_backend(self, reference: dict[str, Any]):
        query = reference["cross_encoder_query"]
        pairs = [(query, doc) for doc in reference["cross_encoder_documents"]]
        expected = np.asarray(reference["cross_encoder_logits"], dtype=np.float64)

        actual = np.asarray(
            OnnxCrossEncoder(reference["cross_encoder_model"]).predict(pairs),
            dtype=np.float64,
        )

        assert np.abs(expected - actual).max() < _MAX_LOGIT_DIFF
        assert list(np.argsort(-actual)) == list(np.argsort(-expected)), (
            "reranker ordering changed vs the reference — retrieval results "
            "would be reordered even though scores look close."
        )

    def test_predict_returns_raw_logits_not_probabilities(self, reference: dict[str, Any]):
        """``CrossEncoderReranker._score`` applies its own sigmoid.

        sentence-transformers v5+ ``predict()`` also returned raw logits, so the
        stored score is ``sigmoid(logit)``. If this backend ever returned
        probabilities instead, that sigmoid would be applied twice and every
        stored score would silently compress into (0.5, 0.73).
        """
        query = reference["cross_encoder_query"]
        pairs = [(query, doc) for doc in reference["cross_encoder_documents"]]
        scores = OnnxCrossEncoder(reference["cross_encoder_model"]).predict(pairs)

        assert any(score < 0.0 for score in scores), (
            f"expected unbounded logits, got {scores} — these look like "
            f"probabilities, which would be double-sigmoided downstream."
        )
        assert all(math.isfinite(score) for score in scores)
