# Repo catalog PR 2: indexed-repo status, corpus hints and catalog health

**Branch:** `feature/repo-catalog-pr2` | **Review Gates:** full | **Component:** retrieval | **Status:** Complete (phases 3a, 3b, 3c, 4, 5 and 4b implemented; merged as #147, `05aaff3`)

## Objective

Land the second of two PRs from the parent design
[`20261016-feature-repo-catalog-and-repos-command.md`](20261016-feature-repo-catalog-and-repos-command.md):
`get_hub_status` reports which repos are indexed, search tools attach a deterministic `corpus_hint` when a
query matches a catalog group whose repos are not indexed, and a weekly health check catches private,
archived or renamed catalog entries. It builds on PR 1 (catalog, loader, `repos` command), which merges first.

## Context

**Required reading before any phase, in this order:** the parent plan's *Delivery* section (runbook,
gating block, frozen contracts, file ownership), then its Decisions 3, 4, 6 and 10, its *Test plan* bullets
labelled `[PR2]`, and its *Facts resolved* list. The parent is the single source for behaviour. If this file
and the parent disagree, stop and hand back.

**Start condition.** This branch is created from PR 1's Phase 1 commit, so `shared/repo_catalog.py` and
`load_catalog()` exist. Phases 3a to 5 need only that commit. Phase 4b also needs PR 1's Phase 2d commit
(`cli_repos.py`), so it runs last. Do not start this plan before the Phase 1 commit exists. Before Phase 3a,
run `uv run mypy src/ tests/` on the Phase 1 commit; if it fails, hand back to PR 1 instead of fixing it here.

**Phase order and gating.** The order is 3a, 3b, 3c, 4, 5, 4b. Run
`/skein:conduct <this plan> --autonomous --max-phases 5` so it walks 3a to 5 and hands back.
Then rebase this branch onto `feature/repo-catalog-pr1` once PR 1's Phase 2d commit exists, or onto `main`
after PR 1 merges. Log the rebase in the parent's Handoff log. Then run
`/skein:conduct <this plan> --resume --run-ci-parity --ci-cmd '<AGENTS.md gate>'` for Phase 4b (conduct does not
persist the parity flags across `--resume`), and the gauntlet auto-chains after it.

Facts a phase worker needs and cannot cheaply rediscover:

- `annotate_response(result_json, index_store)` (`shared/staleness.py`) has a pinned signature
  (`test_no_parsed_shortcut_parameter`); do not add a parameter to it. The hint is a separate function.
- `cli_query._maybe_warn_poor_results` runs before `annotate_response`; the CLI computes the hint first,
  suppresses the stderr hint when one exists, and attaches after `annotate_response`.
- `get_counts_by_repo` exists on `IndexStore` and the FTS index but not on the `ServingIndex` protocol
  (`shared/interfaces.py`) or `ReopeningIndexStore` (`services/index/reopening.py`); it keys by the
  configured slug verbatim and injects a synthetic `docs.pipecat.ai` key. `refresh` in `cli.py` already
  calls it; leave those callers alone.
- Existing test fakes differ. `MagicMock()` stores (`tests/unit/test_server.py`, `tests/unit/test_cli_query.py`)
  auto-create `get_counts_by_repo` and return a `MagicMock`, not a mapping; the `Any`-typed `_Stub` in
  `test_server.py` may lack the method or let it raise. So `indexed_repos` is the only caller on the hint
  and status paths, and it treats a missing method, a raise or a non-`Mapping` result as unreadable (`None`).
- `config.sources.effective_repos` already strips tainted slugs; `config.sources.tainted_repos` is a
  property. `_QueryRuntime` and `_query_runtime` live in `cli_query.py`.
- `shared/` has no module-level runtime import of `services/` (`shared/reranker.py` imports it under
  `TYPE_CHECKING` and function-locally inside `probe_reranker`). No test enforces this. For `corpus_hint.py`
  it is a choice made here: `TYPE_CHECKING` imports only.
- PR 1 Phase 2d rewrites the `tests/unit/test_cli_query.py` line-16 import. Phases 3c and 4 extend the
  same module. On rebase keep both sides.
- Tests import package modules only, except scripts: `scripts/` is not a package, so a script's tests load
  it by path (`importlib.util.spec_from_file_location`, as `tests/unit/test_probe_mcp_2x.py` does).
- The Windows CI job lists test files by name. A new test module that touches no index store joins the list
  in the commit that creates it. Index-store and Chroma tests stay off it: `ci.yml`'s Windows step and
  `docs/README.md` say the persisted index is POSIX-only and fails closed on Windows. So tests this plan
  adds to Windows-listed modules (`test_corpus_hint.py`, `test_hub_status.py`, `test_cli.py`) use fakes or
  `FTSIndex`, never `IndexStore`. Existing tests in those modules are not audited here.
- Never `git add -A`; stage explicit paths.

**Frozen real queries (2026-10-17).** One CLI loop, `search-examples` and `search-docs` with `--limit 3`,
against the maintainer's live index. Copy these strings verbatim into the tests.

| Query | `search_examples` | `search_docs` |
|---|---|---|
| `AWS Bedrock Nova Sonic voice agent` | 0.95, top hit `aaronsu11/Dum-E` | 0.95 |
| `Tavus avatar` | low, 0.100 | 0.95 |
| `Murf TTS` | low, 0.1015 | 0.95 |
| `Supermemory memory` | 0.815, top hit `supermemoryai/pipecat-memory` | low, 0.1008 |

Confidence is not an input to the hint. Those results come from a live index no test can reproduce, so a
test freezes the strings and asks what the fixture index and fixture catalog say. `Supermemory memory` looks
already indexed through examples. Its expected outcome (hint or none) comes from the fixture index, not an
assumption. Read the catalog data from PR 1's Phase 1 before asserting which groups fire for any query. The
expected-outcome table in Phase 4 is provisional until that read (a Phase 4 step fixes it).

## Requirements

1. `indexed_repos`, `configured_no_records` and `catalog_groups_not_indexed` on `get_hub_status`
   (parent Decision 10). `indexed_repos(index_store)` returns `None` on an unreadable count.
   `groups_not_indexed(catalog, None)` returns `[]` and `corpus_hint` returns `None`.
2. `corpus_hint` / `attach_corpus_hint` on `search_docs`, `search_examples`, `search_api`,
   `get_code_snippet`, on both front doors with identical JSON (parent Decisions 3 and 4).
3. The health check and curation scripts and the weekly workflow (parent Decision 6).
4. A seam test against PR 1's real `repos` command (Phase 4b).
5. Every parent *Test plan* bullet labelled `[PR2]` has tests. Every mutation check labelled `[PR2]` is run
   in the phase named below (revert it, confirm its test fails). Record each result in the PR description.

## Review Focus

- The two-door parity: the same `corpus_hint` from `server/main.py` and `cli_query.py`, and the CLI
  suppression order.
- The tainted skip: a tainted catalog repo appears in neither `repos_not_indexed` nor `add_command`.
- The frozen `add_command` format (parent contract C1, with `--config-only`) must be a command PR 1's
  `repos add` accepts for exactly those slugs, including under a shadowing `.env`.
- An unreadable count never produces a hint (`None`, not an empty set).

## Implementation Checklist

### Phase 3a: `get_counts_by_repo` on the serving index

**Goal:** serve and the one-shot CLI can read per-repo record counts through the protocol they already hold.
**Impl files:** `src/pipecat_context_hub/shared/interfaces.py`, `src/pipecat_context_hub/services/index/reopening.py`
**Test files:** `tests/unit/test_index_lock.py`, `tests/unit/test_cli.py`
**Test command:** `uv run pytest tests/unit/test_index_lock.py tests/unit/test_cli.py -v && uv run mypy src/ tests/`

- Add `get_counts_by_repo` to the `ServingIndex` protocol and a `ReopeningIndexStore` delegate inside
  `_using`. The protocol change runs under strict mypy, hence the mypy step in the command.
- In `TestReopeningIndexStore` (`test_index_lock.py`) add the reopen case: counts read after a reopen come
  from the new store, so a stale `serve` hints correctly after a `refresh`.
- Add the delegate case to `test_cli.py`, where the serve handle is already asserted to be a
  `ReopeningIndexStore`.

### Phase 3b: `indexed_repos` and `groups_not_indexed`

**Goal:** one predicate decides "this group is not indexed", used by both the hint and `get_hub_status`.
**Impl files:** `src/pipecat_context_hub/shared/corpus_hint.py`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_corpus_hint.py`
**Test command:** `uv run pytest tests/unit/test_corpus_hint.py -v`

- `indexed_repos(index_store)` is casefolded and drops the synthetic `docs.pipecat.ai` key. It returns
  `None` when the count is unreadable: `AttributeError` (a fake without the method),
  `IndexUnavailableError`, any raise from `get_counts_by_repo`, or a result that is not a `Mapping` (a
  `MagicMock` store returns a `MagicMock`). It never returns an empty set for that case. On the `None` path
  it logs at warning level, like `annotate_response`; `get_hub_status` collapses `None` to empty lists, so
  the log is the only signal that a count was unreadable.
- `groups_not_indexed(catalog, indexed)` follows parent Decision 10. With `indexed is None` it returns `[]`.
- The configured-no-records comparison (configured minus indexed, casefolded on both sides) lives in this
  module too, used by status and hint.
- `test_corpus_hint.py` joins the Windows file list in `ci.yml` in this commit. Its fixture index is a fake
  exposing `get_counts_by_repo` only (no `IndexStore`), so the module stays Windows-eligible.
- Named tests: `docs.pipecat.ai` absent from `indexed_repos`; `groups_not_indexed` for a fully indexed, a
  partly indexed and a `parseable = false`-only group; a bare `MagicMock()` store gives `None`; a case-variant
  pair (configured `Owner/Repo`, index key `owner/repo`) lands in indexed, not in configured-no-records.
- Mutation checks:
  - Drop the casefold in `indexed_repos`: the case-variant slug test must fail.
  - Drop the casefold on the configured side of the configured-no-records comparison: the same
    case-variant pair test must fail.
  - Replace the `None` result with an empty set: the unreadable-count test must fail, because every
    catalog group would fire a false hint.

### Phase 3c: config values and `get_hub_status`

**Goal:** `get_hub_status` reports indexed, configured-no-records and uncovered catalog groups on both doors.
**Impl files:** `src/pipecat_context_hub/shared/types.py`, `src/pipecat_context_hub/server/tools/get_hub_status.py`, `src/pipecat_context_hub/server/main.py`, `src/pipecat_context_hub/cli_query.py`, `src/pipecat_context_hub/cli.py`
**Test files:** `tests/unit/test_hub_status.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_server.py`, `tests/unit/test_cli.py`
**Test command:** `uv run pytest tests/unit/test_hub_status.py tests/unit/test_cli_query.py tests/unit/test_server.py tests/unit/test_cli.py -v`

- `HubStatusOutput` (in `shared/types.py`) gains three `list[str]` fields, default empty: `indexed_repos`,
  `configured_no_records` and `catalog_groups_not_indexed`.
- Thread `configured_repos` and `tainted_repos` through `serve`, `create_server`, `handle_get_hub_status`,
  `_QueryRuntime` and `_query_runtime`, in this one commit, with a both-front-doors parity test. They are two
  `frozenset[str]` values captured once at process start from `config.sources`, not callables (so no
  `*_provider` name, unlike `reranker_status_provider`). On every changed signature they are keyword-only
  and default to empty, so the existing `handle_get_hub_status` and `create_server` callers and test fakes
  keep working unedited (`cli_query._dispatch` passes `handle_get_hub_status` arguments positionally).
- `test_cli.py` gains the serve wiring test: `serve` passes `effective_repos` and `tainted_repos` from
  `config.sources` into `create_server`.
- A `get_counts_by_repo` that raises (or is missing on a fake) leaves `get_hub_status` returning with
  the three fields empty and no exception. Test it on both doors; mirror
  `test_store_errors_never_break_the_response`. Tests added to `test_hub_status.py` and `test_cli.py` use
  fakes or `FTSIndex` only (Windows-listed modules).
- A status test with one indexed, one configured-no-records and one unconfigured catalog repo asserts all
  three fields on both doors. A case-variant test (configured `Owner/Repo`, index key `owner/repo`) asserts
  the slug is in `indexed_repos` and absent from `configured_no_records`.
- The catalog-load-failure status half lives here: patch PR 1's `_read_catalog_text` (in
  `shared/repo_catalog.py`) to return a broken catalog, so `load_catalog()` returns its cached `None`;
  call `load_catalog.cache_clear()` before and after each case. Then `catalog_groups_not_indexed == []`
  on both doors.

### Phase 4: corpus hints

**Goal:** a vendor question whose repos are not indexed carries one compact, deterministic hint, with no network and no embeddings.
**Impl files:** `src/pipecat_context_hub/shared/corpus_hint.py`, `src/pipecat_context_hub/server/main.py`, `src/pipecat_context_hub/cli_query.py`, `.github/ISSUE_TEMPLATE/retrieval-quality.yml`, `docs/smoke-test.md`
**Test files:** `tests/unit/test_corpus_hint.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_server.py`, `tests/unit/test_staleness.py`
**Test command:** `uv run pytest tests/unit/test_corpus_hint.py tests/unit/test_cli_query.py tests/unit/test_server.py tests/unit/test_staleness.py -v`

- `corpus_hint(index_store, *, tool, args, configured_repos, tainted_repos)` (the two repo sets are the
  `frozenset[str]` values from Phase 3c) and `attach_corpus_hint(result_json, hint)` and both front-door call
  sites in one commit; CLI order compute, `_maybe_warn_poor_results` (suppressed), `annotate_response`, attach.
- MCP door: `_annotate(result_json)` in `server/main.py` takes only the result today. Replace it with one
  thread function `(name, args, result_json)` that computes the hint, calls `annotate_response`, then
  attaches, all inside the existing single `asyncio.to_thread` hop. `check_deprecation` and
  `get_hub_status` route through it unchanged (no hint for non-search tools).
- The per-tool text table in `corpus_hint.py` is a third list of tool names. A drift-guard test in
  `test_corpus_hint.py`: the table keys are a subset of the registered tool names and are exactly the four
  Requirement 2 tools (`search_docs`, `search_examples`, `search_api`, `get_code_snippet`). Pin the set;
  do not derive it from input-model fields (`CheckDeprecationInput` also has `symbol`).
- `test_staleness.py` runs here: it pins the `annotate_response` signature
  (`test_no_parsed_shortcut_parameter`) and holds `TestBothFrontDoors`, which the hint mirrors.
- Output shape, `add_command` format (parent contract C1: `pipecat-context-hub repos add --config-only
  --group <group>` per matched group, joined with `; `), `refresh_command`, and absent-not-null behaviour
  per parent Decision 4.
- The parent Hints bullet in full, by file:
  - `test_corpus_hint.py`: text matching (alias negatives such as "laws" vs "aws", the verbatim concept
    queries `TTS + nvidia` and `TTS & nvidia`, uppercase alias), `get_code_snippet` from `symbol`/`intent`
    and not `path` alone, a partial group listing only missing repos, catalog-ordered `matched_groups`, the
    `parseable = false` and tainted skips, `repos_configured_no_records` (the hint field; `configured_no_records`
    is the status field), and 0.95 confidence still hinting. Unreadable counts: a store whose
    `get_counts_by_repo` raises, a fake lacking the method and a bare `MagicMock()` store each give
    `corpus_hint(...) is None`. Case variant: configured `Owner/Repo` with index key `owner/repo` gives no
    `repos_configured_no_records` entry and no `refresh_command`.
  - `test_server.py` and `test_cli_query.py`: golden `corpus_hint` per door and identical JSON (the golden
    object includes `report_issue_url == RETRIEVAL_QUALITY_ISSUE_URL`), non-search tools never hint,
    `configured_repos` reflects process start, coexistence with the staleness footer, and the stderr hint
    suppressed when a hint exists. With an unreadable count (a store that raises, a fake lacking the method,
    a `MagicMock()` store) a vendor query has no `corpus_hint` key on both doors and the CLI stderr hint
    still prints; existing `MagicMock`-store golden tests stay unchanged.
  - `test_server.py` (the existing `_SERVER_INSTRUCTIONS` pin tests near lines 613-644, and the
    issue-template check near line 864): `_SERVER_INSTRUCTIONS` pins `corpus_hint`, `--unlisted` and
    the draft-not-file wording; `retrieval-quality.yml` carries the optional *Candidate repos* field.
- The catalog-load-failure half lives here: with `load_catalog` returning `None` (seam: patch
  `_read_catalog_text` to return a broken catalog, with `load_catalog.cache_clear()` before and after each case) there is no `corpus_hint` key on either door, and the
  CLI stderr hint is still printed.
- Frozen queries: the four strings in Context, copied verbatim into `test_corpus_hint.py`. For each, assert
  against a fake fixture index (exposing `get_counts_by_repo` only, so the module stays Windows-eligible)
  and a fixture catalog: hint when the matched group's repos are absent, no hint when present.
- Read the PR 1 Phase 1 catalog (`data/repo_catalog.toml`) before writing those assertions. For each frozen
  query record the matched group, its aliases and its parseable members; then fix the table below and the
  smoke-test expectations from that data, and record any changed verdict in the PR description.
- A new numbered item in `docs/smoke-test.md` reuses the four queries. PR 1 Phase 2d also adds an item
  there, so put this one in the same list as PR 1's items and number it after that list's highest item on
  rebase (docs/smoke-test.md has two numbered lists: the main list and "CLI query smoke"; PR 1's items are 53
  and 54 in the main list, so expect 55).
- Expected outcomes for the four frozen queries (provenance: the maintainer's live index, 2026-10-17).
  **Provisional** until the catalog read above; the verdicts hold only under the stated catalog condition:

  | Query | Expected | Why (live-index provenance) |
  |---|---|---|
  | `AWS Bedrock Nova Sonic voice agent` | hint | The AWS group is only partly indexed: `aws-samples/build-intelligent-ai-voice-agents-with-pipecat-and-amazon-bedrock` (435 records), `pipecat-ai/aws-deepgram-workshop` (390) and `pipecat-ai/aws-workshops` (33) are indexed; four other AWS seed repos are not. The 0.95 top hit `aaronsu11/Dum-E` is not an AWS repo, so confidence must not suppress the hint. |
  | `Tavus avatar` | hint | `aws-samples/sample-voice-ai-tavus-avatar-demo` has zero records and is not configured. The hint fires only if a catalog group with a `tavus` alias contains it (one group per vendor); if it sits only in the AWS group, `Tavus avatar` matches no alias and there is no hint. |
  | `Murf TTS` | no hint | `murf-ai/pipecat-murf-tts` is indexed (24 records). Holds only if every parseable repo in the Murf group is indexed. |
  | `Supermemory memory` | no hint | `supermemoryai/pipecat-memory` is indexed (462 records). Holds only if every parseable repo in the Supermemory group is indexed. |

  These expectations assume one catalog group per vendor (PR 1 Phase 1 requires it), which does not bound
  repos per group (hence the "every parseable repo" conditions). The unit tests derive each
  outcome from a fixture index and fixture catalog, never from the live index; the live numbers are recorded
  only as provenance.
- Mutation checks (revert, confirm the named test fails):
  - Alias word boundary: the "laws" negative.
  - Skip `parseable = false`: the `parseable = false` skip test.
  - Skip tainted: the tainted-in-neither-field test, on both doors.
  - CLI stderr suppression: the one-hint-not-two test.
  - `add_command` format: the golden-object test, including `--config-only` and the `; ` join.
  - Catalog-load-failure fallback: the no-`corpus_hint`-key test.
  - Unreadable count: replace `None` with an empty set in `corpus_hint`'s call to `indexed_repos`; the
    unreadable-count tests (raising store, missing method, `MagicMock` store) must fail.

### Phase 5: catalog health and curation scripts

**Goal:** private, archived and renamed catalog entries are caught weekly, without network in PR CI.
**Impl files:** `scripts/check_repo_catalog.py`, `scripts/curate_repo_catalog.py`, `.github/workflows/catalog-health.yml`, `.github/workflows/ci.yml`, `justfile`
**Test files:** `tests/unit/test_check_repo_catalog.py`
**Test command:** `uv run pytest tests/unit/test_check_repo_catalog.py -v`

- Parent Decision 6 in full: `httpx` with `follow_redirects=False`, the classifier taking an injected
  response, the inconclusive outcome, the aggregate verdict and exit code, `GITHUB_TOKEN` header
  behaviour, and a test that the workflow invokes the script (the `test_audit_sync.py` pattern). This list
  is the checklist; each is a named test:
  - 90-day advisory boundary at 90 days (ok) and 91 days (advisory).
  - Exit codes over one mixed injected result set: ok with an old `verified` date exits 0; stale exits
    nonzero; inconclusive-only exits nonzero in its own report section.
  - Inconclusive, never labelled inaccessible, for each of 403, 429, 5xx, timeout and network error.
  - A rename reports the new name; a case-only `full_name` difference is not a rename.
  - `Authorization` header sent when `GITHUB_TOKEN` is set and omitted when not; a 301 routed through
    `httpx.MockTransport` is not followed.
- Add `just catalog-health` and `just catalog-curate` recipes (the local run matches the workflow
  invocation); the workflow-invokes-script test also pins the recipe names.
- Decided now: the tests load both scripts by path with `importlib.util.spec_from_file_location`
  (the `test_probe_mcp_2x.py` pattern). No package module, so `httpx` GitHub code stays out of the wheel.
- Frozen stale fixtures from 2026-10-16, all owner `pipecat-ai`:
  - Real private. Freeze both observed shapes as fixtures: a 404 (what the job's `GITHUB_TOKEN` sees, and
    what an unauthenticated call sees, indistinguishable from missing) and a 200 with `private: true`
    (what a maintainer's local token sees; the job never sees it). The classifier must report stale on
    either: `nvidia-sgmaker`,
    `pipecat-websocket-transport`, `hosted-smart-turn`, `pipecat-krisp`, `instant-voice-demo`,
    `openai-realtime-todo-example`, `pipecat.ai-agents`, `gradient-bang-subagents`,
    `gradient-bang-synth-forge`.
  - Real archived: `pipecat-cli`, `pipecat-client-cxx-daily`.
  - Synthetic, no real example was recorded: the 301-renamed, the deleted 404, the `full_name` rename
    beyond case, and the case-only difference. Label them synthetic in the test. The classifier reads
    the new name from the 301 `Location` header (fallback: the body `url` field), and the synthetic 301
    fixture is built that way. It is unverified against a live rename until the first weekly run.
- `curate_repo_catalog.py` takes an injectable `httpx` client or transport. A real run needs a GitHub token
  (code search rejects unauthenticated requests): read `GITHUB_TOKEN`, else `gh auth token`, and exit
  nonzero with a message when neither exists. Its test runs it with an
  `httpx.MockTransport` returning canned search results (no network), and asserts the catalog file is
  byte-identical and never opened for writing.
- `test_check_repo_catalog.py` joins the Windows file list in `ci.yml` in this commit. It reads files by
  repo-relative path and asserts no permission bits.
- Mutation checks: none are assigned to this phase.

### Phase 4b: seam test against the real `repos` command

**Prerequisite:** HEAD must contain PR 1's Phase 2d commit. Rebase onto `feature/repo-catalog-pr1` once 2d
exists, or onto `main` after PR 1 merges. If `src/pipecat_context_hub/cli_repos.py` is absent, hand back.

**Goal:** what `repos add` writes is what the hint reads, proven offline.
**Impl files:** `tests/unit/test_repos_hint_seam.py`
**Test files:** `tests/unit/test_repos_hint_seam.py`
**Test command:** `uv run pytest tests/unit/test_repos_hint_seam.py -v`

- Fixture index: a real on-disk `IndexStore` (`create=True`, no model needed), filled with records built
  like `_make_record(repo=<slug>)` in `tests/unit/test_index_store.py`. Copy that pattern; do not import
  the private helper. Use a fixture catalog by patching `_read_catalog_text` (PR 1's seam), with `load_catalog.cache_clear()`
  before and after each case.
- With a temp `config.toml`-only layout (no env var, no `.env`): `repos add <catalog slug>` then a new
  `_query_runtime` puts the slug in `configured_repos`. A tainted slug yields neither field.
- Group-level expectations. In a two-repo group with one repo added, `repos_not_indexed` lists the other
  and `add_command` still names the group. In a single-repo group with no records for the added slug,
  the hint has `repos_configured_no_records` and `refresh_command` and no `add_command`. With records, no hint.
- The test runs the exact `add_command` string against the temp `config.toml`-only layout. It splits on
  `; `, strips the leading `pipecat-context-hub`, and invokes each part on the real `repos` command, exit 0.
  The test states this layer in a comment. Afterwards it asserts the written `config.toml` slug set equals
  the `repos_not_indexed` slugs of the matched groups (read back through `load_global_config`), and that a
  recomputed hint has no `add_command`. Each case uses a fresh temp layout, so an earlier `repos add` case
  cannot leak slugs into this equality.
- A second case sets `PIPECAT_HUB_EXTRA_REPOS` in a cwd `.env`. The `--config-only` form exits 0; plain
  `repos add` exits 1 (parent Decision 7). That proves `--config-only` is accepted under a shadowing layer. After it, the slugs are in `config.toml`
  but not in `effective_repos`.
- `test_repos_hint_seam.py` does **not** join the Windows file list: it opens a real `IndexStore`, which
  fails closed on Windows (the POSIX-only platform contract in `ci.yml` and `docs/README.md`). Guard the
  module with `pytest.mark.skipif(os.name != "posix")` so a local Windows run skips rather than fails. This
  phase leaves `ci.yml` unchanged (the parent's ownership table agrees).
- Mutation check: change the C1 `add_command` to drop `--config-only`; the shadowing-`.env` case must fail.

**Closing steps (not a phase; before opening the PR).** Run the offline smoke
(`uv run pytest tests/smoke/ -v`). Install the branch with `uv tool install --force .` and reconnect
(`/mcp`). Run the new numbered `docs/smoke-test.md` item for one unindexed and one indexed vendor query.
Put the result and the mutation-check results in the PR description. Restore the released tool afterwards.

<!-- reviewed: 2026-10-18 @ dc6c339d9f3d78e5d2367a9725579d3628b1cc2e -->

## Progress

- [x] Phase 3a: `get_counts_by_repo` on the serving index
- [x] Phase 3b: `indexed_repos` and `groups_not_indexed`
- [x] Phase 3c: config values and `get_hub_status`
- [x] Phase 4: corpus hints
- [x] Phase 5: catalog health and curation scripts
- [x] Phase 4b: seam test against the real `repos` command

## Findings

### Review Waivers

_(none yet; this plan has not been through `/review-plan`)_

### Conduct reviewer notes (advisory)

- **Phase 3a:** `ServingIndex.get_counts_by_repo` docstring does not state the key shape (configured slug as-is, not casefolded, plus synthetic `docs.pipecat.ai`). Minor.
- **Phase 3b:** `groups_not_indexed` / `missing_repos` take no tainted set. Phase 4's hint must exclude tainted repos (parent Decision 3) while `catalog_groups_not_indexed` is meant to share the one predicate (Decision 10). Resolve in Phase 4: add a keyword-only casefolded `tainted` argument to the shared predicate rather than forking it, or record that status ignores taint. Important.
- **Phase 3b:** `indexed_repos` logs `IndexUnavailableError` at WARNING; `annotate_response` logs it at DEBUG (expected refresh race). Match it when Phase 4 wires the hint in. Minor.
- **Phase 3b:** `indexed_repos` is typed against a private narrow `_CountsSource` protocol, not `ServingIndex` as Decision 10 says. Structurally compatible; chosen so test fakes need no cast. Minor.
- **Phase 3c:** `catalog_groups_not_indexed` passes the catalog to `groups_not_indexed` without a tainted set, so a group whose only unindexed repo is tainted stays listed forever while the Phase 4 hint skips it (same root as the 3b Important note). Fix the predicate once, in Phase 4. Minor.
- **Phase 3c:** `handle_get_hub_status` makes one more synchronous `get_counts_by_repo` call on the MCP event loop; `server/main.py`'s `_annotate` moves its reads off the loop with `asyncio.to_thread`. Follows the handler's existing pattern; revisit if latency shows. Minor.
- **Phase 4 (fixed in-phase):** reviewer found Bandit B101 on a new `assert index_store is not None` in `server/main.py::_finish`; `_finish` now takes the already-checked store as a parameter. `bandit -r src` is clean. The gauntlet should still run the PR's Bandit step.
- **Phase 4 (resolved, deliberate deviations):** the shared predicate (`missing_repos` / `groups_not_indexed`) now takes a keyword-only casefolded `tainted` set, and `get_hub_status.py` passes `tainted_repos` to it (a Phase 3c file edited in Phase 4, resolving the 3b/3c tainted notes). `indexed_repos` logs `IndexUnavailableError` at DEBUG, matching `annotate_response` (an expected refresh race), WARNING for other failures; this departs from the 3b text "every None path logs at warning".
- **Phase 4 (open, Minor):**
  - `TestFrozenRealQueries` pins the shipped `data/repo_catalog.toml`, not a fixture catalog; a catalog edit (alias rename, repo added to murf/tavus) breaks them. Freeze a fixture or state that it is intended.
  - The "`configured_repos` reflects process start" tests check start-time env flows through; none changes config after `create_server` to show a running hint is unchanged.
  - The tainted-only `catalog_groups_not_indexed` check is covered on the MCP door only; the CLI door shares the predicate but has no test.
  - Broad catalog aliases (`google`, `gemini`, `turn detection`, `ios`, `android`) make core-topic questions hint while any repo in the group is unindexed (known Decision 3 cost).
- **Phase 4 mutation checks (run by the conductor):** alias boundary, `parseable = false` skip, tainted skip, CLI stderr suppression, `--config-only`, the `; ` join and unreadable-count `None` were each killed by the named test. Two mutants survived as equivalent: dropping `- tainted` from `configured` (redundant: `missing_repos` already drops tainted repos) and replacing the catalog-`None` `return None` with `pass` (the blanket `except` returns `None` anyway).
- **Phase 5 (resolved by the follow-up below; was open, Important):** the reviewer recalls (unverified against a live rename) that GitHub answers a renamed or transferred repo with a 301 whose `Location` and body `url` are `https://api.github.com/repositories/{id}`, not `/repos/owner/new-name`. If so, `_name_from_api_url` returns `None`, so the weekly run reports `renamed` (and exits 1) without the new name, which Decision 6 says to report. The synthetic 301 fixture uses the `/repos/owner/new` shape, so no test can catch this; the plan has the same assumption. Fix if confirmed: on an `/repositories/{id}` target, make one more non-following GET and read `full_name` from the 200 body. Verify on the first weekly run or against a real renamed repo before relying on the new-name field.
- **Phase 5 (Minor; all resolved except the permission-bit item):**
  - (resolved; review-gauntlet round 5 widened it to every field read) A 200 whose body is not a JSON object or lacks `full_name` is classified `ok` (a captive-portal HTML 200 gives a false all-clear); `test_non_json_200_does_not_crash` pins that. Prefer `inconclusive`.
  - (resolved: exit 3 and a stdout line) `check_repo_catalog.main` returns 1 (stale) for a catalog-load failure, and argparse errors exit 2 (inconclusive); the load message goes to stderr only, so the workflow's empty-report guard skips the tracking issue. Use a distinct exit code and a stdout line.
  - (resolved; round 5 moved every response read behind one validator) `curate_repo_catalog.discover_vendors` does not guard a malformed search response (`resp.json()` ValueError, non-dict body, `repository: null`); `repo_stats` handles most of it. It also returns `EXIT_NO_TOKEN` for catalog-load failures, and ignores the git-tree `truncated: true` flag.
  - (open) `test_scripts_have_no_permission_bit_dependence` only asserts `is_file()`.
  - (resolved: the check and issue jobs are split; only the issue job holds `issues: write`) The workflow grants `issues: write` job-wide (matches `smoke-drift.yml`). In-phase: actions are SHA-pinned (security-review finding), each token is scoped to its step, and checkout uses `persist-credentials: false`.
- **Phase 5 (note):** the packaged catalog is `src/pipecat_context_hub/data/repo_catalog.toml`, not `data/repo_catalog.toml` as phase 4's text says. Phase 4's frozen-query tests read the shipped catalog; both phases are consistent with the real path. Phase 5 ran test-writer after implementer (sequential, interface coupling: the scripts are new and the plan names behaviours, not function names).
- **Phase 5 follow-up (resolved, coordinator-directed):** the rename shape was confirmed live (`curl -I https://api.github.com/repos/joyent/node` gives 301 with `location: .../repositories/211666`). `check_repo_catalog` now makes one more non-following GET on an `/repositories/{id}` target (API host only) and reads `full_name`; verified end to end against the real redirect (`joyent/node` -> `nodejs/node-v0.x-archive`), and disabling the second hop fails 2 tests. Also fixed: a 200 without a JSON-object body or string `full_name` is `inconclusive`; catalog-load failure has its own exit code (3) and a stdout line in both scripts; `discover_vendors` and `repo_stats` record malformed responses; a truncated git tree is marked. `TestFrozenRealQueries` is documented as deliberately pinning the shipped catalog. The Phase 5 Minor notes above for these items are closed; the rest stay open. Review-gauntlet round 6: the new name is live-verified against `joyent/node` only (re-run with `check_slug` on 2026-10-17), not yet by a weekly run on a catalog entry; caveat documented in docs/configuration.md.
- **Phase 4b (mutation check, run by the conductor):** dropping `--config-only` from `_ADD_COMMAND` fails 4 of 9 seam tests, including the shadowing-`.env` case; source restored.
- **Phase 4b (Minor; first item resolved in 0277aa3, second still open):**
  - (resolved in 0277aa3) `test_tainted_slug_yields_neither_field` asserts `MIX_TAINT not in hint["add_command"]`, which always passes because `add_command` names only the group; the test never runs that command for a group with a tainted member. Suggested: run it, assert exit 0 and that `layout.written() == {MIX_OK}`.
  - The only tainted case is a slug `repos add` refuses, so absence from `configured_repos` is trivially true. The realistic path, a slug already in `config.toml` that is tainted later, is untested. Suggested: write it into the temp `config.toml`, set `PIPECAT_HUB_TAINTED_REPOS`, and assert it is absent from `configured_repos`, `repos_not_indexed` and `repos_configured_no_records`.
