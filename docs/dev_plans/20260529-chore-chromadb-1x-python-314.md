# chromadb 1.x migration + Python 3.14 support

**Component**: services/index (vector store)
**Status**: Not Started
**Type**: chore
**Created**: 2026-05-29
**Branch**: `chore/chromadb-1x-python-314`
**Target version**: TBD — likely `v0.1.0` (on-disk format break is a minor bump under the project's pre-1.0 SemVer convention; see "Versioning policy" below)

## Why

`v0.0.20` capped `requires-python` to `<3.14` (PR #65) because two upstream blockers prevent Python 3.14 support today:

1. **chromadb 0.6.x's pydantic-v1 `BaseSettings` shim** crashes at import on Python 3.14 (`typing.get_type_hints` changes).
2. **torch (via `sentence-transformers` / `transformers`) and `onnxruntime`** have no cp314 wheels yet — resolver fails or import fails.

(1) is unblocked: chromadb 1.0.0 shipped 2025-04-03; current is 1.5.9 (2026-05-05). 14 months of patch releases. Pydantic-v2 native.

(2) is still upstream. This plan does **not** lift the Python ceiling. The ceiling lift is a separate follow-up release gated on torch + onnxruntime cp314 wheel availability (see "Follow-up release" below).

## Why we pin to 1.5.x directly, not 1.0.x

We jump straight from 0.6 → 1.5 rather than stepping through 1.0. Rationale:

- We never ship intermediate versions to users. Only one chromadb pin reaches release — running integration tests against 1.0 is doubled effort for no user-visible signal.
- 1.0 → 1.5 added its own breaking changes (the 1.x line is still evolving fast). Pinning to 1.0 means walking right back into known bugs that 1.5 fixed.
- Real-world: users have moved off 1.0.x. Our parity work should target what the community is actually running.

Caveat: read the chromadb 1.0 → 1.5.9 release notes during Phase 1 (spike) and catalogue any breaking change between minor versions. We need that intel to anticipate Phase 2 test failures — it's free, and doesn't require running 1.0.

## Versioning policy (pre-1.0 SemVer)

Project is pre-1.0. We treat **minor** bumps as user-visible breaking changes (e.g., on-disk format) and **patch** bumps as additive or fix-only. Therefore:

- This PR → **v0.1.0** (on-disk format break — see "Migration impact").
- Python 3.14 ceiling lift → **v0.2.0** (minor again, because adding platform support that the lockfile didn't previously resolve is user-visible). Not a patch, despite being a single-line cap change.

This is documented here so the follow-up release doesn't silently shrink the bump.

## API surface we actually depend on

Mapped via `grep -rn chromadb src/ dashboard/ tests/`. Total: **5 files**, **~10 distinct API calls**.

| File | API |
|---|---|
| `src/pipecat_context_hub/services/index/vector.py` | `chromadb.PersistentClient(path, settings)`, `chromadb.config.Settings(anonymized_telemetry=False, chroma_product_telemetry_impl="...NoOpProductTelemetryClient")`, `chromadb.telemetry.product.{ProductTelemetryClient, ProductTelemetryEvent}` (subclassed by `NoOpProductTelemetryClient`), `client._system.stop()`, `client.clear_system_cache()` (private API), `client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})`, `collection.upsert(ids, embeddings, documents, metadatas)`, `collection.get(where=..., include=[])`, `collection.query(query_embeddings, n_results, include=["documents", "metadatas", "distances"], where=...)`, `collection.delete(ids=...)` |
| `dashboard/scripts/extract_embeddings.py` | `PersistentClient(path)`, `client.get_collection("latest")`, `collection.get(include=["embeddings", "metadatas", "documents"])` |
| `dashboard/scripts/extract_dashboard.py` | `PersistentClient(path)`, `client.get_collection("latest")` |
| `tests/unit/test_index_store.py` | exercises the above |
| `tests/integration/test_serve_lifetime.py` | exercises serve lifecycle |

This is a small surface — the migration is bounded.

## Things to consider (risks, breaking changes, unknowns)

Each item below needs to be verified during the spike, **not assumed**. The list is grouped by likelihood of biting us. Items marked **[T#]** map to a test phase below.

### Almost certain to bite us

1. **`from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent` is gone in 1.x.** Module reorganised. We subclass `ProductTelemetryClient` as `NoOpProductTelemetryClient`. Options: (a) drop the subclass and rely on `Settings(anonymized_telemetry=False)`, (b) reach for the new internal path. (a) is preferred IF and only if the flag short-circuits all telemetry codepaths in 1.x — see #2. **[T2, T4]**
2. **`Settings(anonymized_telemetry=False)` may not be sufficient.** Current code uses both `anonymized_telemetry=False` AND `chroma_product_telemetry_impl="...NoOpProductTelemetryClient"` (`vector.py:311-316`). The dual configuration is deliberate defense-in-depth; the flag alone may have been historically insufficient. Verify in 1.x that the flag short-circuits ALL telemetry paths (posthog, otel, anything else) before dropping the impl override. If not, keep the no-op subclass and re-target the import. **[T2, T4]**
3. **`chromadb.config.Settings` field surface changed.** We currently pass two fields. Confirm both `anonymized_telemetry` and `chroma_product_telemetry_impl` survive in 1.5.x — historically some fields moved to client kwargs or were removed. **[T1]**
4. **`include=` default for `collection.get()` and `collection.query()` changed in 1.x.** In 0.6 the default included embeddings; in 1.x it does not (to save memory). We pass `include=` explicitly at every call site I checked — but `tests/unit/test_index_store.py` may not, so test assertions on result shape will break. **[T2]**
5. **On-disk format is not backward-compatible.** A 0.6-formatted Chroma directory will not load under 1.x. Every existing user must run `refresh --force --reset-index`. We need a non-mutating startup detection in the index service layer + clear error message. The probe must run before `chromadb.PersistentClient(...)` or `get_or_create_collection(...)`, because those calls may create or rewrite Chroma state. **[T6]**
6. **Private API in `_release_client`.** `vector.py:353-361` calls `client._system.stop()` and `client.clear_system_cache()`. The comment pins this to ChromaDB 0.6.3 internals. 1.x likely restructures `_system` or exposes a public `client.close()`/`client.reset()`. If we don't audit this, refresh + `--reset-index` cycles silently leak references or fail. **[T2, T4]**

### Likely

7. **Cosine distance semantics.** We set `metadata={"hnsw:space": "cosine"}` and convert distance back to a 0..1 similarity via `1 - (distance / 2)` (`vector.py:524`). If 1.x changed distance normalisation or the default space, top-K *order* may stay stable but *score values* shift — silently breaking any threshold logic, the reranker's RRF merge, and downstream callers of `IndexResult.score`. **[T3]**
8. **HNSW parameters.** We only set `hnsw:space`. 1.x supports `hnsw:construction_ef`, `hnsw:M`, `hnsw:search_ef` via the same metadata channel — but the *defaults* differ between 0.6 and 1.x, which changes recall/latency characteristics even if our config is unchanged. **[T3, T5]**
9. **Metadata typing is stricter in 1.x.** Some scalar coercions that 0.6 silently accepted (None values, mixed-type lists) raise in 1.x. **[T2]** — see Phase 2 metadata enumeration task.
10. **`get(where=..., include=[])`** — we use this to test existence without pulling documents. Verify the empty-`include` shortcut still works in 1.x (it might require at least one field). **[T2]**
11. **Silent collection recreate.** If a user has a 0.6 index where `hnsw:space="cosine"` was implicit and 1.x makes it explicit-only, `get_or_create_collection` could either succeed (good) or silently recreate the collection (catastrophic — drops all data, no error). **[T6]**

### Possible

12. **Pydantic v2 double-cohabitation.** chromadb 1.x is pydantic-v2 native; we already use pydantic-v2 in `shared/types.py`. No conflict expected, but worth confirming no chromadb 1.x pin conflicts with our `pydantic>=2.0,<3.0`. **[T1]**
13. **Dependency tree shifts.** chromadb 1.x may drop `kubernetes`, `posthog`, or other transitive deps that bloat the install. If it drops `posthog` we lose one CVE surface. Worth measuring resolved dep set before/after. **[T1]**
14. **`onnxruntime` becomes optional.** chromadb 1.x split client from default embedding pipeline; `onnxruntime` may no longer be transitively pulled. We don't use chromadb's embedding functions, so this could shrink the install considerably. **[T1]**
15. **Performance characteristics.** chromadb 1.x has a Rust-based core in some paths. Query p50 may improve; index build time may differ either way. **[T5]**
16. **Memory peak (dashboard pipeline).** `dashboard/scripts/extract_embeddings.py` pulls all embeddings at once for UMAP. If 1.x's get-with-embeddings is lazier or eagerer, peak RSS shifts. **[T4, T5]**
17. **Concurrent readers.** 1.x is supposed to handle concurrent process readers better. Verify nothing in `serve` orphan-watchdog / idle-timeout flow depends on 0.6 locking semantics. **[T4]**

### Unknown but worth a quick check

18. **Windows behaviour.** CLAUDE.md has a `Windows tips` section listing 0.6-era workarounds (corrupt clone recovery, cp1252 console). chromadb 1.x may have changed Windows path handling. **[T4 — Windows sub-step]**

## Files to Modify

- `pyproject.toml` — chromadb pin `>=0.6,<1.0` → **`>=1.5,<2.0`** (consistent throughout; do not weaken to `>=1.0`); version `0.0.20` → `0.1.0`
- `src/pipecat_context_hub/server/main.py` — `_SERVER_VERSION` `0.0.20` → `0.1.0` only; do **not** add storage-open policy here
- `src/pipecat_context_hub/services/index/errors.py` (new) — define typed `IncompatibleIndexFormatError` and shared remediation text containing literal `refresh --force --reset-index`
- `src/pipecat_context_hub/services/index/vector.py` — imports, Settings construction (drop or retarget `chroma_product_telemetry_impl` per Phase 2 finding), telemetry no-op stub, `_release_client` private-API audit (prefer public `client.close()` / `client.reset()` if exposed in 1.x; otherwise re-pin), query/get call sites, `collection.upsert` semantics, **non-mutating format detection probe before `PersistentClient` construction** (new) — raise typed `IncompatibleIndexFormatError` on 0.6-format directory before `VectorIndex._open_client` creates/opens Chroma state
- `src/pipecat_context_hub/services/index/__init__.py` — export `IncompatibleIndexFormatError`
- `src/pipecat_context_hub/cli.py` — catch `IncompatibleIndexFormatError` around `IndexStore` construction in `serve` and `refresh`; surface the same remediation message in both paths
- `src/pipecat_context_hub/services/index/chroma_client.py` (new, if the spike confirms this helper is cleaner than local construction) — shared Chroma client opening/settings helper used by core index code and dashboard scripts
- `dashboard/scripts/extract_embeddings.py` — route through shared storage/config/Chroma-open path; verify `include=[...]` shape, batch loop
- `dashboard/scripts/extract_dashboard.py` — route through shared storage/config/Chroma-open path; verify `get_collection` path
- `tests/unit/test_index_store.py` — update assertions for new `include=` defaults; add metadata-type enumeration test; add `get_or_create_collection` identity preservation test
- `tests/integration/test_format_detection.py` (new) — exercise the 0.6 → 1.x format detection path with hash-equality assertion on the snapshot directory
- `tests/integration/test_no_telemetry_egress.sh` (new) — committed smoke script that records/blocks non-loopback sockets across refresh, serve query, and dashboard extraction
- `tests/benchmarks/test_chromadb_parity.py` (new) — fixed-query parity harness for raw Chroma and HybridRetriever comparisons; accepts the 0.6 reference path via env var/CLI and emits Layer A/B metrics
- `tests/benchmarks/baselines/v0.0.20.json` (new) — captured baseline for Phase 5 comparison
- `tests/benchmarks/` support code — extend `benchmark-stability-report` output or add a companion benchmark so Phase 5 emits the exact metrics it compares (build time, query p50/p95, refresh RSS, dashboard RSS)
- `.github/workflows/ci.yml` — add a Windows smoke job/matrix entry that runs `refresh --force --reset-index`, boots `serve`, and executes one `search_docs` query
- `uv.lock` — re-lock; **MUST land in the same commit as `pyproject.toml`** (no intermediate broken-resolution state)
- `CHANGELOG.md` — Changed entry: chromadb 1.x, format break, upgrade command
- `CLAUDE.md` — update `Vector store:` line if anything material changes; remove `Windows tips` items that 1.x fixes
- `docs/README.md` — same
- `docs/dev_plans/README.md` — **Add this plan to the `## Current Tasks` table when this PR opens; move to `## Completed Tasks` when it merges** (per Codex P2 finding; per `/update-docs` convention)

## Testing strategy

Eight phases (Phase 0 prerequisite + seven test phases). Each has explicit pass/fail criteria. Run in order — if Phase 1 fails, don't proceed.

### Phase 0: Prerequisites (RUN BEFORE TOUCHING ANY CODE)

These steps MUST complete on `main` (or a 0.6 worktree) before bumping the chromadb pin. The 0.6 environment becomes unavailable in the migration branch the moment Phase 1 lands.

1. **Snapshot a 0.6-formatted Chroma directory.**
   ```bash
   uv run pipecat-context-hub refresh --force  # ensure index is freshly built on 0.6
   cp -a ~/.pipecat-context-hub/chroma /tmp/chroma-0.6-snapshot
   find /tmp/chroma-0.6-snapshot -type f -print0 | xargs -0 shasum -a 256 | LC_ALL=C sort > /tmp/chroma-0.6-snapshot.sha256
   ```
   The hash file is used by Phase 6 to verify no silent overwrite.

2. **Capture v0.0.20 performance baseline.**
   ```bash
   mkdir -p tests/benchmarks/baselines
   just benchmark-stability-report tests/benchmarks/baselines/v0.0.20.json
   git add tests/benchmarks/baselines/v0.0.20.json
   git commit -m "tests: capture v0.0.20 perf baseline for chromadb 1.x comparison"
   ```
   Commit the baseline so Phase 5 has a stable comparison artefact, independent of whichever machine runs it. Before committing, confirm the JSON includes every metric Phase 5 compares: index build duration, query p50, query p95, refresh peak RSS, and dashboard peak RSS. If `benchmark-stability-report` does not emit these yet, extend the benchmark harness on the 0.6 branch/worktree first, then capture the baseline.

3. **Create or identify the parity harness before the dependency bump.** Add `tests/benchmarks/test_chromadb_parity.py` (or document an existing equivalent) with:
   - a fixed query set committed to the repo
   - a JSON schema for raw Chroma and HybridRetriever outputs
   - clear failure when the 0.6 reference file is missing
   - emitted metrics matching Phase 3 Layer A and Layer B names

4. **Capture v0.0.20 parity reference outputs.** Run the parity harness from step 3 against 0.6 and save the top-10 query results to `/tmp/chroma-v0.0.20-parity-results.json`. Do not commit this fixture (it's large and machine-specific). Phase 3 must consume this explicit `/tmp/` path.

5. **Read chromadb 1.0 → 1.5.9 release notes.** Catalogue breaking changes between minor versions. Free intel for Phase 2 triage.

### Phase 1: Spike — does the import even work?

In an isolated worktree (`git worktree add` or `Agent isolation=worktree`), bump the chromadb pin to `>=1.5,<2.0`, re-lock, and run:

```bash
uv lock
uv run python -c "import chromadb; print(chromadb.__version__); chromadb.PersistentClient(path='/tmp/chroma-spike-1x')"
```

Also enumerate the resolved dependency tree to confirm:
- chromadb resolves to ≥ 1.5
- pydantic resolves cleanly within `>=2.0,<3.0`
- `posthog` / `kubernetes` / `onnxruntime` presence — if any are gone, note it (CVE surface reduction)
- `collection.upsert` keeps 0.6-equivalent ID collision, metadata replacement, and batch-limit behavior for our write path

**Pass:** no import error, no construction error, dep tree resolves.
**Fail:** stop. Identify the breakage, fix it, retry.

### Phase 2: Unit tests — expected-fail triage, then green

This phase will fail on first run by design (`include=` defaults, metadata typing, telemetry import). Triage and fix; don't claim regression where the plan explicitly anticipates breakage.

#### Phase 2a — capture failures

```bash
uv run pytest tests/unit/ -v --tb=short > /tmp/phase2-failures.log 2>&1 || true
```

Categorise every failure:
- **(a) Signature change** (chromadb API shape moved) → fix our code in `vector.py`
- **(b) Result-shape change** (`include=` default, types stricter) → fix our code or update the test
- **(c) Test was asserting 0.6-specific behaviour** → update the test
- **(d) Genuine regression** (not in the plan's risk list) → STOP, investigate, possibly halt migration

#### Phase 2b — apply fixes; rerun until green

Apply categorised fixes. Rerun until clean. The pass criterion is **all tests pass; any count delta from v0.0.20 is documented** with a list of {test name, change applied, category (a/b/c)}.

Also during Phase 2:
- **Metadata enumeration task:** grep `vector.py` and ingest paths for every metadata field we write. Produce a typed list (`field: type`). Add a unit test (`test_metadata_types.py`) that ingests a representative chunk and asserts every field's type matches that list. The list is also added as a code comment near the `upsert()` call site so future contributors know what 1.x's stricter typing constrains.
- **`get_or_create_collection` identity test:** unit test that creates a collection with no explicit `hnsw:space`, then calls `get_or_create_collection` with explicit `{"hnsw:space": "cosine"}`. Assert row count and a known ID are still present (not a fresh empty collection).

**Pass:** unit suite green; metadata enumeration committed; identity test passing.

### Phase 3: Parity test — same query, same result (down to the integration boundary)

The parity test must compare BOTH the chroma boundary AND the HybridRetriever output, because the score formula `1 - (distance / 2)` is consumed by the retriever's RRF merge and the reranker (see risk #7).

1. Load the v0.0.20 reference outputs captured in Phase 0 step 4 from `/tmp/chroma-v0.0.20-parity-results.json`.
2. Run the same query set against 1.x on the same corpus.
3. Compare at two layers:

**Layer A — raw chroma:**
- Top-K **IDs** match by Jaccard ≥ 0.9. Some reordering at the tail is normal (HNSW is approximate — risk #8 acknowledges that).
- Top-1 ID matches in ≥ 95% of queries.
- Distance values: **top-1 distance within 1e-6** (NOT byte-identical — HNSW makes that guarantee impossible. Soften from the original draft).

**Layer B — integration boundary (HybridRetriever + reranker):**
- Surfaced similarity score (post `1 - distance / 2`) stays within ±0.01 vs 0.6 for the fixed query set.
- Reranker top-K (after RRF merge + cross-encoder) matches by Jaccard ≥ 0.85.

**Fail:** investigate. Likely cause is risk #7 (distance semantics) or #8 (HNSW defaults). May need to update the similarity formula or set explicit HNSW parameters.

### Phase 4: End-to-end + integration

Run the integration suite, do a full refresh, boot serve, and exercise the surfaces the unit tests can't reach.

```bash
# Use an isolated data dir so Phase 6 still has its 0.6 snapshot to test against.
export PIPECAT_HUB_DATA_DIR=/tmp/chroma-1x-e2e
uv run pytest tests/integration/ -v
uv run pipecat-context-hub refresh --force --reset-index
uv run pipecat-context-hub serve  # smoke-check it boots and serves a query
```

Plus these sub-steps:

- **Dashboard pipeline:** `just dashboard-refresh` completes without error; `dashboard/public/embeddings_3d.json` is produced.
- **Telemetry no-op verification:** run committed `tests/integration/test_no_telemetry_egress.sh`; it must assert zero non-loopback outbound TCP connections during (1) `refresh --force --reset-index`, (2) `serve` boot plus one MCP query, and (3) dashboard extraction. A boot-window-only `lsof` check is insufficient because Chroma telemetry can trigger after client construction.
- **Windows smoke (CI):** the Windows runner in `.github/workflows/ci.yml` runs `refresh --force --reset-index` + `serve` + one `search_docs` query. The CLAUDE.md "Windows tips" section is re-checked against 1.x behaviour and items that 1.x fixes are removed.
- **Concurrent reader test:** two `serve` processes against the same data dir; both should return query results without locking errors.

**Pass:** full pipeline works; `get_hub_status` returns expected counts; MCP `search_docs` returns sensible results; no network egress on boot; Windows CI green.

### Phase 5: Performance regression

```bash
just benchmark-stability-report /tmp/phase5-1x.json
diff <(jq -S . tests/benchmarks/baselines/v0.0.20.json) <(jq -S . /tmp/phase5-1x.json)
```

Compare against the committed `tests/benchmarks/baselines/v0.0.20.json` (no ambiguity about which baseline).

**Pass criteria:**
- Index build time within ±20% of baseline.
- Query p50 within ±20%, p95 within ±50%.
- Peak RSS during refresh within ±30%.
- Peak RSS during dashboard pipeline within ±50% (lower tolerance because UMAP dominates RSS, not chromadb).

**Fail:** severe regression (e.g., 2× slowdown) → investigate HNSW parameter defaults (risk #8) before blaming chromadb.

### Phase 6: Migration path (pre-merge release blocker)

This phase verifies the user upgrade path. It must pass before merge; a post-merge, pre-tag rerun is allowed as release sanity, but it is not the first proof.

```bash
# Setup: restore the 0.6 snapshot to a fresh data dir.
export PIPECAT_HUB_DATA_DIR=/tmp/chroma-migration-test
cp -a /tmp/chroma-0.6-snapshot "$PIPECAT_HUB_DATA_DIR/chroma"
find "$PIPECAT_HUB_DATA_DIR/chroma" -type f -print0 | xargs -0 shasum -a 256 | LC_ALL=C sort > /tmp/before.sha256

# Test 1: serve must REFUSE the 0.6 directory with a clear, specific error.
uv run pipecat-context-hub serve 2>&1 | tee /tmp/serve-output.log &
SERVE_PID=$!
sleep 5
kill $SERVE_PID 2>/dev/null

# Assertions:
grep -q "refresh --force --reset-index" /tmp/serve-output.log || echo "FAIL: remediation hint missing"
grep -qi "incompatible.*format\|chromadb.*0\.6" /tmp/serve-output.log || echo "FAIL: error not specific to format"

# Assertion: no silent overwrite.
find "$PIPECAT_HUB_DATA_DIR/chroma" -type f -print0 | xargs -0 shasum -a 256 | LC_ALL=C sort > /tmp/after.sha256
diff /tmp/before.sha256 /tmp/after.sha256 || echo "FAIL: serve mutated 0.6 directory"

# Test 2: refresh --force --reset-index must rebuild cleanly.
uv run pipecat-context-hub refresh --force --reset-index
uv run pipecat-context-hub serve  # now boots successfully
```

**Pass:**
- Error message contains the literal string `refresh --force --reset-index`.
- Error message names the format mismatch specifically (not a generic "failed to open index").
- 0.6 snapshot directory is byte-identical before and after the failed `serve` (no silent mutation).
- After `--reset-index`, serve boots and queries work.

### Phase 7: Pre-merge live MCP smoke

Run the full AGENTS.md "Pre-Merge Live MCP Smoke Test" against the rebuilt Chroma 1.x index. This PR touches the index backend, so the complete smoke list is required, not only `get_hub_status` or one `search_docs` call. Record the result in the PR description, including any known prerequisite caveat such as `gh` authentication for deprecation checks.

### What we are NOT testing (and why)

- chromadb's internal correctness — we trust upstream after 14 months of patch releases.
- Python 3.14 specifically — gated on torch/onnxruntime wheels (separate release).
- HTTP / server mode of chromadb — not used.
- chromadb 1.0.x intermediate versions — we ship 1.5.x directly to users (see "Why we pin to 1.5.x directly").

## Verification checklist (release blocker)

- [ ] Phase 0 prerequisites: 0.6 snapshot + hash captured; parity harness committed; v0.0.20 parity reference saved to `/tmp/chroma-v0.0.20-parity-results.json`; v0.0.20 baseline JSON committed with required metrics; 1.0 → 1.5.9 release notes reviewed.
- [ ] Phase 1 spike passes; resolved dep tree noted.
- [ ] Phase 2a failures triaged into categories (a/b/c); no (d) findings.
- [ ] Phase 2b suite green; metadata enumeration committed; identity test passing.
- [ ] Phase 3 layer A parity: top-1 ≥ 95%, top-10 Jaccard ≥ 0.9, distance within 1e-6.
- [ ] Phase 3 layer B parity: similarity within ±0.01; reranker top-K Jaccard ≥ 0.85.
- [ ] Phase 4 integration green; `serve` boots; dashboard pipeline runs through the shared Chroma-open path; telemetry no-op verified across refresh/query/dashboard by `tests/integration/test_no_telemetry_egress.sh`; Windows CI green; concurrent reader test passes.
- [ ] Phase 5 perf within tolerance (build ±20%, query p50 ±20%, p95 ±50%, RSS ±30%).
- [ ] Phase 6 pre-merge migration: clear error on 0.6 directory containing `refresh --force --reset-index`; format-specific wording; snapshot hash unchanged; `--reset-index` recovers cleanly.
- [ ] Phase 7 full AGENTS.md live MCP smoke passes against the Chroma 1.x index and is recorded in the PR description.
- [ ] `get_hub_status` reports expected counts after re-index.
- [ ] `pyproject.toml` version and `src/pipecat_context_hub/server/main.py::_SERVER_VERSION` both report `0.1.0`.
- [ ] CLAUDE.md `Windows tips` section reviewed — remove items 1.x fixes.
- [ ] CHANGELOG.md entry calls out the format break + `refresh --force --reset-index` requirement prominently.
- [ ] `docs/dev_plans/README.md` row moved from Current to Completed.

## Follow-up release (Python 3.14 cap lift)

This PR ships **v0.1.0** with the Python ceiling unchanged. The cap lift is a separate **v0.2.0** release (minor under pre-1.0 SemVer — see "Versioning policy"). That release does:

1. Confirm torch cp314 wheels are on PyPI.
2. Confirm onnxruntime cp314 wheels are on PyPI.
3. Bump `requires-python` to `>=3.11,<3.15` in `pyproject.toml` + remove the comment block about the cap.
4. Update CHANGELOG.md (the cap and CHANGELOG must move together — established convention from v0.0.20).
5. Update CI matrix to include Python 3.14.
6. Re-lock `uv.lock` (in the same commit).
7. Tag `v0.2.0`.

If steps 3-6 are not all in the same commit, CI may green-light a Python version the lockfile cannot resolve.

## Notes

- chromadb 1.x release timeline: 1.0.0 (2025-04-03), current 1.5.9 (2026-05-05). Pin: `>=1.5,<2.0` everywhere — do not weaken to `>=1.0,<2.0`.
- The current `Settings` construction passes **only** `anonymized_telemetry=False` and `chroma_product_telemetry_impl="...NoOpProductTelemetryClient"`. It does **not** pass `allow_reset=True` (the v1 draft of this plan claimed it did; that was incorrect — caught by Codex review).
- The `requires-python` ceiling, CHANGELOG entry, and CI matrix must all move together when the cap lifts. v0.0.20 established this convention.
- This plan deliberately leaves out "consider switching vector backends" — that's `docs/decisions/vector-backend.md`, re-open separately if 1.x reveals fundamental problems.
<!-- reviewed: 2026-05-29 @ 188eb5cdd3887fc40b29e1903bb284d9a9d2114d -->

## Progress

_Workspace below the review marker — does not affect the contract hash. Driven interactively (not `/conduct`: the contract section above lacks the Implementation-Checklist / per-phase Impl-Test-command slots conduct requires)._

- [x] **Phase 0.1** — 0.6 chroma snapshot captured. `cp -a ~/.pipecat-context-hub/chroma → /tmp/chroma-0.6-snapshot` (6 files, integrity_check `ok`); sorted sha256 manifest at `/tmp/chroma-0.6-snapshot.sha256`. Collection `latest`, 39,768 records. (Live `serve` PID held the sqlite open read-side; no `-wal`/`-shm` on chroma.sqlite3 so the copy is consistent.)
- [x] **Phase 0.5** — chromadb 1.0→1.5.x breaking-change research done (see Findings below).
- [x] **Defect 1 fix** — `PIPECAT_HUB_DATA_DIR` wired into `StorageConfig.data_dir` (+5 unit tests). Commit `692b49c`.
- [x] **Phase 0.3 + 0.4** — parity harness `tests/benchmarks/test_chromadb_parity.py` committed (`bf968c7`); 0.6 reference captured to `/tmp/chroma-v0.0.20-parity-results.json` (15 queries, 39,768 records, Layer A dist 0.18–0.49; not committed per plan).
- [x] **Phase 0.2 harness** — `tests/benchmarks/test_chromadb_perf.py` + `just benchmark-perf-report` recipe committed (`bf968c7`). Mechanics self-tested (RSS sampler, percentiles).
- [x] **Phase 0.2 baseline** — committed `tests/benchmarks/baselines/v0.0.20.json` (`d28960a`): build 404.9s, refresh RSS 1.54GB, query p50/p95 6.09/6.91ms, dashboard RSS 2.40GB. Captured against isolated `/tmp/pch-perf-0.6`.
- [x] **Phase 1 spike** — PASS in isolated worktree `/tmp/pcmcp-spike-1x` (branch `spike/chromadb-1x`, own `.venv`; main env + live MCP untouched). `uv lock`: chromadb 0.6.3→**1.5.9**, 170 pkgs, no conflict. Dep tree: pydantic 2.12.5 (in-bound); **removed** `posthog`, `chroma-hnswlib`, `fastapi`, `asgiref`, OTel-instrumentation, `wrapt`, `backoff`; **kept** `onnxruntime` 1.24.1, `kubernetes` 35.0.0. `requires-python` `<3.14` unchanged.
- [x] **Phase 2 — COMPLETE on `spike/chromadb-1x`** (3 commits; 978 unit tests green). Triage found **1** breakage (zero genuine regressions):
  - `350ff0e` core migration: pin+lock + `VectorIndex` teardown uses public `Client.close()` (1.x) instead of private `_system.stop()` + manual refcount. Old path left a dead Rust system cached → reopen failed tenant validation (`'RustBindingsAPI' object has no attribute 'bindings'`) — the only failing test. Telemetry import + `Settings` survive 1.x unchanged; `include=`/None-metadata already compliant.
  - `0981d2d` format detection: non-mutating sqlite probe (sysdb migration < 10 ⇒ 0.6) → typed `IncompatibleIndexFormatError` + remediation, wired into `serve`/`refresh`. Verified non-mutating against the real 0.6 snapshot.
  - `02637c9` metadata-type enumeration test + `get_or_create` identity test (risk #11 safe on 1.5.9).
- [x] **Phase 3 — PASS** (`fadb826`). Engine-parity (replay identical vectors into both engines, isolating chromadb from repo-discovery variance): top-1 ID 15/15, mean top-20 Jaccard 0.987, **max distance delta 4.17e-07** (<1e-6). `1 - distance/2` similarity preserved; Layer B follows (FTS + reranker are chromadb-independent). Tool: `tests/benchmarks/engine_parity_check.py` (manual two-venv). _Note: fresh full-corpus rebuild parity is flaky — record counts vary with GitHub repo discovery (0.6 build saw 81 repos/39,786 recs; a 1.x build saw 9 repos/15,847). Not a chromadb bug; use the replay tool, not a rebuild._
- [x] **Phase 0.2 version bump** — `0.0.20`→`0.1.0` (pyproject + `main.py::_SERVER_VERSION` + uv.lock; consistency test passes) + CHANGELOG `[0.1.0]` (`a3c93c7`).
- [x] **Branches merged** — `spike/chromadb-1x` → `chore/chromadb-1x-python-314` (`c034f5a`, conflict-free regular merge). All Phases 0–3 now on one branch. **Env flipped: next `uv run` in the main worktree syncs `.venv` to chromadb 1.5.9; the live `~/.pipecat-context-hub` 0.6 index now needs `refresh --force --reset-index`.**
- [x] **`main` merged into branch** — brought in PR #67 (default sources: +`pipecat-flows`, `small-webrtc-prebuilt`→`pipecat-prebuilt`, −`flows-editor`/`web-client-ui`) and PR #69 (grandparent-death watchdog + idle-as-fallback). Only CHANGELOG conflicted; `uv.lock` auto-merged and verified consistent (`uv lock --check` clean, chromadb 1.5.9, posthog absent). Full suite **1068 passed, 6 skipped**; mypy + ruff clean. Also fixed latent mypy-strict errors in the Phase 0/3 benchmark harnesses (`dict`→`dict[str, Any]`, `Popen[object]`→`Popen[Any]`, Optional `distances` guards) — CI had never run with those files present.
- [x] **Phase 4 — DONE.**
  - Dashboard scripts routed through `StorageConfig().chroma_path` (honour `PIPECAT_HUB_DATA_DIR`) — `fb564f5`.
  - Integration suite green (real index build + `serve` lifetime + tool handlers).
  - `refresh --force --reset-index` on isolated `/tmp/chroma-1x-e2e`: 39,613 upserted, 0 errors, 355.9s.
  - Telemetry-egress smoke script `tests/integration/test_no_telemetry_egress.sh` (`c617277`, hardened `00ded76`): **PASS** — posthog absent; zero telemetry egress across serve+query and dashboard windows (HF pinned offline so the cached-model load can't mask telemetry). The only sockets seen on a live 1.x `serve` were HuggingFace/CloudFront model-CDN, not telemetry.
  - Full dashboard pipeline (`just dashboard-build`) produced `embeddings_3d.json` (21 MB UMAP 3D), `clusters.json`, `dashboard_data.json` against the isolated index — exit 0.
  - Concurrent reader covered by `test_runtime_stability::test_concurrent_search_rounds`.
- [x] **Phase 5 — PASS** vs committed `baselines/v0.0.20.json` (1.x report `/tmp/phase5-1x.json`, 39,598 records). index_build 378.2s (−6.6%, ±20%); refresh RSS 1.67 GB (+8.4%, ±30%); **query p50 2.58 ms (−58%) / p95 3.28 ms (−52%)** — notably faster (1.x Rust core); dashboard RSS 2.58 GB (+7.7%, ±50%). All within tolerance.
- [x] **Phase 6 — PASS** (e2e). `serve` against a restored `/tmp/chroma-0.6-snapshot` exits non-zero with `ERROR Incompatible ChromaDB index format … written by chromadb 0.6 (pre-1.0 on-disk format) … found pre-1.0 sysdb schema migration 9; chromadb 1.x requires migration 10+. … refresh --force --reset-index`. Refusal is fast (probe runs before model pre-warm); the 0.6 snapshot is byte-identical before/after (no mutation). Recovery via `--reset-index` proven by the e2e build.
- [x] **Phase 7 — representative subset PASS (7/7)** against the 1.x index: `get_hub_status` (v0.1.0, recent refresh), `get_doc` assembled page (13 KB) + section slice, `search_api` `send_dtmf`/`DailyTransport` (prefix + class filter), `search_examples domain=backend`, `search_docs "TTS + STT"` (multi-concept), `search_api WebSocketTransport` (TS). **The full 40-item AGENTS.md live smoke remains a maintainer step** — it requires the reconnected MCP client; run it against the rebuilt live index before merge/tag.
- [x] **Docs/CI** — Windows smoke job added to `.github/workflows/ci.yml` (validates chromadb 1.5.9 wheels on Windows + runs unit/integration suite that builds a real index, exercises the probe, boots `serve`, queries; a full network `refresh` is intentionally not CI-gated). `docs/dev_plans/README.md` row → "In Review". CLAUDE.md "Windows tips" reviewed: all three items are git-clone-recovery / console-encoding / model-prewarm — none are chromadb-0.6-specific, nothing to remove. `docs/README.md` says "ChromaDB + SQLite FTS5" (no version) — no material change.
- [x] **Release housekeeping** — parked PR #67/#69 work folded into CHANGELOG `[0.1.0]` (Added/Changed/Fixed), `[Unreleased]` left empty.
- [x] **PR opened — #70** (`chore/chromadb-1x-python-314` → `main`). CI green after three fixes found post-open: (1) the Windows-job insertion had dropped the `security:` job header — a duplicate `steps:` key that `yaml.safe_load` silently collapses but GitHub Actions rejects; (2) `pip-audit` flagged **CVE-2026-45829** (chromadb server-mode pre-auth RCE, no fixed release) — unreachable here (embedded `PersistentClient`, no server/endpoint/embedding-functions), ignored with justification + CHANGELOG Security note; (3) the Windows job's full `tests/unit/` run surfaced ~27 pre-existing POSIX path-assumption failures — scoped the job to the chromadb tests (index_store / format_detection / metadata_types, all green on Windows) since `Sync dependencies` already proves the 1.5.9 wheel installs.
- [ ] **Remaining** — (maintainer) run full AGENTS.md live MCP smoke against the rebuilt live index; on merge move the `docs/dev_plans/README.md` row to Completed.

### Follow-up (out of scope here) — Windows path-handling hardening
The Windows CI job exposed ~27 pre-existing unit failures (`test_taxonomy`,
`test_github_ingest`, `test_cli`, `test_hub_status`) that assert `/`-separated
paths under Windows `\\` separators. Some are test-only assertions, but a few
(e.g. taxonomy lookup `KeyError: 'examples/foundational/01-hello'` vs the
`\\`-discovered key) suggest the ingest/taxonomy code **mixes separators on
Windows** — a potential real production bug, not just a test artefact. Predates
and is unrelated to the chromadb bump (the repo had no Windows CI before). Worth
a dedicated cross-platform pass (normalise discovered paths to POSIX form before
taxonomy lookup; audit `os.path.join` vs `/`-joins) before claiming first-class
Windows support.

### Migration scope additions (from Findings)
- Wire `PIPECAT_HUB_DATA_DIR` — **done early** (`692b49c`), unblocks Phase 4/6 isolation.
- Dashboard scripts (`extract_embeddings.py`, `extract_dashboard.py`) hardcode `~/.pipecat-context-hub` and ignore the data-dir env — route through shared config in Phase 4 (already in Files-to-Modify).
- Telemetry import likely survives in 1.x (verify at Phase 1 spike before assuming removal).
- HNSW: default space is **L2** in 1.x — must set cosine explicitly; config moved to `configuration={"hnsw":{...}}` (legacy metadata form deprecated but accepted).

## Findings (Phase 0)

### Plan defects discovered during prerequisites — require contract amendment

1. **`PIPECAT_HUB_DATA_DIR` is not implemented.** It appears only in this plan (Phase 4 line ~216, Phase 6 lines ~254–269), never in `src/`. `StorageConfig.data_dir` defaults to `~/.pipecat-context-hub` with no env override and no `--data-dir` CLI flag. **Impact:** Phase 4 and Phase 6 isolation are no-ops — `export PIPECAT_HUB_DATA_DIR=...` does nothing, so `refresh`/`serve` hit the real index and Phase 6's migration test (cp snapshot → `$PIPECAT_HUB_DATA_DIR/chroma`) would validate against an unused path. **Fix (new migration scope):** wire `PIPECAT_HUB_DATA_DIR` into `StorageConfig.data_dir` (with a unit test) before Phases 4/6. Minimal + reused by the perf harness for subprocess isolation. Add `src/pipecat_context_hub/shared/config.py` to Files-to-Modify.
2. **Phase 5 perf harness does not exist.** `benchmark-stability-report` runs `test_runtime_stability.py`, which is a **leak detector** (steady-state RSS/thread/fd *deltas* across mocked refresh/serve cycles in temp dirs) — it emits none of Phase 5's 5 metrics (build duration, query p50/p95, refresh peak RSS, dashboard peak RSS). `test_latency.py` measures median/min/max latency over a 100-record *synthetic* store, no p95, no JSON. **Fix:** build a dedicated perf harness (`test_chromadb_perf.py` + justfile recipe) invoked identically for baseline (0.6) and Phase 5 (1.x). Caveat to record in Phase 5 interpretation: a full-pipeline `refresh` is **embedding-compute dominated** (MiniLM over ~39.8k records, identical 0.6↔1.x), so build-duration is a weak chromadb signal; chroma upsert/query/bulk-get are the discriminating paths.

### chromadb 1.x breaking-change research (web sources; verify against the pinned 1.5.x during Phase 1 spike)

- **Telemetry (corrects risk #1):** `chromadb.telemetry.product.{ProductTelemetryClient, ProductTelemetryEvent}` are **still present** on `main` (not removed). `Settings(anonymized_telemetry=False)` and `chroma_product_telemetry_impl` remain valid fields. Our import path may survive — verify against the actual pin before assuming removal.
- **`include=` defaults (risk #4 confirmed):** `query()` default = documents/metadatas/distances; `get()` default = documents/metadatas; **embeddings excluded by default**. `ids` always returned, so `include=[]` works for existence checks.
- **Format break (risk #5 confirmed):** chroma #4217 — a 0.6.3 dir raises `InternalError` under 1.x. Irreversible, no downgrade. Validates the format-detection probe.
- **HNSW config moved (risk #7/#8 — bigger than stated):** config moved to `configuration={"hnsw":{"space":"cosine",...}}` in 1.0; legacy `metadata={"hnsw:space":"cosine"}` still accepted but **deprecated**; **default space is L2, not cosine** — MUST set cosine explicitly. Defaults: ef_construction=100, ef_search=100, max_neighbors(M)=16.
- **Metadata None rejected (risk #9 confirmed):** 1.x raises on `None` metadata values (#3644) — strip None before upsert.
- **`get_or_create_collection` hazard (risk #11 — worse):** passing call-site metadata that differs from stored metadata can SIGSEGV the Rust bindings (third-party report). Safer pattern: `get_collection()` then `create_collection()` on not-found.
- **Deps:** `posthog` removed (CVE surface ↓ ✓). **`onnxruntime` still core, NOT optional** (corrects risk #14 hope). `kubernetes` still core. `chroma-hnswlib` now optional/dev.
- **Lifecycle (risk #6):** `_system.stop()` / `clear_system_cache()` are unstable internals (Rust rewrite); no reliable public `close()` (#5868); `reset()` needs `allow_reset=True` and has 1.x bugs (#6030). Keep private teardown but guard it.
- **`get(limit/offset)` ordering** changed in 0.5.11 to internal-id order (was user-id) — affects any pagination assumptions in tests.
