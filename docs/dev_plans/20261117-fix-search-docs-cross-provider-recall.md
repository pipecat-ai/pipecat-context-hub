# `search_docs` misses cross-provider additions (speaker diarization, 1.16): document the limitation

## Status

Planned. Decision (grilled, 2026-11-18, after four `/review-plan` rounds): **close as a documented limitation, with no retrieval or ingest code change.** Branch `fix/search-docs-cross-provider-recall`. Split out of #166 (closed); no issue filed yet (Phase 0 files it).

Earlier revisions planned a ranking fix (a per-page cap, cross-encoder windows, table splitting). Round 4 measured that none of them can deliver a defensible Goal; "What we know" records why, and "Follow-up" keeps the one finding worth its own plan.

## Goal

Make the gap discoverable and the workaround verified, without changing ranking: a caller who gets a short `search_docs` result for `"speaker diarization speaker_format"` can find, from the issue and `docs/smoke-test.md` item 57, which query or `get_doc` path returns each provider's diarization settings.

## What we know

Measured 2026-11-17 and 2026-11-18 against the live index (framework 1.16.0, `last_refresh_at` 2026-11-18T07:50). Phase 0 re-takes the numbers it relies on.

- **Not a content gap.** The content is indexed and readable via `get_doc`: `speaker_format` / `diarize` rows in `stt/elevenlabs.md` (lines 81-82), `stt/aws.md` (82-83), `stt/openai.md` (101, 121), `stt/deepgram.md` (121, 136), `stt/soniox.md` (104-105); `TranscriptionFrame.speaker` in `frames/data-frames.md` (173-177); the RTVI `speaker` field in `standards/rtvi-standard.md` (374-383). `stt/gladia.md` has none, which is correct.
- **At a large limit the results are complete.** `search_docs("speaker diarization speaker_format", limit=20)` returns a Settings chunk for each of the twelve pages that hold a `speaker_format` table (AssemblyAI, AWS, Deepgram, ElevenLabs, Meta, NVIDIA, OpenAI, Smallest AI, Soniox, Speechmatics, xAI, Zoom AI): ranks 7-19 with the reranker on, 6-19 with it off. Two more STT pages contain the substring `diarize` (`stt/dotwave.md`, which says it is "Not supported", and `stt/hakim.md`); neither is in the top 50 in either mode. The 1.16.0 changelog adds diarization only to ElevenLabs, AWS Transcribe and OpenAI and lists Deepgram and Soniox with xAI, Smallest AI, Meta and Zoom AI as now reporting the speaker (`CHANGELOG.md` of the indexed pipecat clone, lines 132-134 and 142-145; not this repo's changelog).
- **At small limits the Settings chunks fall below the cut.** With the reranker on, ranks 1-6 are prose sections about diarization (for example a Speechmatics "Speaker Diarization" section) and the twelve Settings chunks follow. With the reranker off they rank 6-19, behind Speechmatics (x3), AssemblyAI, NVIDIA (x3) and Meta. A caller using `limit=5` or `limit=10` sees a subset of the twelve, which one depends on the reranker mode (ElevenLabs is the twelfth distinct page with the reranker on, Soniox with it off).
- **Why no ranking change fixes it.** The twelve Settings chunks are near-identical tables for different providers, and no query-independent signal prefers five of them over the other seven. Scores run 2.2e-5 to 9.8e-5 with the reranker on (distinct, but all rounding to 0.0000). The measured alternatives:
  - A per-page cap or other reorder of the cross-encoder-scored window (`top_n` = 20, `shared/config.py:207`) cannot change which 20 chunks return, and a simulated cap on the live list still leaves ElevenLabs as page 12 at `limit=10`.
  - Windowed cross-encoder scoring only re-scores those same 20 candidates, and the diarization text starts at token 468-711 of each table chunk (the 512-token ceiling sits at character 1087-2153), so most tables would need several windows per candidate.
  - Table markup dominates the token budget (see Follow-up), but fixing that lifts all table chunks alike and does not separate the providers.
- **Code facts** behind the above (read 2026-11-18): the cross-encoder scores `r.chunk.content[:1000]` for the top `top_n` candidates (`services/retrieval/cross_encoder.py:97`), keeps the rest at RRF-scale scores (`cross_encoder.py:116-117`), and caps input at 512 tokens including the query (`services/onnx_backend.py:73`); the bi-encoder embeds at most 256 tokens (`onnx_backend.py:72`); `_apply_diversity` (`services/retrieval/rerank.py:334`) is a no-op for docs-site hits because they carry no repo (`rerank.py:384-395`, `docs_crawler.py:272-280`).
- **Reranker default.** `RerankerConfig.enabled` defaults to `True` (`shared/config.py:181-182`); `serve` and the CLI run reranker-off when the config disables it, when the model is not cached (`shared/reranker.py:32`, `probe_reranker`), or at runtime when it fails to load (`shared/reranker.py:58-77`). The `RerankerConfig` docstring ("Disabled by default", `config.py:178`) and the `AGENTS.md` Review Checklist entry "**[Logic] won't-fix**: Confidence scores are optimistic on weak `search_examples` results" (2026-03-24, which says the cross-encoder is "disabled by default") are stale.
- **Unknown:** the `limit` the reporter used; whether each per-provider query (below) puts that provider's Settings chunk in its top five. Phase 0 measures the second.

## Plan

No retrieval, ranking or ingest code changes, so no regression set, benchmark gate or index backup is needed.

### Phase 0: measure the workaround (read-only)

Use the one-shot CLI on the live index (`uv run pipecat-context-hub search-docs ... --limit N`, `get-doc --path ...`); it changes nothing.

1. Record `last_refresh_at` and the framework version (`status`).
2. **Baseline.** `search_docs("speaker diarization speaker_format")` at `limit` 5, 10 and 20, with the reranker on (default) and off (`PIPECAT_HUB_RERANKER_ENABLED=false`), listing the page path of each hit.
3. **Per-provider query.** For each of ElevenLabs, AWS Transcribe, OpenAI, Deepgram and Soniox, run `search_docs("<Provider> speech-to-text diarization")` at `limit=5`, reranker on and off, and record whether that provider's `stt/<provider>.md` Settings chunk (a chunk of that page containing `speaker_format` or `diarize`, matched as a case-sensitive substring) is in the top five.
4. **`get_doc` fallback.** For each of the five providers, `get_doc` on `stt/<provider>` and record whether the page text contains `speaker_format` or `diarize`.
5. **File the issue** with these results and "What we know", so the limitation has a durable reference. State plainly that the fix is not a ranking change and why.

### Phase 1: document it

1. **`docs/smoke-test.md` item 57** (the next free number in the main list). It records the Phase 0 results per provider: the per-provider query's pass or fail at `limit=5` in both reranker modes, and the `get_doc` path that always works. The expected result is observable: each provider's page appears for its own query, or the item names the `get_doc` path for the providers whose query does not. A provider whose per-provider query fails stays listed as failing; the `get_doc` fallback is not what gates the item.
2. Correct the stale "disabled by default" text in the `RerankerConfig` docstring (`config.py:178`) and in the `AGENTS.md` Review Checklist entry cited above: the default is on.
3. Link the issue from item 57.

The multi-concept form (`"speaker diarization + speaker_format"`) is not a documented workaround: it surfaces OpenAI, Zoom AI and `data-frames.md` in its top 14 but misses ElevenLabs, AWS, Deepgram, Soniox and Smallest AI with the reranker on.

### Follow-up (not in this plan)

A separate plan may take table-markup compaction at ingest, for its own goal (embedding and cross-encoder quality on every table chunk in the corpus, not this query). What the reviews established, so it is not rediscovered:

- The separator row of a markdown table costs 317-457 embedding tokens (WordPiece emits one token per dash). Header plus separator is 650-930 characters before any data row, so today's table embedding holds the header and about 245 dash tokens and no data row.
- A fix needs: a compaction step in `docs_crawler.py`; a skip marker bound to the content hash (store `<chunker_version>:<content_hash>` next to the raw `docs:content_hash`, drop both in `_distrust_skip_markers` and the write-ahead delete, and write both in one batch); `get_doc` handling if tables are split; and attention to the vector store's metadata allowlist (`services/index/vector.py:107-180` drops `title`, `section` and any new key, so a new chunk flag lives only on FTS records).
- Branches D (per-page cap) and A (windowed cross-encoder scoring) are dropped, for the reasons under "What we know".

### Out of scope

- Any change to ranking, the cross-encoder, `top_n`, ingest or chunking in this plan.
- A hub-side "capability matrix" built from release notes (the same fragile prose-parsing the #166 discussion rejected).
- Upstream docs changes (the pipecat docs could add a prose section per provider); noted as a possible outcome, not planned here.

## Verification

- The issue exists and carries the Phase 0 results; item 57 records them per provider and links the issue.
- `uv run pytest tests/smoke/ -v` and `uv run ruff check src/ tests/` pass; `uv run mypy src/ tests/` passes (the code change is a docstring only).
- `git diff` shows only `docs/smoke-test.md`, `src/pipecat_context_hub/shared/config.py` (docstring), `AGENTS.md` and this plan's tracking text; no file under `services/` changes.

## Risk

Low. The limitation stays: a caller using a small `limit` still sees a subset of the twelve Settings pages. The mitigation is documentation and the verified per-provider query. If Phase 0 shows a per-provider query does not reach its page for some provider, item 57 says so and the `get_doc` path is the only workaround for that provider.

<!-- reviewed: YYYY-MM-DD @ <hash> -->

## Progress

- [ ] Phase 0: measure the workaround, file the issue
- [ ] Phase 1: smoke item 57, stale-docstring fixes

## Findings

### Review Waivers

None. Rounds 1-4 of `/review-plan` (runs 20261118T090148, 20261118T095852, 20261118T033032, 20261118T034000_4) drove the plan from a ranking fix to this limitation; the decision to close as a limitation was taken in a grill on 2026-11-18 after round 4 showed the Goal already passes at `limit=20` and branches D and A cannot move it.
