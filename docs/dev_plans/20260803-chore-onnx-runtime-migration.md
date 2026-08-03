# Replace sentence-transformers/torch with ONNX Runtime

**Component**: services/embedding, services/retrieval (inference backend)
**Status**: Complete — awaiting review
**Type**: chore
**Created**: 2026-08-03
**Branch**: `chore/onnx-runtime-migration`
**Target version**: TBD — proposed `v0.5.0` (see "Versioning policy" below)

## Why

The installed package is far larger than a local-first dev tool should be.
Measured with clean runtime-only installs (no dev extras):

| Platform | Size | Packages |
|---|---|---|
| macOS arm64 | **1.0 GB** | 110 |
| Linux x86_64 | **5.1 GB** | 129 |

The Linux figure is roughly 2× what the team assumed (~2.5 GB); 2.87 GB of
compressed wheels unpack to 5.1 GB.

One dependency causes it. `sentence-transformers` → `torch` → 15 `nvidia-*` CUDA
packages plus `triton`. On Linux that is `nvidia/` (2.7 GB) + `torch` (1.1 GB) +
`triton` (702 MB) = **4.5 GB, 88% of the install** — GPU kernels shipped to run a
22 MB MiniLM model that `EmbeddingService` explicitly pins to `device="cpu"`.

`onnxruntime` and `tokenizers` were **already** in the lock (via chromadb and
transformers), and all four models in the reranker allowlist publish official
ONNX exports. So the swap promotes existing transitives to direct dependencies
and adds no new package.

Two further problems were found during the work and are fixed here — see
"Findings".

## Why ONNX rather than the cheaper alternatives

- **CPU-only torch index** (`download.pytorch.org/whl/cpu`) drops the CUDA
  packages but not `torch` itself, and index configuration lives in
  `[tool.uv]` — it is stripped from the published wheel, so it would help
  repo/CI installs and do nothing for `pip install pipecat-ai-context-hub` or
  `uvx`. Rejected: it does not help the people the size actually hurts.
- **qint8 ONNX weights** would cut the HF model cache 180 MB → 46 MB, but they
  change the embedding values, forcing a full reindex, and ship as four
  arch-specific files needing CPU-capability detection. Rejected: it optimises
  ~134 MB of model cache at the cost of exact parity, while the 4.75 GB of
  dependency bloat is the actual problem. **fp32 chosen deliberately.**
- **Keeping sentence-transformers behind an optional extra** was considered and
  rejected: two inference paths, two test matrices, for an escape hatch nothing
  needs once parity is proven.

## Versioning policy (pre-1.0 SemVer)

Project convention: **minor** = user-visible breaking change, **patch** =
additive or fix-only.

Proposed **v0.5.0 (minor)**. There is no on-disk format break and **no reindex
is required**, but the change is user-visible: the dependency set changes
drastically, and the first `refresh` after upgrading downloads ~180 MB of ONNX
weights (the cached `.safetensors` files are not reused). Flagging rather than
deciding — a maintainer may reasonably call this a patch since nothing breaks.

Per the release process, this PR leaves `CHANGELOG.md` under `[Unreleased]` and
does **not** bump `pyproject.toml` / `_SERVER_VERSION`; the release chore does that.

## Parity is the whole argument

An ONNX export is a format change, not a math change. Measured against the
pre-migration backend, same weights, same machine:

- **Bi-encoder**: cosine `1.000000` per text, max elementwise diff **2.5e-07**,
  including at the 256-token truncation boundary and across the batch boundary.
- **Cross-encoder**: max logit diff **7.6e-06**, ranking identical.
- **Through the full retrieval stack**: identical result sets, identical
  ranking, score deltas ≤ 1.2e-06.

Hence **existing indexes stay valid**. This was confirmed in practice, not just
in theory: an incremental `refresh` wrote 13,955 new records into an index
already containing torch-computed vectors, and retrieval remained correct.

## Files modified

| File | Change |
|---|---|
| `services/onnx_backend.py` | **new** — tokenizer + ORT session, repo-id resolution, topology reading, cache probe, revision pinning |
| `services/embedding.py` | `_get_model` returns `OnnxTextEncoder`; new `ensure_model()` |
| `services/retrieval/cross_encoder.py` | `_load_model` builds `OnnxCrossEncoder`; `is_model_cached` / `resolve_hf_cache_dir` delegate to the backend |
| `shared/config.py` | field descriptions no longer say "sentence-transformers" |
| `shared/model_loading.py` | drop `TRANSFORMERS_VERBOSITY` (nothing reads it now) |
| `cli.py` | `refresh` pre-downloads the embedding model; pre-warm docstring rewritten |
| `pyproject.toml` | drop `sentence-transformers`, `transformers`; add `onnxruntime`, `tokenizers`, `huggingface-hub`, `numpy`; mypy override for the two untyped libs |
| `uv.lock` | regenerated — torch, triton, 15 nvidia packages, sklearn/scipy/sympy removed |
| `.github/workflows/ci.yml`, `justfile` | drop the two torch `--ignore-vuln` entries (in lockstep — `test_audit_sync.py` enforces it) |
| `AGENTS.md` | two security entries won't-fix → resolved; new CLI smoke item 8 |
| `CHANGELOG.md`, `CLAUDE.md`, `docs/CONTRIBUTING.md`, `docs/security/threat-model.md`, `docs/decisions/vector-backend.md` | prose + stack updates |

Tests added: `tests/unit/test_onnx_backend.py`,
`tests/integration/test_onnx_parity.py` (+ committed reference fixture),
`tests/integration/test_concurrent_model_load.py`.

## Things that would have bitten us

1. **Bare model names do not resolve on the Hub.** `EmbeddingConfig.model_name`
   defaults to `all-MiniLM-L6-v2`, which sentence-transformers silently
   prefixed with its own org. `hf_hub_download("all-MiniLM-L6-v2", …)` is a 404.
   Renaming the default is not an option — it is public config and pinned by
   `test_config.py`. Handled by `resolve_repo_id`, which is a pure string
   decision so it behaves identically under `HF_HUB_OFFLINE=1`.
2. **The cache probe would have silently lied.** `is_model_cached` checked for
   `config.json`, which every pre-migration cache already has — but those caches
   have no `onnx/` directory. Left alone, an upgraded install would report the
   reranker as cached, enable it at startup, then fail on the first query because
   `quiet_model_loading` sets `HF_HUB_OFFLINE=1`. Now probes `onnx/model.onnx`.
   Verified against a real pre-migration cache snapshot and pinned by a test.
3. **Construction must stay I/O-free.** `test_retrieval.py` builds
   `CrossEncoderReranker(enabled=True)` and asserts `.enabled` immediately, with
   no model cached. Any ORT session in `__init__` breaks it.
4. **The manual sigmoid must stay.** `_score` applies its own sigmoid;
   sentence-transformers v5+ `predict()` already returned raw logits, so the ONNX
   backend must too. Returning probabilities would double-sigmoid every stored
   score into (0.5, 0.73) — order-preserving, therefore invisible to ranking
   tests. Pinned explicitly.
5. **`uv sync --frozen` in all four workflows** — the lock had to be regenerated
   in the same commit or every job fails before a test runs.
6. **mypy `--strict` with no `ignore_missing_imports`** — `onnxruntime` and
   `tokenizers` ship no stubs; scoped override added.

## Testing performed

- Full suite: **1212 passed, 6 skipped**. `ruff`, `mypy --strict`, `bandit`,
  `pip-audit` all clean.
- **Parity**, against reference vectors captured from the old backend and
  committed as a fixture so the test runs without torch installed.
- **A/B against a pre-migration build** (worktree at HEAD, same 0.4.0) plus the
  released 0.2.1 tool as an independent cross-check:
  - CLI `--help` byte-identical across all 12 commands; exit codes match.
  - MCP `tools/list` — 8 tools, full schemas byte-identical.
  - MCP `tools/call` responses byte-identical.
  - Query outputs byte-identical (reranker off) / identical ranking with
    ≤1.2e-06 score deltas (reranker on), across every tool, filter, and lookup mode.
- **Pre-Merge Live MCP Smoke Test**: all 54 checks pass against the live index.
- `scripts/smoke_check_deprecation.py` and `scripts/smoke_check_removals.py`: pass.
- CLI query smoke items 1–5, 7–8: pass.
- Wheel smoke (mirrors `release.yml`): clean 311 MB install, `serve` exits 2 on an
  empty index before touching a model.
- Retrieval-quality benchmark: passes.

## Results

| | before | after |
|---|---|---|
| Linux x86_64 install | 5.1 GB | **351 MB** (−93%) |
| macOS install | 1.0 GB | **310 MB** (−70%) |
| Linux packages | 129 | 94 |
| `embed_query` | 2.91 ms | **0.76 ms** |
| `embed_texts` (64) | 18.90 ms | **14.55 ms** |
| rerank (20 pairs) | 8.12 ms | **7.36 ms** |
| first model load | 1765 ms | **127 ms** |
| `serve` boot-to-ready | 2183 ms | **1043 ms** |
| `search-docs` end-to-end | 3775 ms | **1238 ms** |
| peak RSS (search + rerank) | 1104 MB | **908 MB** |
| standing `pip-audit` ignores | 3 | **1** |

Commands that load no model (`status`, `check-deprecation`, `--version`) are
unchanged within noise.

## Findings

### Multi-concept CLI queries were crashing before this change

Found while A/B testing, not anticipated. On the pre-ONNX backend,
`search-docs "TTS + STT"` with the reranker enabled (the default) failed
**12 out of 12 runs** — 10 SIGSEGV/SIGBUS, 2 hangs — on macOS arm64 / Python
3.14.4 / torch 2.13. Reproduced on both a source build and the released 0.2.1
tool install, so it is not an artefact of the test setup. The ONNX backend
passes 12/12.

Mechanism: multi-concept fans out one concurrent search per concept, and the
one-shot CLI does no pre-warm, so several `asyncio.to_thread` workers raced to
lazily construct the torch cross-encoder. `serve` pre-warms at boot,
single-threaded, before any query — which is why only the CLI front door
exposed it, and why it went unnoticed.

Multi-concept search is a documented headline feature, so this was a total
failure of it from the CLI. Frozen twice per the AGENTS.md rule:
`tests/integration/test_concurrent_model_load.py` and CLI smoke item 8.

**Scope**: confirmed on macOS arm64 / Python 3.14.4 / torch 2.13. Not tested on
Linux or Windows.

### A batch-throughput regression, caught and fixed pre-merge

The first implementation set `intra_op_num_threads = 1` to avoid oversubscribing
a developer machine. Benchmarking showed that cost ~35% on batch work for no
single-query benefit. A thread sweep (18-core, median, batch of 32) found:
1 thread 13.8 ms, 2 threads 16.4 ms, 4 threads 9.5 ms, all cores 10.2 ms, while
single-query latency is flat at ~0.85 ms throughout. Settled on
`min(4, cpu_count)` — the knee, and faster than the torch backend it replaces.
Re-verified parity afterwards; multi-threaded reduction order did not perturb results.

### Model downloads are now revision-pinned

`bandit` B615 flagged the direct `hf_hub_download` calls. The right fix was
substantive rather than a suppression: all four shipped models are pinned to
immutable commit SHAs. This closes a supply-chain gap that existed under the old
backend too (both resolved `main` at fetch time), and — more importantly here —
protects the migration's core premise. An upstream re-export of
`onnx/model.onnx` would otherwise silently start writing incompatible vectors
into indexes built from the old ones. Bump a pin only alongside a re-run of the
parity test, and treat a failure there as "this needs a reindex".

Bandit also caught a real bug in passing: after routing downloads through the
pinned helper, one `hf_hub_download` call remained whose import had been removed —
a latent `NameError` on the session-creation path.

## Follow-ups (not in this PR)

- **chromadb's server-mode dependencies.** `kubernetes` (41 MB) plus `grpc` and
  `opentelemetry` are hard dependencies of chromadb but unreachable for the
  embedded `PersistentClient` the hub uses — verified by uninstalling
  `kubernetes` and confirming `PersistentClient` still works. Not expressible in
  `pyproject.toml`; worth an upstream issue.
- **`EmbeddingConfig.dimension`** is set but read nowhere in `src/`. Dead config;
  left alone to keep this diff scoped.
- **`ruff format` drift**: 22 files in the repo do not match the formatter. CI
  gates on `ruff check` and `mypy` only, not format, so this is pre-existing and
  untouched here (the count went 24 → 22 because two files this PR edits were
  formatted).
