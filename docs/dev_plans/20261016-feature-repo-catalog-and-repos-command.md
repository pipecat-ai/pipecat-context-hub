# Repo catalog, `repos` command and corpus hints

**Branch:** `feature/repo-catalog` | **Review Gates:** full | **Component:** config | **Status:** Planned (design grilled 2026-10-16; `/review-plan` 2026-10-17, findings routed)

## Problem

A developer who asks a vendor-specific question ("how do I use NVIDIA Nemotron speech with Pipecat?")
or wants examples that are not in the default corpus has no way to find out that a curated repo exists,
or to add it. Evidence gathered on 2026-10-16:

- `.env.example` is the only catalog of extra repos, and it is only reachable by cloning this repo.
  Users who install with `uv tool install`, `uvx`, or the `pipecat` CLI never see it, and a cwd `.env`
  is the wrong place for them anyway (`~/.config/pipecat-context-hub/config.toml` is the machine-global
  home).
- The catalog had rotted. Of ~56 listed slugs, nine were **private** (clone fails without access:
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

- No network access during tool calls (the docs promise none). Hints come from the static catalog.
- No automatic indexing by an agent. The agent proposes; the user runs or approves the command.
- No change to the default repo list, and no support for non-Python/TS/RST parsing.

## Decisions (grilled 2026-10-16)

Ten decisions, each recommended once and answered by the maintainer. Where the answer changed the
recommendation, the change is stated. Decisions 8-10, and the edits to 2, 3, 4, 6 and 7, come from
`/review-plan` on 2026-10-17.

1. **TOML writer: `tomlkit`, a new runtime dependency.** `repos add/remove` edits only the
   `PIPECAT_HUB_EXTRA_REPOS` key in `~/.config/pipecat-context-hub/config.toml`, preserving comments,
   with an atomic write. It goes through the dependency review gate. (`tomlkit` is not in `uv.lock`;
   `tomli-w` is, but only via `pip-audit` (dev) and drops comments.)
2. **Catalog: TOML with groups** (changed from a flat per-repo list at the maintainer's request).
   File `src/pipecat_context_hub/data/repo_catalog.toml`, shipped in the wheel (probed), validated by a
   loader. Shape:
   ```toml
   verified = 2026-10-16                      # file-level date of the last full health pass
   [groups.nvidia]
   kind = "vendor"                            # vendor | topic
   description = "NVIDIA Nemotron speech and voice agents"
   aliases = ["nvidia", "nemotron", "riva"]   # used for hint matching
     [[groups.nvidia.repos]]
     slug = "NVIDIA-AI-Blueprints/nemotron-voice-agent"
     note = "replaces archived NVIDIA/voice-agent-examples"
   ```
   The catalog is **public-only**: a private or archived repo is a schema error, never an entry. The
   only default-overriding flag is `parseable = false` (the repo clones but yields no chunks, so hints
   skip it); personal repos carry `maintainer`. Community repos are grouped by **topic**, not
   by person (a person's name is not what a user types in a question). `.env.example` and the docs
   table are **generated** from the catalog; a test fails when they differ.
3. **Hint trigger: deterministic and independent of confidence.** Fire when a group alias appears in
   the query (word boundary, case-insensitive, matched against the whole query including every
   ` + ` / ` & ` concept) **and** at least one `parseable` repo of that group is not indexed
   (decision 10). Applies to the four search tools only (`search_docs`, `search_examples`,
   `search_api`, `get_code_snippet`). No embeddings, no network. Changed from the grilled "low
   confidence or empty" gate after `/review-plan`: on the maintainer's live index (2026-10-17)
   `search_docs` reported a not-low confidence (0.95) for three of four unindexed-vendor queries, and
   `search_examples` reported 0.95 for AWS Nova Sonic by matching `aaronsu11/Dum-E`, the behaviour the
   AGENTS.md Review Checklist already records. A confidence gate would miss most real misses. Known
   cost: the hint also appears on vendor queries that were answered well, so it stays one compact
   object.
   - **Agent-assist scope (maintainer addition):** the hint and the server instructions tell the
     agent that if it finds a relevant repo on GitHub it should *suggest* `repos add <slug>
     --unlisted` to the **user** (it writes the user's `config.toml`, never the shared catalog), and
     offer to draft a retrieval-quality issue listing candidate repos. An optional *Candidate
     repos* field is added to `.github/ISSUE_TEMPLATE/retrieval-quality.yml`. The agent drafts; the
     user files. A read-only `search_repo_catalog` MCP tool plus a `repos search` CLI twin (required by
     `test_every_mcp_tool_has_a_cli_command`) is a **later phase**, after hints show whether they
     suffice.
4. **Placement: a shared function beside `annotate_response`, not a new parameter on it.**
   `annotate_response(result_json, index_store)` receives no query or tool name and a test
   (`test_no_parsed_shortcut_parameter`) pins its signature. The hint is attached by a new
   `shared/corpus_hint.py: attach_corpus_hint(result_json, index_store, *, tool, query)`, which both
   front doors (`server/main.py`, `cli_query.py`) call immediately after `annotate_response`, in the
   same commit. It adds a top-level `corpus_hint` object `{matched_groups, repos_not_indexed,
   add_command, report_issue_url}`, **absent** when it does not apply. When present, the CLI's existing
   `_maybe_warn_poor_results` stderr hint is suppressed (one hint, not two) and `_SERVER_INSTRUCTIONS`
   points at the field. A parity test mirrors `TestBothFrontDoors`. `get_hub_status` (never annotated)
   gains `indexed_repos`, `configured_no_records` and `catalog_groups_not_indexed`.
5. **`repos add` does not run `refresh`.** It edits `config.toml`, prints
   `pipecat-context-hub refresh`, and notes that a running `serve` reopens the rebuilt index itself.
6. **Health check: existence first.** `scripts/check_repo_catalog.py` runs locally and in a new
   weekly `catalog-health.yml` using the job's `GITHUB_TOKEN` (the unauthenticated limit of 60
   requests/hour is too low for ~55 slugs). It requests each repo with redirects disabled and fails on **inaccessible** (a 404 with the job
   token: missing or private, indistinguishable, and the catalog is public-only so both fail),
   **archived**, or **renamed** (a 301, or a `full_name` differing from the slug case-insensitively,
   reporting the new name); a `verified` date older than 90 days is advisory. The classifier takes an
   injected response, so offline unit tests freeze the 2026-10-16 stale entries (private, archived,
   404, 301). Changed from the recommendation at the maintainer's request: CI should not have private
   repos; a maintainer who needs one uses `--unlisted` with their own credentials. PR CI runs only the offline checks
   (schema, uniqueness, generated `.env.example` matches). A separate, report-only
   `scripts/curate_repo_catalog.py` reproduces the 2026-10-16 manual analysis (source-file counts,
   activity, vendor discovery by code search) for a human to review; it never edits the catalog.
7. **Guard rails on `repos add/remove`.**
   - Catalog slugs add freely; any other slug needs `--unlisted` and is syntax-checked. Visibility is
     unknowable offline: a private `--unlisted` slug fails at the next `refresh` as a per-source clone
     error. Adding a slug already configured is a no-op (exit 0) that prints the `refresh` reminder.
   - If a real env var or cwd `.env` sets `PIPECAT_HUB_EXTRA_REPOS`, the edit would not change the
     effective list, so `repos add` **refuses** (exit 1, naming the shadowing layer) unless
     `--config-only` is passed. Contract: after a successful add **without** `--config-only`, `repos
     list` shows the repo effective; with it, `config.toml` changes and `repos list` says the repo is
     shadowed by that layer. The layer comes from decision 9. `repos list` prints the cwd whose `.env`
     it consulted, because a `serve` launched from another directory reads a different `.env`.
   - Default and tainted repos cannot be added or removed through `repos`.
   - `repos add` is offline; a bad slug surfaces as a per-source clone error at the next `refresh`.

8. **`tomlkit` writer contract** (grilled 2026-10-17). The writer resolves the path with
   `resolve_global_config_path()` (honours `PIPECAT_HUB_CONFIG_FILE`); for a symlink it writes the temp
   file in the target's directory and renames over the resolved target, so the link survives. It first
   applies the reader's own refusals (non-regular, foreign-owned, world-writable), creates a new file
   0600 and its directory 0700, and preserves an existing file's mode. It edits only the top-level
   `PIPECAT_HUB_EXTRA_REPOS` key and writes a native array of strings; a CSV-string value is read and
   rewritten as an array. Before the rename it re-parses the temp file through `load_global_config`
   and aborts, leaving the original untouched, if the list differs from the intended one.
9. **Layer provenance** (grilled 2026-10-17). `load_cwd_dotenv` and `load_global_config` return a
   per-key map `{key: layer}` built with their own blank-is-absent rule; `load_env_layers` exposes it;
   `repos` reads that map and never re-parses a layer. This is the first Phase 2 task (the loaders
   merge into `os.environ` first-writer-wins and record no source today).
10. **"Indexed" means at least one record** (grilled 2026-10-17). `indexed_repos(index_store)` in
    `shared/corpus_hint.py` reads `IndexStore.get_counts_by_repo` (the store is passed in, so `shared/`
    imports nothing from `services/`). A configured repo with zero records is reported separately as
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
| `repos` (one-shot CLI) | env layers via the provenance map, `config.toml`, the catalog data file | `config.toml` only | on demand |
| `refresh` (one-shot) | the effective repo list from all layers | the index | user-run, after an add |
| `serve` (long-lived) | the index (reopened after a refresh), the catalog | nothing | per tool call; config read at startup |
| one-shot query CLI | the index, the catalog | nothing | per invocation |
| `catalog-health.yml` (weekly) | the catalog, the GitHub API | a report only | weekly |

The hint reads **indexed** state from the index and group membership from the catalog, never the
config, so a stale `serve` still hints correctly after a `refresh`. The catalog loader is
`shared/repo_catalog.py` and imports nothing from `cli`, `server` or `services`.

## Phases

1. **Catalog, generator, offline checks.** Schema and loader (`shared/repo_catalog.py`); initial data
   from the 2026-10-16 audit (51 verified public slugs); the `.env.example` generator (a delimited
   generated block; header prose and non-repo settings come from a checked-in template, the default
   list from `SourceConfig.repos`) and a generated catalog section in `docs/configuration.md`,
   separate from the env-var table that `TestConfigTomlExampleParity` pins. The equality test, the
   regenerated `.env.example` and the docs section land in **one commit**. Offline schema/uniqueness
   tests; wheel-contents test for the data file.
2. **`repos list|add|remove`.** In order: (a) add `tomlkit` to `pyproject.toml` and `uv.lock` as the
   first commit (so `pip-audit` and the SBOM see it before code imports it) and confirm it resolves
   alongside `pipecat-ai[cli]`; (b) provenance, decision 9; (c) the writer, decision 8; (d) the
   commands with the guard rails of decision 7, `--group` selection, `EXIT_INVALID_INPUT = 1` added to
   `shared/exit_codes.py`, docs in `docs/configuration.md`, and the `repos --help` bridge test.
3. **`get_hub_status` reports indexed repos**: `indexed_repos`, `configured_no_records`,
   `catalog_groups_not_indexed` (decision 10).
4. **Corpus hints**: `attach_corpus_hint` and both front-door call sites in one commit, stderr-hint
   suppression, the server-instructions text, the *Candidate repos* field in `retrieval-quality.yml`,
   frozen real queries, a smoke-test item.
5. **Catalog health and curation scripts** plus the weekly `catalog-health.yml`.
6. **Dogfood migration**, in this order: `repos add --config-only <slug>` per slug (the repo-root `.env`
   sets `PIPECAT_HUB_EXTRA_REPOS`, so a plain add refuses), delete that `.env` line, `repos list` to
   check the effective list equals the 47-repo list, then `refresh`. `.env.backup` exists. Record
   anything awkward.
7. **Later, only if hints prove insufficient:** `search_repo_catalog` MCP tool + `repos search` CLI.
   `test_mapped_commands_are_registered_on_main` checks top-level command names, so either register
   `repos search` top-level or extend `_TOOL_TO_COMMAND` and that test deliberately.

## Test plan

- Catalog: schema, unique slugs, a private or archived entry is a schema error, generated
  `.env.example` block and docs section equal the checked-in files, wheel contains the catalog.
- Classifier (offline, injected responses): ok, 404, archived, 301, case-only name difference; the
  90-day advisory boundary.
- Provenance: per-key map for each layer, including a blank higher-layer value treated as absent
  (mirror `TestBlankHigherLayerValueDoesNotShadow`).
- Writer: round trip through `load_global_config` for no file, empty file, comments preserved, CSV
  value, symlinked config, `PIPECAT_HUB_CONFIG_FILE`, world-writable and foreign-owned refusal, mode
  kept or created 0600, and a failed re-parse leaving the original untouched.
- `repos` table: layers (env var / cwd `.env` / blank `.env` / `config.toml` only / none) x (add /
  remove / list / add `--config-only`), asserting `SourceConfig.effective_repos`, the shadow refusal
  (exit 1), and a golden `repos list` per layer including the consulted cwd; the `--unlisted` syntax
  check, default and tainted refusal, idempotent add, and a cross-cwd case.
- Indexed state for `get_hub_status`: indexed, `configured_no_records`, not configured.
- Hints: frozen real queries (a vendor question with the repo unindexed must hint, the same with it
  indexed must not); a 0.95-confidence response still hints; alias negatives ("laws" must not match
  "aws"); `TTS + nvidia`; `parseable = false` entries skipped; non-search tools never hint; both front
  doors give the same `corpus_hint`; the CLI stderr hint is suppressed when `corpus_hint` is present.
- Mutation checks: revert each fix and confirm its test fails.
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

None. All 38 `/review-plan` findings (2026-10-17) were routed into the plan above; the health-check
recommendation was overridden by the maintainer (see decision 6).
