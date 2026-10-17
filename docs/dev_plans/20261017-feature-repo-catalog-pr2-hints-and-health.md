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
frozen contracts, file ownership), then its Decisions 3, 4, 6 and 10, its *Test plan* bullets labelled
`[PR2]`, and its *Facts resolved* list. The parent is the single source for behaviour. If this file and
the parent disagree, stop and hand back.

**Start condition.** This branch is created from PR 1's Phase 1 commit (so `shared/repo_catalog.py` and
`load_catalog()` exist) and is rebased onto `main` after PR 1 merges. Phases 3b onward import the catalog.
Do not start this plan before that commit exists.

Facts a phase worker needs and cannot cheaply rediscover:

- `annotate_response(result_json, index_store)` (`shared/staleness.py`) has a pinned signature
  (`test_no_parsed_shortcut_parameter`); do not add a parameter to it. The hint is a separate function.
- `cli_query._maybe_warn_poor_results` runs before `annotate_response`; the CLI computes the hint first,
  suppresses the stderr hint when one exists, and attaches after `annotate_response`.
- `get_counts_by_repo` exists on `IndexStore` and the FTS index but not on the `ServingIndex` protocol
  (`shared/interfaces.py`) or `ReopeningIndexStore` (`services/index/reopening.py`); it keys by the
  configured slug verbatim and injects a synthetic `docs.pipecat.ai` key.
- `config.sources.effective_repos` already strips tainted slugs; `config.sources.tainted_repos` is a
  property. `_QueryRuntime` and `_query_runtime` live in `cli_query.py`.
- `shared/` imports nothing from `services/`. Tests import package modules only. The Windows CI job lists
  test files by name; a new test module joins the list in the commit that creates it.
- Never `git add -A`; stage explicit paths.

## Requirements

1. `indexed_repos`, `configured_no_records` and `catalog_groups_not_indexed` on `get_hub_status`
   (parent Decision 10).
2. `corpus_hint` / `attach_corpus_hint` on `search_docs`, `search_examples`, `search_api`,
   `get_code_snippet`, on both front doors with identical JSON (parent Decisions 3 and 4).
3. The health check and curation scripts and the weekly workflow (parent Decision 6).
4. A seam test against PR 1's real `repos` command (Phase 4b).
5. Every parent *Test plan* bullet labelled `[PR2]` has tests; every mutation check labelled `[PR2]` is
   run (revert it, confirm its test fails).

## Review Focus

- The two-door parity: the same `corpus_hint` from `server/main.py` and `cli_query.py`, and the CLI
  suppression order.
- The tainted skip: a tainted catalog repo appears in neither `repos_not_indexed` nor `add_command`.
- The frozen `add_command` format (parent Delivery, contract C1) must be a command PR 1's `repos add`
  accepts for exactly those slugs.

## Implementation Checklist

### Phase 3a: `get_counts_by_repo` on the serving index

**Goal:** serve and the one-shot CLI can read per-repo record counts through the protocol they already hold.
**Impl files:** `src/pipecat_context_hub/shared/interfaces.py`, `src/pipecat_context_hub/services/index/reopening.py`
**Test files:** `tests/unit/test_index_lock.py`
**Test command:** `uv run pytest tests/unit/test_index_lock.py -v`

- Add `get_counts_by_repo` to the `ServingIndex` protocol and a `ReopeningIndexStore` delegate inside
  `_using`. `ReopeningIndexStore` is covered today by `tests/unit/test_index_lock.py` and
  `tests/unit/test_cli.py`; extend those, or add a focused module and list it here.

### Phase 3b: `indexed_repos` and `groups_not_indexed`

**Goal:** one predicate decides "this group is not indexed", used by both the hint and `get_hub_status`.
**Impl files:** `src/pipecat_context_hub/shared/corpus_hint.py`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_corpus_hint.py`
**Test command:** `uv run pytest tests/unit/test_corpus_hint.py -v`

- `indexed_repos(index_store)` (casefolded, drops the synthetic `docs.pipecat.ai` key, an unreadable count
  produces no hint) and `groups_not_indexed(catalog, indexed)` (parent Decision 10).
- The new test module joins the Windows file list in `ci.yml` in this commit.

### Phase 3c: config providers and `get_hub_status`

**Goal:** `get_hub_status` reports indexed, configured-no-records and uncovered catalog groups on both doors.
**Impl files:** `src/pipecat_context_hub/server/tools/get_hub_status.py`, `src/pipecat_context_hub/server/main.py`, `src/pipecat_context_hub/cli_query.py`, `src/pipecat_context_hub/cli.py`
**Test files:** `tests/unit/test_hub_status.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_server.py`
**Test command:** `uv run pytest tests/unit/test_hub_status.py tests/unit/test_cli_query.py tests/unit/test_server.py -v`

- Thread `configured_repos` and `tainted_repos` providers (read once at process start from
  `config.sources`) through `serve`, `create_server`, `handle_get_hub_status`, `_QueryRuntime` and
  `_query_runtime`, in this one commit, with a both-front-doors parity test.

### Phase 4: corpus hints

**Goal:** a vendor question whose repos are not indexed carries one compact, deterministic hint, with no network and no embeddings.
**Impl files:** `src/pipecat_context_hub/shared/corpus_hint.py`, `src/pipecat_context_hub/server/main.py`, `src/pipecat_context_hub/cli_query.py`, `.github/ISSUE_TEMPLATE/retrieval-quality.yml`, `docs/smoke-test.md`
**Test files:** `tests/unit/test_corpus_hint.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_server.py`
**Test command:** `uv run pytest tests/unit/test_corpus_hint.py tests/unit/test_cli_query.py tests/unit/test_server.py -v`

- `corpus_hint(index_store, *, tool, args, configured_repos, tainted_repos)` and
  `attach_corpus_hint(result_json, hint)` and both front-door call sites in one commit; CLI order
  compute, `_maybe_warn_poor_results` (suppressed), `annotate_response`, attach.
- Output shape, `add_command` format (contract C1), `refresh_command`, and absent-not-null behaviour per
  parent Decision 4.
- `_SERVER_INSTRUCTIONS` points at the field and pins the `--unlisted` and draft-not-file wording; add the
  optional *Candidate repos* field to `retrieval-quality.yml`.
- Frozen real queries copied verbatim into the test file (the four 2026-10-17 unindexed-vendor queries and
  the AWS Nova Sonic query), and a new numbered item in `docs/smoke-test.md` reusing them.

### Phase 4b: seam test against the real `repos` command

**Goal:** what `repos add` writes is what the hint reads, proven offline.
**Impl files:** `tests/unit/test_repos_hint_seam.py`
**Test files:** `tests/unit/test_repos_hint_seam.py`
**Test command:** `uv run pytest tests/unit/test_repos_hint_seam.py -v`

- With a temp `config.toml` and a fixture index: `repos add <catalog slug>` then a new `_query_runtime`
  puts the slug in `configured_repos`; a fixture index with no records for it yields
  `repos_configured_no_records` plus `refresh_command` and no `add_command` for that repo; with records it
  yields no hint. A tainted slug yields neither. The `add_command` text parses as a `repos add` invocation
  that the real command accepts (run it against the temp config).

### Phase 5: catalog health and curation scripts

**Goal:** private, archived and renamed catalog entries are caught weekly, without network in PR CI.
**Impl files:** `scripts/check_repo_catalog.py`, `scripts/curate_repo_catalog.py`, `.github/workflows/catalog-health.yml`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_check_repo_catalog.py`
**Test command:** `uv run pytest tests/unit/test_check_repo_catalog.py -v`

- Parent Decision 6 in full: `httpx` with `follow_redirects=False`, the classifier taking an injected
  response, the inconclusive outcome, the aggregate verdict and exit code, `GITHUB_TOKEN` header
  behaviour, a test that the workflow invokes the script (the `test_audit_sync.py` pattern), and
  `curate_repo_catalog.py` leaving the catalog byte-identical.
- Tests import the classifier through a package module or load the script by path; `scripts/` is not a
  package, so decide which in this phase and record it.

## Progress

- [ ] Phase 3a: `get_counts_by_repo` on the serving index
- [ ] Phase 3b: `indexed_repos` and `groups_not_indexed`
- [ ] Phase 3c: config providers and `get_hub_status`
- [ ] Phase 4: corpus hints
- [ ] Phase 4b: seam test against the real `repos` command
- [ ] Phase 5: catalog health and curation scripts

## Findings

### Review Waivers

_(none yet; this plan has not been through `/review-plan`)_
