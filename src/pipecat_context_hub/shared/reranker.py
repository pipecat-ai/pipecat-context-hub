"""Shared cross-encoder reranker startup resolution.

``serve`` (long-lived MCP process) and the one-shot CLI both need to decide,
from config + the HF cache, whether the cross-encoder reranker is enabled at
startup and which model is active. That decision was duplicated in two places
and drifted: the CLI path could not report every ``RerankerDisabledReason`` the
serve path knew about. Centralising the probe here means a new disable reason is
added once and both front doors pick it up.

``load_failed`` is deliberately NOT produced here. It is a runtime-only state — a
reranker that constructed successfully but whose ``.enabled`` flag flipped to
``False`` on its first model load — which only the long-lived ``serve`` process
observes at query time. The one-shot CLI exits after a single dispatch, so it
never re-probes a live reranker; ``serve`` layers that check on top of this probe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipecat_context_hub.shared.config import HubConfig
    from pipecat_context_hub.shared.types import RerankerDisabledReason


def probe_reranker(
    config: "HubConfig",
) -> tuple[str, str, "RerankerDisabledReason | None"]:
    """Resolve the reranker's startup state from config and the HF cache.

    Returns ``(active_model, requested_model, disabled_reason)``:

    - ``active_model`` — the allowlisted model that would be loaded.
    - ``requested_model`` — the operator's raw request (env over field), for
      ``get_hub_status``'s ``configured_model``.
    - ``disabled_reason`` — ``None`` when the reranker should be constructed;
      otherwise ``"config_disabled"`` (disabled by env/config) or ``"not_cached"``
      (model not present in the HF cache). ``"load_failed"`` is never returned
      here — see the module docstring.
    """
    from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker

    active_model = config.reranker.effective_model
    requested_model = config.reranker.requested_model
    if not config.reranker.effective_enabled:
        return active_model, requested_model, "config_disabled"
    if not CrossEncoderReranker.is_model_cached(active_model):
        return active_model, requested_model, "not_cached"
    return active_model, requested_model, None
