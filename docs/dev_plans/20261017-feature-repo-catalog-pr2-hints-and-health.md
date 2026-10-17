# Repo catalog PR 2: indexed-repo status, corpus hints and catalog health

**Branch:** `feature/repo-catalog-pr2` | **Review Gates:** full | **Component:** retrieval | **Status:** Planned (derived from the reviewed parent plan; needs its own `/review-plan` sign-off before `/conduct`)

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
(`cli_repos.py`), so it runs last. Do not start this plan before the Phase 1 commit exists.

**Phase order and gating.** The order is 3a, 3b, 3c, 4, 5, 4b. Run
`/skein:conduct <this plan> --autonomous --max-phases 5` so it walks 3a to 5 and hands back.
Then rebase this branch onto `feature/repo-catalog-pr1` once PR 1's Phase 2d commit exists, or onto `main`
after PR 1 merges. Log the rebase in the parent's Handoff log. Then run `/skein:conduct <this plan> --resume`
for Phase 4b, and the gauntlet auto-chains after it.

Facts a phase worker needs and cannot cheaply rediscover:

- `annotate_response(result_json, index_store)` (`shared/staleness.py`) has a pinned signature
  (`test_no_parsed_shortcut_parameter`); do not add a parameter to it. The hint is a separate function.
- `cli_query._maybe_warn_poor_results` runs before `annotate_response`; the CLI computes the hint first,
  suppresses the stderr hint when one exists, and attaches after `annotate_response`.
- `get_counts_by_repo` exists on `IndexStore` and the FTS index but not on the `ServingIndex` protocol
  (`shared/interfaces.py`) or `ReopeningIndexStore` (`services/index/reopening.py`); it keys by the
  configured slug verbatim and injects a synthetic `docs.pipecat.ai` key. `refresh` in `cli.py` already
  calls it; leave those callers alone.
- Existing test fakes (`MagicMock`, the `Any`-typed `_Stub` in `tests/unit/test_server.py`) may lack
  `get_counts_by_repo` or let it raise. So `indexed_repos` is the only caller on the hint and status paths.
- `config.sources.effective_repos` already strips tainted slugs; `config.sources.tainted_repos` is a
  property. `_QueryRuntime` and `_query_runtime` live in `cli_query.py`.
- `shared/` imports nothing from `services/` at runtime (`shared/reranker.py` imports under
  `TYPE_CHECKING` only). No test enforces this; `corpus_hint.py` may use `TYPE_CHECKING` imports only.
- PR 1 Phase 2d rewrites the `tests/unit/test_cli_query.py` line-16 import. Phases 3c and 4 extend the
  same module. On rebase keep both sides.
- Tests import package modules only, except scripts: `scripts/` is not a package, so a script's tests load
  it by path (`importlib.util.spec_from_file_location`, as `tests/unit/test_probe_mcp_2x.py` does).
- The Windows CI job lists test files by name. A new test module joins the list in the commit that creates it.
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
assumption. Read the catalog data from PR 1's Phase 1 before asserting which groups fire for any query.

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
  `IndexUnavailableError`, or any raise from `get_counts_by_repo`. It never returns an empty set for that case.
- `groups_not_indexed(catalog, indexed)` follows parent Decision 10. With `indexed is None` it returns `[]`.
- `test_corpus_hint.py` joins the Windows file list in `ci.yml` in this commit.
- Mutation checks:
  - Drop the casefold in `indexed_repos`: the case-variant slug test must fail.
  - Replace the `None` result with an empty set: the unreadable-count test must fail, because every
    catalog group would fire a false hint.

### Phase 3c: config providers and `get_hub_status`

**Goal:** `get_hub_status` reports indexed, configured-no-records and uncovered catalog groups on both doors.
**Impl files:** `src/pipecat_context_hub/shared/types.py`, `src/pipecat_context_hub/server/tools/get_hub_status.py`, `src/pipecat_context_hub/server/main.py`, `src/pipecat_context_hub/cli_query.py`, `src/pipecat_context_hub/cli.py`
**Test files:** `tests/unit/test_hub_status.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_server.py`, `tests/unit/test_cli.py`
**Test command:** `uv run pytest tests/unit/test_hub_status.py tests/unit/test_cli_query.py tests/unit/test_server.py tests/unit/test_cli.py -v`

- `HubStatusOutput` (in `shared/types.py`) gains three `list[str]` fields, default empty: `indexed_repos`,
  `configured_no_records` and `catalog_groups_not_indexed`.
- Thread `configured_repos` and `tainted_repos` providers (read once at process start from
  `config.sources`) through `serve`, `create_server`, `handle_get_hub_status`, `_QueryRuntime` and
  `_query_runtime`, in this one commit, with a both-front-doors parity test.
- `test_cli.py` gains the serve wiring test: `serve` passes `effective_repos` and `tainted_repos` from
  `config.sources` into `create_server`.
- A `get_counts_by_repo` that raises (or is missing on a fake) leaves `get_hub_status` returning with
  the three fields empty and no exception. Test it on both doors; mirror
  `test_store_errors_never_break_the_response`.
- The catalog-load-failure status half lives here: with `load_catalog` returning `None`,
  `catalog_groups_not_indexed == []` on both doors.

### Phase 4: corpus hints

**Goal:** a vendor question whose repos are not indexed carries one compact, deterministic hint, with no network and no embeddings.
**Impl files:** `src/pipecat_context_hub/shared/corpus_hint.py`, `src/pipecat_context_hub/server/main.py`, `src/pipecat_context_hub/cli_query.py`, `.github/ISSUE_TEMPLATE/retrieval-quality.yml`, `docs/smoke-test.md`
**Test files:** `tests/unit/test_corpus_hint.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_server.py`, `tests/unit/test_staleness.py`
**Test command:** `uv run pytest tests/unit/test_corpus_hint.py tests/unit/test_cli_query.py tests/unit/test_server.py tests/unit/test_staleness.py -v`

- `corpus_hint(index_store, *, tool, args, configured_repos, tainted_repos)` and
  `attach_corpus_hint(result_json, hint)` and both front-door call sites in one commit; CLI order
  compute, `_maybe_warn_poor_results` (suppressed), `annotate_response`, attach.
- `test_staleness.py` runs here: it pins the `annotate_response` signature
  (`test_no_parsed_shortcut_parameter`) and holds `TestBothFrontDoors`, which the hint mirrors.
- Output shape, `add_command` format (parent contract C1: `pipecat-context-hub repos add --config-only
  --group <group>` per matched group, joined with `; `), `refresh_command`, and absent-not-null behaviour
  per parent Decision 4.
- The parent Hints bullet in full, by file:
  - `test_corpus_hint.py`: text matching (alias negatives such as "laws" vs "aws", ` + ` and ` & `
    concepts, uppercase alias), `get_code_snippet` from `symbol`/`intent` and not `path` alone, a partial
    group listing only missing repos, catalog-ordered `matched_groups`, the `parseable = false` and tainted
    skips, `configured_no_records`, and 0.95 confidence still hinting.
  - `test_server.py` and `test_cli_query.py`: golden `corpus_hint` per door and identical JSON, non-search
    tools never hint, `configured_repos` reflects process start, coexistence with the staleness footer,
    and the stderr hint suppressed when a hint exists.
  - `test_server.py` (the line-864 pattern): `_SERVER_INSTRUCTIONS` pins `corpus_hint`, `--unlisted` and
    the draft-not-file wording; `retrieval-quality.yml` carries the optional *Candidate repos* field.
- The catalog-load-failure half lives here: with `load_catalog` returning `None` (seam:
  `load_catalog.cache_clear()` plus a patched loader) there is no `corpus_hint` key on either door, and the
  CLI stderr hint is still printed.
- Frozen queries: the four strings in Context, copied verbatim into `test_corpus_hint.py`. For each, assert
  against a fixture index and fixture catalog: hint when the matched group's repos are absent, no hint when
  present. A new numbered item in `docs/smoke-test.md` reuses them.
- Expected outcomes for the four frozen queries (provenance: the maintainer's live index, 2026-10-17):

  | Query | Expected | Why (live-index provenance) |
  |---|---|---|
  | `AWS Bedrock Nova Sonic voice agent` | hint | The AWS group is only partly indexed: `aws-samples/build-intelligent-ai-voice-agents-with-pipecat-and-amazon-bedrock` (435 records), `pipecat-ai/aws-deepgram-workshop` (390) and `pipecat-ai/aws-workshops` (33) are indexed; four other AWS seed repos are not. The 0.95 top hit `aaronsu11/Dum-E` is not an AWS repo, so confidence must not suppress the hint. |
  | `Tavus avatar` | hint | `aws-samples/sample-voice-ai-tavus-avatar-demo` has zero records and is not configured. It may sit in the AWS group or a Tavus group; the hint fires either way. |
  | `Murf TTS` | no hint | `murf-ai/pipecat-murf-tts` is indexed (24 records). |
  | `Supermemory memory` | no hint | `supermemoryai/pipecat-memory` is indexed (462 records). |

  These expectations assume one catalog group per vendor (PR 1 Phase 1 requires it). The unit tests derive each
  outcome from a fixture index and fixture catalog, never from the live index; the live numbers are recorded
  only as provenance.
- Mutation checks (revert, confirm the named test fails):
  - Alias word boundary: the "laws" negative.
  - Skip `parseable = false`: the `parseable = false` skip test.
  - Skip tainted: the tainted-in-neither-field test, on both doors.
  - CLI stderr suppression: the one-hint-not-two test.
  - `add_command` format: the golden-object test, including `--config-only` and the `; ` join.
  - Catalog-load-failure fallback: the no-`corpus_hint`-key test.

### Phase 5: catalog health and curation scripts

**Goal:** private, archived and renamed catalog entries are caught weekly, without network in PR CI.
**Impl files:** `scripts/check_repo_catalog.py`, `scripts/curate_repo_catalog.py`, `.github/workflows/catalog-health.yml`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_check_repo_catalog.py`
**Test command:** `uv run pytest tests/unit/test_check_repo_catalog.py -v`

- Parent Decision 6 in full: `httpx` with `follow_redirects=False`, the classifier taking an injected
  response, the inconclusive outcome, the aggregate verdict and exit code, `GITHUB_TOKEN` header
  behaviour, and a test that the workflow invokes the script (the `test_audit_sync.py` pattern).
- Decided now: the tests load both scripts by path with `importlib.util.spec_from_file_location`
  (the `test_probe_mcp_2x.py` pattern). No package module, so `httpx` GitHub code stays out of the wheel.
- Frozen stale fixtures from 2026-10-16, all owner `pipecat-ai`:
  - Real private (404 unauthenticated, `private` with a token): `nvidia-sgmaker`,
    `pipecat-websocket-transport`, `hosted-smart-turn`, `pipecat-krisp`, `instant-voice-demo`,
    `openai-realtime-todo-example`, `pipecat.ai-agents`, `gradient-bang-subagents`,
    `gradient-bang-synth-forge`.
  - Real archived: `pipecat-cli`, `pipecat-client-cxx-daily`.
  - Synthetic, no real example was recorded: the 301-renamed, the deleted 404, the `full_name` rename
    beyond case, and the case-only difference. Label them synthetic in the test.
- `curate_repo_catalog.py` takes an injectable `httpx` client or transport. Its test runs it with an
  `httpx.MockTransport` returning canned search results (no network), and asserts the catalog file is
  byte-identical and never opened for writing.
- `test_check_repo_catalog.py` joins the Windows file list in `ci.yml` in this commit. It reads files by
  repo-relative path and asserts no permission bits.
- Mutation checks: none are assigned to this phase.

### Phase 4b: seam test against the real `repos` command

**Prerequisite:** HEAD must contain PR 1's Phase 2d commit. Rebase onto `feature/repo-catalog-pr1` once 2d
exists, or onto `main` after PR 1 merges. If `src/pipecat_context_hub/cli_repos.py` is absent, hand back.

**Goal:** what `repos add` writes is what the hint reads, proven offline.
**Impl files:** `tests/unit/test_repos_hint_seam.py`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_repos_hint_seam.py`
**Test command:** `uv run pytest tests/unit/test_repos_hint_seam.py -v`

- Fixture index: a real on-disk `IndexStore` (`create=True`, no model needed), filled with records built
  like `_make_record(repo=<slug>)` in `tests/unit/test_index_store.py`. Copy that pattern; do not import
  the private helper. Use a fixture catalog through the loader seam PR 1's `repos` tests use.
- With a temp `config.toml`-only layout (no env var, no `.env`): `repos add <catalog slug>` then a new
  `_query_runtime` puts the slug in `configured_repos`. A tainted slug yields neither field.
- Group-level expectations. In a two-repo group with one repo added, `repos_not_indexed` lists the other
  and `add_command` still names the group. In a single-repo group with no records for the added slug,
  the hint has `repos_configured_no_records` and `refresh_command` and no `add_command`. With records, no hint.
- The test runs the exact `add_command` string against the temp `config.toml`-only layout. It splits on
  `; `, strips the leading `pipecat-context-hub`, and invokes each part on the real `repos` command, exit 0.
  The test states this layer in a comment.
- A second case sets `PIPECAT_HUB_EXTRA_REPOS` in a cwd `.env`. The `--config-only` form exits 0; plain
  `repos add` exits 1 (parent Decision 7). That proves `--config-only` is accepted under a shadowing layer.
- `test_repos_hint_seam.py` joins the Windows file list in `ci.yml`, skipping POSIX permission-bit assertions.
- Mutation check: change the C1 `add_command` to drop `--config-only`; the shadowing-`.env` case must fail.

**Closing steps (not a phase; before opening the PR).** Run the offline smoke
(`uv run pytest tests/smoke/ -v`). Install the branch with `uv tool install --force .` and reconnect
(`/mcp`). Run the new numbered `docs/smoke-test.md` item for one unindexed and one indexed vendor query.
Put the result and the mutation-check results in the PR description. Restore the released tool afterwards.

<!-- reviewed: YYYY-MM-DD @ <hash> -->

## Progress

- [ ] Phase 3a: `get_counts_by_repo` on the serving index
- [ ] Phase 3b: `indexed_repos` and `groups_not_indexed`
- [ ] Phase 3c: config providers and `get_hub_status`
- [ ] Phase 4: corpus hints
- [ ] Phase 5: catalog health and curation scripts
- [ ] Phase 4b: seam test against the real `repos` command

## Findings

### Review Waivers

_(none yet; this plan has not been through `/review-plan`)_
