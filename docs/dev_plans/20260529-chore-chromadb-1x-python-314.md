# chromadb 1.x migration + Python 3.14 support

**Component**: services/index (vector store)
**Status**: Not Started
**Type**: chore
**Created**: 2026-05-29
**Branch**: `chore/chromadb-1x-python-314`
**Target version**: TBD — likely `v0.1.0` (on-disk format break warrants a minor bump, not a patch)

## Why

`v0.0.20` capped `requires-python` to `<3.14` (PR #65) because two upstream blockers prevent Python 3.14 support today:

1. **chromadb 0.6.x's pydantic-v1 `BaseSettings` shim** crashes at import on Python 3.14 (`typing.get_type_hints` changes).
2. **torch (via `sentence-transformers` / `transformers`) and `onnxruntime`** have no cp314 wheels yet — resolver fails or import fails.

(1) is unblocked: chromadb 1.0.0 shipped 2025-04-03; current is 1.5.9 (2026-05-05). 14 months of patch releases. Pydantic-v2 native.

(2) is still upstream. We can't lift the ceiling until both torch and onnxruntime publish cp314 wheels, but we can do the chromadb migration now and lift the ceiling as a follow-up release the moment they ship.

## API surface we actually depend on

Mapped via `grep -rn chromadb src/ dashboard/ tests/`. Total: **5 files**, **~10 distinct API calls**.

| File | API |
|---|---|
| `src/pipecat_context_hub/services/index/vector.py` | `chromadb.PersistentClient(path, settings)`, `chromadb.config.Settings`, `chromadb.telemetry.product.{ProductTelemetryClient, ProductTelemetryEvent}` (no-op stub), `client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})`, `collection.add(ids, embeddings, documents, metadatas)`, `collection.get(where=..., include=[])`, `collection.query(query_embeddings, n_results, include=["documents", "metadatas", "distances"], where=...)`, `collection.delete(ids=...)` |
| `dashboard/scripts/extract_embeddings.py` | `PersistentClient(path)`, `client.get_collection("latest")`, `collection.get(include=["embeddings", "metadatas", "documents"])` |
| `dashboard/scripts/extract_dashboard.py` | `PersistentClient(path)`, `client.get_collection("latest")` |
| `tests/unit/test_index_store.py` | exercises the above |
| `tests/integration/test_serve_lifetime.py` | exercises serve lifecycle |

This is a small surface — the migration is bounded.

## Things to consider (risks, breaking changes, unknowns)

Each item below needs to be verified during the spike, **not assumed**. The list is grouped by likelihood of biting us.

### Almost certain to bite us

1. **`from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent` is gone in 1.x.** The telemetry module reorganised. Two options: (a) replace our no-op stub with `Settings(anonymized_telemetry=False)` and delete the import entirely, (b) reach for the new internal path. Prefer (a) — depending on internal modules is what got us here.
2. **`chromadb.config.Settings` field surface changed.** We currently pass `anonymized_telemetry=False, allow_reset=True`. Verify both are still recognised; in 1.x, some fields moved to client kwargs or were removed.
3. **`include=` default for `collection.get()` and `collection.query()` changed in 1.x.** In 0.6 the default included embeddings; in 1.x it does not (to save memory). We pass `include=` explicitly at every call site I checked — but `tests/unit/test_index_store.py` may not, so test assertions on result shape could break.
4. **On-disk format is not backward-compatible.** A 0.6-formatted Chroma directory will not load under 1.x. Every existing user has to `refresh --force --reset-index`. We need a startup detection + clear error message.

### Likely

5. **Cosine distance semantics.** We set `metadata={"hnsw:space": "cosine"}` and convert the returned distance back to a 0..1 similarity score via `1 - (distance / 2)` (`vector.py:522`). If 1.x changed the distance normalisation or default space, scores invalidate silently — top-K order may be stable but the score values surfaced via `search_*` would shift, breaking any threshold logic and the reranker's RRF merge.
6. **HNSW parameters.** We only set `hnsw:space`. 1.x supports `hnsw:construction_ef`, `hnsw:M`, `hnsw:search_ef` via the same metadata channel — but the *defaults* differ between 0.6 and 1.x, which changes recall/latency characteristics even if our config is unchanged.
7. **Metadata typing is stricter in 1.x.** Some scalar coercions that 0.6 silently accepted (e.g. `None` values, mixed-type lists) raise in 1.x. We store ~15 metadata fields per chunk; need to enumerate and verify each is a `str | int | float | bool`.
8. **`get(where=..., include=[])`** — we use this to test existence without pulling documents. Verify the empty-`include` shortcut still works in 1.x (it might require at least one field).

### Possible

9. **Pydantic v2 double-cohabitation.** chromadb 1.x is pydantic-v2 native; we already use pydantic-v2 in `shared/types.py`. No conflict expected, but worth confirming no version pin from chromadb 1.x conflicts with our `pydantic>=2.0,<3.0`.
10. **Dependency tree shifts.** chromadb 1.x may drop `kubernetes`, `posthog`, or other transitive deps that bloat the install. If it drops `posthog` we lose one CVE surface. Worth measuring the resolved dep set before/after.
11. **`onnxruntime` becomes optional.** chromadb 1.x split `chromadb` from the default embedding function pipeline; `onnxruntime` may no longer be pulled in. We don't use chromadb's embedding functions (we embed via `sentence-transformers` directly), so this could shrink the install considerably.
12. **Performance characteristics.** chromadb 1.x has a Rust-based core in some code paths. Query p50 may improve; index build time may differ in either direction. Baseline + comparison required.
13. **Memory peak.** The dashboard pipeline (`extract_embeddings.py`) pulls all embeddings at once for UMAP. If 1.x's get-with-embeddings is lazier or eagerer, peak RSS shifts. Today we batch in groups of N — confirm batching still works.
14. **Concurrent readers.** 1.x is supposed to handle concurrent process readers better. The `serve` orphan-watchdog + idle-timeout flow assumes a single writer + occasional readers; verify nothing in `transport.py` depends on 0.6 locking semantics.

### Unknown but worth a quick check

15. **Windows behaviour.** CLAUDE.md has a `Windows tips` section listing 0.6-era workarounds (corrupt clone recovery, cp1252 console). chromadb 1.x may have changed Windows path handling; smoke-test on a Windows runner before tagging.
16. **`get_or_create_collection` semantics on metadata change.** If a user has a 0.6 index where `hnsw:space="cosine"` was implicit and 1.x makes it explicit-only, the `get_or_create` call could either succeed (good) or recreate the collection silently (catastrophic). Document the expected behaviour.

## Files to Modify

- `pyproject.toml` — chromadb pin `>=0.6,<1.0` → `>=1.0,<2.0`; possibly drop the `<3.14` cap once torch lands
- `src/pipecat_context_hub/services/index/vector.py` — imports, Settings construction, telemetry no-op removal, query/get call sites
- `src/pipecat_context_hub/server/transport.py` (or `main.py`) — on-disk format detection at startup
- `dashboard/scripts/extract_embeddings.py` — verify `include=[...]` shape, batch loop
- `dashboard/scripts/extract_dashboard.py` — verify `get_collection` path
- `tests/unit/test_index_store.py` — update assertions for new include defaults
- `uv.lock` — re-lock
- `CHANGELOG.md` — Changed entry: chromadb 1.x, format break, upgrade command
- `CLAUDE.md` — update `Vector store:` line if anything material changes; remove `Windows tips` items that 1.x fixes
- `docs/README.md` — same
- `docs/dev_plans/README.md` — move this plan from Current to Completed when done

## Testing strategy

Five test phases, each with a clear pass/fail criterion. Run them in order — if (1) fails, don't bother running the rest.

### Phase 1: Spike — does the import even work?

In a throwaway worktree (`isolation: worktree`), bump the chromadb pin and run:

```bash
uv lock
uv run python -c "import chromadb; print(chromadb.__version__); chromadb.PersistentClient(path='/tmp/chroma-spike-1x')"
```

**Pass:** no import error, no construction error.
**Fail:** stop. Identify the breakage, fix it, retry. Do not proceed until this passes.

### Phase 2: Unit tests — does the in-process API still match our usage?

Run the existing suite, focusing on the index-store tests:

```bash
uv run pytest tests/unit/test_index_store.py -v
uv run pytest tests/unit/ -v
```

**Pass:** 1021 passing tests, same count as v0.0.20.
**Fail:** triage each failure. Categorise as (a) signature change → fix our code, (b) result-shape change → fix our code, (c) test was asserting 0.6-specific behaviour → update the test.

### Phase 3: Parity test — same query, same result

Write a one-off comparison harness (do not commit) that:

1. Indexes a fixed corpus (use `tests/fixtures/smoke/` snapshots) with 0.6.
2. Indexes the *same* corpus with 1.x.
3. Runs a curated query set (the 20-30 queries from existing benchmarks).
4. Compares top-10 IDs and distances.

**Pass criteria:**
- Top-K **IDs** match by Jaccard ≥ 0.9 (some reordering at the tail is normal — embedding model is identical, so any drift is from distance/HNSW internals).
- Top-1 ID matches in ≥ 95% of queries.
- Distance values: cosine should be byte-identical (it's deterministic from the same vectors). If not byte-identical, that's a red flag about the `hnsw:space` config or distance normalisation.

**Fail:** investigate. The most likely cause is item #5 from the considerations list (distance semantics drift). May need to adjust the `1 - (distance / 2)` formula.

### Phase 4: End-to-end + integration

```bash
uv run pytest tests/integration/ -v
uv run pipecat-context-hub refresh --force --reset-index
uv run pipecat-context-hub serve  # smoke-check it boots and serves a query
```

**Pass:** full pipeline works — refresh succeeds, serve boots, `get_hub_status` returns expected counts, an MCP `search_docs` query returns sensible results.

### Phase 5: Performance regression

```bash
just benchmark-stability  # compares against the previous baseline
```

Capture:
- Index build wall-time (full corpus)
- Query latency p50 / p95 (using the benchmark's query set)
- Peak RSS during refresh
- Peak RSS during serve

**Pass criteria:**
- Index build time within ±20% of v0.0.20.
- Query p50 within ±20%, p95 within ±50% (HNSW recall trade-offs can shift p95 more than p50).
- RSS within ±30%.

**Fail:** if regression is severe (e.g., 2× slowdown), investigate HNSW parameter defaults (item #6) before blaming chromadb.

### Phase 6 (post-merge, pre-release): Migration path

This one matters because it affects every existing user.

```bash
# Setup: take a v0.0.20 chroma directory and copy it aside.
cp -a ~/.local/share/pipecat-context-hub/chroma /tmp/chroma-0.6-snapshot

# Test: run 1.x serve against the 0.6 directory.
uv run pipecat-context-hub serve
```

**Pass:** clear error message naming the format mismatch and pointing at `refresh --force --reset-index`. No silent corruption. No data overwrite.

Then:

```bash
uv run pipecat-context-hub refresh --force --reset-index
uv run pipecat-context-hub serve
```

**Pass:** rebuilds cleanly, serves normally.

### What we are NOT testing (and why)

- **chromadb's internal correctness.** We trust upstream — 14 months of patch releases is sufficient maturity. We test our *integration*, not their library.
- **Python 3.14 specifically.** Out of scope for this PR; gated on torch wheels. We will test 3.13 (current ceiling under the cap).
- **HTTP / server mode of chromadb.** Not used.

## Verification checklist

- [ ] Phase 1 spike passes
- [ ] Phase 2 unit suite green (1021+ tests)
- [ ] Phase 3 parity test: top-1 match ≥ 95%, top-10 Jaccard ≥ 0.9
- [ ] Phase 4 integration green; `serve` boots; MCP queries return expected shape
- [ ] Phase 5 perf within tolerance (index build, query p50/p95, RSS)
- [ ] Phase 6 migration: clear error on 0.6 index; clean rebuild via `--reset-index`
- [ ] Telemetry no-op verified (no network egress on boot — run with a sniffer or `--offline`)
- [ ] `get_hub_status` reports expected counts after re-index
- [ ] Cross-platform: macOS ✓ (dev), Linux ✓ (CI), Windows ✓ (CI matrix)
- [ ] CLAUDE.md `Windows tips` section reviewed — remove items 1.x fixes
- [ ] Release notes call out the format break + `refresh --force --reset-index` requirement prominently

## Sequencing

1. **This PR** — chromadb 0.6 → 1.x migration, cap stays at `<3.14`. Ship as `v0.1.0` (format break is a minor bump).
2. **Follow-up tracking issue** — monitor torch + onnxruntime cp314 wheel availability on PyPI.
3. **Follow-up patch release** — when both ship, lift `requires-python` to `<3.15` and add Python 3.14 to the CI matrix. Patch-level bump (`v0.1.1`).

## Notes

- chromadb 1.x release timeline: 1.0.0 (2025-04-03), current 1.5.9 (2026-05-05). Pin to `>=1.5,<2.0` rather than `>=1.0` to skip the early-1.x patch churn.
- The `requires-python` ceiling and CHANGELOG entry must move together when the cap lifts. Both are in v0.0.20 as the authoritative record.
- This plan deliberately leaves out a "consider switching vector backends" tangent. The vector-backend decision is documented at `docs/decisions/vector-backend.md` — re-open that decision separately if 1.x migration reveals fundamental problems, not as part of this PR.
