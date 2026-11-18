# `search_docs` misses cross-provider additions (speaker diarization, 1.16)

## Status

Planned. Investigation done; the cause is **not** yet verified (Phase 0). Branch `fix/search-docs-cross-provider-recall`. Split out of #166 (closed); no issue filed yet (Phase 0 files it).

Revised 2026-11-18 after `/review-plan` (38 findings, 6 contradictions): module names corrected, call flow and cause table extended, acceptance criteria and baselines made explicit. Three decisions were taken in a grill on 2026-11-18 and are marked "Decision (grilled)".

## Goal

With default config (reranker on) **and** with the reranker off, `search_docs("speaker diarization speaker_format", limit=10)` returns the STT settings pages of all five providers that gained diarization in pipecat 1.16 (ElevenLabs, AWS Transcribe, OpenAI, Deepgram, Soniox) in its ten hits, alongside Speechmatics / AssemblyAI / NVIDIA. If Phase 0 shows no fix reaches that within the Invariant's regression budget, we record why and ship the documented limitation instead.

The reporter's `limit` is unknown, so the Goal fixes `limit=10` (the tool default); Phase 0 also records `limit=5` and `limit=20`.

## What we know

Verified 2026-11-17 against the index then (framework 1.16.0). The index was rebuilt on 2026-11-18 (`last_refresh_at` 2026-11-18T07:50) and Soniox now ranks 8 with the reranker on, so **Phase 0 re-takes every number below** and records `last_refresh_at` beside each.

- **Not a content gap.** The content is indexed and readable via `get_doc`: `speaker_format` / `diarize` rows in `stt/elevenlabs.md` (lines 81-82), `stt/aws.md` (82-83), `stt/openai.md` (101, 121), `stt/deepgram.md` (121, 136), `stt/soniox.md` (104-105); `TranscriptionFrame.speaker` in `frames/data-frames.md` (173-177); the RTVI `speaker` field in `standards/rtvi-standard.md` (374-383). `stt/gladia.md` has none, which is correct.
- **The right chunks are retrieved but ranked below the limit.** With the reranker off, the "ElevenLabs / OpenAI / AWS Transcribe Speech-to-Text Settings" chunks rank 9-11, behind Speechmatics (x3), AssemblyAI, NVIDIA (x3) and Meta. A caller using a small `limit` sees only the first five.
- **The cross-encoder pushes them further down.** With the reranker on, the same chunks come back at ranks 10-18 with score `0.000` (sigmoid of a strongly negative logit, `cross_encoder.py:110`), under a top five scoring 0.92-0.999.
- **Concept decomposition helps only partly.** `"speaker diarization + speaker_format"` surfaces OpenAI, Zoom AI and `data-frames.md` in the top 14, but still not ElevenLabs, AWS, Deepgram or Soniox.
- **The split is by chunk kind, not by provider** (review of the live index, 2026-11-18). With the reranker on, ranks 1-6 are prose sections (`section: null`, e.g. a "Speaker Diarization" section for Speechmatics); ranks 7-19 are "Settings" table chunks for 12 providers, all scoring 0.0000, Speechmatics, AssemblyAI and NVIDIA settings chunks included. This replaces the earlier hypothesis that diarization being a headline feature explains the order. Phase 0 confirms it per hit.
- **Code facts** (read 2026-11-18; Phase 0 re-confirms with the tokenizer):
  - The cross-encoder scores `r.chunk.content[:1000]` (`services/retrieval/cross_encoder.py:97`) for only the top `top_n` = 20 candidates, and its model input is capped at 512 tokens (`services/onnx_backend.py:73`, `_CROSS_ENCODER_MAX_SEQ`). Raising the window past about 2000 characters therefore changes nothing.
  - The bi-encoder embeds at most 256 tokens (`onnx_backend.py:72`, `_DEFAULT_MAX_SEQ`, tokenizer truncation on). Doc chunks are sized to `doc_max_tokens` = 512 at 4 characters per token (`shared/config.py:105`, `docs_crawler.py:28`), about 2048 characters. Text late in a chunk never reaches the vector leg.
  - `_chunk_section` (`docs_crawler.py:178`) splits only on blank lines, so a markdown table is one paragraph and one chunk even above 2048 characters. The ElevenLabs settings table is about 2900 characters with the diarization rows about 2500 characters in: past the 1000-character window and likely past both token limits.
  - `_apply_diversity` (`services/retrieval/rerank.py:334`) only limits consecutive runs of 3 hits per path, and the cross-encoder then re-sorts the top 20 by its own score, discarding that order. No per-page cap exists after reranking.
  - FTS ANDs every query token (`services/index/fts.py`), so a chunk missing one token gets no keyword rank and no dual-hit bonus.
- **Reranker default.** `RerankerConfig.enabled` defaults to `True` (`shared/config.py:181-182`); `serve` falls back to reranker-off only when the model is not cached (`shared/reranker.py::probe_reranker`). The `RerankerConfig` docstring (line 175-176) and the `AGENTS.md` won't-fix entry on `search_examples` confidence both say "disabled by default"; that text is stale.
- **Unknown:** the `limit` the reporter used; whether the 1.16 provider rows are even *queried* by a user phrasing other than the two forms below.

## Architecture & Call Flow

Two processes touch the candidate fixes.

```
refresh process (ingest)                    serve / CLI process (query)
-------------------------                   -----------------------------------------------
llms-full.txt                               query
 -> docs_crawler._chunk_section                -> decompose (" + " / " & " concepts)
 -> chunks (<= ~2048 chars)                    -> vector leg (embeds <= 256 tokens)  \
 -> embed (<= 256 tokens) + FTS index          -> FTS leg (AND of all tokens)         -> RRF
 -> store (Chroma + SQLite)                    -> heuristics (symbol boost, dual-hit bonus)
 skip marker: docs:content_hash                -> _apply_diversity (runs of 3 per path)
 (hash of raw llms-full.txt only,              -> cross-encoder: top_n=20, content[:1000],
  cli.py:131, 1410-1413)                          512-token cap, re-sorts by own score
                                               -> [:limit]
                                            get_doc: joins a page's chunks with blank lines
```

`_rerank_raw` (`hybrid.py:186`) serves `search_docs`, `search_examples`, `search_api` and `get_code_snippet`; anything changed at the cross-encoder or diversity stage is shared by all four.

## Invariant

Decision (grilled): a change must (1) leave `required_failures` at zero in the quality benchmark and (2) keep the expected hit of every query in the regression set R (below) inside its current top 10, reranker on and off. A rank drop that stays inside the top 10 is allowed; a fall out of it fails. It must be justified by measurements on the real index, not by the one failing query. The candidate fixes necessarily move other results, so "must not lower rank" is not absolute; a fix that cannot meet this bound ends as the limitation outcome.

## Measurement set

Queries (each run with the reranker on and off, at `limit` 5, 10 and 20):

- **Q1** `search_docs("speaker diarization speaker_format")` (the Goal query).
- **Q2** `search_docs("speaker diarization + speaker_format")` (multi-concept form).
- **Q3** `search_docs("<Provider> speech-to-text diarization")` for each of ElevenLabs, AWS Transcribe, OpenAI, Deepgram, Soniox (the per-provider workaround).
- **Q4** `search_docs("Speechmatics speaker diarization")` (a control that works today).

Regression set **R**: every case in `_QUALITY_CASES` (`tests/benchmarks/test_retrieval_quality.py`, run by `just benchmark-quality`) plus the `search_docs`, `search_examples` and `search_api` queries in `docs/smoke-test.md`. If a fix is scoped to doc hits only, R may drop the examples and API queries; a fix at the shared cross-encoder or diversity seam keeps them.

The quality benchmark scores alias coverage at `limit=5` and records no rank, so for every query in R Phase 0 also records the top-10 document paths, to compare rank before and after.

## Plan

### Phase 0: measure (no production code; one test-harness step)

Phase 0 results live in a **separate file**, `docs/dev_plans/20261117-fix-search-docs-cross-provider-recall-phase0.md`, so this plan is not edited after review and its review marker stays valid. Scratch scripts stay in the scratchpad, uncommitted; the results file records the exact command, limit, reranker setting, `last_refresh_at` and `docs:content_hash` of every run.

1. **Benchmark wiring (Phase 0a).** Decision (grilled): the quality benchmark builds `HybridRetriever(store, embedding)` with no cross-encoder (`test_retrieval_quality.py:452`), so it never exercises the reranker and cannot gate a cross-encoder change. Land a small, separate test-harness change first that builds the benchmark retriever with the reranker the way `serve` does (`shared/reranker.py::probe_reranker`), selectable on and off. It touches only `tests/benchmarks/`; run the benchmark on `main` before and after this change to confirm the reranker-off numbers are unchanged.
2. **Freeze the baseline before any retrieval, ranking or ingest code changes.** Record framework version, `last_refresh_at`, `docs:content_hash`, reranker setting. `cp -a` the data directory to a backup; any `refresh --force` later runs against a copy under a scratch `PIPECAT_HUB_DATA_DIR`, never the only live index. Capture R with the wired benchmark: `just benchmark-quality` with `PIPECAT_HUB_BENCHMARK_OUTPUT=<file>`, the smoke queries, and top-10 paths, reranker on and off.
3. **Per-hit trace** for Q1-Q4. Build the scratch retriever the way `cli_query.py:302-366` does (so the reranker probe applies), and call `rerank()` and the cross-encoder separately to keep the pre-rerank rank. Per hit record: chunk id; chunk kind (prose vs table); chunk length in characters; character and token offset of the diarization text (embedding tokenizer, 256; cross-encoder, 512); whether it falls inside the first 1000 characters; vector rank; keyword rank or absent; dual-hit flag; post-heuristic rank; post-diversity rank; cross-encoder score and rank; distinct pages in the top `limit`. Save the full text and chunk ids of the target chunks into the results file: Phase 1's frozen unit-test text comes from there, and an ingest fix replaces those chunks.
4. **Latency baseline** for the quality and smoke queries with `tests/benchmarks/test_latency.py` (reranker on and off), so a window or ingest change is judged on measured cost, not on "~linear in chars".
5. **Pick the branch.** Run the causes below in order and stop at the first fix that brings all five providers into the Goal's top ten on the trace within the Invariant's budget. The causes can overlap, so the rule is "cheapest sufficient fix", ordered D, A, then B/C.

   | # | Cause found | Test on the trace | Candidate fix | Cost / risk |
   |---|---|---|---|---|
   | D | One page takes several top slots, so the missing providers fall below `limit` | distinct pages in the top `limit` is well below `limit`; target pages sit just below | Per-page cap or page collapse for doc hits, applied after the cross-encoder in `_rerank_raw` | No re-ingest. Shared seam: scope to doc hits or extend R to examples and API |
   | A | Diarization rows sit beyond the cross-encoder window | token or character offset past 1000 characters, and the chunk is inside the top 20 | Score the best-matching window of the chunk, or raise the window (setting in `RerankerConfig`, not a literal in `_score`) | Hard ceiling: model input is 512 tokens, so a window beyond about 2000 characters does nothing. Scoring several windows multiplies model calls (`top_n` x windows, per concept). Shared by all four tools |
   | B | The embedding never sees the rows (256-token truncation) and the table is one chunk | offset past 256 tokens; vector rank is low or absent | Decision (grilled): split oversized tables only, per the ingest rule in Phase 1 | Docs re-ingest, bigger index, touches `docs_crawler.py`, and `get_doc` page assembly |
   | E | Ranking is correct for a "which providers support X" question | none of the above holds | Do nothing in retrieval; document the workaround | None |

6. **File the issue** with the measurements, so Phase 1's "state it in the issue" has a target.
7. **Gate.** Record the chosen branch, its files and its tests in the results file. If the branch touches files this plan does not list, update this plan and rerun `/review-plan` before Phase 1.

### Phase 1: the fix chosen in Phase 0, or the documented limitation

Per branch (files and tests):

- **D (page cap).** `services/retrieval/hybrid.py` (`_rerank_raw`) and `rerank.py`. Tests: unit test with the real chunk list frozen from the results file; cap edge cases (fewer pages than `limit`, ties, a single page); reranker on and off; rerun smoke item 52 (ranking does not depend on `limit`).
- **A (window).** `services/retrieval/cross_encoder.py`, `shared/config.py` (`RerankerConfig`). Tests: chunk shorter than the window, match at the boundary, match beyond it; latency within the Phase 0 budget; reranker on and off; smoke item 52.
- **B (ingest).** `services/ingest/docs_crawler.py` (`_chunk_section`), `cli.py` skip marker. Decision (grilled): split only table paragraphs longer than the embedding window (about 1024 characters, 256 tokens) into row groups that each fit it, repeating the header row in every group; prose chunks and small tables stay untouched, and `doc_max_tokens` does not change. `get_doc` drops a repeated header row when it rejoins consecutive table chunks. Tests: every group keeps the header row, small tables stay whole, fenced tables stay intact; a `get_doc` round-trip (a page rebuilt from split chunks shows each header row once); index size before and after. The docs skip marker (`docs:content_hash`) hashes only the raw `llms-full.txt`, so a chunker change would not reach existing indexes on a plain `refresh`: fold a chunker version into the marker so a plain `refresh` re-ingests docs, and measure the after state after a plain `refresh` as well as `refresh --force`, on a scratch data directory. Only the docs source re-ingests, not the whole index.
- **E (limitation).** `docs/smoke-test.md` has no known-gap form (a single numbered list; 57 is the next free number in the main list), so add **item 57** with an observable expected result: the workaround query returns all five providers' pages. Phase 0 must run the documented workaround for all five providers first. The multi-concept form is **not** a workaround unless a measured run shows all five (the Q2 baseline already misses four). Candidate workarounds: `get_doc` by page path, or a per-provider query (Q3). State the result in the issue.

If retrieval changes, also: add the field-found query as a numbered item in `docs/smoke-test.md` (the `AGENTS.md` rule), run `uv run pytest tests/smoke/ -v` and the live smoke queries, and note any ingest-side change in the changelog (`refresh --force` needed on upgrade unless the marker change above lands).

Housekeeping, folded into the Phase 1 PR: correct the stale "disabled by default" text in the `RerankerConfig` docstring and the `AGENTS.md` entry (the default is on). Cite the actual `AGENTS.md` entry in this plan rather than the paraphrase "quality lever, not a correctness one".

### Out of scope

- Re-tuning the reranker globally, or changing `top_n`, without a before/after on R.
- A hub-side "capability matrix" built from release notes (the same fragile prose-parsing the #166 discussion rejected).
- Upstream docs changes (the pipecat docs could add a prose section per provider); noted as a possible outcome, not planned here.

## Verification

- Phase 0 results file complete: every run's command, limit, reranker setting, `last_refresh_at` and `docs:content_hash`; per-hit trace; chosen branch.
- Goal met on Q1 at `limit=10`, reranker on and off, or the limitation recorded with the workaround verified for all five providers.
- R before/after on the live index, reranker on and off: no `required_failures` in the quality benchmark; top-10 paths compared per the Invariant's bound.
- Latency before/after with `tests/benchmarks/test_latency.py`.
- `uv run ruff check src/ tests/`, `uv run mypy src/ tests/`, `uv run pytest tests/ -q` (backgrounded).

## Risk

Retrieval changes shift ranking for unrelated queries; that is why Phase 0 comes first and why "limitation" is an acceptable outcome. The cross-encoder and diversity stages are shared by `search_docs`, `search_examples`, `search_api` and `get_code_snippet`, so a change there is judged against R for all four unless scoped to doc hits. An ingest-side change needs a skip-marker change (or `refresh --force`), a changelog note, and a re-measurement on a scratch data directory so the "before" index is never overwritten.

<!-- reviewed: 2026-11-18 @ 9d20ae9ba64ca9fb7403b60055e8e9b6981e22ad -->

## Progress

- [ ] Phase 0: measure (0a benchmark wiring, baseline, trace, branch pick, issue, gate)
- [ ] Phase 1: chosen fix (D / A / B) or documented limitation (E)

## Findings

### Review Waivers

None. All 38 `/review-plan` findings (run 20261118T090148) were addressed in the plan body; the three contradictions without an obvious answer were decided in a grill on 2026-11-18.
