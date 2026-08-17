"""Unit tests for the ONNX Runtime inference backend.

These pin the contracts that make the sentence-transformers → ONNX swap safe:
repo-id resolution for bare model names, and a cache probe that does not
mistake a pre-migration cache for a usable one. Neither test loads a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from pipecat_context_hub.services.onnx_backend import (
    ONNX_WEIGHTS,
    is_model_cached,
    resolve_repo_id,
)
from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker


class TestResolveRepoId:
    """Bare model names must gain the sentence-transformers org prefix.

    ``EmbeddingConfig.model_name`` defaults to the bare ``all-MiniLM-L6-v2``,
    which sentence-transformers resolved implicitly. The Hub does not:
    ``hf_hub_download("all-MiniLM-L6-v2", ...)`` is a 404.
    """

    def test_bare_name_gets_sentence_transformers_org(self):
        assert resolve_repo_id("all-MiniLM-L6-v2") == "sentence-transformers/all-MiniLM-L6-v2"

    def test_namespaced_name_is_left_alone(self):
        for name in (
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
            "cross-encoder/ms-marco-MiniLM-L-12-v2",
            "cross-encoder/ms-marco-TinyBERT-L-2-v2",
        ):
            assert resolve_repo_id(name) == name

    def test_default_config_model_resolves(self):
        """The shipped default must resolve to a real repo id."""
        from pipecat_context_hub.shared.config import EmbeddingConfig

        assert resolve_repo_id(EmbeddingConfig().model_name) == (
            "sentence-transformers/all-MiniLM-L6-v2"
        )

    def test_local_directory_is_left_alone(self, tmp_path: Path):
        local = tmp_path / "my-model"
        local.mkdir()
        assert resolve_repo_id(str(local)) == str(local)


def _write_snapshot(cache_dir: Path, repo_id: str, files: list[str]) -> None:
    """Build a HF-cache-shaped directory containing exactly ``files``."""
    snapshot = cache_dir / f"models--{repo_id.replace('/', '--')}" / "snapshots" / "abc123"
    for name in files:
        path = snapshot / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")


class TestIsModelCached:
    """The probe must look for ONNX weights, not ``config.json``.

    A cache populated by the previous sentence-transformers backend holds
    ``config.json`` and ``model.safetensors`` but no ``onnx/`` directory.
    Probing ``config.json`` would report that model as cached, which enables
    the reranker at startup and then fails on the first query, because
    ``quiet_model_loading`` sets ``HF_HUB_OFFLINE=1`` and the download cannot
    happen. This is the migration hazard for every existing install.
    """

    def test_pre_migration_cache_reports_not_cached(self, tmp_path: Path):
        repo = "cross-encoder/ms-marco-MiniLM-L-6-v2"
        _write_snapshot(tmp_path, repo, ["config.json", "tokenizer.json", "model.safetensors"])
        with patch(
            "pipecat_context_hub.services.onnx_backend.resolve_hf_cache_dir",
            return_value=tmp_path,
        ):
            # try_to_load_from_cache reads the real HF cache, so force the
            # directory-scan fallback to exercise the tmp_path fixture.
            with patch.dict("sys.modules", {"huggingface_hub": None}):
                assert is_model_cached(repo) is False

    def test_cache_with_onnx_weights_reports_cached(self, tmp_path: Path):
        repo = "cross-encoder/ms-marco-MiniLM-L-6-v2"
        _write_snapshot(tmp_path, repo, ["config.json", "tokenizer.json", ONNX_WEIGHTS])
        with patch(
            "pipecat_context_hub.services.onnx_backend.resolve_hf_cache_dir",
            return_value=tmp_path,
        ), patch.dict("sys.modules", {"huggingface_hub": None}):
            assert is_model_cached(repo) is True

    def test_missing_cache_dir_reports_not_cached(self, tmp_path: Path):
        with patch(
            "pipecat_context_hub.services.onnx_backend.resolve_hf_cache_dir",
            return_value=tmp_path / "does-not-exist",
        ), patch.dict("sys.modules", {"huggingface_hub": None}):
            assert is_model_cached("cross-encoder/ms-marco-MiniLM-L-6-v2") is False

    def test_probe_target_is_the_onnx_export(self):
        assert ONNX_WEIGHTS == "onnx/model.onnx"

    def test_reranker_delegates_to_backend_probe(self):
        """``CrossEncoderReranker.is_model_cached`` is patched by name in four
        CLI tests; keep it delegating rather than reimplementing the probe."""
        with patch(
            "pipecat_context_hub.services.onnx_backend.is_model_cached",
            return_value=True,
        ) as probe:
            assert CrossEncoderReranker.is_model_cached("some/model") is True
            probe.assert_called_once_with("some/model")

    def test_pinned_revision_snapshot_without_refs_main_reports_cached(
        self, tmp_path: Path, monkeypatch
    ):
        """Regression for #115.

        ``_download`` fetches shipped models at a pinned SHA, which populates
        ``blobs/`` and ``snapshots/<sha>/`` but never writes ``refs/main`` —
        there is no branch name involved, so there is no ref to record.
        ``try_to_load_from_cache`` defaults to ``revision="main"``, which
        cannot resolve against a SHA-only snapshot. Before the fix, this
        reported ``False`` — reranking permanently disabled — even
        immediately after a successful ``refresh`` populated the cache.
        """
        from pipecat_context_hub.services.onnx_backend import _PINNED_REVISIONS

        repo = "cross-encoder/ms-marco-MiniLM-L-6-v2"
        revision = _PINNED_REVISIONS[repo]
        snapshot = (
            tmp_path / f"models--{repo.replace('/', '--')}" / "snapshots" / revision / ONNX_WEIGHTS
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("{}", encoding="utf-8")
        # No refs/ directory is created — matches a real pinned download.
        assert not (tmp_path / f"models--{repo.replace('/', '--')}" / "refs").exists()

        monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(tmp_path))
        assert is_model_cached(repo) is True

    def test_unpinned_model_probe_still_checks_main(self, tmp_path: Path, monkeypatch):
        """A user-supplied model has no pin; the probe must still resolve
        ``main`` for it exactly as before."""
        repo = "some-org/custom-model"
        snapshot = tmp_path / f"models--{repo.replace('/', '--')}" / "snapshots" / "deadbeef"
        (snapshot / "onnx").mkdir(parents=True)
        (snapshot / ONNX_WEIGHTS).write_text("{}", encoding="utf-8")
        refs = tmp_path / f"models--{repo.replace('/', '--')}" / "refs"
        refs.mkdir(parents=True)
        (refs / "main").write_text("deadbeef", encoding="utf-8")

        monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(tmp_path))
        assert is_model_cached(repo) is True


class TestRevisionPinning:
    """Shipped models resolve to immutable commits, not a moving branch.

    Two things depend on this: supply-chain safety (bandit B615), and the
    embedding-space stability that lets an existing index stay valid. An
    upstream re-export of ``onnx/model.onnx`` would otherwise start writing
    subtly different vectors into an index built from the old ones.
    """

    def test_every_shipped_model_is_pinned(self):
        from pipecat_context_hub.services.onnx_backend import _PINNED_REVISIONS
        from pipecat_context_hub.shared.config import (
            _ALLOWED_RERANKER_MODELS,
            EmbeddingConfig,
        )

        expected = {resolve_repo_id(EmbeddingConfig().model_name)} | {
            resolve_repo_id(model) for model in _ALLOWED_RERANKER_MODELS
        }
        missing = expected - set(_PINNED_REVISIONS)
        assert not missing, f"models shipped without a pinned revision: {sorted(missing)}"

    def test_pins_are_full_commit_shas(self):
        from pipecat_context_hub.services.onnx_backend import _PINNED_REVISIONS

        for repo_id, revision in _PINNED_REVISIONS.items():
            assert len(revision) == 40, f"{repo_id} pin is not a full commit sha: {revision}"
            assert set(revision) <= set("0123456789abcdef"), (
                f"{repo_id} pin is not hex — a branch or tag is mutable and "
                f"defeats the point of pinning: {revision}"
            )

    def test_pinned_model_downloads_pass_the_revision(self):
        from pipecat_context_hub.services.onnx_backend import _PINNED_REVISIONS, _download

        repo = "sentence-transformers/all-MiniLM-L6-v2"
        with patch("huggingface_hub.hf_hub_download", return_value="/tmp/x") as download:
            _download(repo, "onnx/model.onnx")
        download.assert_called_once_with(repo, "onnx/model.onnx", revision=_PINNED_REVISIONS[repo])

    def test_unpinned_custom_model_still_resolves(self):
        """A user-supplied model has no pin available; it must still work."""
        from pipecat_context_hub.services.onnx_backend import _download

        with patch("huggingface_hub.hf_hub_download", return_value="/tmp/x") as download:
            _download("some-org/custom-model", "onnx/model.onnx")
        download.assert_called_once_with("some-org/custom-model", "onnx/model.onnx")


class TestNoTorchDependency:
    """The whole point of the swap: torch must not come back silently."""

    def test_torch_and_sentence_transformers_are_absent(self):
        import importlib.util

        assert importlib.util.find_spec("torch") is None, (
            "torch is installed again — it pulls ~4.5 GB of CUDA packages on "
            "Linux. Check whether a dependency re-introduced sentence-transformers."
        )
        assert importlib.util.find_spec("sentence_transformers") is None

    def test_importing_the_hub_does_not_import_torch(self):
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import pipecat_context_hub.cli, pipecat_context_hub.services.embedding, sys; "
                "print('torch' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False"


class TestTopologyConfigIsRead:
    """Pooling/truncation come from the repo, not hard-coded constants."""

    def test_default_model_topology_matches_published_config(self):
        from pipecat_context_hub.services.onnx_backend import _load_topology

        topology = _load_topology("sentence-transformers/all-MiniLM-L6-v2")
        # sentence_bert_config.json: max_seq_length 256
        # 1_Pooling/config.json: pooling_mode_mean_tokens true
        # modules.json: includes a Normalize module
        assert topology.max_seq_length == 256
        assert topology.mean_pooling is True
        assert topology.normalize is True

    def test_missing_pooling_config_falls_back_to_mean_and_normalize(self, tmp_path: Path):
        from pipecat_context_hub.services.onnx_backend import _load_topology

        with patch(
            "pipecat_context_hub.services.onnx_backend._fetch",
            return_value=None,
        ):
            topology = _load_topology("some/model-without-st-config")
        assert topology.mean_pooling is True
        assert topology.normalize is True
        assert topology.max_seq_length == 256

    def test_cls_pooling_is_honoured(self, tmp_path: Path):
        from pipecat_context_hub.services.onnx_backend import _load_topology

        pooling = tmp_path / "pooling.json"
        pooling.write_text(
            json.dumps({"pooling_mode_mean_tokens": False, "pooling_mode_cls_token": True}),
            encoding="utf-8",
        )

        def fake_fetch(repo_id: str, filename: str) -> str | None:
            return str(pooling) if filename == "1_Pooling/config.json" else None

        with patch("pipecat_context_hub.services.onnx_backend._fetch", side_effect=fake_fetch):
            topology = _load_topology("some/cls-model")
        assert topology.mean_pooling is False
