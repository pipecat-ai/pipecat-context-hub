"""Environment defaults for quiet, offline-first model loading.

Shared by the CLI query commands and ``serve`` — the two places that *load*
already-downloaded models. ``refresh`` must never call this: it is the one
code path that downloads models, and offline mode would break first-time
setup.
"""

from __future__ import annotations

import os


def quiet_model_loading() -> None:
    """Silence third-party model-loading noise and skip HF revalidation.

    Must run before the ONNX backend imports ``huggingface_hub``.
    ``setdefault`` throughout, so an explicitly set environment always wins
    (e.g. ``HF_HUB_OFFLINE=0`` to force revalidation).

    - ``HF_HUB_OFFLINE=1`` skips huggingface_hub's ~20 HEAD revalidation
      requests per model load — seconds of latency and a network dependency
      that loading a *cached* model shouldn't have. Safe for callers that
      only load: a non-empty index implies ``refresh`` already downloaded
      the models on this machine (``serve`` refuses to start otherwise, and
      it probes rather than downloads the reranker). In the rare
      cache-wiped-but-index-survived case, the loader's error names the
      offline mode; set ``HF_HUB_OFFLINE=0`` and re-run.
    - Progress bars are an interactive affordance; in captured CLI output
      they are agent-context noise, and in ``serve``'s MCP logs they bury the
      one-line telemetry operators grep for.

    ``TRANSFORMERS_VERBOSITY`` was set here while the hub loaded models
    through ``sentence-transformers``. The ONNX backend does not import
    ``transformers``, so the variable no longer has a reader and is not set.
    """
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
