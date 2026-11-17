# `search_docs` misses cross-provider additions (speaker diarization, 1.16)

## Status

Planned. Investigation done; the cause of the cross-encoder scores is **not** yet verified (Phase 0). Branch `fix/search-docs-cross-provider-recall`. Split out of #166 (closed); no issue filed yet.

## Goal

`search_docs("speaker diarization speaker_format")` should surface the provider pages that gained diarization in pipecat 1.16 (ElevenLabs, AWS Transcribe, OpenAI, Deepgram, Soniox) alongside Speechmatics / AssemblyAI / NVIDIA, or we record why it cannot and stop.

## What we know (verified 2026-11-17, index at framework 1.16.0)

- **Not a content gap.** The content is indexed and readable via `get_doc`: `speaker_format` / `diarize` rows in `stt/elevenlabs.md` (lines 81-82), `stt/aws.md` (82-83), `stt/openai.md` (101, 121), `stt/deepgram.md` (121, 136), `stt/soniox.md` (104-105); `TranscriptionFrame.speaker` in `frames/data-frames.md` (173-177); the RTVI `speaker` field in `standards/rtvi-standard.md` (374-383). `stt/gladia.md` has none, which is correct.
- **The right chunks are retrieved but ranked below the limit.** With the reranker off, the "ElevenLabs / OpenAI / AWS Transcribe Speech-to-Text Settings" chunks rank 9-11, behind Speechmatics (x3), AssemblyAI, NVIDIA (x3) and Meta. A caller using a small `limit` sees only the first five.
- **The cross-encoder pushes them further down.** With the reranker on, the same chunks come back at ranks 10-18 with score `0.000` (sigmoid of a strongly negative logit), under a top five scoring 0.92-0.999.
- **Concept decomposition helps only partly.** `"speaker diarization + speaker_format"` surfaces OpenAI, Zoom AI and `data-frames.md` in the top 14, but still not ElevenLabs, AWS, Deepgram or Soniox.
- **Why Speechmatics/AssemblyAI/NVIDIA win (hypothesis, unverified):** diarization is their headline feature, so their pages are about the query; for the 1.16 providers it is one table row in a long settings page.
- **Not verified:** what the low-scoring chunks actually contain, and whether the 1000-character cross-encoder window (`r.chunk.content[:1000]` in `cross_encoder.py::_score`) cuts the diarization rows off. (My first attempt to test this used the 300-character search snippet, which proves nothing.)
- **Unknown:** the `limit` the reporter used.

## Invariant

A change must not lower recall or rank for the existing retrieval-quality queries, and must be justified by a measurement on the real index, not by the one failing query. (`AGENTS.md` already accepts that the reranker is a quality lever, not a correctness one; this is a recall case, not a bug in a contract.)

## Plan

### Phase 0: measure (no code)

For `"speaker diarization speaker_format"` and the other three queries in the "What we know" list, record per hit: chunk type, full chunk length, whether the diarization text falls inside the first 1000 characters, RRF rank, and cross-encoder score and rank. Use `HybridRetriever` directly with debug output in a scratch script (scratchpad, not committed). Decide which of the causes below applies, or that the result is a limitation.

| Cause found | Candidate fix | Cost / risk |
|---|---|---|
| Rows sit beyond the 1000-char window | Score the best-matching window of the chunk, or raise the window | Latency (~linear in chars); changes ranking for every query |
| Chunks are long settings tables, so no single embedding is about diarization | Split large tables by row group at ingest, or index a per-row text for parameter tables | Full re-ingest; bigger index; touches `docs_ingest` |
| Ranking is correct for a "which providers support X" question | Do nothing in retrieval; make the answer reachable another way (see below) | None |

### Phase 1: the fix chosen in Phase 0, or the documented limitation

- If retrieval changes: unit test with the real chunk text frozen into the test; run the offline smoke (`uv run pytest tests/smoke/ -v`) and the live queries in `docs/smoke-test.md`; add this query as a new numbered item there (the `AGENTS.md` rule for field-found failures).
- If it is a limitation: add the "cross-provider capability question" case to `docs/smoke-test.md` as a known gap with the workaround (`search_api` per provider, or multi-concept `diarization + speaker_format` queries per provider), and state it in the issue.

### Out of scope

- Re-tuning the reranker globally, or changing `top_n`, without a before/after on the existing retrieval-quality benchmark.
- A hub-side "capability matrix" built from release notes (the same fragile prose-parsing the #166 discussion rejected).

## Verification

- Phase 0 table filled in and committed to this plan.
- Before/after for the four queries above, and for the existing smoke queries, on the live index.
- `uv run ruff check src/ tests/`, `uv run mypy src/ tests/`, `uv run pytest tests/ -q` (backgrounded).

## Risk

Retrieval changes shift ranking for unrelated queries; that is why Phase 0 comes first and why "limitation" is an acceptable outcome. Any ingest-side change needs `refresh --force` and a version note in the changelog.
