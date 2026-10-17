# Cross-Encoder Reranking

Part of the [Pipecat Context Hub README](README.md).

Cross-encoder reranking is **enabled by default**. It scores query-result pairs
for semantic relevance after RRF merge, significantly improving result quality
(especially for `search_examples` and multi-concept queries).

- **First run:** `uv run pipecat-context-hub refresh` downloads the model (size
  depends on selection — see table below)
- **Disable:** `PIPECAT_HUB_RERANKER_ENABLED=0` env var
- **Swap models:** `PIPECAT_HUB_RERANKER_MODEL=<name>` env var (unknown values
  log a warning and fall back to the default — server always boots)
- **Latency:** ~50-100ms per query on CPU (MiniLM-L-6-v2)
- **Offline:** gracefully disabled if model not cached (falls back to RRF-only)
- **Startup warning:** when reranker is disabled at boot, `serve` logs
  `Reranker disabled at startup: reason=<config_disabled|not_cached> configured_model=…`
  plus a remediation hint. For `not_cached`, the hint names the exact HF cache
  directory probed (respects `HF_HOME` / `HUGGINGFACE_HUB_CACHE`).
- **Startup banner:** on `serve` boot, one `INFO` line reports version,
  data directory (home-redacted to `~/…`), total record count, and
  `counts_by_type={code=N,doc=N,source=N}` — use this to confirm the upgraded
  binary is running and the index is populated.
- **Verify active model:** call `get_hub_status` — `reranker_enabled` reports
  live runtime state (not just configured intent), `reranker_model` is the
  active model name, `reranker_configured_model` is what the operator asked
  for, and `reranker_disabled_reason` explains why reranking is off
  (`config_disabled` | `not_cached` | `load_failed`) when `reranker_enabled` is false

| Model | ONNX download | Notes |
|-------|------|-------|
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | ~91 MB | Default — balanced quality/speed |
| `cross-encoder/ms-marco-MiniLM-L-12-v2` | ~134 MB | Higher quality, slower |
| `cross-encoder/ms-marco-TinyBERT-L-2-v2` | ~18 MB | Fastest download, lower quality — use on slow/throttled networks |

After swapping models, run `uv run pipecat-context-hub refresh` once to
pre-download the new model before the first MCP query.
