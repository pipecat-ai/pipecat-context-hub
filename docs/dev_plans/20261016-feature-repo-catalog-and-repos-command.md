# Repo catalog, `repos` command and corpus hints

**Branch:** `feature/repo-catalog` | **Review Gates:** full | **Component:** config | **Status:** Planned (design grilled 2026-10-16; `/review-plan` runs `20261016T231344` and `20261017T004623` after the first sign-off; the contradictions they found were grilled 2026-10-17; findings routed)

## Problem

A developer who asks a vendor-specific question ("how do I use NVIDIA Nemotron speech with Pipecat?")
or wants examples that are not in the default corpus has no way to find out that a curated repo exists,
or to add it. Evidence gathered on 2026-10-16:

- `.env.example` is the only catalog of extra repos, and it is only reachable by cloning this repo.
  Users who install with `uv tool install`, `uvx`, or the `pipecat` CLI never see it, and a cwd `.env`
  is the wrong place for them anyway (`~/.config/pipecat-context-hub/config.toml` is the machine-global
  home).
- The catalog had rotted. Of the slugs listed before PR #143 (the exact count is not reproducible from history), nine were **private** (clone fails without access:
  `nvidia-sgmaker`, `pipecat-krisp`, `hosted-smart-turn`, ...) and two were **archived**
  (`pipecat-cli`, `pipecat-client-cxx-daily`). Nothing detects this.
- Vendor coverage was found by code search, not org name: the NVIDIA repo that matters is
  `NVIDIA-AI-Blueprints/nemotron-voice-agent`, which an `--owner NVIDIA` search missed. Google and
  Meta publish no Pipecat repos that are worth indexing (checked 2026-10-16).
- The maintainer's own `.env` held 62 extra repos, 33 of them absent from the example (private,
  archived, stale, zero-chunk Swift/Kotlin, forks), so "replace my `.env` with the example" is wrong;
  the two lists need a merge story, not a copy.
- `get_hub_status` does not report which repos are indexed, so an agent cannot tell "not indexed"
  from "not found". The only miss-handling today is the "file a retrieval-quality issue" text
  (`server/main.py`, `cli_query.py`).
- Precedence is first-writer-wins per key: a cwd `.env` `PIPECAT_HUB_EXTRA_REPOS` **shadows**
  `config.toml`'s list, it does not merge (`shared/env_loading.py`, documented in
  `docs/configuration.md`). Any tool that edits one list must say which one is in force.

## Goals

1. One **catalog** of known repos (vendor, topic, language, status), the single source of truth for
   `.env.example`, docs and hints.
2. A **`repos` command** (`list`, `add`, `remove`) that edits `config.toml` so installed users never
   touch a `.env`.
3. **Hints**: when the query matches a catalog group whose repos are not indexed, say so, name the command, and tell the agent how to propose repos it
   finds on GitHub (to the user's `config.toml` only) and how to report the miss.
4. **Catalog health check** that catches private/archived/renamed entries (this would have caught the
   eleven stale entries removed on `docs/env-example-repo-coverage`).

## Non-goals

- No network access during tool calls (true today; this plan keeps it so). Hints come from the static catalog.
- No automatic indexing by an agent. The agent proposes; the user runs or approves the command.
- No change to the default repo list, and no support for non-Python/TS/RST parsing.

## Decisions (grilled 2026-10-16)

Ten decisions, each recommended once and answered by the maintainer. Where the answer changed the
recommendation, the change is stated. Decisions 8-10, and the edits to 2, 3, 4, 6 and 7, come from
the first `/review-plan`. Two later passes (runs `20261016T231344`, 34 findings, and `20261017T004623`,
38 findings, none Critical in either) added the writer/provenance seams, the case-fold and `--group`
rules, the hint plumbing order and the test gaps; those edits are folded into decisions 2-4 and 6-10,
the phases and the test plan, not listed separately. Three contradictions were grilled and resolved
(2026-10-17): the Windows writer mirrors the reader (decision 8); taint is a command-time and
hint-time concern, not a loader invariant (decisions 2, 3, 7); `verified` is the date of the last
manual audit (decisions 2, 6).

1. **TOML writer: `tomlkit`, a new runtime dependency.** `repos add/remove` edits only the
   `PIPECAT_HUB_EXTRA_REPOS` key in `~/.config/pipecat-context-hub/config.toml`, preserving comments,
   with an atomic write. It goes through the dependency review gate. (`tomlkit` is not in `uv.lock`;
   `tomli-w` is, but only via `pip-audit` (dev) and drops comments.)
2. **Catalog: TOML with groups** (changed from a flat per-repo list at the maintainer's request).
   File `src/pipecat_context_hub/data/repo_catalog.toml`, shipped in the wheel (probed), validated by a
   loader. Shape:
   ```toml
   verified = 2026-10-16                      # date of the last manual catalog audit
   [groups.nvidia]
   kind = "vendor"                            # vendor | topic
   description = "NVIDIA Nemotron speech and voice agents"
   aliases = ["nvidia", "nemotron", "riva"]   # used for hint matching
     [[groups.nvidia.repos]]
     slug = "NVIDIA-AI-Blueprints/nemotron-voice-agent"
     note = "replaces archived NVIDIA/voice-agent-examples"
   ```
   The catalog is **public-only**, enforced in two places. Offline, the schema has no visibility or
   status field and rejects `private`, `archived` and `status` keys. Visibility itself is unknowable
   offline, so the weekly health check (decision 6) is what fails on a private or archived entry. The
   loader also enforces static cross-group invariants, all casefolded: slugs unique across groups,
   aliases unique across groups, no default (`SourceConfig.repos`) repo in the catalog, `kind` in
   `{vendor, topic}`, `parseable` a bool, `verified` an ISO date. Tainted repos
   (`PIPECAT_HUB_TAINTED_REPOS`) are per-user runtime policy and are **not** a loader concern: `repos
   add` and `--group` refuse them at command time, and hints skip them like `parseable = false`.
   `verified` is the date of the last *manual* audit: a maintainer bumps it with
   `just catalog-regen --bump-verified` after reviewing a clean health report. The weekly job is
   report-only and never edits the file. A catalog that fails to load disables hints and
   `catalog_groups_not_indexed` (no `corpus_hint`, an empty list) and logs once; it never fails a
   tool call. The
   only default-overriding flag is `parseable = false` (the repo clones but yields no chunks, so hints
   skip it); personal repos carry `maintainer`. Community repos are grouped by **topic**, not
   by person (a person's name is not what a user types in a question). `.env.example` and the docs
   table are **generated** from the catalog; a test fails when they differ.
3. **Hint trigger: deterministic and independent of confidence.** Fire when a group alias appears in
   the query text (word boundary, case-insensitive, matched against the whole text including every
   ` + ` / ` & ` concept) **and** at least one `parseable` repo of that group is not indexed
   (decision 10), and the repo is not tainted. Applies to the four search tools only (`search_docs`, `search_examples`,
   `search_api`, `get_code_snippet`). The text matched is per tool, from one table in
   `shared/corpus_hint.py`: `query` for the three search tools; `symbol` and `intent` for
   `get_code_snippet` (a path-only lookup never hints). `matched_groups` follows catalog order, so
   multi-group output is deterministic. No embeddings, no network. Changed from the grilled "low
   confidence or empty" gate after `/review-plan`: on the maintainer's live index (2026-10-17)
   `search_docs` reported a not-low confidence (0.95) for three of four unindexed-vendor queries, and
   `search_examples` reported 0.95 for AWS Nova Sonic by matching `aaronsu11/Dum-E`, the behaviour the
   AGENTS.md Review Checklist already records. A confidence gate would miss most real misses. Known
   cost: the hint also appears on vendor queries that were answered well, so it stays one compact
   object.
   - **Agent-assist scope (maintainer addition):** the hint and the server instructions tell the
     agent that if it finds a relevant repo on GitHub that is not in the catalog it should *suggest*
     `pipecat-context-hub repos add <slug> --unlisted` to the **user** (catalog repos use plain
     `repos add <slug>`; `add_command` in the hint is always the `pipecat-context-hub` form, which the
     `pipecat context-hub` front door mirrors) (it writes the user's `config.toml`, never the shared catalog), and
     offer to draft a retrieval-quality issue listing candidate repos. An optional *Candidate
     repos* field is added to `.github/ISSUE_TEMPLATE/retrieval-quality.yml`. The agent drafts; the
     user files. A read-only `search_repo_catalog` MCP tool plus a `repos search` CLI twin (required by
     `test_every_mcp_tool_has_a_cli_command`) is a **later phase**, after hints show whether they
     suffice.
4. **Placement: a shared function beside `annotate_response`, not a new parameter on it.**
   `annotate_response(result_json, index_store)` receives no query or tool name and a test
   (`test_no_parsed_shortcut_parameter`) pins its signature. The hint is split into a compute step and an attach step in a new
   `shared/corpus_hint.py`: `corpus_hint(index_store, *, tool, args, configured_repos) -> dict | None`
   (`args` is the raw tool arguments; the per-tool text extraction lives in the one table in that
   module, so the front doors do not duplicate it) and `attach_corpus_hint(result_json, hint)`. Both
   front doors (`server/main.py`, `cli_query.py`) compute the hint, call `annotate_response`, then
   attach, in the same commit. The catalog comes from `load_catalog()` (`functools.cache`, with
   `cache_clear()` as the test seam). On the MCP door both annotations run in the one
   existing `asyncio.to_thread` hop (metadata reads take the generation lock); the catalog loads once
   per process and counts are read per call. Cache counts per index generation (via
   `ReopeningIndexStore.add_open_listener`) only if the Phase 4 smoke run shows the cost matters. On
   the CLI the compute step runs **before** `_maybe_warn_poor_results`, which already prints before
   `annotate_response` (`cli_query.py`), and its presence is passed in, so suppression happens before
   stderr is written; the attach step still runs after `annotate_response`, as on the MCP door, so the
   two doors produce the same JSON. It adds a top-level `corpus_hint` object `{matched_groups,
   repos_not_indexed, repos_configured_no_records, add_command, report_issue_url}`, **absent** (the key
   is missing, not null) when it does not apply. A group repo that is configured but has no records
   (failed clone) goes in `repos_configured_no_records` and the hint tells the user to run `refresh`;
   `add_command` covers only repos that are not configured. When present, the CLI's existing
   `_maybe_warn_poor_results` stderr hint is suppressed (one hint, not two) and `_SERVER_INSTRUCTIONS`
   points at the field. A parity test mirrors `TestBothFrontDoors`. `get_hub_status` (never annotated)
   gains `indexed_repos`, `configured_no_records` and `catalog_groups_not_indexed`.
   `configured_no_records` needs the configured repo list, so `handle_get_hub_status` and
   `create_server` gain a `configured_repos` provider, wired from `serve` and `cli_query._dispatch`
   (`_QueryRuntime` gains a `configured_repos` field, set from `config.source.effective_repos` in
   `_query_runtime`). It reflects the config **at process start**, not a later `repos add`.
5. **`repos add` does not run `refresh`.** It edits `config.toml`, prints
   `pipecat-context-hub refresh`, and notes that a running `serve` reopens the rebuilt index itself.
6. **Health check: existence first.** `scripts/check_repo_catalog.py` runs locally and in a new
   weekly `catalog-health.yml` using the job's `GITHUB_TOKEN` (the unauthenticated limit of 60
   requests/hour is too low for the whole catalog). It uses `httpx` (already the project's outbound client) with
   `follow_redirects=False`, not `gh`, which follows redirects and would hide a 301. It fails on **inaccessible** (a 404 with the job
   token: missing or private, indistinguishable, and the catalog is public-only so both fail),
   **archived**, or **renamed** (a 301, or a `full_name` differing from the slug case-insensitively,
   reporting the new name). Any other outcome (403/429 rate limit, 5xx, timeout, network error) is
   **inconclusive**: reported separately, never labelled inaccessible or private, and it fails the job
   so it gets a re-run. The script's aggregate verdict: exit 0 only when every entry is ok (a stale
   `verified` date alone is advisory and does not fail); nonzero when any entry is inaccessible,
   archived or renamed; nonzero, in its own report section, when the only problems are inconclusive.
   A `verified` date older than 90 days is advisory. The classifier takes an
   injected response, so offline unit tests freeze the 2026-10-16 stale entries (private, archived,
   404, 301). Changed from the recommendation at the maintainer's request: CI should not have private
   repos; a maintainer who needs one uses `--unlisted` with their own credentials. PR CI runs only the offline checks
   (schema, uniqueness, generated `.env.example` matches). A separate, report-only
   `scripts/curate_repo_catalog.py` reproduces the 2026-10-16 manual analysis (source-file counts,
   activity, vendor discovery by code search) for a human to review; it never edits the catalog (a
   test checks the file is byte-identical after a run).
7. **Guard rails on `repos add/remove`.**
   - Catalog slugs add freely; any other slug needs `--unlisted` and is syntax-checked. Visibility is
     unknowable offline: a private `--unlisted` slug fails at the next `refresh` as a per-source clone
     error. Checks run in a fixed order: (1) default/tainted refusal, (2) slug syntax and catalog
     membership, (3) the shadow check below, (4) the no-op. "Already configured" means present in
     `config.toml` (casefolded), never "effective through another layer": adding a slug already in
     `config.toml` is a no-op (exit 0) that prints `Already in config.toml` and the `refresh` reminder;
     a successful add prints `Added <slug>` and the reminder.
   - If a real env var or cwd `.env` sets `PIPECAT_HUB_EXTRA_REPOS`, the edit would not change the
     effective list, so `repos add` and `repos remove` **refuse** (exit 1, naming the shadowing layer)
     unless `--config-only` is passed. This runs before the no-op, so a slug present only in a
     shadowing `.env` also exits 1 without `--config-only`. Contract: after a successful add **without** `--config-only`, `repos
     list` shows the repo effective; with it, `config.toml` changes and `repos list` says the repo is
     shadowed by that layer. The layer comes from decision 9. `repos list` prints the cwd whose `.env`
     it consulted, because a `serve` launched from another directory reads a different `.env`.
   - Default and tainted repos cannot be added or removed through `repos` (tainted is read from
     `PIPECAT_HUB_TAINTED_REPOS` at command time).
   - `repos remove <slug>` of a slug not in `config.toml` follows the same order: if a shadowing layer
     sets the key it refuses (exit 1, naming the layer) unless `--config-only`, with which it is a
     no-op (exit 0, `Not in config.toml`, naming the layer); with no shadowing layer it is a no-op
     (exit 0, `Not in config.toml`).
   - Case: `add` writes the slug in the catalog's spelling (as typed with `--unlisted`) and treats a
     case-variant of a configured slug as already configured; `remove` matches casefolded and removes
     whatever spelling is configured.
   - `--group <name>` (on `add` and `remove`; mutually exclusive with a positional slug and with
     `--unlisted`) acts on the catalog group's `parseable`, non-tainted repos: `add` skips repos
     already in `config.toml` and prints the `parseable = false` and tainted ones it skipped;
     `remove` removes only the group's repos present in `config.toml`. When nothing changes it exits 0
     and prints `Nothing to add` / `Nothing to remove`. The shadow refusal and `--config-only` apply
     as for a single slug; an unknown group is exit 1 listing the valid groups.
   - `repos add` is offline; a bad slug surfaces as a per-source clone error at the next `refresh`.

8. **`tomlkit` writer contract** (grilled 2026-10-17). The writer resolves the path with
   `resolve_global_config_path()` (honours `PIPECAT_HUB_CONFIG_FILE`); for a symlink it writes the temp
   file in the target's directory and renames over the resolved target, so the link survives. It first
   applies every predicate `load_global_config` applies to the file (non-regular, foreign-owned,
   world-writable, a foreign-owned or world-writable-without-sticky containing directory, a
   foreign-owned symlink hop), via the public trust predicates, so it cannot write a file the reader
   would ignore; creates a new file
   0600 and its directory 0700, and preserves an existing file's mode. It edits only the top-level
   `PIPECAT_HUB_EXTRA_REPOS` key and writes a native array of strings; a CSV-string value is read and
   rewritten as an array. A non-array, non-string existing value is refused (exit 1) rather than
   overwritten. Before the rename it re-parses the temp file through a new pure
   `parse_global_config(path) -> dict[str, object]` (the raw TOML values; `load_global_config` coerces
   them and keeps its warnings) and aborts, leaving the original untouched, if the
   list differs from the intended one. `load_global_config` cannot do this job: it takes no path and
   only sets `os.environ` keys that are absent. `parse_global_config` is extracted in Phase 2(b) and
   `load_global_config` becomes a wrapper over it. Modules: `shared/config_writer.py` (the writer, using
   `env_loading`'s trust predicates made public), `cli_repos.py` with `register_repos_commands(main)`
   (the `cli_install.py` pattern). The read-modify-write holds an advisory lock on a sibling
   `config.toml.lock` (the `shared/index_lock.py` pattern; Phase 2(c) checks how that primitive behaves
   on Windows and records it) so two concurrent `repos` invocations cannot drop one edit. On Windows
   the writer **mirrors the reader** (grilled 2026-10-17): the regular-file check only, no 0600/0700
   guarantee, a round-trip test on Windows CI, the POSIX-only tests skipped there.
9. **Layer provenance** (grilled 2026-10-17). `load_env_layers` first snapshots the non-blank
   `PIPECAT_HUB_*` keys already in `os.environ` as layer `env`, because neither loader records a key it
   skips. `load_cwd_dotenv` and `load_global_config` then return a per-key map `{key: layer}` of the
   keys they actually set, mirroring each loader's *own* rule, which differ today: `load_cwd_dotenv`
   uses presence (`key not in os.environ`, so a blank real env var hides a `.env` value), while
   `load_global_config` treats a blank as absent. So a blank real env var plus a non-blank `.env` plus
   `config.toml` resolves to `config.toml`, and `repos add` does not refuse. A loader sets only keys
   still absent or blank, so the three key sets are disjoint and the merged map is their union (no
   overwrite rule is needed). `load_env_layers` exposes the map; `main()` stores it in `ctx.obj["env_provenance"]` for the `repos` subcommand, and the
   return value is optional so the dashboard and smoke scripts are untouched. `repos` never re-reads a
   layer to decide which one is in force (the writer's verification parse in decision 8 is a separate
   concern). This is the first Phase 2 task after the pure parse extraction (the loaders merge into
   `os.environ` first-writer-wins and record no source today).
10. **"Indexed" means at least one record** (grilled 2026-10-17). `indexed_repos(index_store)` in
    `shared/corpus_hint.py` reads `get_counts_by_repo` (the store is passed in, so `shared/`
    imports nothing from `services/`). Serve's handle is `ServingIndex`/`ReopeningIndexStore`, which
    have no such method today, so Phase 3 adds it to the `ServingIndex` protocol and a
    `ReopeningIndexStore` delegate (inside `_using`) and types `indexed_repos` against the protocol.
    Slug matching is **casefolded on both sides** (GitHub slugs are case-insensitive; index keys are
    the configured slug verbatim), for `indexed_repos`, `configured_no_records` and the `repos add`
    no-op. `groups_not_indexed(catalog, indexed)` in the same module is the single predicate behind the
    hint trigger and `catalog_groups_not_indexed`. `indexed_repos` drops the synthetic `docs.pipecat.ai` key that `get_counts_by_repo` injects. A configured repo with zero records is reported separately as
    `configured_no_records` (failed clone, zero-chunk repo). Hints skip `parseable = false` entries; an
    unreadable count produces no hint, never a false one. No new refresh-time key, so existing indexes
    work without a re-refresh.

Standing decisions from the design discussion: the agent **proposes** and the user approves (a repo
that is added is cloned and indexed, and the index feeds later answers, so a prompt-injected page must
not be able to add one: same posture as the PR #121 restriction of cwd `.env` to `PIPECAT_HUB_*`
keys); and design-first verification (the PR #139 lesson): each phase states its contract and checks
the whole state space. The contract here is *`repos add X` then `repos list` shows X, `effective_repos`
contains X, and `repos remove X` undoes it*, over every precedence-layer combination.

### Facts resolved during the interview

- A click group bridges through `plugin.py`'s passthrough (`[command_name, *ctx.args]`) with no bridge
  change; `test_every_click_command_is_bridged` compares names only. Add a `repos --help` bridge test.
- Data files under `src/pipecat_context_hub/` ship in the wheel (probe build included
  `data/_probe.toml`, then removed).
- `.github/ISSUE_TEMPLATE/retrieval-quality.yml` already exists, with a diagnostic prompt for the agent.
- `annotate_response` (`shared/staleness.py`) is the single annotation point for both front doors, but its signature is pinned and carries no query (decision 4).

## Architecture & call flow

| Process | Reads | Writes | When |
|---|---|---|---|
| `repos` (one-shot CLI) | env layers via the provenance map, `config.toml`, the catalog data file | `config.toml` and its lock file only | on demand |
| `refresh` (one-shot) | the effective repo list from all layers | the index | user-run, after an add |
| `serve` (long-lived) | the index (reopened after a refresh), the catalog, `configured_repos` (read at startup) | nothing | per tool call |
| one-shot query CLI | the index, the catalog, `configured_repos` (for `status`) | nothing | per invocation |
| `catalog-health.yml` (weekly) | the catalog, the GitHub API | a report only | weekly |

The hint reads **indexed** state from the index and group membership from the catalog, never the
config, so a stale `serve` still hints correctly after a `refresh`. The catalog loader is
`shared/repo_catalog.py` and imports nothing from `cli`, `server` or `services`.

## Phases

1. **Catalog, generator, offline checks.** Schema and loader (`shared/repo_catalog.py`, including the
   cross-group invariants of decision 2); a `just catalog-regen` recipe with `--bump-verified` (the equality test's failure
   message names it, and it is the entry point for fixing any health-check finding; a test checks the
   recipe exists, in the `test_audit_sync.py` style); initial data
   from the 2026-10-16 audit: every non-default public slug in `.env.example` (the generator emits the default block from `SourceConfig.repos`; the equality test pins the count, so no number is hardcoded here); the `.env.example` generator (a delimited
   generated block; header prose and non-repo settings come from a checked-in template, the default
   list from `SourceConfig.repos`) and a generated catalog section in `docs/configuration.md`,
   separate from the env-var table that `TestConfigTomlExampleParity` pins. The equality test, the
   regenerated `.env.example` and the docs section land in **one commit**. Offline schema/uniqueness
   tests; wheel-contents test for the data file.
2. **`repos list|add|remove`.** In order: (a) add `tomlkit` to `pyproject.toml` and `uv.lock` as the
   first commit (so `pip-audit` and the SBOM see it before code imports it) and prove it resolves
   alongside `pipecat-ai[cli]` (`pipecat-ai` is not in `uv.lock`, so `uv lock` proves nothing): in a
   scratch venv, from the branch with `tomlkit` already in `pyproject.toml`, run
   `uv pip install --dry-run . "pipecat-ai[cli]"` (this includes the hub's own pins) and record the
   resolved versions in the PR; (b) in one commit: extract the pure `parse_global_config(path)` and make
   `load_global_config` wrap it, add the provenance map of decision 9 (loaders return it, `main()`
   stores it in `ctx.obj["env_provenance"]`, plus the `env` snapshot), and update the test that patches both loaders with
   None-returning lambdas (`tests/unit/test_config.py`, `test_load_env_layers_calls_both_loaders_in_order`);
   (c) the writer, decision 8, verifying through `parse_global_config`, with the new writer and `repos`
   test modules added to the explicit file list of the Windows job in `.github/workflows/ci.yml`; (d) the commands (`cli_repos.py`)
   with the guard rails and `--group` semantics of decision 7, `EXIT_INVALID_INPUT = 1` added to
   `shared/exit_codes.py` with `cli_query._EXIT_BAD_INPUT` migrated to it in the same commit
   (including the import at `tests/unit/test_cli_query.py:16`), docs in
   `docs/configuration.md`, and the `repos --help` bridge test.
3. **`get_hub_status` reports indexed repos**: `indexed_repos`, `configured_no_records`,
   `catalog_groups_not_indexed` (decision 10). In order: (a) `get_counts_by_repo` on the `ServingIndex`
   protocol and the `ReopeningIndexStore` delegate; (b) `shared/corpus_hint.py` with `indexed_repos` and
   `groups_not_indexed` (Phase 4 adds `corpus_hint` and `attach_corpus_hint` to the same module); (c) the `configured_repos` provider threaded
   through `handle_get_hub_status`, `create_server` and `cli_query._dispatch` in **one commit**, with a
   both-front-doors parity test.
4. **Corpus hints**: `corpus_hint`/`attach_corpus_hint` and both front-door call sites in one commit, stderr-hint
   suppression (CLI: compute before the warning, attach after `annotate_response`, as in decision 4), the server-instructions text, the *Candidate repos* field in `retrieval-quality.yml`,
   frozen real queries, a smoke-test item.
5. **Catalog health and curation scripts** plus the weekly `catalog-health.yml`.
6. **Dogfood migration**, in this order. The repo-root `.env` sets `PIPECAT_HUB_EXTRA_REPOS`, so every
   add needs `--config-only`. (i) Catalog slugs: `repos add --config-only <slug>`, or
   `--group <name> --config-only` per group. (ii) The slugs that are not in the catalog (about 10 today; confirm by running `repos list` first):
   `repos add --config-only --unlisted <slug>`. (iii) Three slugs in the `.env` are `SourceConfig.repos`
   defaults (verified 2026-10-17); `repos` refuses them by design, so skip them and record the refusals. (iv) Target:
   `config.toml` holds the `.env` list minus the defaults (44 slugs today) and `effective_repos` is the
   defaults plus that list; confirm both counts rather than "the 47-repo list". Then delete the `.env`
   line, run `repos list`, then `refresh`. `.env.backup` exists. Record anything awkward.
7. **Later, only if hints prove insufficient:** `search_repo_catalog` MCP tool + `repos search` CLI.
   `test_mapped_commands_are_registered_on_main` checks top-level command names, so either register
   `repos search` top-level or extend `_TOOL_TO_COMMAND` and that test deliberately.

## Test plan

- Catalog: schema, unique slugs, a `private`/`archived`/`status` key is a schema error (visibility
  itself is only caught by the health check), static cross-group invariants (alias collision, a slug
  in two groups, a default repo, bad `kind`/`parseable`/`verified`), a tainted slug in the environment
  does **not** break loading, a catalog that fails to load disables hints without failing a tool call,
  generated `.env.example` block and docs section equal the checked-in files, wheel contains the
  catalog, the `just catalog-regen` recipe exists.
- Classifier (offline, injected responses): ok, 404, archived, 301, a real rename (`full_name` differs
  beyond case, new name reported), case-only name difference; inconclusive for 403/429/5xx/timeout/
  network error (never labelled inaccessible); the 90-day advisory boundary; the script's aggregate
  verdict and exit code over a mixed injected result set (ok with an old `verified` date exits 0;
  stale exits nonzero; inconclusive-only exits nonzero in its own section); `follow_redirects=False`
  asserted by routing a 301 through `httpx.MockTransport`; `curate_repo_catalog.py` leaves the catalog
  byte-identical.
- Provenance: per-key map for each layer, including a key set only in the real environment (layer
  `env`), a blank higher-layer value treated as absent (mirror `TestBlankHigherLayerValueDoesNotShadow`),
  plus the row blank real env var + non-blank `.env` + `config.toml` (provenance `config.toml`, no
  refusal). The `ctx.obj["env_provenance"]` carrier: through `main()` both loaders run once per `repos`
  invocation, and a pre-set provenance map alone drives the shadow decision (no layer is re-read).
- Writer: round trip through `parse_global_config` and `load_global_config` for no file, empty file,
  comments preserved, other keys byte-identical, CSV value, non-array/non-string value refused,
  symlinked config, `PIPECAT_HUB_CONFIG_FILE`, non-regular, world-writable and foreign-owned refusal,
  mode kept or created 0600, directory created 0700, and a failed re-parse leaving the original
  untouched, the containing-directory and symlink-hop refusals, and two concurrent writers not losing an
  edit. POSIX-only cases are skipped on Windows, where a round-trip test asserts the mirror-the-reader
  contract.
- `repos` table: layers (env var / cwd `.env` / blank `.env` / `config.toml` only / none) x (add /
  remove / list / add `--config-only`), asserting `SourceConfig.effective_repos`, the shadow refusal
  (exit 1), and a golden `repos list` per layer including the consulted cwd; the `--unlisted` syntax
  check, default and tainted refusal, idempotent add, a slug present only in a shadowing `.env` (add
  without `--config-only` exits 1, not 0), `remove` of an unconfigured slug in three rows (no other
  layer: exit 0; shadowing layer without `--config-only`: exit 1; with it: exit 0, names the layer),
  a table test for inputs that trigger two checks at once (default repo under a shadowing env,
  malformed `--unlisted` slug under a shadowing env), a case-variant add (no-op) and remove, golden
  stdout for add/no-op/remove including the `refresh` reminder, `--group` (partial membership,
  `parseable = false` and tainted skipped and reported, unknown group, nothing to change, mutual
  exclusion with a slug and `--unlisted`, shadow with and without `--config-only`), refusals use
  `EXIT_INVALID_INPUT` (mirror `test_refresh_in_progress_exit_code_is_shared`), and a cross-cwd case.
- Indexed state for `get_hub_status`: indexed, `configured_no_records`, not configured; case-variant
  slugs match; the synthetic `docs.pipecat.ai` key is dropped; `catalog_groups_not_indexed` for fully, partly and `parseable = false`-only groups;
  `get_counts_by_repo` raising yields no hint and no crash (mirror `test_store_errors_never_break_the_response`);
  the count read through `ReopeningIndexStore`, including after a reopen (a stale `serve` hints
  correctly after a `refresh`).
- Hints: frozen real queries (a vendor question with the repo unindexed must hint, the same with it
  indexed must not); a 0.95-confidence response still hints; alias negatives ("laws" must not match
  "aws"); `TTS + nvidia` and `TTS & nvidia`; an uppercase alias; `parseable = false` and tainted
  entries skipped; a configured-but-empty repo lands in `repos_configured_no_records`; the
  `corpus_hint` key is absent (not null) when it does not apply; `get_code_snippet` hints from
  `symbol`/`intent` and not from `path` alone; a partially indexed group lists only the missing repos
  in `repos_not_indexed`; multi-group queries give catalog-ordered `matched_groups`; the hint coexists
  with the staleness footer; non-search tools never hint; both front doors give the same
  `corpus_hint`; the CLI stderr hint is suppressed when `corpus_hint` is present (the hint is computed
  before `_maybe_warn_poor_results` runs); `_SERVER_INSTRUCTIONS` pins the `corpus_hint`, `--unlisted` and draft-not-file wording (the
  `tests/unit/test_server.py` pattern) and `retrieval-quality.yml` carries the *Candidate repos* field.
- Mutation checks: for each of these, revert it and confirm its test fails: the shadow refusal, the
  default/tainted refusal, the check order (default/tainted before shadow before no-op), the writer's
  failed-re-parse abort, the alias word boundary, the
  `parseable = false` skip, the casefold in slug matching, and the CLI stderr-hint suppression.
- Real data: run against a copy of `~/.pipecat-context-hub` and the maintainer's real `config.toml`.
- `repos --help` and `repos add --help` through the Typer bridge match the direct CLI.
- Live MCP smoke at PR creation (AGENTS.md pre-merge checklist), with a new numbered item.

## Already done (merged in PR #143, before this plan)

- `.env.example`: removed the private and archived repos, added NVIDIA, Google, AWS and
  other-provider sections and a community section; every slug verified public and not archived
  (2026-10-16). Stale pipecat-cli/krisp mentions fixed in `docs/CONTRIBUTING.md`, `docs/README.md`
  and the `SourceConfig` docstring.
- `docs/README.md`: the Environment Variables table and the long Data Sources paragraph moved into
  `docs/configuration.md` (parity test repointed).
- The maintainer's `.env` pruned from 62 to 47 repos and `refresh --prune` run; the index matches the
  config exactly.

## Not done / open

- Everything under Phases above.
- `docs/smoke-test.md` still says `pipecat-cli` is "intentionally NOT a default"; accurate, but could
  say archived.
- Vendor discovery method (code search per org) is not automated; the catalog job should rerun it.

<!-- reviewed: 2026-10-17 @ 5ef11629f28da79ad241071f67a05c60ef94b599 -->

## Progress

- [ ] Phase 1: catalog, generator, offline checks
- [ ] Phase 2: `repos list|add|remove`
- [ ] Phase 3: `get_hub_status` indexed repos
- [ ] Phase 4: corpus hints
- [ ] Phase 5: catalog health and curation scripts
- [ ] Phase 6: dogfood migration
- [ ] Phase 7: `search_repo_catalog` (only if hints prove insufficient)

## Findings

### Review Waivers

None. All 38 findings of the first `/review-plan`, all 34 of run `20261016T231344` and all 38 of run
`20261017T004623` were routed into the plan above, with the recommended resolution taken for each
("fix all"); the three contradictions that were real decisions were grilled and accepted as recommended
(Windows writer mirrors the reader; taint is not a loader invariant; `verified` is the manual-audit
date). The health-check recommendation was overridden by the maintainer (see decision 6).
