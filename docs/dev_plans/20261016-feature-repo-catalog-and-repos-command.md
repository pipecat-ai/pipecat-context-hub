# Repo catalog, `repos` command and corpus hints

**Branch:** `feature/repo-catalog` | **Review Gates:** full | **Component:** config | **Status:** Planned (design grilled 2026-10-16; ready for `/review-plan`)

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
3. **Hints**: when a result is low-confidence or empty and the query matches a catalog group whose
   repos are not indexed, say so, name the command, and tell the agent how to propose repos it
   finds on GitHub (to the user's `config.toml` only) and how to report the miss.
4. **Catalog health check** that catches private/archived/renamed entries (this would have caught the
   eleven stale entries removed on `docs/env-example-repo-coverage`).

## Non-goals

- No network access during tool calls (the docs promise none). Hints come from the static catalog.
- No automatic indexing by an agent. The agent proposes; the user runs or approves the command.
- No change to the default repo list, and no support for non-Python/TS/RST parsing.

## Decisions (grilled 2026-10-16)

Seven decisions, each recommended once and answered by the maintainer. Where the answer changed the
recommendation, the change is stated.

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
   Per-repo flags appear only when they differ from the defaults (`private=false`, `archived=false`,
   `parseable=true`); personal repos carry `maintainer`. Community repos are grouped by **topic**, not
   by person (a person's name is not what a user types in a question). `.env.example` and the docs
   table are **generated** from the catalog; a test fails when they differ.
3. **Hint trigger: narrow and deterministic.** Fire only when the result is `low_confidence` or
   empty, **and** a group alias appears in the query (word boundary, case-insensitive), **and** at
   least one repo of that group is not indexed. No embeddings, no network. Known cost: a confident
   but generic answer to a vendor question gets no hint; widen from real misses frozen into tests.
   - **Agent-assist scope (maintainer addition):** the hint and the server instructions tell the
     agent that if it finds a relevant repo on GitHub it should *suggest* `repos add <slug>
     --unlisted` to the **user** (it writes the user's `config.toml`, never the shared catalog), and
     offer to draft a retrieval-quality issue listing candidate repos. An optional *Candidate
     repos* field is added to `.github/ISSUE_TEMPLATE/retrieval-quality.yml`. The agent drafts; the
     user files. A read-only `search_repo_catalog` MCP tool plus a `repos search` CLI twin (required by
     `test_every_mcp_tool_has_a_cli_command`) is a **later phase**, after hints show whether they
     suffice.
4. **Placement: follow the `index_staleness` precedent.** A top-level `corpus_hint` object
   `{matched_groups, repos_not_indexed, add_command, report_issue_url}` is added in the shared
   `annotate_response` step used by both the MCP server and the one-shot CLI, and is **absent** when
   it does not apply. `get_hub_status` gains `indexed_repos` and `catalog_groups_not_indexed`.
5. **`repos add` does not run `refresh`.** It edits `config.toml`, prints
   `pipecat-context-hub refresh`, and notes that a running `serve` reopens the rebuilt index itself.
6. **Health check: existence first.** `scripts/check_repo_catalog.py` runs locally and in a new
   weekly `catalog-health.yml` using the job's `GITHUB_TOKEN` (the unauthenticated limit of 60
   requests/hour is too low for ~55 slugs). It fails on missing, private, archived or renamed
   entries; a `verified` date older than 90 days is advisory. PR CI runs only the offline checks
   (schema, uniqueness, generated `.env.example` matches). A separate, report-only
   `scripts/curate_repo_catalog.py` reproduces the 2026-10-16 manual analysis (source-file counts,
   activity, vendor discovery by code search) for a human to review; it never edits the catalog.
7. **Guard rails on `repos add/remove`.**
   - Catalog slugs add freely; any other slug needs `--unlisted` and is syntax-checked.
   - A catalog entry flagged `private` needs `--allow-private`.
   - If a real env var or cwd `.env` sets `PIPECAT_HUB_EXTRA_REPOS`, the edit would not change the
     effective list, so `repos add` **refuses** (exit 1, naming the shadowing layer) unless
     `--config-only` is passed. Contract: after a successful add, `repos list` shows the repo effective.
   - Default and tainted repos cannot be added or removed through `repos`.
   - `repos add` is offline; a bad slug surfaces as a per-source clone error at the next `refresh`.

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
- `annotate_response` (`shared/staleness.py`) is the single annotation point for both front doors.

## Phases

1. **Catalog, generator, offline checks.** Schema and loader; initial data from the 2026-10-16 audit
   (51 verified slugs); the `.env.example` and docs generator; offline schema/uniqueness tests; a
   wheel-contents test for the data file.
2. **`repos list|add|remove`.** `tomlkit` edit of `config.toml` (atomic), effective-list view with
   layer attribution, the guard rails of decision 7, exit codes per `shared/exit_codes.py`,
   `--group` selection, docs in `docs/configuration.md`, and the `repos --help` bridge test.
3. **`get_hub_status` reports indexed repos** and the catalog groups not indexed.
4. **Corpus hints**: `corpus_hint` in `annotate_response`, the server-instructions text, the
   *Candidate repos* field in `retrieval-quality.yml`, frozen real queries, a smoke-test item.
5. **Catalog health and curation scripts** plus the weekly `catalog-health.yml`.
6. **Dogfood migration**: move the maintainer's `.env` (`.env.backup` taken 2026-10-16) into
   `config.toml` with the new commands; record anything awkward.
7. **Later, only if hints prove insufficient:** `search_repo_catalog` MCP tool + `repos search` CLI.

## Test plan

- Catalog: schema, unique slugs, no `private` entry reachable from a public bundle, generated
  `.env.example` equals the checked-in file.
- `repos` round-trip table over layers (env var set / cwd `.env` set / `config.toml` only / none) x
  (add / remove / list), asserting `SourceConfig.effective_repos` and the shadowing warning.
- Mutation checks: revert each fix and confirm its test fails.
- Real data: run against a copy of `~/.pipecat-context-hub` and the maintainer's real `config.toml`.
- `repos --help` and `repos add --help` through the Typer bridge match the direct CLI.
- Generated `.env.example` and docs equal the checked-in files; wheel contains the catalog.
- Hints: frozen real queries (a vendor question with the vendor repo unindexed must hint; the same
  query with it indexed must not; an unrelated empty result must not).
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
