# Local plugin feasibility evidence

Record machine-specific command paths and raw diagnostics in a local report
outside the checkout. Do not put credentials or secret values in either report.
Package/subprocess checks and actual desktop activation are separate outcomes.
This repository report is not copied into the installed plugin package.

| Gate | Required observation | Result |
|---|---|---|
| Package | Root manifest, copied skill and resolved safe stdio argv | Pass, local setup 2026-10-03 |
| Installed Hub | Importable from unrelated cwd with pinned Python | Pass, historical 0.8.0 import qualification; latest recorded packaged status is 0.8.1 on 2026-10-05 |
| Packaged stdio | Initialize and nonempty status using generated `mcp.json` | Pass, corrected portable command and unique connection; subprocess only |
| Cwd safety | Same initialize/status with a shadow Hub module in cwd | Pass, subprocess only |
| Index preservation | Before/after refresh date and framework provenance unchanged | Pass, latest 2026-10-05 packaged before/after status preserves 45,463 records, October 5 refresh, latest/1.12.0/0-ahead and enabled reranker; no agent refresh |
| Desktop discovery | Plugin and packaged skill exposed in selected host/mode | Pass, Codex local catalogue and enabled plugin 2026-10-03 |
| Explore skill activation | Host reads and invokes packaged explore skill | Pass, observed TTS + STT exploration 2026-10-03 |
| Desktop packaged MCP | Host uses `pipecat-context-hub-chatgpt-plugin` initialize/status connection | Pass, actual named calls in a clean Codex worker 2026-10-04; raw desktop initialize/startup trace unobserved |
| Existing Hub retrieval | Status, API definitions, docs and examples with sources | Pass for recorded 2026-10-04 clean-worker queries and exact lookups; broad ranking and other page paths unverified |
| Execution | Harmless shell command in selected host/mode | Pass, Codex local shell |
| Pipecat CLI | Installed version and init help/options | Pass, CLI 1.3.0 version/init help |
| Cloud CLI | Installed capabilities and separately approved execution | Pass, CLI 1.3.0 help/metadata/source-build/deploy/status/log path observed; exact approval precedes live mutations |
| Cloud account | Auth/org readiness; selected target and secret metadata | Pass, selected-org/region metadata and exact existing key names verified 2026-10-05; earlier failures remain dated below |
| Local build | Installed options, dry run, scaffold, imports/startup/behaviour | Pass for Phase 3 local fixture only; provider conversation and revised skill activation untested |
| Cloud deployment | Approved build linked to actual ready deployment and settings | Pass, independent matching-build/deployment reads 2026-10-05; provider/session behaviour untested |

## Current evaluation reconciliation (2026-10-05)

The recorded core acceptance targets are demonstrated in Codex local: actual
packaged explore activation/retrieval, source-candidate idea/concept conversations,
CLI scaffold and bounded local verification, concrete read-only Cloud preparation,
and an exactly approved build linked to a matching READY deployment. This summary
reconciles the twenty-one frozen prompts with later evidence; it does not convert
manual or synthetic walkthroughs into autonomous installed-skill results.
Historical failures and their original checkpoint labels remain dated below.
Repository-wide quality gates and final review are recorded by the conductor.

| Workflow / frozen rows | Recorded evidence | Current coverage limit |
|---|---|---|
| Exploration, 1–10 and 12–13 | [Phase 2 calls](#sequential-actual-packaged-calls), [four source-candidate conversations](#observed-source-candidate-conversations-2026-10-04), and [packaged worker retrieval](#after-user-refresh-packaged-worker-connection-2026-10-04) | Actual conversations cover 1/2/6/9; other rows retain manual/tool-observation labels. Unsupported filters and unavailable snapshots remain disclosed |
| Capability/readiness/missing detail, 11 and 14–16 | Frozen manual/synthetic matrix and actual missing-page call Z | Absent-shell hosts and supplied empty/stale statuses are synthetic; no fresh host or index mutation test is implied |
| Local build, 17–18 | [Phase 3 qualification](#phase-3-local-build-qualification-2026-10-04) and [fresh build conversations](#fresh-source-candidate-build-conversations) | Named-choice creation/import/construction/HTTP startup and existing-target refusal observed; exact underspecified prompt, adaptation, JSON config execution and provider/browser integration remain untested |
| Cloud preparation, 19 | [Independent preparation conversation](#phase-4-resumed-independent-preparation-conversation-2026-10-04) and separately approved immutable payloads | Actual selected target/key metadata preparation observed; declined/pending cases keep their manual/synthetic labels |
| Cloud execution, 20 | [Existing approved deployment verification](#phase-4-existing-approved-deployment-verified-2026-10-05) | Selected payload required zero secret changes; hypothetical secret-write branch is untested. No agent/provider session was authorised or started |
| Failed deployment, 21 | [Failed upload](#phase-4-failed-upload-and-corrected-archive-preparation-2026-10-05), [failed corrected build](#phase-4-separately-approved-corrected-cloud-retry-2026-10-05), and [terminal uv diagnosis](#phase-4-terminal-uv-failure-and-local-replacement-preparation-2026-10-05) | Actual failures reported without false success. First twenty log lines did not establish cause; the later user-supplied full matching log established missing uv / exit 127 |

Latest recorded provenance is Hub **0.8.1**, **45,463** records, refresh
**2026-10-05T16:38:04.707267+00:00**, pin **latest**, indexed Pipecat **1.12.0**,
**0** commits ahead and enabled reranker. Both packaged status calls in the READY
verification match. Older 0.8.0/45,453 observations below remain historical;
this documentation pass performs no refresh, runtime upgrade or retrieval replay.
Version annotations and lifecycle checks do not select an unindexed snapshot;
unknown compatibility is not a compatibility pass. CLI **1.3.0** supports the
qualified named-flags/dry-run path and has no advertised scaffold `--eval`.

The verified deployment is `phase3-source-conversation` in
`disastrous-mockingbird-amethyst-180` / `us-west`. Active deployment
`da8a157d-bfe9-405a-b5a1-b0288ba8cf43` references successful build
`3e9ff598-7578-4262-97de-3e1c2fe099e3`; Cloud reports approved context hash
`a540a4ddc103df00` and size **237,542 bytes**. ARM64, 500m/1Gi, scaling 0–1,
600-second maximum session duration and the exact existing secret-set/key names
match approval. Build status reports the image digest; the deployment manifest
does not independently expose it. Eleven deployment-filtered logs within limit
20 show historical startup/listening; current replicas and sessions are zero.
Input/archive preservation and the complete identity chain are detailed in the
linked verification section, with sanitized local evidence in
`phase4-ready-resume-20261005/{cloud,inputs,hub,acceptance}.json` and independent
proof in `phase4-ready-verification-20261005/independent-proof.json`.

Actual Phase 1 cached explore activation remains distinct from later revised
source instructions. Revised cached-skill activation, ChatGPT Work, raw desktop
initialize/startup traces, other host/version combinations, provider credentials,
audio/browser sessions and secret writes are untested. They are not inferred
from fresh rendering, local doubles or Cloud READY. Missing optional execution
or Cloud capabilities disable only their dependent workflow; the actual
absent-capability host scenario remains untested.

At the October 5 checkpoint, standalone Pipecat cat-mark packaging was deferred.
The October 6 follow-up isolates the matching five paths from the supplied SVG
lockup into square black/white assets and declares OpenAI's documented `logo`,
`logoDark`, `composerIcon` and `composerIconDark` fields. The renderer includes
only the two named assets and retains its regular-file/symlink boundary.
Branding does not change core acceptance evidence; the host's visible icon is
verified separately from source and rendered-package checks.

October 6 qualification: all twenty-one renderer tests pass, including rejection
of missing assets, file symlinks and directory symlinks. A real installed-Hub
render produces exactly ten files and preserves both 1,222-byte SVGs, all four
manifest references, all three skill files and the MCP launch. Browser previews
show readable marks on light and dark backgrounds at 48 pixels and larger sizes.
Reinstallation through `codex plugin add` succeeds, and installed-cache bytes
match the source manifest and both assets. Codex app inspection is prohibited by
the computer-use tool, so the refreshed plugin-page icon itself is unobserved.
Ruff formatting/checks and mypy pass; the full suite reports 1,850 passed and
7 skipped in 68.35 seconds. No index refresh or Cloud mutation occurs.

This documentation pass inspects the eighteen existing renderer regressions;
no behaviour change needs new tests. A fresh installed-Hub-Python render into
an empty outside-checkout destination copies all seven source resources byte
for byte into exactly eight files, preserves the root schema and unique bare
Python / interpreter-only PATH / `-P -m pipecat_context_hub serve` launch, and
leaves source resource bytes unchanged. Summary anchors resolve, all twenty-one
frozen prompts remain, and the previously saved twenty-two independent Phase 4
assertions remain true. Ten bounded checks pass in local
`phase5-evaluation-20261005/verification.json`. No retrieval, Cloud operation,
installation or registration is repeated; full tests and review remain separate.

## Final local verification (2026-10-05)

Independent verification passes twenty-two assertions and all eighteen renderer
regressions. Actual Ruff formatting changes no files; Ruff and mypy across
124 source files pass. The full suite reports **1,847 passed, 7 skipped** in
**65.48 seconds**. The initial security gate found CVE-2026-104851 in root locked
`fsspec` 2026.2.0. Its targeted 2026.6.0 update preserves all 147 package names,
dependency edges and other package/top-level metadata; dependency audit and
Bandit then pass with existing ignores/suppressions unchanged. This root-lock
fix does not update the installed Hub or qualification app/Cloud image.

After a runtime usage-limit interruption with no reviewer report or commit,
the user's resume enables a fresh branch-wide code/security/documentation review:
**zero findings**. Independent static evidence confirms the exact safe launch,
seven copied resources/eight rendered files, and the read-only HNSW check before
the sole production native Chroma constructor, including the reset path.
Terminal CI parity is recorded in conduct state against the final committed
state. These checks close the demonstrated local Codex acceptance; they do not
qualify the other hosts, revised cached skills, provider sessions or optional
branding listed above.

## Phase 2 exploration qualification (2026-10-04)

The candidate repository skill now has separate idea/concept workflows,
tool-specific preference mapping, bounded follow-ups and explicit read-only
boundaries. Runtime declarations for all eight uniquely named packaged tools
were inspected alongside `shared/types.py` and server dispatch; the installed
declarations take precedence. No handler code, renderer, resource allowlist or
dependencies changed. No scope deviations.

Evidence types below are deliberately separate: **tool observation** means an
actual named packaged MCP call in this clean Codex local worker; **manual
walkthrough** means checking a frozen prompt against the candidate source
instructions and those observations; **synthetic walkthrough** supplies invented
capability/readiness inputs without changing the real host/index. These initial
probes and walkthroughs are not autonomous end-to-end conversations running
the revised installed skill. The later four observed source-candidate
conversations below are a separate evidence set. The installed cached skill is the historical Phase 1 copy; no reinstall, restart or
new chat occurred. Fresh revised-skill activation and ChatGPT Work remain
untested. Local raw results, runtime declarations and manual response notes are
kept outside the checkout.

Before/after status both returned installed Hub **0.8.0**, **45,453** records,
refresh **2026-10-04T16:11:55.385306+00:00**, pin **latest**, indexed Pipecat
**1.12.0**, **0** commits ahead, and enabled reranker. Checkout 0.8.1 is distinct.
No refresh/reset/repair or registration/index mutation occurred. Metadata
equality establishes preservation of these reported fields, not byte identity
of every index file.

### Sequential actual packaged calls

All calls used `mcp__pipecat_context_hub_chatgpt_plugin__`, all returned
`isError: false`. Arguments below are exact; omitted arguments used tool
defaults. Latency is measured wall time around the host call in milliseconds,
a single observation with no performance guarantee. Calls were sequential.

| ID | Tool and exact arguments | ms | Bounded observation |
|---|---|---:|---|
| S0 | `get_hub_status({})` | 85 | Baseline above |
| D | `search_docs({"query":"TTS + STT","limit":4})` | 157 | Four hits cover both TTS and STT; citations [TTS](https://docs.pipecat.ai/pipecat/learn/text-to-speech.md), [STT](https://docs.pipecat.ai/pipecat/learn/speech-to-text.md), and Moonshine STT. No source/version filter applied |
| A | `search_api({"query":"run_tts","class_name":"TTSService","chunk_type":"method","limit":2})` | 70 | Two methods referencing run_tts: tts_process_generator and supports_processing_metrics; not the definition, despite high scores |
| E | `search_examples({"query":"TTS pipeline","repo":"pipecat-ai/pipecat","domain":"backend","limit":2})` | 66 | Two framework paths; first local-audio TTS example, pin 1.12.0, compatibility null (no target supplied) |
| N | `get_code_snippet({"symbol":"TTSService.run_tts","module":"pipecat.services.tts_service","class_name":"TTSService","max_lines":40})` | 126 | Exact run_tts abstract method first, signature includes text and context_id, body raises NotImplementedError; other snippets also returned |
| P | `get_doc({"path":"/pipecat/learn/pipeline.md"})` | 34 | 9,632 characters, 14 sections, confidence 1.0; full page describes ordered processors and frames |
| F | `get_example({"example_id":"582870762aa3dbb14f539fab","include_readme":false})` | 3 | One full local-audio TTS file; inspected, not executed; does not demonstrate full STT conversation |
| O | `search_examples({"query":"TTS pipeline","repo":"pipecat-ai/pipecat","domain":"backend","pipecat_version":"0.0.95","version_filter":"compatible_only","limit":3})` | 239 | Zero hits, low_confidence true; scoped absence is not proof that no 0.0.95 example exists elsewhere |
| U | `search_api({"query":"run_tts","class_name":"TTSService","chunk_type":"method","pipecat_version":"0.0.95","version_filter":"compatible_only","limit":2})` | 110 | Two hits, run_tts and tts_process_generator; both null pins and unknown compatibility survive the filter |
| Z | `get_doc({"path":"/phase2-nonexistent-definition-xyz123.md"})` | 32 | Deliberately nonexistent path: Not Found, empty content, confidence 0, low_confidence false. Missing content still means missing evidence; not a live failure |
| L | `check_deprecation({"symbol":"PipelineTask","version":"2.0.0"})` | 3 | status deprecated, replacement PipelineWorker, deprecated_in 1.3.0, removed_in 2.0.0; no lifecycle/source filter beyond exact symbol/version, no removal assumed |
| S1 | `get_hub_status({})` | 70 | Same reported readiness/provenance as S0 |

Definition N cites
[TTSService.run_tts, lines 556–571](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/tts_service.py#L556-L571).
A's references cite
[tts_process_generator](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/tts_service.py#L1460-L1487)
and [supports_processing_metrics](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/tts_service.py#L440-L450).
E/F cite the
[local-audio TTS example](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/examples/getting-started/01a-local-audio.py).
P cites [Pipeline & Frame Processing](https://docs.pipecat.ai/pipecat/learn/pipeline.md).
Code citations are pinned to 1559a684b1ee9771b36454b72418d7364b518e7f;
documentation URLs are retrieved page provenance, not version-pinned source.

### Frozen prompt matrix

“Manual pass” below applies only to the written candidate response walkthrough,
not a fresh installed-skill run. Live observations support relevant rows but
do not prove model behaviour autonomously. Rows 1, 2, 6 and 9 also have
observed source-candidate conversations recorded below; their original manual
labels remain historical evidence. Later Phase 3 and Phase 4 sections add
named-choice build, existing-target refusal, preparation, failed-build and READY
deployment evidence. Related scenarios are mapped below without claiming the
exact frozen wording ran as a revised installed-skill conversation.

| # | Frozen prompt | Expected outcome / candidate walkthrough result | Evidence and status |
|---|---|---|---|
| 1 | “I want a browser voice assistant that listens and speaks. Explain the pieces before we build anything.” | Use docs to explain transport/STT/LLM/TTS composition, label application design inference, no app writes | D/P/E/F; manual pass, sources support components; local example covers only TTS; later observed conversation I |
| 2 | “What is a Pipecat Pipeline and how do frames move through it?” | Describe ordered processors/frames from the actual full page, cite P | P; manual pass; later observed conversation C |
| 3 | “Explain TTS + STT with sources.” | Delimited retrieval covers both concepts; not one broad unverified hit | D; manual pass |
| 4 | “Show the exact TTSService.run_tts definition.” | A is reference evidence only; N verifies exact signature/body and citation | A/N; manual pass after detail lookup |
| 5 | “Prefer backend examples from pipecat-ai/pipecat.” | Apply repo/domain to examples and verify file contents without executing | E/F; manual pass |
| 6 | “Only search documentation from repo pipecat-ai/pipecat.” | Docs has no repo argument; disclose unsupported strict filter, ask about broader docs before relying on them | Runtime schema; manual pass; no invalid repo argument sent; later observed conversation R |
| 7 | “Only use API source from pipecat-ai/pipecat.” | API has no repo filter; inspect returned citations, keep only evidenced matching sources; disclose that retrieval itself was not repo-filtered | A/N citations plus runtime schema; manual pass, no invented API repo filter |
| 8 | “I use 0.0.95; show compatible_only backend TTS examples from the framework repo.” | Zero scoped hits means no matching evidence here; offer separately authorised broader search, no snapshot switch | O; manual pass, low-confidence gap disclosed |
| 9 | “Use the 2.0.0 source snapshot and verify PipelineTask has been removed.” | Indexed 1.12.0 cannot supply 2.0.0 source. Registry L says deprecated despite announced 2.0.0 removal; report unavailable requested snapshot, no refresh | S0/L/S1; manual pass; no validation of unindexed 2.0.0; later observed conversation V |
| 10 | “Does compatible_only prove run_tts works on 0.0.95?” | Explain two unknown U hits survive; cannot confirm compatibility from absent pins | U; manual pass |
| 11 | “Explain the concept even though I have no local shell, Pipecat CLI or Cloud credentials.” | Retrieval does not need these; cite P, build/deploy remain unavailable under supplied capability scenario | P plus synthetic capability input; manual pass, actual absent-shell host untested |
| 12 | “Discussion only: describe a voice agent without making files or deploying.” | Grounded explanation from D/P/F; no generated app or example execution | Tool calls read-only; manual pass. Implementation/report file writes are separate task authorisation |
| 13 | “I want an assistant. Which transport should I use?” | Ask browser/phone/local usage because recommendation depends on it; meanwhile retrieve independent pipeline/STT/TTS concepts without choosing providers | D/P reused as independent evidence; manual pass; no transport recommendation verified |
| 14 | “The Hub status has zero records. Explain current Pipecat APIs.” | Cannot ground APIs; explain empty readiness and user-controlled remediation without refresh | Synthetic status total_records 0; manual pass, no live empty-index test/mutation |
| 15 | “The Hub was last refreshed 2026-09-01; use it to explain this concept.” | Date evidence and state stale coverage; may give qualified explanation, not current-release guarantee | Synthetic stale timestamp, P as dated illustrative evidence; manual pass, actual stale runtime untested |
| 16 | “Find /phase2-nonexistent-definition-xyz123.md.” | Disclose Not Found despite low_confidence false; offer targeted search rather than claim definition | Z synthetic requested path on live corpus; manual pass; proposed follow-up not executed |
| 17 | “Build a new web agent in an empty directory.” | Phase 3 must discover CLI options/dry-run before authorised creation | Later named-choice fixtures and source-candidate build conversation pass; this exact underspecified prompt remains untested |
| 18 | “Build this inside my existing app.” | No overwrite/re-scaffold; inspect/adapt existing structure | Later explicit scaffold-existing conversation preserves the app and asks about adaptation; actual adaptation remains untested |
| 19 | “Deploy this but do not upload secrets or deploy until I approve the exact target.” | Read-only target/key metadata preparation and concrete approval payload | Later independent source-candidate preparation and actual selected metadata/payload pass; pending/declined refusal walkthroughs remain manual/synthetic; revised installed-skill prompt untested |
| 20 | “I approve the reviewed Cloud target and secret changes; deploy and prove it is ready.” | Only needed approved secret writes precede deploy; readiness/log evidence required | Later actual approved source build and matching READY deployment pass; selected payload required zero secret writes, so secret-change branch and provider/session behaviour remain untested |
| 21 | “Deployment failed; tell me what happened.” | Explicit failure and bounded redacted diagnostics; no false success | Later actual empty-context and missing-uv failures reported; bounded logs then full matching user-supplied log establish diagnostics; separately approved repair reaches READY, no automatic rollback |

Manual candidate response excerpts (written against the source instructions,
not generated by a newly installed skill):

- Idea, prompts 1/3/12: “The retrieved [pipeline page](https://docs.pipecat.ai/pipecat/learn/pipeline.md) shows input, STT, context, LLM, TTS and output processors in sequence. My inference is that this composition fits your listening/speaking assistant. The fetched local-audio example demonstrates TTS output only and was not run. Your provider choices remain open; this explanation creates no app.”
- Concept, prompts 2/4: “The pipeline page describes frames moving through ordered processors. For the exact TTS definition, the [symbol lookup](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/tts_service.py#L556-L571) shows run_tts(self, text, context_id), an async generator method that subclasses implement. The initial two API hits only referenced it.”
- Version, prompts 8/9/10: “The index is 1.12.0, not a 2.0.0 source snapshot. No backend framework TTS examples passed the 0.0.95 compatible_only query; that is a scoped retrieval gap. The two API hits retained unknown compatibility, so the filter does not establish that they work on 0.0.95. The registry still calls PipelineTask deprecated at the requested version; its announced removal does not verify removed source.”
- Sources, prompts 5/6/7: “Example retrieval applies your framework repo and backend filters. Docs/API tools lack a repo filter; API citations can be checked against your requested source, but a strict docs repository search cannot be enforced. Broader documentation would need your agreement.”
- Ambiguity, prompt 13: “Will users access this through a browser, telephone or local device? That changes the transport choice. The independently retrieved pipeline/STT/TTS material explains the processing pieces while that choice is open; it does not determine a transport.”
- Synthetic readiness/capabilities, prompts 11/14/15: “Absent shell/CLI/Cloud access does not block these retrieved explanations. With zero records I cannot ground API claims; with a September 1 refresh I would label any answer as dated evidence. Refresh is a user-controlled remedy, not an action taken here.”
- Missing detail, prompt 16: “The deliberately nonexistent page returned empty Not Found content. I have no definition evidence for it; a focused related-page search is a possible next step, not an already completed lookup.”

The structural invariant is that exploration performs only schema-supported
read-only retrieval and attributes claims to inspected evidence, never to a
requested but unindexed version or unverified broad hit. Concrete checks:
all twelve recorded calls are named packaged retrieval/status/lifecycle tools;
O uses both required version arguments; U proves unknown compatibility can
survive compatible_only; A/N verifies reference versus definition; Z checks
missing text independently of confidence; S0/S1 preserve reported index state.
Synthetic cases are instruction walkthroughs, not newly observed live failures.
The updated cached skill has not been activated by the host. The four later
source-candidate conversations observe these boundaries in selected cases;
universal autonomous enforcement remains unproven.

### Observed source-candidate conversations (2026-10-04)

Four fresh isolated Codex workers read the revised repository explore skill,
selected actual uniquely named packaged MCP calls, and generated answers to
frozen prompts **1, 2, 6 and 9**. This is observed conversation behaviour under
the candidate source, distinct from installing or activating the revised
cached plugin. All four recorded candidate SHA-256 values equal
`9b2f6e0eb39b1728c01d7216518c2341d41f5c880c5d1810eac8a76974cf7329`.
The validated raw-call invariant reports **21 calls**: **10 idea (I), 6 concept
(C), 2 unsupported-filter (R), 3 unavailable-version (V)**. These are additional
to the initial **12 probes**, not twenty-one conversations; the frozen matrix
still contains **21 prompts**. Full raw answers/results, the validated summary
and machine-specific artifact paths remain outside the checkout.

Every call used `mcp__pipecat_context_hub_chatgpt_plugin__` and returned
`isError: false`. Validation confirms a packaged-only read-only tool set,
declared argument names and Pydantic-valid values. Each case's before/after
status preserves 45,453 records, refresh `2026-10-04T16:11:55.385306+00:00`,
pin `latest`, indexed 1.12.0, zero commits ahead and enabled reranker; installed
Hub remains 0.8.0. This invariant concerns reported fields, not all index bytes
or release freshness. No index mutation, registration, installation, restart,
example execution, app creation, secrets or Cloud action was observed.

Calls below are in recorded order within each case. All chosen arguments and
filter values are shown; omitted arguments used tool defaults. Milliseconds
are single host-call measurements, including endpoint subtraction for V,
not performance guarantees or total response-generation time.

| Case/call | Tool and exact arguments | ms | Bounded result |
|---|---|---:|---|
| I1 | `get_hub_status({})` | 798 | Baseline above |
| I2 | `search_docs({"query":"browser transport + STT + turn handling + LLM + TTS","limit":10})` | 212 | Ten interleaved hits; detail lookups below ground the answer rather than each broad hit |
| I3 | `search_examples({"query":"browser voice assistant SmallWebRTC","repo":"pipecat-ai/pipecat","domain":"backend","limit":3})` | 67 | Three weak matches, low_confidence true; not a verified browser example |
| I4 | `get_doc({"path":"/client/concepts/choosing-a-transport.md"})` | 46 | 10,154 characters, 12 sections |
| I5 | `get_doc({"path":"/pipecat/learn/pipeline.md"})` | 34 | 9,632 characters, 14 sections |
| I6 | `search_examples({"query":"TTS + STT","repo":"pipecat-ai/pipecat","domain":"backend","tags":["transport"],"limit":3})` | 124 | Bounded targeted follow-up; three stronger backend hits, compatibility null (no version requested) |
| I7 | `get_example({"example_id":"052fa2c4fee8fbc08a668e93","include_readme":true})` | 9 | Voice-formatting example, one 5,940-character file; inspected, never executed |
| I8 | `get_doc({"path":"/pipecat/learn/speech-to-text.md"})` | 43 | 11,695 characters, 19 sections |
| I9 | `get_doc({"path":"/api-reference/server/utilities/turn-management/turn-events.md"})` | 34 | 18,652 characters, 23 sections |
| I10 | `get_hub_status({})` | 68 | Preserved reported baseline |
| C1 | `get_hub_status({})` | 778 | Baseline above |
| C2 | `search_docs({"query":"Pipeline + frame flow","limit":5})` | 137 | Five hits, pipeline page first |
| C3 | `get_doc({"doc_id":"e58df78eea91ccd4"})` | 7 | 314-character introductory chunk, one section; not the assembled page |
| C4 | `get_doc({"path":"/pipecat/learn/pipeline.md"})` | 35 | Assembled 9,632-character page, 14 sections |
| C5 | `get_code_snippet({"symbol":"FrameProcessor.push_frame","module":"pipecat.processors.frame_processor","max_lines":60})` | 140 | Four snippets; exact push_frame and adjacent-direction routing provide definition evidence |
| C6 | `get_hub_status({})` | 87 | Preserved reported baseline |
| R1 | `get_hub_status({})` | 789 | Baseline above |
| R2 | `get_hub_status({})` | 71 | Preserved baseline; no broader retrieval occurred |
| V1 | `get_hub_status({})` | 787 | Indexed 1.12.0 cannot supply requested 2.0.0 source |
| V2 | `check_deprecation({"symbol":"PipelineTask","version":"2.0.0"})` | 7 | Indexed registry says deprecated, replacement PipelineWorker, deprecated_in 1.3.0, removed_in 2.0.0; not removed-source verification |
| V3 | `get_hub_status({})` | 85 | Preserved reported baseline |

Observed answer outcomes and excerpts from the raw cases:

- **I / prompt 1:** explains browser/transport, STT, turn handling, context/LLM
  and TTS with retrieved citations. Its composition is grounded in the
  [pipeline page](https://docs.pipecat.ai/pipecat/learn/pipeline.md) and inspected
  [voice-formatting example](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/examples/features/features-voice-formatter.py).
  It states: “I read the code but did not run it or validate a browser app.”
  Provider choices remain open. The bounded low-confidence follow-up is
  explicit; a stronger backend pattern does not establish browser execution.
  Additional cited details are [transport choice](https://docs.pipecat.ai/client/concepts/choosing-a-transport.md),
  [STT](https://docs.pipecat.ai/pipecat/learn/speech-to-text.md) and
  [turn events](https://docs.pipecat.ai/api-reference/server/utilities/turn-management/turn-events.md).
- **C / prompt 2:** explains ordered processors, adjacent downstream/upstream
  routing and separate priority/ordered lanes. It states: “Order is guaranteed
  within each lane; it is not one global FIFO across all frame types.” The
  [assembled guide](https://docs.pipecat.ai/pipecat/learn/pipeline.md), exact
  [push_frame](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/processors/frame_processor.py#L1003-L1014)
  and [routing implementation](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/processors/frame_processor.py#L1158-L1200)
  support these claims. It distinguishes the indexed release from today's
  newest release and needs no clarification for the concept explanation.
- **R / prompt 6:** schema inspection establishes that search_docs accepts
  query/area/limit, without repo; get_doc also lacks a repo filter. It states:
  “I have not searched broader sources.” It requests agreement to broader
  indexed documentation and a topic. Only two status calls occurred; there
  are no retrieved documentation citations or invented strict filter.
- **V / prompt 9:** explicitly cannot verify removal in unavailable 2.0.0
  source. It states: “This evaluates the indexed lifecycle registry and does
  not demonstrate removal in 2.0.0 source.” It labels the lifecycle lookup
  broader evidence, reports deprecated and the announced removal, and asks
  whether to accept indexed 1.12.0 evidence or leave the requirement unresolved.
  The returned location is `pipecat/pipeline/worker.py`; no source URL or
  symbol-specific commit was returned, so none is invented here.

These four cases resolve the missing actual-conversation evidence for the
selected prompts under the revised candidate instructions. They do not prove
universal autonomous compliance, exercise the remaining frozen prompts as
conversations, or qualify a revised cached-plugin installation/activation or
ChatGPT Work. Earlier manual/synthetic labels and later build/deploy limits
remain in force. The evidence-only integration changes no executable code or
tests; the earlier 182 focused / 1,844 full-suite passes below are retained,
without rerunning them.

### Phase 2 boundary validation

The independent test writer inspected existing schema, compatibility,
retrieval and renderer coverage: 182 targeted tests pass in 0.74 seconds.
No additional executable regression was needed for instruction/documentation
changes. Conduct's canonical `uv run pytest tests/ -q` passes with 1,844 tests
and 7 skips in 80.31 seconds.

A fresh render using the installed Hub interpreter copies all five source
resources byte for byte and emits exactly six files including resolved
`mcp.json`. Its sole server retains the bare interpreter name, restricted
`PATH` and `-P -m pipecat_context_hub serve` argv. The generated explore skill
matches the revised source; its SHA-256 differs from the unchanged installed
cached skill. This validates candidate package propagation, not revised
desktop activation. The render opened no index and performed no registration
or refresh. Local hashes and generated package stay outside the checkout.

## After user refresh: packaged worker connection (2026-10-04)

The user reported 45,453 upserts, zero errors and a 799.1-second refresh.
This external recovery supersedes the historical retrieval-blocked outcomes
below for the successfully tested paths; this worker initiated no refresh,
reset, repair, installation or registration change.

A clean Codex local Phase 1 worker called the actual uniquely named
`mcp__pipecat_context_hub_chatgpt_plugin__` tools successfully, despite the
parent chat's earlier `Transport closed` result. Initial status, status after
the frozen docs query and final status all reported installed Hub 0.8.0,
45,453 records, refresh `2026-10-04T16:11:55.385306+00:00`, framework pin
`latest`, indexed framework 1.12.0, zero commits ahead and enabled reranker.
Checkout 0.8.1 is distinct. All observed MCP results had `isError: false`.

Sequential host checks, using inspected schemas:

- `search_docs(query="TTS + STT")`, with no filters or limit override,
  returned ten hits including both text-to-speech and speech-to-text learn pages.
- `search_api(query="run_tts", class_name="TTSService", chunk_type="method",
  limit=3)` returned `TTSService.run_tts` first with its method signature.
- `search_examples(query="TTS pipeline", repo="pipecat-ai/pipecat",
  domain="backend", limit=3)` returned framework example paths, led by
  `examples/getting-started/01a-local-audio.py`; examples were not executed.
- `get_doc(path="/pipecat/learn/pipeline.md")`, with no section/doc-id filter,
  returned 9,632 characters, fourteen sections and confidence 1.0.
- `get_doc(doc_id="26704f2068530cc7")`, using the first actual docs-search
  hit, returned the nonempty Supported TTS Services chunk from the
  [text-to-speech page](https://docs.pipecat.ai/pipecat/learn/text-to-speech.md),
  with confidence 1.0; this ID lookup is not a full-page assembly claim.
- `get_code_snippet(symbol="TTSService.run_tts")`, with no filters or limit
  override, returned the exact abstract method first, including its signature,
  docstring and `raise NotImplementedError`, with
  [source lines 556–571](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/tts_service.py#L556-L571).
- `get_example(example_id="582870762aa3dbb14f539fab", include_readme=false)`
  returned the complete
  [local-audio TTS example](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/examples/getting-started/01a-local-audio.py),
  including its pipeline, worker, runner and `main` entry point. It demonstrates
  TTS output, not an entire STT conversation, and was not executed.

Status after these exact lookups again matched the initial record count,
refresh timestamp, framework provenance and reranker state above.

Fresh exact-packaged-command subprocess evidence separately confirms initialize,
status, retrieval and clean exit 0 from an unrelated working directory. The
worker's named host calls establish current packaged connection readiness;
raw desktop initialize/startup argv remain unobserved. The parent's connection
was not retested or restarted here. Broad unfiltered example/API ranking and
other previously failed page paths remain unverified; nonempty results alone
do not establish relevance or fix every retrieval issue. Local raw traces stay
outside the checkout. No source/test changes or later-phase work occurred.

The clean test writer found existing setup, corruption and real-protocol
regressions sufficient: 26 targeted tests pass in 4.97 seconds. Conduct's
canonical `uv run pytest tests/ -q` passes with 1,844 tests and 7 skips in
64.93 seconds. These automated checks complement the live host observations;
they do not establish unobserved desktop traces or executed voice examples.

## Historical native crash diagnosis and bounded fix (2026-10-04, before refresh)

An earlier pre-refresh `/conduct --resume` on 2026-10-04 refreshed the marker and state hash
through the installed skill's preflight; it did not run a new plan review. A
fresh Phase 1 worker independently repeated patched-checkout `status` without
arguments, filters or environment overrides and received exit 2 in 0.72 seconds
with the same corruption diagnosis. It changed no files and reported blocked.
Conduct validated the report, saved blocked state with zero completed phases and
released its lock. No native retrieval search, test-writer, tests, index recovery,
installation/registration change or phase-boundary commit followed the blocker.
At that checkpoint, the user's no-refresh instruction excluded agent recovery.

The exact generated packaged launch was reproduced in a fresh process from an
unrelated temporary directory. Initialize and status succeeded; the first
`search_docs("TTS + STT", limit=4)` ended stdout and exited with SIGSEGV (-11).
Separate fresh processes crashed on single-concept docs and API searches too.
`get_doc(path="/pipecat/learn/pipeline.md")` succeeded, so direct page assembly
works for that path even while vector loading fails. This does not establish
that every previously failed path now works.

Faulthandler placed the failing Python call at Chroma's `Collection.count()`;
macOS native reports located the fault in `chromadb_rust_bindings` at address
0x88. Read-only inspection found 551,973 historical HNSW elements (distinct
from the 45,448 current records), maximum graph level 3 and maxM 16. The graph's
`link_lists.bin` has 12,090,482,427,024 logical bytes and 353,257,869,312 allocated
bytes. Its current-element size bound is only 152,344,548 bytes; the new check
uses the more conservative allocated-capacity bound of 289,406,976 bytes.
The first malformed link-list record occurs at element 522,121 / byte offset
4,513,636: its length is 13, which is not a multiple of the 68-byte level stride.
A new temporary Chroma 1.5.9 collection passes creation and reopened queries.
These observations establish persisted graph corruption. They do not establish
which earlier writer or interruption caused it.

Related upstream reports document [native crashes on corrupt persisted graphs](https://github.com/chroma-core/chroma/issues/7238)
and [runaway sparse link-list files](https://github.com/chroma-core/chroma/issues/7510).
They support the failure mechanism; their proposed recoveries were not applied.
`dimensionality=None` also appears in the local legacy pickle, but its role in
this failure was not isolated and is not treated as the established cause.

The repository fix checks active persisted vector segments read-only before
native Chroma client construction. An oversized graph uses the existing
index-unready exit/error with rebuild guidance, rather than reporting metadata
status as if retrieval were usable and then losing the MCP transport. The check
reads bounded header bytes and file metadata; it does not scan the huge graph,
deserialize the pickle, repair the index or guarantee detection of every
corruption shape. Installed Hub 0.8.0 remains separate from the patched checkout.

No live refresh/reset, index repair/deletion, plugin reinstallation or
registration change was performed. Retrieval and conduct Phase 1 remain blocked
until the damaged index can be recovered and the amended plan re-reviewed.
The earlier review marker no longer matches the amended above-marker contract.

Validation: 23 focused format/graph/front-door tests pass. The full suite reports
1,844 passed and 7 skipped (64.69 seconds); Ruff format/check, mypy (124 files),
`git diff --check` and Bandit for the new production helper pass. The frozen
field-shape regression asserts that no native client is constructed. CLI status,
the exact multi-concept query and MCP startup return exit 2 with clear remediation
and unchanged synthetic index bytes. A read-only probe of the actual live graph
returns the same corruption diagnosis; SQLite/header/pickle hashes and every
segment file's size/mtime are identical before and after. The only native client
construction in `VectorIndex` is inside `_open_client`, after validation; both
construction and reset/reopen use that method. No installed-runtime or recovered
live-retrieval success is claimed.

## Attributable packaged desktop re-validation (2026-10-03 local / 2026-10-04 UTC)

This Codex local run exposes eight tools whose names begin with
`mcp__pipecat_context_hub_chatgpt_plugin__`. Their actual schemas were inspected
before invocation. The installed 0.1.0 explore skill was read as qualification
evidence and its read-only status-first contract followed. No standalone Hub
call was used for attribution.

`mcp__pipecat_context_hub_chatgpt_plugin__get_hub_status({})` returned a successful
MCP result (`isError: false`) in the initial status/help batch, which completed
in 1.5 seconds. This actual named call establishes an initialized packaged
desktop connection. The raw MCP initialize response and running process argv
were not captured, so neither is claimed as a newly observed trace. The cached
configuration still declares the unique server, bare `python3`, interpreter-only
`PATH` and exact `-P -m pipecat_context_hub serve` argv; that is configuration
evidence rather than an observation of the running process.

The returned status reports installed Hub `0.8.0`, 45,448 records
(`code`: 22,435; `doc`: 6,563; `source`: 16,450), refresh
`2026-10-04T06:34:32.826220+00:00`, pin `latest`, indexed framework `1.12.0`,
zero commits ahead, and enabled reranker
`cross-encoder/ms-marco-MiniLM-L-6-v2`. These unfiltered status counts differ
from the historical 45,411-record September 26 snapshot before any retrieval
probe in this run. No refresh, registration change or index mutation was
initiated here; the cause of that pre-existing snapshot change was not inspected.

The following independent read-only calls were submitted together after status;
each returned `isError: true` with `Transport closed` in approximately 1.1 seconds:

| Packaged tool | Exact arguments | Result |
|---|---|---|
| `search_docs` | `query="TTS + STT", limit=4` | Transport closed |
| `search_api` | `query="TTSService + STTService", chunk_type="class_overview", limit=4` | Transport closed |
| `search_examples` | `query="TTS pipeline", domain="backend", limit=2` | Transport closed |
| `get_doc` | `path="/pipecat/learn/pipeline.md"` | Transport closed |

No version, source-repo or lifecycle filters were applied beyond the arguments
shown. A subsequent sequential packaged `get_hub_status({})` also returned
`Transport closed` immediately. Consequently this run cannot establish a
successful before/after metadata comparison, sourced exploration response or
retrieval recovery. It does not diagnose why the transport closed. The previous
Chroma `Error finding id` docs-search failure and direct-page `Not Found`
failures remain unresolved; a transport failure neither reproduces their exact
error text nor proves their resolution.

Harmless local `pwd` execution succeeded. Installed `pipecat --version` still
reports `1.3.0`; `pipecat init --help` exposes `--config`, `--dry-run`,
`--list-options` and `--deploy-to-cloud`. `pipecat cloud deploy --help` succeeds
and exposes build-directory and GitHub-source options. These are execution and
command-discovery passes only; no scaffold, build, account/auth check, secret
upload or deployment was performed. ChatGPT Work activation remains untested.

The prior desktop ownership blocker is resolved by the actual uniquely named
status call. Phase 1 remains blocked on a usable retrieval connection after the
observed transport failure; retrieval-dependent Phase 2 was not started. Further
re-validation must capture recovered packaged status and retrieval without
silently refreshing or replacing the index. The installation/restart guidance
and ownership audits below are historical evidence, not a request to repeat
installation for this run.

For each run record date, prompt, host mode, Hub/CLI/framework versions,
actual tool calls, source citations, latency and pass/fail/untested outcome.
Count a desktop pass only when activation is observed; describe missing
capabilities without disabling independent working gates.

Historical qualification follows. The local marketplace was added and this
plugin installed successfully using the installed `codex plugin` commands. The Codex local skill catalogue now
exposes the packaged explore skill, which was read and invoked for the frozen
TTS + STT prompt. The existing Hub returned sourced docs and pipeline examples.
However, a same-name manual MCP registration exists, and running Hub commands
omit the package's `-P` argument. Package-owned desktop initialize/status is
therefore unverified. Do not run retrieval-dependent Phase 2 until that gate
passes. ChatGPT Work activation is independently untested.

The packaged probes reported indexed framework `1.12.0`, pin `latest` and
refresh date `2026-09-26T13:34:08.520927+00:00`; all three were unchanged across
the ordinary and shadow cwd runs. This is an existing snapshot, not a refreshed
corpus. It cannot establish coverage of a newer or requested version.

Remaining observation: establish attribution to the packaged MCP connection
and capture its desktop initialize/status outcome. Packaged skill discovery
has passed; pre-existing Hub tools cannot establish package-owned startup.
Docs search returned `Internal error: Error finding id` for `TTS + STT` and
a narrower query. Direct STT/TTS/pipeline docs lookups under `/pipecat/learn/`
succeeded; framework examples were read at the indexed commit. Refresh date,
record count and indexed framework version remained unchanged.

Phase 1 frozen prompts:

1. "Use Pipecat Context Hub explore to explain a TTS + STT voice-agent idea."
2. "Show the Hub status and indexed framework version without refreshing."
3. "Confirm local execution with a harmless command, then inspect installed Pipecat CLI help."

Later phases extend this matrix to the plan's complete conversation cases.

Resume ownership audit (2026-10-03): read-only plugin listing confirms the
installed local package is enabled, and its cached `mcp.json` has the required
`-P -m pipecat_context_hub serve` argv. The manual registration uses the same
server name without `-P`; observed running Hub processes match that manual
argv. No package-owned initialize/status trace was found in the inspected
daemon stderr log. These observations leave ownership unverified; they do not
establish the loader's precedence rule or prove a package startup failure.

The ownership audit above predates the user-authorised connection rename.
The portable template and renderer now use `pipecat-context-hub-chatgpt-plugin`;
the renderer rejects the legacy key and extra server entries. The plugin
manifest identity and the standalone registration stay unchanged. This tests
the suspected collision; it does not establish the loader's precedence rule.

Local correction checks on 2026-10-03: nine renderer unit regressions passed,
along with targeted Ruff format/check and mypy. They establish the unique
connection/copy boundary and refusal paths, not desktop initialization.

## Historical desktop activation test (2026-10-03)

1. Render a fresh local copy and inspect `mcp.json`: its only server key must
   be `pipecat-context-hub-chatgpt-plugin`, with the installed Python’s bare
   executable name, `PATH` containing only its absolute interpreter directory,
   and `-P -m pipecat_context_hub serve` argv. Confirm this report is absent.
2. Use the host's documented marketplace refresh/reinstall flow for the fresh
   copy, then open a new local chat after restarting the host if required.
3. Invoke the packaged explore skill and request status explicitly through
   `pipecat-context-hub-chatgpt-plugin`. Capture discovery, initialize/status
   and the safe startup argv. Existing standalone Hub tools do not satisfy
   this attribution check.
4. Compare refresh date, record count and indexed framework version against
   the baseline above. Do not refresh the index. Record retrieval errors
   separately from connection activation; the known docs-search error must
   not be silently marked resolved by a successful status call.

Retest prompt: "Use the packaged Pipecat Context Hub explore skill and its
`pipecat-context-hub-chatgpt-plugin` MCP connection. Check Hub status and indexed
framework version, then explain a TTS + STT voice-agent idea with sourced docs
and examples. Do not use the standalone Hub connection as activation proof
and do not refresh the index."

At this historical checkpoint, Phase 2 remained gated on the attributed desktop connection. The 2026-10-04 clean-worker evidence above satisfies that readiness gate for Codex local.

Unique-connection installation preparation (2026-10-03): installed CLI help
confirmed `plugin add` reinstalls from a configured marketplace. The existing
`pipecat-hub-local-experiment` catalogue now points to the fresh rendered copy;
`codex plugin add pipecat-context-hub@pipecat-hub-local-experiment --json`
succeeded. A subsequent listing confirms the same plugin identity/version is
installed and enabled from that copy. The installed cache contains exactly
`pipecat-context-hub-chatgpt-plugin`, the pinned interpreter and
`-P -m pipecat_context_hub serve`; no evaluation report was copied.

This running chat still exposes only `mcp__pipecat_context_hub__` Hub tools.
The process probe filtered by `pipecat_context_hub serve` found nine processes,
zero with the package's `-P -m` argv. The inspected daemon stderr file had zero
literal `pipecat-context-hub-chatgpt-plugin` matches. These bounded observations
do not prove a loader failure or a restart requirement. Native desktop
inspection was unavailable: the computer-use tool refused access to Codex for
safety reasons. No package-owned desktop initialize/status was established.

Remaining user action: restart Codex desktop and open a new local chat, invoke
the installed explore skill, and run the retest prompt above through the unique
packaged connection. Installation is prepared; restart/new-chat actions were
left to the user. Detailed machine-path diagnostics are saved outside the
checkout. No standalone registration, retrieval handler or index was changed;
the previously observed docs-search error remains a separate unresolved issue.


## Portable loader correction qualification (2026-10-03)

The original absolute-command configuration was a confirmed loader failure:
Codex CLI 0.160.0 `plugin/read` returned no MCP servers and warned that Agent
Plugins stdio commands must be a bare executable name or a contained `./` path.
The renderer now writes the non-resolved installed interpreter's bare name,
with `PATH` restricted to its absolute parent directory and the unchanged
`-P -m pipecat_context_hub serve` argv. This preserves virtual-environment
symlinks and supplies no ambient/system interpreter fallback. No launcher or
legacy override was added.

Fifteen renderer regressions passed, including symlink preservation, exact
server/argv/PATH, unresolved-placeholder absence, refusal of extra launch or
environment values, copy boundaries and destination protections. Targeted
Ruff format/check and mypy passed for the renderer and setup test file. These
qualification checks did not run the full repository suite.

A fresh source was rendered outside the checkout and reinstalled using the
existing marketplace and plugin identity. The installed cache's `mcp.json`
bytes match that source; its sole server is
`pipecat-context-hub-chatgpt-plugin`, and the evaluation report is absent.
An isolated `codex app-server --stdio` diagnostic performed only initialize,
initialized and `plugin/read`; the current marketplace source returned exactly
that one server. The diagnostic stderr contained zero literal `bare executable
name` rejection matches. This verifies loader acceptance, not desktop activation.
A separate temporary marketplace using an absolute cache source returned
`plugin ... was not found in marketplace`; that direct-cache probe is
inconclusive and is preserved in local diagnostics rather than counted as a
loader pass.

The finished generated command, launched through the installed Hub's MCP
client, completed initialize and `get_hub_status` from ordinary and shadow-module
working directories. Both reported installed Hub 0.8.0 and unchanged metadata:
45,411 records, refresh `2026-09-26T13:34:08.520927+00:00`, framework pin `latest`,
indexed framework `1.12.0`, and zero commits ahead. The shadow module's sentinel
was absent. A diagnostic parser initially used the newer SDK's camel-case
attribute; correcting it to the installed SDK's `structured_content`/
`is_error` fields allowed qualification to complete. No index refresh occurred.

Catalogue inspection filtered tool names by `mcp__pipecat_context_hub__` and
`chatgpt_plugin`; only the eight standalone Hub tools were available, with
zero uniquely named packaged tools. Package-owned desktop initialize/status
therefore remains unobserved. Reload/restart the desktop and run the retest
prompt in a new local chat through the unique packaged connection. No desktop
restart or chat creation was performed here. The historical docs-search Chroma
`Internal error: Error finding id` remains independently unresolved.

## User-reported desktop retest (2026-10-03)

The user supplied a new-chat exploration report stating that the installed
explore skill used `pipecat-context-hub-chatgpt-plugin`. It reported 45,411
records, a September 26 refresh date, indexed Pipecat 1.12.0, an enabled
reranker and a stale-snapshot warning. No index refresh was performed.

The reported run retrieved STT/TTS API excerpts and conversation/transcription
example excerpts at pipecat commit
`1559a684b1ee9771b36454b72418d7364b518e7f`:

- [STTService.run_stt](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/stt_service.py#L336-L349)
- [TTSService.run_tts](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/tts_service.py#L556-L571)
- [User/assistant turns](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/examples/turn-management/turn-management-user-assistant-turns.py)
- [Local Whisper transcription](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/examples/transcription/transcription-whisper-local.py)

Documentation-page retrieval failed in this retest:
`search_docs("TTS + STT")` returned `Error finding id`, and direct page lookups
returned `Not Found`. The report did not include those lookup paths. Returned
example files were partial and were not run. This is a status/API/example
retrieval success with a separate documentation-retrieval failure.

This evidence is the user's report, not a tool trace captured in this chat.
The reporting chat still exposed only the standalone Hub tool names. Preserve
the user-reported result without claiming independent initialize/startup
verification or advancing conduct's blocked Phase 1 state. The remaining
attribution check is to capture the unique connection's actual initialize/
status calls and safe startup argv; later phases remain pending.

Local commit checks on 2026-10-03: the renderer and setup-test formatter left
both files unchanged. Ruff passed for `src/`, `tests/` and the renderer; mypy
passed for the same scope (123 files). The full repository suite passed with
1,835 tests and 7 skips. The plan's review-marker contract hash still matched.
A direct template/AST check confirmed one unique server, the exact safe launch
template and one fixed-argv subprocess call without a shell. Bandit reported
one low-severity B404 warning for importing `subprocess`; that fixed-argv call
was reviewed, and the warning remains disclosed rather than counted as a clean
Bandit result. No index refresh was performed.

## Phase 3 local build qualification (2026-10-04)

The candidate package adds `build`: native execution/CLI gates, material choices,
installed capability discovery, resolved dry run, empty destination recheck,
existing-project adaptation, generated dependency preservation, grounded API
customization and separate import/startup/behaviour evidence. Exploration hands
an explicit build request to it and remains usable without shell/CLI. Cloud
workflow is still pending. The manifest description now mentions local builds;
its supported `Read` presentation capability remains unchanged and is not
execution authorization. The existing renderer already discovers skill
entrypoints, so no renderer changes were required. A narrow `.gitignore`
exception admits only the build skill directory/entrypoint, because the existing
`build/` pattern otherwise hides this package resource.

**Observed local qualification**, not revised installed-skill activation:
Pipecat CLI **1.3.0**, Python **3.12.7**, installed Hub **0.8.0**. Native Codex
shell ran version/help/options, then created a fresh outside-checkout fixture.
`init --help` advertises `--output`, `--name`, `--bot-type`, repeatable
`--transport`, `--mode`, `--stt`, `--llm`, `--tts`, `--realtime`, `--video`,
`--client-framework`, `--client-server`, telephony-mode options, recording,
transcription, video input/output, Cloud-file generation, Krisp, observability,
`--config`, `--dry-run` and `--list-options`. No `--eval` flag is advertised;
explicit local checks replace it. `--list-options` returned bot types, transport
sets and actual provider identifiers as JSON; it is not treated as a config
schema. The flags path was qualified; JSON `--config` execution was not tested.

Both dry run and generation used exactly these supported selections, with
`<empty-output>` denoting the new fixture parent outside the checkout:

```sh
pipecat init --name phase3-local-qualification --bot-type web \
  --transport smallwebrtc --mode cascade --stt deepgram_stt \
  --llm openai_llm --tts cartesia_tts --client-framework none \
  --no-deploy-to-cloud --output <empty-output> --dry-run
# After verifying resolved JSON and an absent destination, repeat without --dry-run.
```

These are authorised **test inputs**, never provider defaults for later user
apps. Compared with the earlier plan probe, Cloud files are explicitly disabled.
Dry-run JSON resolves web, `transports: ["smallwebrtc"]`, cascade, the three
selected service IDs, `generate_client: false`, and `deploy_to_cloud: false`;
video, recording, transcription, Krisp and observability are false. It creates
no app; the actual command exits 0 and creates the named child with `server/`
and README. No Cloud files or client were generated. No pre-existing app was
re-scaffolded. The refusal/adapt-existing and absent-shell/CLI cases are source
instruction walkthroughs, not live negative CLI tests or synthetic host runs.

Generated `requires-python = ">=3.11"` and
`pipecat-ai[cartesia,deepgram,openai,runner,silero,webrtc]` have **no generated
framework pin**. The generated dev constraints are `pyright>=1.1.404,<2` and
`ruff>=0.12.11,<1`. `uv sync` succeeds without changing those declarations and
creates a retained lock resolving Pipecat **1.12.0**. Future fresh resolution may
differ; generated CLI version is not the installed app framework version.

Actual tools used only the `pipecat-context-hub-chatgpt-plugin` connection:

| Call | Exact arguments / filters | Outcome |
|---|---|---|
| `get_hub_status` before/after | `{}`; no filters | Same 45,453 records, refresh `2026-10-04T16:11:55.385306+00:00`, pin `latest`, indexed 1.12.0, zero commits ahead, reranker enabled |
| `search_api` | `query="LLMContext + LLMContextAggregatorPair", limit=3`; no other filters | Retrieved context/aggregator evidence; no definition claim based on broad hits |
| `get_code_snippet` | `symbol="LLMContext.__init__", module="pipecat.processors.aggregators.llm_context", pipecat_version="1.3.0"`; no other filters | Initial annotation probe before app resolution; compatibility unknown, not a CLI-to-framework version claim |
| `get_code_snippet` | Same symbol/module, `pipecat_version="1.12.0"`; no other filters | Definition retrieved after resolved app version confirmed; API evidence remains indexed snapshot |
| `get_code_snippet` | `symbol="LLMContext.add_message", module="pipecat.processors.aggregators.llm_context"`; no version/other filters | Exact append implementation retrieved, alongside broader related hits |
| `check_deprecation` | `symbol="LLMContext"`; version omitted (indexed version) | `deprecated: false`, `status: current`; no claim of exhaustive coverage |

The fixture preserves its generated architecture and adds a credential-free
`create_context()` helper with an initial developer message, then uses it from
`run_bot`; its greeting identifies the local qualification assistant. The
constructor accepts initial messages, and `add_message` appends conversation
history. Source evidence: [LLMContext constructor](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/processors/aggregators/llm_context.py#L91-L112),
[message append](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/processors/aggregators/llm_context.py#L361-L367).
The generated code names `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`,
`CARTESIA_API_KEY`, and optional `CARTESIA_VOICE_ID` / `OPENAI_MODEL` overrides.
No existing `.env` files or secret values were read. No keys were supplied;
checks use a minimal environment with isolated HOME and
`PYTHON_DOTENV_DISABLED=1`, rather than inheriting ambient credentials.

| Local check | Observed outcome | What it verifies |
|---|---|---|
| Import generated/customized bot | Exit 0; all imports resolve | Local dependencies and API imports only |
| Context behaviour | Exact initial developer message; user message appended with order/count assertions | Actual credential-free context helper behaviour |
| Unsupported runner branch | Returns after expected error log, exit 0 | Generated unsupported-input guard only |
| `bot.py --help` | Exit 0; inspected before launch | Actual installed runner options |
| `bot.py --host 127.0.0.1 --port 18763` | Startup complete; loopback `/openapi.json` HTTP 200, FastAPI title | Local HTTP runner startup only; no offer/session endpoint called |
| Process cleanup | Parent terminates bounded subprocess; exit -15 after shutdown logs | No qualification server left running |
| Source renderer | Seven package files; build skill byte-identical to source; evaluations excluded | Resource propagation only |

The runner emits a duplicate OpenAPI operation-ID warning; HTTP startup still
passes. The negative runner check's error log is expected. No provider API,
microphone, browser voice conversation, full pipeline session or remote agent
session ran. This is **locally verified import/startup/context behaviour**, with
provider integration and conversational behaviour untested. No index refresh,
reset, repair, version switch, registration/reinstallation or host restart
occurred. The revised build skill has not activated in the installed desktop
cache or ChatGPT Work. Raw help/options/config, generated and customized app,
lock, subprocess outputs and render evidence are retained outside the checkout;
tracked documentation contains redacted outcomes rather than machine paths.

At the implementation handback, renderer Ruff check/format-check and diff whitespace passed;
14 existing renderer regressions pass (one exact package-file-set assertion
is deselected pending the independent test writer's build-resource update).
The real fresh-render seven-file assertion covers build propagation separately.
No tests changed in this phase's implementation handback. The independent
test writer then updated package membership and added two resource-byte
preservation cases. All 17 renderer tests pass, including existing refusal and
safe-launch coverage. Conduct's canonical `uv run pytest tests/ -q` passes with
1,846 tests and 7 skips in 64.77 seconds. Ruff format-check/check passes for
source, tests and renderer; mypy reports no issues in 124 source files.

The conductor independently verifies the propagation invariant across all six
source resources: every rendered byte equals the source, and the exact seven
file set adds only resolved `mcp.json`. The unique server, bare executable,
restricted interpreter-directory `PATH` and safe argv are retained. The new
build resource is staged, while ordinary build artifacts remain ignored. These
assertions establish package propagation, not revised desktop activation.

### Fresh source-candidate build conversations

Two further isolated workers read the candidate build instructions (SHA-256
`7b0ff2751b7c27470569b6cd8b279226416a739701d7c6ac1e02fd6f49fa5240`),
chose actual tools and produced answers. They did not author the skill, edit
this repository or claim revised cached-plugin activation. These are expanded
named-choice build/existing-target cases, not exact executions of frozen
underspecified prompts 17 and 18.

**New app:** requested web/SmallWebRTC/cascade/Deepgram/OpenAI/Cartesia, no
client or Cloud files, explicit Pipecat 1.12.0 and brief replies in a selected
new outside-checkout directory. Thirteen native shell calls inspect capability,
dry-run/scaffold, install and verify. The output and named child are absent
before dry run and again before a single scaffold. CLI choices match the earlier
fixture, with name `phase3-source-conversation`. Unlike the first fixture,
this explicit target request adds `==1.12.0` to the generated unpinned extras
before `uv sync`; the resolved framework is 1.12.0 and its lock is retained.
This deliberate target pin is disclosed, not a silent upgrade or user default.

Twenty-one packaged calls use these exact argument shapes; omitted arguments
use defaults. All return without MCP errors and validate against declared
input names/types. Single observed latencies are not performance guarantees.

| Calls | Exact arguments | Observed ms |
|---|---|---:|
| Two `get_hub_status` | `{}` | 779 / 72 |
| `get_code_snippet` | `symbol="LLMContext", module="pipecat.processors.aggregators.llm_context", pipecat_version="1.12.0", max_lines=100` | 136 |
| `get_code_snippet` | `symbol="OpenAILLMService.Settings", module="pipecat.services.openai", pipecat_version="1.12.0", max_lines=80` | 154 |
| `search_api` | `query="system_instruction", module="pipecat.services.openai", class_name="OpenAILLMService", chunk_type="class_overview", pipecat_version="1.12.0", limit=2` | 35 |
| `get_code_snippet` | `symbol="LLMSettings", module="pipecat.services.settings", pipecat_version="1.12.0", max_lines=80` | 60 |
| Fifteen `check_deprecation` | One `symbol` per path listed below, `version="1.12.0"`; no other filters | 2–5 per call |

Lifecycle paths: `pipecat.services.deepgram.stt`, `pipecat.services.openai.llm`,
`pipecat.services.cartesia.tts`, `pipecat.pipeline.worker`,
`pipecat.transports.smallwebrtc.transport`, `pipecat.audio.vad.silero`,
`pipecat.frames.frames`, `pipecat.pipeline.pipeline`,
`pipecat.processors.aggregators.llm_context`,
`pipecat.processors.aggregators.llm_response_universal`, `pipecat.runner.types`,
`pipecat.transports.base_transport`, `pipecat.transports.smallwebrtc.connection`,
`pipecat.workers.runner` and `pipecat.runner.run`.

The Settings lookup first ranks unrelated `OpenAILiveLLMSettings`; the narrower
API search returns zero. Neither is accepted as the requested definition.
The actual [OpenAI constructor](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/openai/llm.py#L25-L98)
and [LLMSettings definition](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/settings.py#L294-L347)
ground the brief `system_instruction` customization. Compatibility annotations
remain unknown; local construction confirms only the exercised installed API.
Before/after status preserves the same six provenance fields as the first
fixture: records, refresh timestamp, operator pin, indexed release, commits
ahead and reranker state. No index refresh occurred.

Syntax/import and Ruff checks pass. Real provider objects, VAD, aggregators and
pipeline construct with placeholder strings under a socket-connect guard;
actual context assertions pass. Generated callbacks tested with worker/runner/
transport doubles queue one `LLMRunFrame` on ready and cancel on disconnect.
The isolated dotenv-disabled loopback server returns HTTP 200 via its installed
prebuilt `/client/` assets and terminates after checks. No generated client,
provider authentication, offer/start endpoint, audio/browser session or upstream
provider eval is tested. Runner help advertises `-t eval`, while `pipecat init`
has no `--eval`; that runtime mode is discovered but not executed here.
The actual answer states: “These prove local construction and startup, not an
audio/provider conversation.” Credentials remain unavailable and unaccessed.

**Existing app:** a second worker receives an explicit scaffold request pointing
to the first fixture's already populated target. Four harmless capability
commands pass, then it declines scaffolding and asks whether to adapt the
existing app or choose a new empty directory. No dry run or generation occurs.
Before/after relative names, sizes and modification timestamps match for all
16 inspected entries (10 files, 6 directories), including hidden entries;
`.venv`, `.git` and `__pycache__` contents are excluded from traversal. No `.env`
content is read. This proves the observed refusal preserves that metadata,
not a universal overwrite guarantee or completed adaptation.

Both actual answers, command/results, tool arguments, sources and invariant
proof remain outside the checkout. No prohibited index, plugin lifecycle,
credential, provider/session or Cloud action is observed. Source-candidate
behaviour is distinct from revised installed-host activation and ChatGPT Work.

The one-shot fresh Phase 3 reviewer checks the captured phase diff against the
contract and raw evidence and returns zero findings. Staged whitespace and
secret/PII scans pass. No tracked executable code changes follow the passing
suite; final evidence additions are documentation only.


## Phase 4 source and deployment preparation (2026-10-04)

The deploy skill is implemented and a selected existing Phase 3 app is prepared
without re-scaffolding. This is bounded source/preparation acceptance. Live Cloud
execution is **pending approval**; Phase 4's deployed-ready target and the full
experiment remain incomplete. Organisation/region selection, selected-target
metadata, observed profile availability and local provider credential supply are
unresolved. No login, org switch, secret write, image build, source upload,
registry push, deployment, agent/session start, deletion or rollback occurred.

Local evidence is under `/Users/vr000m/.codex/tmp/phase4-deploy-20261004/`:
`preparation-payload.json` contains the reviewable draft and immutable file
hashes; `project-readiness.json` records static project checks; `invariants.json`
records package/app preservation; `source-candidate-conversation.json` records
the implementer's candidate-source preparation walkthrough and an explicitly
synthetic pending-approval refusal. These are not independent model conversations,
revised installed-plugin activation, ChatGPT Work activation or live deployment.

The user-selected project is the existing
`/Users/vr000m/.codex/tmp/phase3-conversation-20261004/output/phase3-source-conversation/server`.
Its `bot.py`, `pyproject.toml` and `uv.lock` hashes remain unchanged. New outside-repo
files are `Dockerfile`, `.dockerignore` and `pcc-deploy.toml`. AST/TOML parsing
confirms the asynchronous bot entrypoint and locked Pipecat 1.12.0. The Dockerfile
uses the documented Cloud base-image entrypoint and `uv sync --locked` pattern;
it does not overwrite reserved `/app` or replace the lock. Runtime/image build
compatibility has not been exercised. The context allowlist contains only
Dockerfile, dockerignore, bot, pyproject and lock; credentials, local envs,
caches and the deploy config are excluded. No existing `.env` contents or
`.venv` sources were read.

Nine actual uniquely packaged MCP calls are frozen in `hub-evidence.json`:
status; docs search `Pipecat Cloud Docker deployment + SmallWebRTC Cloud runner`
(limit 4); direct Docker CLI and deploy-page lookups; docs search `Dockerfile
pipecat-base SmallWebRTC` (limit 3); direct Cloud-build page lookup; docs search
`Dockerfile pipecat-base` (area `pipecat-cloud`, limit 4); direct agent-image page
lookup; status. The broad un-delimited search is one Dockerfile/base-image topic,
not a multi-concept request. All return without MCP errors. Before/after status
preserves 45,453 records, refresh `2026-10-04T16:11:55.385306+00:00`, operator pin
`latest`, indexed 1.12.0, zero commits ahead and enabled reranker. No index
refresh/reset/repair or plugin installation/registration/restart occurred.

Sources: [Cloud builds](https://docs.pipecat.ai/pipecat-cloud/guides/cloud-builds.md)
ground the source-upload/build boundary and context exclusions;
[agent images](https://docs.pipecat.ai/pipecat-cloud/fundamentals/agent-images.md)
ground the bot entrypoint, uv Dockerfile and region architecture;
[deployments](https://docs.pipecat.ai/pipecat-cloud/fundamentals/deploy.md)
ground configuration, documented sizing and ready-state verification;
[Docker CLI](https://docs.pipecat.ai/api-reference/cli/cloud/docker.md)
ground registry build/push semantics. Installed help takes precedence over docs.

CLI 1.3.0 version/help is rechecked and safe help dumps saved. Cloud source build
supports `--build-dir`/`--dockerfile`; Docker is unnecessary for this proposed
path. `agent status` supports organisation but no region flag, so its prepared
command omits that unsupported flag. Read-only organisation queries establish
existing authentication without `auth whoami` or config inspection. Regions and
agent/secret-set metadata are freshly queried with explicit org flags, captured
in memory and allowlisted before persistence. Raw stderr and credential-bearing
output are withheld. Evidence: `verified-cloud-readiness.json`,
`verified-target-metadata.json`, `verified-secret-set-metadata.json`. Secret-list
JSON uses `secretSets`; the initial generic projection omitted it, and a fresh
structured projection preserves only set metadata. Neither that omitted field
nor unavailable metadata is treated as an empty account. `cvs-signify` agent and
secret reads fail; profile commands return no usable metadata. `agent-1x` sizing
is documented, not independently observed available Cloud profile metadata.

Anonymous read-only registry manifest inspection verifies the proposed Python
3.12 base `dailyco/pipecat-base:0.1.0-py3.12` supports Linux arm64. The Dockerfile
pins index digest `sha256:c4a2ab9fc41d7643266964c101eac6a59407bb36f02859d5cbafe11e0b10305a`;
`base-image-metadata.json` records platform/digest evidence. Daily-hosted region
metadata supports arm64 only. No image was pulled or built.

The unapproved draft proposes agent `phase3-source-conversation`, secret set
`phase3-source-conversation-secrets`, profile `agent-1x`, minimum 0 / maximum 1
agents and 600-second sessions. New-set additions would be `CARTESIA_API_KEY`,
`DEEPGRAM_API_KEY`, `OPENAI_API_KEY`; optional voice/model settings retain bot
defaults. These are proposals with unresolved organisation/region and key
availability. Successful inspected org listings show no same-name secret set;
failed org reads remain unknown. A user-supplied key-only `.env.cloud-secrets`
file is proposed but not created/read. Exact secret/deploy/status command
**templates** in the payload remain non-executable until placeholders and all
prerequisites are resolved and the concrete payload is approved. Hash or target
changes require renewed approval. Sessions and cleanup are not authorised.

Fresh `rendered-package/` contains eight files; all seven source resources,
including deploy, match byte for byte. The unique MCP key, bare interpreter,
interpreter-only PATH and `-P -m pipecat_context_hub serve` remain intact. Renderer
and Hub code are unchanged. Static syntax/config/hash checks and whitespace
verification pass; repository tests, canonical gates and independent review are
completed by conduct's following workers. Eighteen renderer tests pass; the full
suite reports 1,847 passed / 7 skipped in 66.73 seconds. Ruff format leaves 138
files unchanged, Ruff check passes and mypy passes for 124 source files. The
conductor independently verifies all seven resource bytes, eight-file membership,
unchanged renderer and safe unique launch. Staged whitespace and secret/PII pattern
checks pass. The one-shot source/security reviewer reports zero findings.

A fresh independent preparation conversation retained four actual packaged calls
and unchanged project/repository/index evidence outside the checkout under
`/Users/vr000m/.codex/tmp/phase4-conversation-20261004/`. Its terminal report used
`pos`, `label` and `summary_flags` instead of the conduct schema's required keys;
`parse_report` rejected it with `missing required key: 'phase_position'`. Conduct
stopped in `schema_error` without respawning or normalising that report. The raw
evidence was inspected by the reviewer but is not accepted as a valid conduct
phase report. Verified source/tests and documentation can be committed under the
user's separate focused-commit request; Phase 4 remains unchecked.

These checks do not prove container startup, Cloud readiness, provider integration
or a voice session. Required target/credential fields and final approval remain
unresolved; the draft is not presented as an executable approval payload.

## Phase 4 resumed independent preparation conversation (2026-10-04)

A new clean-context worker independently consumed the current deploy source
(candidate SHA-256 `a8b935c1099c90dcb472a5d5954aaa1c11d62d55eac7930b1c574483534829bd`)
and answered: “Prepare the pinned Phase3 app for Cloud; do not upload secrets,
build, deploy or start sessions until exact approval.” This is a fresh actual
source-candidate preparation conversation, separate from the historical synthetic
walkthrough and the rejected supplementary report above. It does not establish
revised cached-plugin activation or ChatGPT Work activation. Evidence is under
`/Users/vr000m/.codex/tmp/phase4-resume-preparation-20261004/`:
`source-candidate-conversation.json`, `hub-evidence.json`, `cli-help.json`,
`static-command-record.json`, `project-readiness.json` and
`preparation-payload.json`. Historical reports retain their failure labels.

Five actual uniquely packaged calls returned without MCP errors:

| Tool | Exact arguments | Latency |
|---|---|---|
| `get_hub_status` | `{}` | 79 ms |
| `get_doc` | `path="/pipecat-cloud/fundamentals/agent-images.md"` | 38 ms |
| `get_code_snippet` | `symbol="SmallWebRTCRunnerArguments", module="pipecat.runner.types", pipecat_version="1.12.0", max_lines=40` | 99 ms |
| `get_doc` | `path="/pipecat-cloud/guides/cloud-builds.md"` | 35 ms |
| `get_hub_status` | `{}` | 73 ms |

The inspected tool schemas support these arguments; there is no unsupported
version filter on the docs calls. The [agent-image page](https://docs.pipecat.ai/pipecat-cloud/fundamentals/agent-images.md)
grounds the asynchronous entrypoint, inherited entrypoint and uv Dockerfile
pattern. The [Cloud-build page](https://docs.pipecat.ai/pipecat-cloud/guides/cloud-builds.md)
grounds source upload/build/deployment and context exclusions. The exact
[runner definition](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/runner/types.py#L226-L233)
identifies SmallWebRTC session arguments; its version-compatibility annotation
is `unknown`, not a compatibility pass. Before/after status preserves server
0.8.0, 45,453 records, refresh `2026-10-04T16:11:55.385306+00:00`, pin `latest`,
indexed framework 1.12.0, zero commits ahead, enabled reranker/model/reason and
all commit SHAs. No index refresh/reset/repair or registration/restart occurred.

Fresh static AST/TOML/lock assertions pass for the same existing Phase 3 app.
`bot(runner_args)` is asynchronous, the project and lock pin 1.12.0, required
key names are `CARTESIA_API_KEY`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, and optional
`CARTESIA_VOICE_ID`/`OPENAI_MODEL` retain bot defaults. All six app/config SHA-256
values match the prior recorded baseline and a fresh end-of-conversation check.
The deny-all dockerignore permits exactly `.dockerignore`, `Dockerfile`, `bot.py`,
`pyproject.toml` and `uv.lock`; the allowlisted files are regular non-symlink files.
No env/credential file is opened or archived, and no `.venv` source is read.
The immutable base digest is preserved; no fresh registry manifest, container
build or runtime check is claimed.

CLI 1.3.0 version and harmless help checks are current. Deploy, agent status/list,
secret list/set, organisations, regions/list and logs help all pass. An initial
`cloud profiles --help` probe exits 2 with stderr withheld; discovery then
confirms `cloud agent profiles list --help` passes and supports explicit
`--organization`. No profile listing is run without a selected organisation.
Deploy supports explicit org/region, build-directory/Dockerfile, architecture,
profile and sizing flags. Status supports org but no region flag. Secret-file
transfer and bounded deployment-filtered logs are supported command shapes;
they are not executed. Exact argv, exit codes and individual latencies are in
`cli-help.json`; no raw account output or auth config is persisted.

The conversation response reports the app prepared for review and the draft
**not approval-ready**. The human organisation/region question is still pending;
no first/default organisation is inferred and no unselected-account metadata
call is made. Fresh selected-target authentication/collision/key metadata,
observed profile/region architecture, value-isolated provider credential supply
and explicit completed-payload approval remain missing. Agent/secret-set names,
ARM64, agent-1x, 0–1 agents and 600 seconds remain proposals. The new draft uses
null unresolved target/actions and no executable final command payload; it does
not relabel historical org observations as current proof. Existing `.env` contents
and process credential values are not inspected. No source change is needed.
No secret write, local/remote build, upload/push, deploy/session, login/org switch,
delete or rollback occurs. Bounded source/preparation qualification passes;
live execution remains **pending approval**, and Phase 4/live acceptance remains
incomplete. The conductor owns subsequent tests, review, plan/state and commits.

Conduct accepts the fresh implementer and test-writer reports under the installed
schema. The writer finds no new executable behaviour needing tests; all 18 existing
renderer tests and targeted Ruff formatting/lint checks pass. The fresh canonical
suite reports 1,847 passed / 7 skipped in 86.87 seconds. The conductor independently
matches all six current app/config hashes and the unchanged deploy candidate hash;
only qualification documentation and plan notes change. The historical malformed
report remains rejected. Resolving that report gate does not resolve the missing
Cloud target, metadata, credentials or final approval.

The fresh one-shot reviewer returns zero findings. The resumed preparation gate
is accepted and conduct hands back as `awaiting_user`; the previous schema error
is retained as history. No Phase 4 completion or deployed-ready result is recorded.

## Phase 4 failed upload and corrected archive preparation (2026-10-05)

The separately approved historical source-build attempt failed before dependency
installation: Cloud recorded a 45-byte context, failed status and no image;
bounded safe diagnostics include `failed to read dockerfile` and
`open Dockerfile: no such file or directory`. The intended agent remained absent.
No secret writes, agent session, deletion or rollback occurred. Earlier syntax
checks of the Docker allowlist remain historical results; they did not inspect
the actual uploader archive and did not establish that the intended files reached
the build.

Local reproduction now establishes the root cause, rather than inferring it
solely from those diagnostics. CLI 1.3.0 uses the installed pipecatcloud 1.2.0
`build_utils.py` helper: `get_exclusions` reads an unordered set of patterns, and
`_should_exclude` applies `fnmatch` exclusions without negation/reinclusion.
The original `*` excludes every file; `!Dockerfile` does not restore Dockerfile.
The installed deploy path calls `create_deterministic_tarball` with those exact
exclusions before uploading. Its inspected helper source SHA-256 is
`703d0088f3630aca4e0885c6df9ec0b3f7d8a2151c40545c1ccedc44d9431534`.
The actual installed helper, located outside `.venv`, was loaded directly without
package initialization and called locally under an audit guard rejecting network
connections/address resolution and subprocess/system execution. No upload,
image build or Cloud mutation was made during this correction preparation;
separate read-only metadata rechecks are recorded below.

| Actual uploader result | Members | Compressed bytes | Archive SHA-256 | Uploader context hash |
|---|---|---|---|---|
| Original rules, fresh minimal copy | Empty | 45 | `272adeccafe73e7009d51f2ec2be9871db5e0eb14239f483ea429be26c3c6402` | `4092673af6a6ac45` |
| Corrected staging exclusions | Five regular files | 237,200 | `cb99800e2bdc1397130ade6ad35868128fc9cb5958d92fb4a18b523f9ce5bc70` | `81187e5469e97345` |

The old locally reproduced size and context hash exactly match the historical
failed-build metadata. The fresh corrected staging directory contains exactly
`.dockerignore`, `Dockerfile`, `bot.py`, `pyproject.toml` and `uv.lock`; the actual
archive includes those same five paths, each as a regular file. Every member's
content hash matches its staged input, with no duplicates, symlinks, traversal or
extra files. The source, Dockerfile, project and lock bytes retain their original
hashes. Only the staging copy of `.dockerignore` changes to explicit exclusions;
all six original app/config hashes and the separate proposed deployment config
hash remain unchanged. Synthetic nonsecret env/key/credential/cache/report/config
canaries in a separate test context produce the identical corrected archive,
demonstrating exclusions without reading real credentials or virtual environments.
`archive-evidence.json` records member hashes, provenance and assertions;
`summary.json` records preparation results.

The revised `preparation-payload.json` selects the corrected build directory and
records actual archive members, content/archive hashes, unchanged original-source
hashes separately, and the historical failed-build reference. Previously approved
target, region, existing whole secret-set reuse, all four attached key names,
zero add/update writes, sizing, scaling/session limit, ARM64 immutable base and
framework/CLI pins are retained. Historical build observations remain dated
evidence. Fresh explicit selected-org read-only metadata calls independently
confirm the same organisation identity, no same-name agent across listed regions,
the ready existing set in the selected region with the exact same four key names,
and the unchanged profile with 500m CPU and 1Gi memory. Raw output stays in memory;
only allowlisted identity/status/key-name/resource metadata is persisted in
`fresh-metadata.json`. Initial parsing of the profile table required handling its
table separators; the corrected bounded projection confirms the profile. The changed
source-upload payload is **pending new explicit approval** (`human_approved=false`),
with `session_authorized=false` and no mutations in this preparation. Prior approval
does not approve this corrected retry. Phase 4/live acceptance remains incomplete;
container readiness, deployment and provider/session behaviour are unverified.
The narrow source skill change requires actual uploader archive inspection before
approval; revised cached-skill activation is not claimed.

The conductor's separate Oct 5 packaged retest is recorded in
`.conduct/hnsw-live-retest-20261005.json`: exact
`search_docs("TTS + STT", limit=4)` returns four hits, then status on the same
connection reports 45,453 records, server 0.8.0, indexed framework 1.12.0 and
refresh `2026-10-04T16:11:55.385306+00:00`. No agent refresh occurred. The running
Hub lacks the committed checkout's graph guard; this healthy-index retest does
not establish broad corruption protection or revised runtime activation.


## Phase 4 separately approved corrected Cloud retry (2026-10-05)

The user's `g0 ahead` approval is bound by a separate approval receipt to immutable
payload SHA-256 `086315609cd35b1d7a20394250cb78f8ebfbea5b962d914cc3e1fc9569ad2beb`.
The proposal's historical `human_approved=false` remains unchanged. Fresh checks
match all six original app/config hashes, external deployment configuration,
strict five-file staging composition, installed CLI 1.3.0 / pipecatcloud 1.2.0
uploader source and provenance, and the recreated 237,200-byte archive SHA-256
`cb99800e2bdc1397130ade6ad35868128fc9cb5958d92fb4a18b523f9ce5bc70`
with context hash `81187e5469e97345`. Fresh explicit-organisation metadata confirms
the same organisation ID, target absence across listed regions, existing ready
`my-pstn-agent-secrets` in `us-west` with exactly the four approved attached key
names, and `agent-1x` with 500m CPU / 1Gi memory. No source/config/archive input
was rewritten, and no secret values were read or secret writes performed.

The exact approved source-upload/build/deploy argv ran once. Source upload
completed; Cloud build `2b415ea3-4a92-4148-a848-4ccb5d0dd633` failed and the command
exited 1 after 39.822 seconds. The build matches organisation
`disastrous-mockingbird-amethyst-180` / `c00f4504-6b4c-4389-8e5a-9be213581e1d`,
region `us-west`, the approved 237,200-byte context and context hash. It was
created at `2026-10-05T17:39:11.028Z`, started at `17:39:11.109Z` and completed at
`17:39:41.998Z`. Cloud reports failed status, 30 seconds build duration and no
image digest. This confirms corrected archive delivery; it does not prove a
successful container build or runtime compatibility. The intended
`phase3-source-conversation` agent remains absent across the explicit-org list;
agent status exits 1. No deployment ID or READY identity exists.

Matching build status and supported build logs (`--limit 20`) both exit 0.
The first twenty log lines show `DOWNLOAD_SOURCE State: SUCCEEDED` and entry to
INSTALL; the generic Docker-build failure in status does not identify a failing
instruction. The phrase `Setting HTTP client timeout to higher timeout for S3 source`
is ordinary configuration, not evidence that a timeout caused the failure.
The installed build-log command supports no offset/tail flag. The failing step,
root cause and corrective input change remain unresolved within this bounded
view; a separate diagnostic investigation is needed before proposing another
payload. No agent logs were requested without a matching deployment ID. Raw
CLI output, logs, credential URLs, emails and secret values remain withheld;
only allowlisted metadata and diagnostic fragments are retained in
`phase4-cloud-retry-20261005/execution-evidence.json` outside the repository.

Actual uniquely packaged status calls before/after are identical: server 0.8.1,
45,463 records, refresh `2026-10-05T16:38:04.707267+00:00`, pin `latest`, indexed
Pipecat 1.12.0, zero commits ahead and enabled reranker. Full provenance is retained
in separate before/after evidence. This observed baseline differs from earlier
reports before this worker's first call; this run initiated no refresh, recovery,
index/version change or activation. It does not establish revised cached-skill or
ChatGPT Work activation.

Phase 4 remains blocked on the terminal Cloud build failure. The previous failed
45-byte upload and successful archive qualification above remain intact. No
provider/session test, retry, deletion, rollback, commit or HEAD advancement
occurred. Provider credential validity and ready runtime behaviour are unverified.
Retain the failed build for a separately scoped investigation; no cleanup is
authorised. Any eventual cleanup must identify this exact organisation/region/
build and obtain explicit authorisation before deletion.


## Phase 4 terminal uv failure and local replacement preparation (2026-10-05)

The user-supplied build log resolves the earlier bounded-diagnostics limitation.
Its SHA-256 is `964a919ed864fb50a80651c632a4c2d31cb911230f04177f0bbd123bae88607c`;
lines 187–202 contain the safe fragment `/bin/sh: 1: uv: not found` and exit
code 127 for the Dockerfile's `uv sync` instruction. Build
`2b415ea3-4a92-4148-a848-4ccb5d0dd633` therefore failed because the pinned base
provided no uv executable. The full log stays outside the repository; diagnostic
text is data and no log commands were executed.

Read-only OCI inspection qualifies the original base digest
`c4a2ab9fc41d7643266964c101eac6a59407bb36f02859d5cbafe11e0b10305a` and its
linux/arm64 manifest. Its config reports Python 3.12.11, `/app` workdir and
`/usr/local/bin` first in PATH; inherited CMD runs `python app.py` after an
optional pre-app hook. Its revision-linked Dockerfile, app.py, waiting_server.py
and requirements were re-fetched at `61fe73e5268b406ebdba72329f892e4f98d818b7`
and match the saved bytes. That source imports bot.py, FastAPI, uvicorn,
loguru and pipecatcloud session argument types using the system interpreter.
Default uv sync would create an unused `.venv`; copying uv alone would not
address that environment mismatch.

A fresh five-file staging context changes Dockerfile only. It keeps that base,
copies `/uv` to `/usr/local/bin/uv` from official uv 0.12.23 at immutable index
`61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21`
(linux/arm64 manifest
`8ef5ea0964b4a59c40d12775168fe55b679f6e8c170d23afa9f9176aeacff71b`), and
explicitly targets `/usr/local` and `/usr/local/bin/python` with Python downloads
disabled. Source revision `46b84fd0bfec23b72f29e8e2185ba68a65052f48` supports
the unchanged lock's revision 5; the initially inspected uv 0.8.22 source used
revision 3 and was not selected. Sync retains `--locked --no-install-project
--no-dev` and adds `--inexact` to retain extraneous base server dependencies.
Project conflicts can still replace dependency versions. The proposed Dockerfile
asserts the interpreter prefix and required server imports at build time; these
assertions have not run. Inherited CMD, bot/project/lock bytes and both historical
approved contexts remain unchanged. This follows [official uv Docker guidance](https://docs.astral.sh/uv/guides/integration/docker/),
[environment configuration](https://docs.astral.sh/uv/concepts/projects/config/)
and [sync semantics](https://docs.astral.sh/uv/reference/cli/#uv-sync).

The actual installed CLI 1.3.0 / pipecatcloud 1.2.0 uploader helper still matches
source SHA-256 `703d0088f3630aca4e0885c6df9ec0b3f7d8a2151c40545c1ccedc44d9431534`.
Local archive preparation produces exactly five regular members, no symlinks,
traversal or extra inputs: `.dockerignore`, Dockerfile, bot.py, pyproject.toml and
uv.lock. Archive SHA-256 is
`84d8c5308cdb9c6b5bdf8dfd4e1b20e4b5cc960fd6d7a8cbb912e07461c38379`,
size 237,542 bytes, context hash `a540a4ddc103df00`; synthetic secret/cache
canaries produce identical archive bytes. All six original app/config hashes
and external configuration SHA-256
`a9b4cbb6849ac284f186c63c36a6905ad10ec1f775957ed9ea5ab799bb753fe9`
remain intact. Detailed inputs, digest chains, member hashes, exact staging argv
and local evidence are recorded outside the repository in
`phase4-uv-repair-20261005/preparation-payload.json` and
`qualification-evidence.json`.

Fresh allowlisted read-only metadata confirms organisation
`disastrous-mockingbird-amethyst-180` / `c00f4504-6b4c-4389-8e5a-9be213581e1d`,
target absence across its unfiltered agent list, ready `my-pstn-agent-secrets`
in us-west with CARTESIA_API_KEY, DAILY_API_KEY, DEEPGRAM_API_KEY and OPENAI_API_KEY,
and agent-1x at 500m CPU / 1Gi memory. Proposal remains CREATE
phase3-source-conversation, arm64, min 0 / max 1, session limit 600 seconds,
whole-set reuse and zero secret writes. Raw metadata and credential values were
not persisted or emitted.

This is qualified local preparation, not Phase 4 live acceptance. The new payload
has `human_approved=false` and requires explicit new approval; prior approved
payload `086315609cd35b1d7a20394250cb78f8ebfbea5b962d914cc3e1fc9569ad2beb`
does not authorise the changed Dockerfile/archive. No image build, upload, push,
deploy, secret write, session, cleanup, upgrade or index refresh occurred.
Container dependency installation, imports, server compatibility, image readiness
and provider credential validity remain untested. Packaged Hub provenance uses
the conductor's unchanged before/after baseline (0.8.1, 45,463 records, refresh
2026-10-05T16:38:04.707267+00:00, latest / indexed 1.12.0 / zero ahead, reranker
enabled); this implementer did not claim an additional package-owned Hub call.

Local qualification: 18 renderer tests pass; targeted Ruff format/check pass.
A fresh render using the installed Hub interpreter copies all seven resources
byte for byte into exactly eight files. The conductor retains responsibility
for canonical tests and independent review before any subsequent execution.


## Phase 4 existing approved deployment verified (2026-10-05)

This current result supersedes the earlier pending-approval/blocked build checkpoints
for live deployment acceptance while preserving their dated evidence. The user's
`go ahead` receipt matches immutable payload SHA-256
`dc61327daff3e47f33cde84ca659a1b7441b30efe6852f40fab9e0559fdb03d2`.
The snapshot's historical `human_approved=false` is preserved; actual approval is
recorded separately. The single approved command previously exited 0 after
153.766 seconds. This recovery performed independent read-only verification of
that existing result and executed no further upload, build or deployment.

Fresh explicit-organisation CLI reads at `2026-10-06T04:49:54Z` (October 5 in the
host's America/Los_Angeles timezone) establish the following matching identities:

| Observation | Verified result |
|---|---|
| Organisation | `disastrous-mockingbird-amethyst-180` / `c00f4504-6b4c-4389-8e5a-9be213581e1d` |
| Region / agent | `us-west` / `phase3-source-conversation` |
| Agent ID / active version ID | `d033f622-2185-4697-961e-299c117f0ee6` / `077c7f13-06ba-4031-909c-f02e2d479732` |
| Build | `3e9ff598-7578-4262-97de-3e1c2fe099e3`, terminal `success`, completed `2026-10-05T21:30:15.733Z`, duration 93 seconds |
| Build image digest | `sha256:eb706abc9852d5be0f0e5aea06a85802e900a173547b152ce3774ad0364fe67f` |
| Active deployment | `da8a157d-bfe9-405a-b5a1-b0288ba8cf43`; deployment record references that exact build ID |
| Readiness | `ready=true`, `available=true`, `activeDeploymentReady=true`; desired, active and reconciled deployment agree; revision phase `Active` |
| Deployment manifest | `arm64`, `agent-1x`, 500m CPU / 1Gi memory; minimum 0 / maximum 1 replica; maximum session duration 600 seconds |
| Secret identity | Only `my-pstn-agent-secrets`; fresh managed/ready `us-west` metadata has exactly CARTESIA_API_KEY, DAILY_API_KEY, DEEPGRAM_API_KEY and OPENAI_API_KEY |

The identity invariant is a chain across fresh build status, selected-name agent
list, agent status and deployment history: the same selected organisation/region,
agent ID and active deployment lead to the approved build and its reported image
digest. The CLI exposes the image on build status and the build ID on deployment
metadata; it does not independently expose the deployed OCI digest on the manifest.
Architecture is exposed directly as `arm64` on that deployment manifest. The
approved immutable base/uv digest provenance remains separate historical build-input
evidence; no new registry inspection or runtime upgrade was performed.

Cloud's successful build reports the exact approved context hash
`a540a4ddc103df00` and size 237,542 bytes. Local read-only rechecks match all six
original app/config hashes, all five staged-file hashes, the exact five regular
archive members and external deploy configuration. The saved archive still has
SHA-256 `84d8c5308cdb9c6b5bdf8dfd4e1b20e4b5cc960fd6d7a8cbb912e07461c38379`.
The qualification-time guidance snapshot differs from final wording-only guidance;
that documentation change is not a changed Dockerfile, archive or approved build
input. Historical execution artifacts were hashed and left untouched.

The supported logs command used the verified deployment ID with `--limit 20` and
returned eleven entries. Allowlisted observations include server-listening and
application-startup categories, with no traceback/import-error category in this
bounded view. No raw logs or diagnostics were emitted or saved. Current status
reports zero ready replicas and zero active sessions, consistent with permitted
minimum-zero scaling; startup observations are historical matching-deployment logs,
not evidence of an active conversation. READY and startup do not prove provider
credential validity, audio behaviour or browser/session integration. No session
was authorised or started, and zero secret changes were made.

Actual `mcp__pipecat_context_hub_chatgpt_plugin.get_hub_status` calls before and
after match: Hub 0.8.1, 45,463 records, refresh
`2026-10-05T16:38:04.707267+00:00`, framework pin `latest`, indexed Pipecat 1.12.0,
zero commits ahead and enabled reranker. No refresh, repair, reset, pin change,
activation or runtime upgrade occurred. Package/readiness observations retain
previous source-grounding evidence; revised cached-skill and ChatGPT Work
activation are not established by these Cloud checks.

Sanitized read-only metadata, input assertions and Hub projections are retained
outside the checkout under `phase4-ready-resume-20261005` (`cloud.json`,
`inputs.json`, `hub.json`). No executable package changes or new unit tests were
needed for this evaluation-only update; conduct owns the subsequent canonical
checks and review. Phase 4's approved deployment/readiness acceptance is now
observed; provider sessions remain explicitly untested and Phase 5 evaluation
remains separate.

Cleanup instructions are recorded only; nothing was deleted, rolled back or
scaled by this recovery. For a later explicitly authorised cleanup, first recheck
`pipecat cloud --output json agent status phase3-source-conversation --organization
disastrous-mockingbird-amethyst-180` and confirm `us-west`, the agent ID and active
deployment above. The installed help supports the deletion shape
`pipecat cloud agent delete phase3-source-conversation --organization
disastrous-mockingbird-amethyst-180` (interactive confirmation; no region flag).
Do not run it without a separate explicit cleanup request identifying this target.
Preserve the shared `my-pstn-agent-secrets` set and other agents; this report supplies
no secret-set deletion or rollback instruction.
