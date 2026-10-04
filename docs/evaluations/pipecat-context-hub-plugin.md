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
| Cloud account | Auth/org readiness (later phase) | Untested |

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
