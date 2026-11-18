# `search_docs` misses cross-provider additions (speaker diarization, 1.16): document the limitation

## Status

Decision (grilled, 2026-11-18, after six `/review-plan` rounds): **close as a documented limitation, with no retrieval or ingest code change.** Branch `fix/search-docs-cross-provider-recall`. Split out of #166 (closed). Lifecycle state lives below the review marker (the Progress checklist, which also records the issue number once Phase 0 files it) and in this plan's row of `docs/dev_plans/README.md`, so recording progress never edits the reviewed contract.

Earlier revisions planned a ranking fix (a per-page cap, cross-encoder windows, table splitting). Round 4 measured that none of them can deliver a defensible Goal; "What we know" records why, and "Follow-up" keeps the findings worth their own plans.

## Goal

Record, against the live index, which calls return each STT provider's diarization settings, and publish that in a GitHub issue and `docs/smoke-test.md` item 57. The audience is maintainers and issue reporters. The plan changes nothing a caller sees by default: an MCP agent that runs the baseline query at a small `limit` still gets no prompt to recover (see "What we know", `low_confidence`). The measurements use the working-tree CLI, which shares the MCP handlers; the MCP client runs the installed tool, so the issue records the CLI's package version beside the result.

The documented workarounds, scoped to the index measured (framework 1.16.0, refreshed 2026-11-18) and verified by Phase 0 on all twelve pages that hold a `speaker_format` table:

1. `search_docs("speaker diarization speaker_format", limit=20)` returns a Settings-section hit for every one of the twelve pages. The margin is one rank (the last Settings hit sits at rank 19), so Phase 0 also records `limit=30`, and the issue states the claim holds for this index only.
2. `get_doc(path="/api-reference/server/services/stt/<page>.md", section="Settings")` returns the Settings section: text that starts with the heading `## Settings` and contains `speaker_format` or `diarize`.

## What we know

All numbers in this section were re-taken on 2026-11-18 (review rounds 5 and 6 re-ran them) against the index refreshed 2026-11-18T07:50, framework 1.16.0, reranker on by default. Token counts and character offsets come from tokenizer measurements made during the reviews; Phase 0 does not re-take them, so the issue marks them as dated notes. Phase 0 re-takes everything else.

- **Not a content gap.** The content is indexed and readable via `get_doc`: `speaker_format` / `diarize` rows in `stt/elevenlabs.md` (lines 81-82), `stt/aws.md` (82-83), `stt/openai.md` (101, 121), `stt/deepgram.md` (121, 136), `stt/soniox.md` (104-105); `TranscriptionFrame.speaker` in `frames/data-frames.md` (173-177); the RTVI `speaker` field in `standards/rtvi-standard.md` (374-383). `stt/gladia.md` has none, which is correct.
- **The twelve pages.** Twelve STT pages hold a `speaker_format` table: AssemblyAI, AWS, Deepgram, ElevenLabs, Meta, NVIDIA, OpenAI, Smallest AI, Soniox, Speechmatics, xAI, Zoom AI. Their paths are `/api-reference/server/services/stt/<page>.md` with `<page>` one of `assemblyai`, `aws`, `deepgram`, `elevenlabs`, `meta`, `nvidia`, `openai`, `smallest`, `soniox`, `speechmatics`, `xai`, `zoom-ai`. Two more STT pages contain the substring `diarize` (`stt/dotwave.md`, which says it is "Not supported", and `stt/hakim.md`); neither is in the top 50 in either mode. The 1.16.0 changelog adds diarization only to ElevenLabs, AWS Transcribe and OpenAI and lists Deepgram and Soniox with xAI, Smallest AI, Meta and Zoom AI as now reporting the speaker (`CHANGELOG.md` of the indexed pipecat clone, lines 132-134 and 142-145; not this repo's changelog).
- **At `limit=20` the results are complete, with a thin margin.** `search_docs("speaker diarization speaker_format", limit=20)` returns thirteen Settings-section hits that cover all twelve pages (NVIDIA has two): ranks 7-19 with the reranker on; ranks 6-19 with it off, where rank 17 is an AssemblyAI Notes chunk. Rank 19 scores 2.2e-5 and rank 20 scores 1.7e-5 with the reranker on. At `limit` 30 and 50 all twelve pages still return (reranker on).
- **At small limits the Settings chunks fall below the cut.** With the reranker on, ranks 1-6 are non-Settings chunks (Speechmatics ×3, AssemblyAI, NVIDIA, Meta). With it off, ranks 1-5 are (Speechmatics ×2, AssemblyAI, NVIDIA, Meta). At `limit=5` no Settings chunk appears in either mode. At `limit=10` four pages appear: xAI, Soniox, Zoom AI and AWS with the reranker on; Speechmatics, NVIDIA, ElevenLabs and OpenAI with it off. ElevenLabs is the twelfth distinct page with the reranker on, Soniox with it off.
- **No caller-facing prompt.** `low_confidence` is `false` at `limit` 5, 10 and 20 in both modes. The server instructions (`src/pipecat_context_hub/server/main.py:129-131`) tell an agent to raise `limit` to 20 and try `get_doc` only on `low_confidence`, zero hits or a user complaint, so none of them fires for this query.
- **A per-provider query is not a workaround.** `search_docs("<Provider> speech-to-text diarization", limit=5)` returns the provider's title, Overview or TTS-page chunks and never a Settings chunk (0 of 10 across ElevenLabs, AWS Transcribe, OpenAI, Deepgram and Soniox, reranker on and off).
- **`get_doc` needs the exact path, and a bad section fails silently.** `get_doc` on `stt/<page>` or `/api-reference/server/services/stt/<page>` (no `.md`) returns `Not Found` with exit 0. Only the full `.md` path resolves. With `--section Settings` the content is the Settings section alone and starts with `## Settings` (AWS: 3328 characters against 7216 for the page). When the requested section matches nothing, `get_doc` returns the whole page with no error (`services/retrieval/hybrid.py:429-432`), and the response's `sections` list always carries the page's full table of contents. The check that the section matched is therefore that the content starts with `## Settings`; the term check alone passes on a fallback page too. `get-doc --doc-id <chunk id>` on a Settings-chunk id returns that chunk's text (AWS: 3044 characters, contains `speaker_format`).
- **The CLI hides chunk text.** `search-docs` returns a 300-character snippet per hit (`services/retrieval/hybrid.py:345`) and the first `speaker_format` or `diarize` match sits at character 1864 or later of each Settings chunk (a dated note), so a hit's snippet cannot show whether the chunk qualifies. The hit's `section` field (`"Settings"`) and citation path identify the chunk; `get-doc --doc-id` returns its text.
- **Why no ranking change fixes it.** The Settings chunks are near-identical tables for different providers, and no query-independent signal prefers five of them over the other seven. Scores run 2.2e-5 to 9.8e-5 with the reranker on (distinct, but all rounding to 0.0000). The measured alternatives:
  - A per-page cap or other reorder of the cross-encoder-scored window (`top_n` = 20, `src/pipecat_context_hub/shared/config.py:207`) cannot change which 20 chunks return, and a simulated cap on the live list still leaves ElevenLabs as page 12 at `limit=10`.
  - Windowed cross-encoder scoring only re-scores those same 20 candidates, and the diarization text starts at token 468-711 of each table chunk (the 512-token ceiling sits at character 1087-2153), so most tables would need several windows per candidate.
  - Table markup dominates the token budget (see Follow-up), but fixing that lifts all table chunks alike and does not separate the providers.
- **Code facts** behind the above (read 2026-11-18): the cross-encoder scores `r.chunk.content[:1000]` for the top `top_n` candidates (`services/retrieval/cross_encoder.py:97`), keeps the rest at RRF-scale scores (`cross_encoder.py:116-117`), and caps input at 512 tokens including the query (`services/onnx_backend.py:73`); the bi-encoder embeds at most 256 tokens (`onnx_backend.py:72`, `_DEFAULT_MAX_SEQ`, a fallback that a model's own `max_seq_length` overrides, `onnx_backend.py:247-251`); `_apply_diversity` (`services/retrieval/rerank.py:334`) leaves docs-site hits in order, because `_would_violate` (`rerank.py:384-395`) compares the empty repo of every docs hit (`docs_crawler.py:272-280`).
- **Reranker default.** `RerankerConfig.enabled` defaults to `True` (`src/pipecat_context_hub/shared/config.py:181-182`); `serve` and the CLI run reranker-off when the config disables it, when the model is not cached (`shared/reranker.py:32`, `probe_reranker`), or at runtime when it fails to load (`shared/reranker.py:58-82`, `load_failed` at line 82). `status` reports `reranker_enabled` and `reranker_disabled_reason`. The `RerankerConfig` docstring ("Enable via ``PIPECAT_HUB_RERANKER_ENABLED=1`` ... Disabled by default.", `src/pipecat_context_hub/shared/config.py:177-178`) and the `AGENTS.md` Review Checklist entry "**[Logic] won't-fix**: Confidence scores are optimistic on weak `search_examples` results" (2026-03-24, line 334, which says the cross-encoder is "disabled by default") are stale. `docs/configuration.md` and `docs/reranking.md` already say it is on.
- **Multi-concept form.** `search_docs("speaker diarization + speaker_format", limit=14)` returns ten hits in either mode, with OpenAI and Zoom AI prose chunks and one Speechmatics Settings chunk (reranker off), and misses the Settings chunks of ElevenLabs, AWS, Deepgram, Soniox and Smallest AI. It is not a workaround.

## Plan

No retrieval, ranking or ingest code changes, so no regression set, benchmark gate or index backup is needed.

### Phase 0: measure the workarounds (no source change)

Use the one-shot CLI on the live index (`uv run pipecat-context-hub search-docs ... --limit N`, `get-doc ...`, `status`, `--version`). Judge a search hit as a Settings hit by its `section == "Settings"` field and its citation path, never by snippet text. Confirm the chunk with `get-doc --doc-id <hit doc_id>`: the returned content must contain `speaker_format` or `diarize` (case-sensitive substring). Phase 0 changes no source file; its two writes are the GitHub issue and the Progress line (step 7).

1. **Index, reranker and version state.** Run `status`; record `last_refresh_at`, the framework version, `reranker_enabled` and `reranker_disabled_reason`. Run `--version` and record the package version. A "reranker on" run needs `reranker_enabled: true` and a null reason, and no stderr line saying the reranker is disabled or its model is not cached. For "reranker off" runs set `PIPECAT_HUB_RERANKER_ENABLED=false` and confirm `status` under that variable reports `reranker_enabled: false`. Keep stderr for every run.
2. **Baseline.** `search_docs("speaker diarization speaker_format")` at `limit` 5, 10, 20 and 30, reranker on and off. Record for each hit its rank, page path, `section` and `doc_id`, and record `low_confidence` for each run. At `limit=20` record the rank of the last Settings hit.
3. **Workaround 1.** From the `limit=20` runs, check that each of the twelve pages has at least one Settings hit whose `get-doc --doc-id` text contains `speaker_format` or `diarize`, reranker on and off. A second hit on the same page (NVIDIA has two) need not pass. Repeat the per-page check on the `limit=30` run.
4. **Workaround 2.** For each of the twelve pages run `get-doc --path /api-reference/server/services/stt/<page>.md --section Settings`. Pass means the content starts with `## Settings` and contains `speaker_format` or `diarize`. Fail means either condition is false (a full-page fallback fails the first). Also run `get-doc --path stt/aws` and the `.md`-less long path once and record `Not Found` with exit 0.
5. **Per-provider query (informational, non-gating).** For ElevenLabs, AWS Transcribe, OpenAI, Deepgram and Soniox run `search_docs("<Provider> speech-to-text diarization", limit=5)`, reranker on and off. Record whether any hit has `section == "Settings"` (expected: none).
6. **Reconcile.** Run `status` again. An unchanged `last_refresh_at` is the signal that no refresh ran during steps 2-5; if it changed, re-run Phase 0 from step 1. Compare the results with "What we know":
   - If workaround 1 or 2 fails on any page, stop and re-scope the plan; write nothing into item 57.
   - If `low_confidence` is `true` at `limit` 5 or 10, or a Settings hit appears at `limit=5`, update "What we know" and the Goal's no-prompt sentence before filing (a `low_confidence` prompt changes the caller-facing story).
   - If other numbers differ, update "What we know" and note the change in the issue.
7. **File the issue and record it.** File the issue with the results and "What we know", the CLI package version and the dated notes marked as such. State plainly that the fix is not a ranking change and why. Write the issue number into the Progress checklist below the marker and commit that edit on the branch.

### Phase 1: document it

1. **Index check.** Run `status` and confirm `last_refresh_at` equals the value in the issue. If the index was refreshed since, re-run Phase 0 steps 2-4, update the issue, and only then write item 57.
2. **`docs/smoke-test.md` item 57** (the next free number: the main list ends at 56), written in the style of items 55 and 56, with the issue linked. Decision (author): the item stays small. The twelve-page table lives in the issue; the item states these invariants, each checkable with the CLI against the live index, in both reranker modes (the item says how to turn it off):
   - `search-docs "speaker diarization speaker_format" --limit 20` returns at least one hit with `section` `Settings` on each of the twelve pages, which the item lists by slug (`assemblyai`, `aws`, `deepgram`, `elevenlabs`, `meta`, `nvidia`, `openai`, `smallest`, `soniox`, `speechmatics`, `xai`, `zoom-ai`); rank order is not asserted; the last Settings hit sits near rank 19.
   - For the five pages the 1.16.0 changelog names (`elevenlabs`, `aws`, `openai`, `deepgram`, `soniox`), `get-doc --path /api-reference/server/services/stt/<page>.md --section Settings` returns content that starts with `## Settings` and contains `speaker_format` or `diarize`.
   Known limitation, recorded and not gating: at `--limit 5` no Settings hit returns, `low_confidence` stays `false`, and `get-doc --path stt/<page>` (no `.md`) returns `Not Found` with exit 0. The item closes with the provenance clause of items 55 and 56 ("verdicts hold only while the live index matches the 2026-11-18 provenance; a changed verdict is a data change, not a regression") and says no unit test pins it, because no code path changed. The per-provider query observations stay in the issue as dated notes.
3. **Stale reranker text.** In the `RerankerConfig` docstring (`src/pipecat_context_hub/shared/config.py:177-178`) replace "Enable via ``PIPECAT_HUB_RERANKER_ENABLED=1`` environment variable or by setting ``enabled=True`` in Python. Disabled by default." with text saying it is enabled by default and how to disable it (``PIPECAT_HUB_RERANKER_ENABLED=false`` or ``enabled=False`` in Python). In the `AGENTS.md` Review Checklist entry (line 334) change "(Phase 1, disabled by default)" to "(enabled by default)" and add one dated sentence: the reranker is on by default (2026-11-18), and the weak-result problem persists (for example `low_confidence` stays `false` for the speaker-diarization query at small limits, see the issue). Afterwards `grep -rni "disabled by default" src AGENTS.md` returns no reranker hit.
4. **README row.** Update this plan's existing row in `docs/dev_plans/README.md` (line 6) to Completed and add the issue number. Do not add a second row.
5. **PR and issue.** The PR body says `Refs #<issue>` and links item 57. Decision (author): the PR does not close the issue; the maintainer closes it after merge, or keeps it open for the Follow-up plan.

### Follow-up (not in this plan)

- **Table-markup compaction at ingest**, for its own goal (embedding and cross-encoder quality on every table chunk in the corpus, not this query). What the reviews established, so it is not rediscovered:
  - The separator row of a markdown table costs 317-457 embedding tokens (WordPiece emits one token per dash). Header plus separator is 650-930 characters before any data row, so today's table embedding holds the header and about 245 dash tokens and no data row.
  - A fix needs: a compaction step in `docs_crawler.py`; a skip marker bound to the content hash (store `<chunker_version>:<content_hash>` next to the raw `docs:content_hash`, drop both in `_distrust_skip_markers` and the write-ahead delete, and write both in one batch); `get_doc` handling if tables are split; and attention to the vector store's metadata allowlist (`services/index/vector.py:107-180` drops `title`, `section` and any new key, so a new chunk flag lives only on FTS records).
  - Branches D (per-page cap) and A (windowed cross-encoder scoring) are dropped, for the reasons under "What we know".
- **A caller-facing hint.** The only thing that would help an MCP agent is a change to what it sees: the server instructions fire only on `low_confidence`, zero hits or a complaint, and `low_confidence` is `false` here. A one-line change to the instructions (`server/main.py`) or to the response is a tool-metadata change with its own smoke and test obligations, so it needs its own plan.

### Out of scope

- Any change to ranking, the cross-encoder, `top_n`, ingest, chunking or the server instructions in this plan.
- A hub-side "capability matrix" built from release notes (the same fragile prose-parsing the #166 discussion rejected).
- Upstream docs changes (the pipecat docs could add a prose section per provider); noted as a possible outcome, not planned here.

## Verification

- The issue exists, carries the Phase 0 results, and its number is in the Progress checklist; item 57 links it and records the invariants.
- The item 57 commands, run on the live index by hand in both reranker modes, produce the stated results.
- The full gate passes: `uv run ruff check src/ tests/`, `uv run ruff format --check src/ tests/`, `uv run mypy src/ tests/`, `uv run pytest tests/ -q`. The unit suite runs because `src/pipecat_context_hub/shared/config.py` is edited and `tests/unit/test_repo_catalog.py` parses it. No unit test reads the `AGENTS.md` checklist (`tests/unit/test_audit_sync.py` only names it in a docstring), so the grep below is the only check on that edit.
- `git diff` against main shows only `docs/smoke-test.md`, `src/pipecat_context_hub/shared/config.py` (docstring), `AGENTS.md`, `docs/dev_plans/README.md` and this plan; no file under `services/` or `server/` changes.
- `grep -rni "disabled by default" src AGENTS.md` shows no stale reranker text.

## Risk

Low. The limitation stays: a caller using a small `limit` still sees at most four of the twelve Settings pages, and gets no `low_confidence` prompt to try more. The mitigation is documentation of the two verified workarounds (`limit=20`, which holds on this index with a one-rank margin, and `get_doc` on the exact `.md` path with `--section Settings`). If Phase 0 shows either workaround failing for a page, the plan stops and is re-scoped before anything is written into item 57.

<!-- reviewed: YYYY-MM-DD @ <hash> -->

## Progress

- [ ] Phase 0: measure the workarounds, file the issue
- [ ] Issue number: #____ (Phase 0 step 7 fills this in)
- [ ] Phase 1: index check, smoke item 57, stale reranker text fixes, README row, PR

## Findings

### Review Waivers

- **Unit regression test (AGENTS.md "freeze it twice").** Waived. The plan changes no code path, only a docstring and documentation, so there is no behaviour for a test to freeze. Item 57 records live data, and its provenance clause says a changed verdict is a data change.
- **Caller-facing hint (architecture and assumptions lenses, round 5).** Deferred to a separate plan, recorded under Follow-up. The user chose to close this plan as a documented limitation with no code change.

Rounds 1-6 of `/review-plan` (runs 20261118T090148, 20261118T095852, 20261118T033032, 20261118T034000_4, 20261118T102356_5, 20261118T103507_6) drove the plan from a ranking fix to this limitation; the decision to close as a limitation was taken in a grill on 2026-11-18 after round 4 showed the old Goal already passes at `limit=20` and branches D and A cannot move it.
