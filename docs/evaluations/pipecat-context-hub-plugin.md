# Local plugin feasibility evidence

Record machine-specific command paths and raw diagnostics in a local report
outside the checkout. Do not put credentials or secret values in either report.
Package/subprocess checks and actual desktop activation are separate outcomes.
This repository report is not copied into the installed plugin package.

| Gate | Required observation | Result |
|---|---|---|
| Package | Root manifest, copied skill and resolved safe stdio argv | Pass, local setup 2026-10-03 |
| Installed Hub | Importable from unrelated cwd with pinned Python | Pass, installed Hub 0.8.0 |
| Packaged stdio | Initialize and nonempty status using generated `mcp.json` | Pass, corrected portable command and unique connection; subprocess only |
| Cwd safety | Same initialize/status with a shadow Hub module in cwd | Pass, subprocess only |
| Index preservation | Before/after refresh date and framework provenance unchanged | Pass, 2026-10-04 clean-worker before/after status preserves the user-refreshed baseline; no agent refresh |
| Desktop discovery | Plugin and packaged skill exposed in selected host/mode | Pass, Codex local catalogue and enabled plugin 2026-10-03 |
| Explore skill activation | Host reads and invokes packaged explore skill | Pass, observed TTS + STT exploration 2026-10-03 |
| Desktop packaged MCP | Host uses `pipecat-context-hub-chatgpt-plugin` initialize/status connection | Pass, actual named calls in a clean Codex worker 2026-10-04; raw desktop initialize/startup trace unobserved |
| Existing Hub retrieval | Status, API definitions, docs and examples with sources | Pass for recorded 2026-10-04 clean-worker queries and exact lookups; broad ranking and other page paths unverified |
| Execution | Harmless shell command in selected host/mode | Pass, Codex local shell |
| Pipecat CLI | Installed version and init help/options | Pass, CLI 1.3.0 version/init help |
| Cloud CLI | Installed deploy help; no mutations | Pass, deploy help only |
| Cloud account | Auth/org readiness; selected target and secret metadata | Authenticated read-only org discovery passes; one org's agent/secret reads fail; selected target/profile/key availability unresolved |
| Local build | Installed options, dry run, scaffold, imports/startup/behaviour | Pass for Phase 3 local fixture only; provider conversation and revised skill activation untested |

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
labels remain historical evidence. The later Phase 3 section adds named-choice
build qualification and an existing-target conversation; Cloud cases stay untested.

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
| 19 | “Deploy this but do not upload secrets or deploy until I approve the exact target.” | Read-only target/key metadata preparation and concrete approval payload | Future Cloud/account/approval workflow: untested |
| 20 | “I approve the reviewed Cloud target and secret changes; deploy and prove it is ready.” | Approved secret writes precede deploy; readiness/log evidence required | Future live deployment: untested |
| 21 | “Deployment failed; tell me what happened.” | Explicit failure and bounded redacted diagnostics; no false success | Future failed-deploy workflow: untested |

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
