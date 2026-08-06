"""Shared cross-encoder reranker startup resolution.

``serve`` (long-lived MCP process) and the one-shot CLI both need to decide,
from config + the HF cache, whether the cross-encoder reranker is enabled at
startup and which model is active. That decision was duplicated in two places
and drifted: the CLI path could not report every ``RerankerDisabledReason`` the
serve path knew about. Centralising the probe here means a new disable reason is
added once and both front doors pick it up.

``probe_reranker`` deliberately never returns ``"load_failed"``. That is a
runtime-only state — a reranker that constructed successfully but whose
``.enabled`` flag flipped to ``False`` on its first (lazy) model load — which
can only be observed after a reranker instance exists and has been given a
chance to load. ``runtime_reranker_reason`` (below) is the complement: called
with the live ``CrossEncoderReranker`` (or ``None``) and this probe's startup
reason, it folds in ``"load_failed"`` for both front doors — ``serve``'s
``_reranker_status()`` closure calls it at each ``get_hub_status`` query, and
the one-shot CLI calls it once after its single dispatch, since
``cross_encoder`` may have already failed to load lazily during that dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker
    from pipecat_context_hub.shared.config import HubConfig
    from pipecat_context_hub.shared.types import RerankerDisabledReason


def probe_reranker(
    config: HubConfig,
) -> tuple[str, str, RerankerDisabledReason | None]:
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


def runtime_reranker_reason(
    cross_encoder: CrossEncoderReranker | None,
    startup_reason: RerankerDisabledReason | None,
) -> RerankerDisabledReason | None:
    """Resolve the reranker's *current* disabled reason, including runtime failures.

    ``probe_reranker`` only knows what the HF cache and config say before a
    reranker is constructed; it deliberately never returns ``"load_failed"``
    (see the module docstring). This is the runtime complement: given the
    (possibly ``None``) live ``CrossEncoderReranker`` produced from that probe
    and the ``startup_reason`` the probe returned, it reports the reason *as
    of right now* — including a model that loaded lazily, failed, and flipped
    ``.enabled`` to ``False`` after construction.

    - ``cross_encoder is None`` — the probe already found it disabled; pass
      ``startup_reason`` straight through.
    - ``cross_encoder.enabled`` — reranking is live; no disabled reason.
    - otherwise — the reranker was constructed (probe found it enabled) but
      its first load attempt failed at runtime: ``"load_failed"``.
    """
    if cross_encoder is None:
        return startup_reason
    if cross_encoder.enabled:
        return None
    return "load_failed"
