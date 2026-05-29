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
| `src/pipecat_context_hub/services/index/vector.py` | `chromadb.PersistentClient(path, settings)`, `chromadb.config.Settings(anonymized_telemetry=False, chroma_product_telemetry_impl="...NoOpProductTelemetryClient")`, `chromadb.telemetry.product.{ProductTelemetryClient, ProductTelemetryEvent}` (subclassed by `NoOpProductTelemetryClient`), `client._system.stop()`, `client.clear_system_cache()` (private API), `client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})`, `collection.add(ids, embeddings, documents, metadatas)`, `collection.get(where=..., include=[])`, `collection.query(query_embeddings, n_results, include=["documents", "metadatas", "distances"], where=...)`, `collection.delete(ids=...)` |
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
5. **On-disk format is not backward-compatible.** A 0.6-formatted Chroma directory will not load under 1.x. Every existing user must run `refresh --force --reset-index`. We need a startup detection in the index service layer + clear error message. **[T6]**
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

- `pyproject.toml` — chromadb pin `>=0.6,<1.0` → **`>=1.5,<2.0`** (consistent throughout; do not weaken to `>=1.0`)
- `src/pipecat_context_hub/services/index/vector.py` — imports, Settings construction (drop or retarget `chroma_product_telemetry_impl` per Phase 2 finding), telemetry no-op stub, `_release_client` private-API audit (prefer public `client.close()` / `client.reset()` if exposed in 1.x; otherwise re-pin), query/get call sites, **format detection probe (new) — `VectorIndex._open_client` raises typed `IncompatibleIndexFormatError` on 0.6-format directory**
- `src/pipecat_context_hub/services/index/__init__.py` — export `IncompatibleIndexFormatError`
- `src/pipecat_context_hub/cli.py` — catch `IncompatibleIndexFormatError` in the `refresh` codepath and surface the same remediation message as `serve`
- `src/pipecat_context_hub/server/main.py` — catch `IncompatibleIndexFormatError` in the `serve` startup and surface the remediation message (must contain literal `refresh --force --reset-index`)
- `dashboard/scripts/extract_embeddings.py` — verify `include=[...]` shape, batch loop
- `dashboard/scripts/extract_dashboard.py` — verify `get_collection` path
- `tests/unit/test_index_store.py` — update assertions for new `include=` defaults; add metadata-type enumeration test; add `get_or_create_collection` identity preservation test
- `tests/integration/test_format_detection.py` (new) — exercise the 0.6 → 1.x format detection path with hash-equality assertion on the snapshot directory
- `tests/benchmarks/baselines/v0.0.20.json` (new) — captured baseline for Phase 5 comparison
- `uv.lock` — re-lock; **MUST land in the same commit as `pyproject.toml`** (no intermediate broken-resolution state)
- `CHANGELOG.md` — Changed entry: chromadb 1.x, format break, upgrade command
- `CLAUDE.md` — update `Vector store:` line if anything material changes; remove `Windows tips` items that 1.x fixes
- `docs/README.md` — same
- `docs/dev_plans/README.md` — **Add this plan to the `## Current Tasks` table when this PR opens; move to `## Completed Tasks` when it merges** (per Codex P2 finding; per `/update-docs` convention)

## Testing strategy

Seven phases (Phase 0 prerequisite + six test phases). Each has explicit pass/fail criteria. Run in order — if Phase 1 fails, don't proceed.

### Phase 0: Prerequisites (RUN BEFORE TOUCHING ANY CODE)

These steps MUST complete on `main` (or a 0.6 worktree) before bumping the chromadb pin. The 0.6 environment becomes unavailable in the migration branch the moment Phase 1 lands.

1. **Snapshot a 0.6-formatted Chroma directory.**
   ```bash
   uv run pipecat-context-hub refresh --force  # ensure index is freshly built on 0.6
   cp -a ~/.local/share/pipecat-context-hub/chroma /tmp/chroma-0.6-snapshot
   shasum -a 256 -r /tmp/chroma-0.6-snapshot | sort > /tmp/chroma-0.6-snapshot.sha256
   ```
   The hash file is used by Phase 6 to verify no silent overwrite.

2. **Capture v0.0.20 performance baseline.**
   ```bash
   just benchmark-stability > tests/benchmarks/baselines/v0.0.20.json
   git add tests/benchmarks/baselines/v0.0.20.json
   git commit -m "tests: capture v0.0.20 perf baseline for chromadb 1.x comparison"
   ```
   Commit the baseline so Phase 5 has a stable comparison artefact, independent of whichever machine runs it.

3. **Capture v0.0.20 parity reference outputs.** Run the parity harness (described in Phase 3) against 0.6 and save the top-10 query results as `tests/fixtures/parity/v0.0.20-results.json`. Do not commit this fixture (it's large and machine-specific) — keep it in `/tmp/` and reference from Phase 3.

4. **Read chromadb 1.0 → 1.5.9 release notes.** Catalogue breaking changes between minor versions. Free intel for Phase 2 triage.

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
- **Metadata enumeration task:** grep `vector.py` and ingest paths for every metadata field we write. Produce a typed list (`field: type`). Add a unit test (`test_metadata_types.py`) that ingests a representative chunk and asserts every field's type matches that list. The list is also added as a code comment near the `add()` call site so future contributors know what 1.x's stricter typing constrains.
- **`get_or_create_collection` identity test:** unit test that creates a collection with no explicit `hnsw:space`, then calls `get_or_create_collection` with explicit `{"hnsw:space": "cosine"}`. Assert row count and a known ID are still present (not a fresh empty collection).

**Pass:** unit suite green; metadata enumeration committed; identity test passing.

### Phase 3: Parity test — same query, same result (down to the integration boundary)

The parity test must compare BOTH the chroma boundary AND the HybridRetriever output, because the score formula `1 - (distance / 2)` is consumed by the retriever's RRF merge and the reranker (see risk #7).

1. Load the v0.0.20 reference outputs captured in Phase 0 step 3.
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
- **Telemetry no-op verification:** boot `serve` with `lsof -i -P -n -p <pid>` running; assert zero outbound TCP connections during the boot window (excluding loopback). Procedure: pick `lsof` over a netns approach; it's the simplest portable check. Concrete script committed under `tests/integration/test_no_telemetry_egress.sh`.
- **Windows smoke (CI):** the Windows runner in the CI matrix runs `refresh --force --reset-index` + `serve` + one `search_docs` query. The CLAUDE.md "Windows tips" section is re-checked against 1.x behaviour and items that 1.x fixes are removed.
- **Concurrent reader test:** two `serve` processes against the same data dir; both should return query results without locking errors.

**Pass:** full pipeline works; `get_hub_status` returns expected counts; MCP `search_docs` returns sensible results; no network egress on boot; Windows CI green.

### Phase 5: Performance regression

```bash
just benchmark-stability > /tmp/phase5-1x.json
diff <(jq -S . tests/benchmarks/baselines/v0.0.20.json) <(jq -S . /tmp/phase5-1x.json)
```

Compare against the committed `tests/benchmarks/baselines/v0.0.20.json` (no ambiguity about which baseline).

**Pass criteria:**
- Index build time within ±20% of baseline.
- Query p50 within ±20%, p95 within ±50%.
- Peak RSS during refresh within ±30%.
- Peak RSS during dashboard pipeline within ±50% (lower tolerance because UMAP dominates RSS, not chromadb).

**Fail:** severe regression (e.g., 2× slowdown) → investigate HNSW parameter defaults (risk #8) before blaming chromadb.

### Phase 6: Migration path (post-merge, pre-tag — release blocker)

This phase verifies the user upgrade path. It's a release blocker, not optional.

```bash
# Setup: restore the 0.6 snapshot to a fresh data dir.
export PIPECAT_HUB_DATA_DIR=/tmp/chroma-migration-test
cp -a /tmp/chroma-0.6-snapshot "$PIPECAT_HUB_DATA_DIR/chroma"
shasum -a 256 -r "$PIPECAT_HUB_DATA_DIR/chroma" | sort > /tmp/before.sha256

# Test 1: serve must REFUSE the 0.6 directory with a clear, specific error.
uv run pipecat-context-hub serve 2>&1 | tee /tmp/serve-output.log &
SERVE_PID=$!
sleep 5
kill $SERVE_PID 2>/dev/null

# Assertions:
grep -q "refresh --force --reset-index" /tmp/serve-output.log || echo "FAIL: remediation hint missing"
grep -qi "incompatible.*format\|chromadb.*0\.6" /tmp/serve-output.log || echo "FAIL: error not specific to format"

# Assertion: no silent overwrite.
shasum -a 256 -r "$PIPECAT_HUB_DATA_DIR/chroma" | sort > /tmp/after.sha256
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

### What we are NOT testing (and why)

- chromadb's internal correctness — we trust upstream after 14 months of patch releases.
- Python 3.14 specifically — gated on torch/onnxruntime wheels (separate release).
- HTTP / server mode of chromadb — not used.
- chromadb 1.0.x intermediate versions — we ship 1.5.x directly to users (see "Why we pin to 1.5.x directly").

## Verification checklist (release blocker)

- [ ] Phase 0 prerequisites: 0.6 snapshot + hash captured; v0.0.20 baseline JSON committed; 1.0 → 1.5.9 release notes reviewed.
- [ ] Phase 1 spike passes; resolved dep tree noted.
- [ ] Phase 2a failures triaged into categories (a/b/c); no (d) findings.
- [ ] Phase 2b suite green; metadata enumeration committed; identity test passing.
- [ ] Phase 3 layer A parity: top-1 ≥ 95%, top-10 Jaccard ≥ 0.9, distance within 1e-6.
- [ ] Phase 3 layer B parity: similarity within ±0.01; reranker top-K Jaccard ≥ 0.85.
- [ ] Phase 4 integration green; `serve` boots; dashboard pipeline runs; telemetry no-op verified via `lsof`; Windows CI green; concurrent reader test passes.
- [ ] Phase 5 perf within tolerance (build ±20%, query p50 ±20%, p95 ±50%, RSS ±30%).
- [ ] Phase 6 migration: clear error on 0.6 directory containing `refresh --force --reset-index`; format-specific wording; snapshot hash unchanged; `--reset-index` recovers cleanly.
- [ ] `get_hub_status` reports expected counts after re-index.
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
