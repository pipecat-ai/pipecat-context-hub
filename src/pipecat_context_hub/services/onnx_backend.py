"""ONNX Runtime inference backend for embeddings and cross-encoder reranking.

Replaces ``sentence-transformers`` (and its ``torch`` dependency) with direct
``onnxruntime`` + ``tokenizers`` inference against the *same* model weights.
Both libraries were already installed transitively via ``chromadb``, so this
removes ~4.75 GB from a Linux install without adding a package.

The models publish official ONNX exports (``onnx/model.onnx``) alongside their
PyTorch weights. An ONNX export is a format change, not a math change, so
vectors are identical to the previous backend to float32 rounding noise
(measured: cosine 1.000000, max elementwise diff 1.45e-07). Existing indexes
therefore remain valid — no reindex is required.

Model topology is read from the repo (``sentence_bert_config.json``,
``1_Pooling/config.json``, ``modules.json``) rather than hard-coded, so any
standard sentence-transformers model keeps working through
``EmbeddingConfig.model_name``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Files fetched from the model repo. ``ONNX_WEIGHTS`` doubles as the cache
# probe target (see ``is_model_cached``): a pre-migration cache holds
# ``config.json`` but no ``onnx/`` directory, so probing anything else would
# report a false positive and send the first query to the network.
ONNX_WEIGHTS = "onnx/model.onnx"
_TOKENIZER = "tokenizer.json"
_ST_CONFIG = "sentence_bert_config.json"
_POOLING_CONFIG = "1_Pooling/config.json"
_MODULES = "modules.json"
_TOKENIZER_CONFIG = "tokenizer_config.json"

# The org that hosts the bare-named sentence-transformers models.
_ST_ORG = "sentence-transformers"

# Immutable commit SHAs for every model the hub ships with. Pinning serves two
# purposes:
#
# 1. Supply chain. An unpinned download resolves ``main`` at fetch time, so a
#    compromised or simply re-uploaded repo would be trusted silently. This is
#    what bandit's B615 warns about.
# 2. Embedding-space stability. Vectors are only comparable to the ones already
#    in a user's index if they come from the same weights. If upstream
#    re-exported ``onnx/model.onnx``, an unpinned hub would start writing
#    subtly different vectors into an index built with the old ones, degrading
#    retrieval with no visible error. The pin makes that impossible, and keeps
#    tests/integration/test_onnx_parity.py meaningful over time.
#
# Update deliberately: bump a SHA only alongside a re-run of the parity test,
# and treat a failure there as "this needs a reindex", not "loosen the test".
_PINNED_REVISIONS: dict[str, str] = {
    "sentence-transformers/all-MiniLM-L6-v2": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    "cross-encoder/ms-marco-MiniLM-L-6-v2": "c5ee24cb16019beea0893ab7796b1df96625c6b8",
    "cross-encoder/ms-marco-MiniLM-L-12-v2": "7b0235231ca2674cb8ca8f022859a6eba2b1c968",
    "cross-encoder/ms-marco-TinyBERT-L-2-v2": "81d1926f67cb8eee2c2be17ca9f793c7c3bd20cc",
}


# Fallbacks when a repo omits the sentence-transformers config files. These
# match all-MiniLM-L6-v2, the default and by far the most common case.
_DEFAULT_MAX_SEQ = 256
_CROSS_ENCODER_MAX_SEQ = 512

# Matches the sentence-transformers default; bounds peak memory on large
# ingest batches without measurably affecting throughput.
_BATCH_SIZE = 32


def _download(repo_id: str, filename: str) -> str:
    """Fetch a repo file, pinned to an immutable revision where one is known.

    Models the hub ships with are pinned (see ``_PINNED_REVISIONS``). A
    user-supplied ``EmbeddingConfig.model_name`` has no pin available, so it
    resolves the default branch — the same exposure the previous
    sentence-transformers backend had for any model.
    """
    from huggingface_hub import hf_hub_download

    revision = _PINNED_REVISIONS.get(repo_id)
    if revision is None:
        # nosec B615 - no pin exists for a caller-supplied custom model; the
        # repo id comes from local config, not from untrusted input.
        return hf_hub_download(repo_id, filename)  # nosec B615
    return hf_hub_download(repo_id, filename, revision=revision)


def resolve_repo_id(model_name: str) -> str:
    """Resolve a model name to a fully-qualified HuggingFace repo id.

    ``EmbeddingConfig.model_name`` defaults to the bare ``all-MiniLM-L6-v2``,
    which sentence-transformers silently resolved by prepending its own org.
    The Hub does not: ``hf_hub_download("all-MiniLM-L6-v2", ...)`` raises
    ``RepositoryNotFoundError``. Renaming the default is not an option — it is
    public config and pinned by tests.

    A name is used as-is when it is already namespaced (``org/model``) or
    points at a local directory; otherwise the sentence-transformers org is
    prepended. This is a pure string decision rather than a Hub probe so it
    behaves identically offline, where ``HF_HUB_OFFLINE=1`` makes a
    "does this repo exist" query impossible to answer.
    """
    if "/" in model_name or Path(model_name).exists():
        return model_name
    return f"{_ST_ORG}/{model_name}"


def _fetch(repo_id: str, filename: str) -> str | None:
    """Download a repo file, returning ``None`` when it is absent.

    Used for the optional topology config files, which not every repo ships.
    """
    try:
        return _download(repo_id, filename)
    except Exception:
        logger.debug("Optional model file %s not available for %s", filename, repo_id)
        return None


def _read_json(path: str | None) -> dict[str, Any]:
    """Parse a JSON config file, tolerating absence or corruption."""
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            parsed: Any = json.load(handle)
    except Exception:
        logger.debug("Could not parse model config at %s", path)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_model_cached(model_name: str) -> bool:
    """Check whether a model's ONNX weights are present in the HF cache.

    Probes ``onnx/model.onnx`` specifically. Probing ``config.json`` instead
    would return ``True`` for any cache populated by the old
    sentence-transformers backend, which downloaded ``config.json`` and
    ``model.safetensors`` but never the ONNX export — a false positive that
    enables the reranker and then fails on the first query under
    ``HF_HUB_OFFLINE=1``.
    """
    repo_id = resolve_repo_id(model_name)
    try:
        from huggingface_hub import try_to_load_from_cache

        return isinstance(try_to_load_from_cache(repo_id, ONNX_WEIGHTS), str)
    except ImportError:
        logger.debug("huggingface_hub unavailable, falling back to cache-dir probe")
    except Exception:
        logger.debug("Model cache lookup failed, falling back to cache-dir probe", exc_info=True)

    cache_dir = resolve_hf_cache_dir()
    if not cache_dir.exists():
        return False
    safe_name = repo_id.replace("/", "--")
    for entry in cache_dir.iterdir():
        if not entry.is_dir() or not entry.name.endswith(safe_name):
            continue
        if any(entry.glob(f"snapshots/*/{ONNX_WEIGHTS}")):
            return True
    return False


def resolve_hf_cache_dir() -> Path:
    """Return the HuggingFace hub cache directory that would be probed.

    Used for operator diagnostics so a failed cache check can name the exact
    path the server looked at. Respects ``HF_HOME`` and
    ``HUGGINGFACE_HUB_CACHE`` when ``huggingface_hub`` is installed.
    """
    try:
        from huggingface_hub.constants import HF_HUB_CACHE

        return Path(HF_HUB_CACHE)
    except Exception:
        return Path.home() / ".cache" / "huggingface" / "hub"


def _pad_token(repo_id: str) -> str:
    """Return the model's padding token, defaulting to the BERT convention."""
    config = _read_json(_fetch(repo_id, _TOKENIZER_CONFIG))
    value = config.get("pad_token")
    if isinstance(value, str):
        return value
    # ``pad_token`` is sometimes an AddedToken dict rather than a bare string.
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return str(value["content"])
    return "[PAD]"  # nosec B105 - a tokenizer padding token, not a credential


@dataclass(frozen=True)
class _Topology:
    """How to turn a model's raw token output into a sentence vector."""

    max_seq_length: int
    mean_pooling: bool
    normalize: bool


def _load_topology(repo_id: str) -> _Topology:
    """Read pooling and truncation settings from the model repo.

    Falls back to mean pooling + L2 normalisation at 256 tokens — the
    all-MiniLM-L6-v2 configuration — when a repo omits these files, with a
    warning so a misconfigured custom model is visible rather than silent.
    """
    st_config = _read_json(_fetch(repo_id, _ST_CONFIG))
    pooling = _read_json(_fetch(repo_id, _POOLING_CONFIG))
    modules = _fetch(repo_id, _MODULES)

    max_seq = st_config.get("max_seq_length")
    if not isinstance(max_seq, int):
        tok_config = _read_json(_fetch(repo_id, _TOKENIZER_CONFIG))
        candidate = tok_config.get("model_max_length")
        max_seq = candidate if isinstance(candidate, int) else _DEFAULT_MAX_SEQ

    if pooling:
        mean_pooling = bool(pooling.get("pooling_mode_mean_tokens", False))
    else:
        logger.warning(
            "Model %s ships no %s; assuming mean pooling with L2 normalisation",
            repo_id,
            _POOLING_CONFIG,
        )
        mean_pooling = True

    normalize = True
    if modules is not None:
        try:
            with open(modules, encoding="utf-8") as handle:
                entries: Any = json.load(handle)
            normalize = any(
                isinstance(entry, dict) and str(entry.get("type", "")).endswith("Normalize")
                for entry in entries
            )
        except Exception:
            logger.debug("Could not parse %s for %s", _MODULES, repo_id)

    return _Topology(max_seq_length=max_seq, mean_pooling=mean_pooling, normalize=normalize)


class _OnnxSession:
    """Shared tokenizer + ONNX session setup for both model kinds."""

    def __init__(self, repo_id: str, max_length: int) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._repo_id = repo_id
        tokenizer: Any = Tokenizer.from_file(_download(repo_id, _TOKENIZER))
        tokenizer.enable_truncation(max_length=max_length)

        # Read the padding token from the model rather than assuming BERT's
        # "[PAD]", so non-BERT tokenizers pad correctly. Padded positions are
        # masked out of the pooled result either way, but a token the
        # vocabulary does not contain would fail to encode.
        pad_token = _pad_token(repo_id)
        pad_id = tokenizer.token_to_id(pad_token)
        tokenizer.enable_padding(pad_id=pad_id if pad_id is not None else 0, pad_token=pad_token)
        self._tokenizer = tokenizer

        # Cap intra-op threads rather than letting ORT scale to every core: the
        # hub shares a developer machine with an editor and language servers,
        # and ORT's default fans out wide enough to hurt. Measured on an
        # 18-core M-series (median, batch of 32): 1 thread 13.8ms, 2 threads
        # 16.4ms, 4 threads 9.5ms, all cores 10.2ms — while single-query
        # latency is flat at ~0.85ms across all settings. Four is the knee, and
        # it beats the sentence-transformers/torch backend it replaced
        # (18.9ms for a batch of 64 vs 15.4ms here).
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(4, os.cpu_count() or 1)
        self._session = ort.InferenceSession(
            _download(repo_id, ONNX_WEIGHTS),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        # Graphs vary in whether they declare token_type_ids; feed only what
        # this one accepts.
        self._input_names = {spec.name for spec in self._session.get_inputs()}

    def run(self, batch: list[str] | list[tuple[str, str]]) -> tuple[Any, Any]:
        """Tokenise and run the session, returning ``(output, attention_mask)``.

        Accepts either plain texts (bi-encoder) or query/document pairs
        (cross-encoder); ``tokenizers`` handles both encodings natively.
        """
        encodings = self._tokenizer.encode_batch(batch)
        ids = np.array([enc.ids for enc in encodings], dtype=np.int64)
        mask = np.array([enc.attention_mask for enc in encodings], dtype=np.int64)
        feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.array([enc.type_ids for enc in encodings], dtype=np.int64)
        return self._session.run(None, feed)[0], mask


class OnnxTextEncoder:
    """Bi-encoder producing sentence embeddings, replacing ``SentenceTransformer``.

    Construction performs model I/O, so callers must keep instantiating it
    lazily (``EmbeddingService._get_model``) rather than at import or
    ``__init__`` time.
    """

    def __init__(self, model_name: str) -> None:
        self._repo_id = resolve_repo_id(model_name)
        self._topology = _load_topology(self._repo_id)
        self._backend = _OnnxSession(self._repo_id, self._topology.max_seq_length)

    def encode(self, texts: list[str]) -> Any:
        """Embed a batch of texts, returning a ``(len(texts), dim)`` array."""
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        chunks = [
            self._encode_batch(texts[start : start + _BATCH_SIZE])
            for start in range(0, len(texts), _BATCH_SIZE)
        ]
        return np.concatenate(chunks, axis=0)

    def _encode_batch(self, texts: list[str]) -> Any:
        output, mask = self._backend.run(texts)

        if self._topology.mean_pooling:
            expanded = mask[..., None].astype(np.float32)
            pooled = (output * expanded).sum(axis=1) / np.clip(expanded.sum(axis=1), 1e-9, None)
        else:
            # CLS pooling: the first token's hidden state represents the input.
            pooled = output[:, 0]

        if self._topology.normalize:
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            pooled = pooled / np.clip(norms, 1e-12, None)
        return pooled.astype(np.float32)


class OnnxCrossEncoder:
    """Cross-encoder scoring query/document pairs, replacing ``CrossEncoder``.

    ``predict`` returns **raw logits**, matching what sentence-transformers v5+
    returns by default. ``CrossEncoderReranker._score`` applies its own sigmoid
    to that output, so stored scores are unchanged by this swap.
    """

    def __init__(self, model_name: str) -> None:
        self._repo_id = resolve_repo_id(model_name)
        self._backend = _OnnxSession(self._repo_id, _CROSS_ENCODER_MAX_SEQ)

    def predict(self, sentences: list[tuple[str, str]]) -> list[float]:
        """Score query/document pairs, returning one raw logit per pair."""
        if not sentences:
            return []
        scores: list[float] = []
        for start in range(0, len(sentences), _BATCH_SIZE):
            logits, _ = self._backend.run(sentences[start : start + _BATCH_SIZE])
            scores.extend(float(value) for value in np.asarray(logits).reshape(-1))
        return scores


def ensure_downloaded(model_name: str) -> None:
    """Pre-download every file the backend needs for ``model_name``.

    Called from ``refresh`` — the one code path allowed to hit the network —
    so a first run fails there with a clear error rather than mid-query under
    ``HF_HUB_OFFLINE=1``.
    """
    repo_id = resolve_repo_id(model_name)
    for filename in (ONNX_WEIGHTS, _TOKENIZER):
        _download(repo_id, filename)
    for filename in (_ST_CONFIG, _POOLING_CONFIG, _MODULES, _TOKENIZER_CONFIG):
        _fetch(repo_id, filename)
