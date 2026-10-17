# Repo catalog, `repos` command and corpus hints

**Branch:** `feature/repo-catalog` | **Review Gates:** full | **Component:** config | **Status:** Planned (design grilled 2026-10-16; independently reviewed by `/review-plan`, see Findings)

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
- **Deferred, built only if hints prove insufficient:** a read-only `search_repo_catalog` MCP tool plus
  a `repos search` CLI twin. `test_mapped_commands_are_registered_on_main` checks top-level command
  names, so either register `repos search` top-level or extend `_TOOL_TO_COMMAND` and that test
  deliberately.

## Decisions (grilled 2026-10-16)

Ten decisions, each recommended once and answered by the maintainer. Where the answer changed the
recommendation, the change is stated. Review history and grilled outcomes are in Findings.

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
   (`PIPECAT_HUB_TAINTED_REPOS`) are per-user runtime policy and are **not** a loader concern: a positional `repos add <tainted
   slug>` is refused at command time, `--group` skips tainted members (decision 7), and hints skip
   them like `parseable = false`.
   `verified` is the date of the last *manual* audit: a maintainer bumps it with
   `just catalog-regen --bump-verified` after reviewing a clean health report. The weekly job is
   report-only and never edits the file. `load_catalog()` returns `Catalog | None`: a catalog that fails to
   load returns `None` (cached, logged once at the first load, never raised), which disables hints
   and `catalog_groups_not_indexed` (no `corpus_hint`, an empty list) and never fails a tool call. The
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
     `test_every_mcp_tool_has_a_cli_command`) is **deferred** (see Non-goals).
4. **Corpus hint: placement and contract.** A shared function pair beside `annotate_response`, not a
   new parameter on it: `annotate_response(result_json, index_store)` receives no query or tool name,
   and `test_no_parsed_shortcut_parameter` pins its signature.
   - **API** (`shared/corpus_hint.py`): `corpus_hint(index_store, *, tool, args, configured_repos,
     tainted_repos) -> dict | None` and `attach_corpus_hint(result_json, hint)`. `args` is the raw tool
     arguments; the per-tool text extraction is one table in that module, so the front doors do not
     duplicate it. The catalog comes from `load_catalog()` (`functools.cache`, `cache_clear()` as the
     test seam); a load failure is a cached `None`, so it logs once.
   - **Call order**, both front doors, landed in the same commit, producing the same JSON. MCP door
     (`server/main.py`): compute, `annotate_response`, attach, all inside the existing
     `asyncio.to_thread` hop (metadata reads take the generation lock). CLI door (`cli_query.py`):
     compute, then `_maybe_warn_poor_results` (its stderr hint is suppressed when a hint exists, so
     the user sees one hint, not two), then `annotate_response`, then attach. Counts are read per
     call; cache them per index generation (`ReopeningIndexStore.add_open_listener`) only if the
     Phase 4 smoke run shows the cost matters.
   - **Output.** A top-level `corpus_hint` object `{matched_groups, repos_not_indexed,
     repos_configured_no_records, add_command, refresh_command, report_issue_url}`, **absent** (the key
     is missing, not null) when it does not apply. `add_command` covers only repos that are neither
     configured nor tainted and is omitted when there are none. A group repo that is configured but has
     no records (failed clone) goes in `repos_configured_no_records`, and `refresh_command`
     (`pipecat-context-hub refresh`) is present only when that list is non-empty. `report_issue_url` is
     `RETRIEVAL_QUALITY_ISSUE_URL`. `_SERVER_INSTRUCTIONS` points at the field. A parity test mirrors
     `TestBothFrontDoors`.
   - **Config inputs** (`configured_repos`, `tainted_repos`) are read once, at process start, from
     `config.sources.effective_repos` and `config.sources.tainted_repos` in `_query_runtime` (which
     gains both fields) and `serve`, and threaded through `create_server` and
     `handle_get_hub_status`. `effective_repos` already strips tainted slugs, which is why the tainted
     list is a separate input. A later `repos add` is not seen by a running `serve`.
   - **`get_hub_status`** (never annotated) gains `indexed_repos`, `configured_no_records` and
     `catalog_groups_not_indexed`, from the same providers.
5. **`repos add` does not run `refresh`.** It edits `config.toml`, prints
   `pipecat-context-hub refresh`, and notes that a running `serve` reopens the rebuilt index itself.
6. **Health check: existence first.** `scripts/check_repo_catalog.py` runs locally and in a new
   weekly `catalog-health.yml` using the job's `GITHUB_TOKEN` (shared runner IPs make the unauthenticated limit unreliable
   even though the catalog is under 60 entries). It uses `httpx` (already the project's outbound client) with
   `follow_redirects=False`, so a 301 is observable (the script does not rely on how `gh` treats redirects). It fails on **inaccessible** (a 404 with the job
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
7. **`repos add/remove` contract.** Checks run in this fixed order for a positional slug on `add`
   and `remove`; the first that applies decides.

   | # | Check | Outcome |
   |---|---|---|
   | 1 | the slug is a default (`SourceConfig.repos`) or tainted (`PIPECAT_HUB_TAINTED_REPOS`, read at command time) repo | exit 1 |
   | 2 | slug syntax and catalog membership | exit 1 if malformed; exit 1 if not in the catalog and `--unlisted` is absent (says to pass it); exit 1 if in the catalog and `--unlisted` is given (says to drop it, so the catalog spelling stays authoritative) |
   | 3 | a real env var or cwd `.env` sets `PIPECAT_HUB_EXTRA_REPOS` (decision 9) and `--config-only` is absent | exit 1, naming the layer |
   | 4 | no-op: `add` of a slug already in `config.toml`, or `remove` of one not in it (casefolded) | exit 0: `Already in config.toml` / `Not in config.toml`; with `--config-only` under a shadowing layer it also names that layer |
   | 5 | write | `Added <slug>` / `Removed <slug>` plus the `refresh` reminder |

   - "Already configured" means present in `config.toml`, never "effective through another layer".
     Check 3 precedes check 4, so a slug present only in a shadowing `.env` also exits 1 without
     `--config-only`.
   - Case: `add` writes the catalog's spelling (as typed, with `--unlisted`), and treats a case-variant of
     a configured slug as already configured; `remove` matches casefolded and removes whatever spelling
     is configured.
   - `--group <name>` (on `add` and `remove`; mutually exclusive with a positional slug and `--unlisted`)
     acts on the catalog group's `parseable`, non-tainted repos: `add` skips repos already in
     `config.toml` and prints the `parseable = false` and tainted ones it skipped; `remove` removes only
     the group's repos present in `config.toml`. Checks 3 to 5 apply as above. When nothing changes
     (including a group whose members are all tainted or `parseable = false`) it exits 0 and prints
     `Nothing to add` / `Nothing to remove` plus the skipped list; an unknown group exits 1 listing the
     valid groups.
   - `repos list` shows the effective list and the consulted cwd (a `serve` launched from another
     directory reads a different `.env`), tags each slug `default`, `catalog` or `unlisted`, and names
     the shadowing layer for any repo that `config.toml` holds but a higher layer hides.
   - `repos add` is offline. Visibility is unknowable offline, so a bad or private `--unlisted` slug
     surfaces as a per-source clone error at the next `refresh`.
8. **`tomlkit` writer contract** (grilled 2026-10-17).
   - **Path.** `resolve_global_config_path()` (honours `PIPECAT_HUB_CONFIG_FILE`). For a symlink the
     temp file is written in the target's directory and renamed over the resolved target, so the link
     survives.
   - **Trust, checked once, in `env_loading`.** A new public `read_trusted_config(path) -> str | None`
     opens the file, applies every predicate `load_global_config` applies on the same open descriptor
     (non-regular, foreign-owned, world-writable, a foreign-owned or world-writable-without-sticky
     containing directory, a foreign-owned symlink hop), and returns the text (`None` for a missing
     file) or raises `ConfigNotTrusted(reason)`. The trust helpers stay private and the writer never
     reimplements them, so it cannot write a file the reader would ignore. A new
     `parse_global_config(path) -> dict[str, object]` is `read_trusted_config` plus `tomllib.loads`
     (raw values; raises on untrusted or invalid TOML). `load_global_config` becomes a wrapper that
     catches the raises, warns, coerces and merges; it stays unable to do the writer's job because it
     takes no path and sets only `os.environ` keys that are absent **or blank**. Both are extracted in
     Phase 2(b).
   - **Edit.** Only the top-level `PIPECAT_HUB_EXTRA_REPOS` key, as a native array of strings; a
     CSV-string value is read and rewritten as an array. Comments and other keys are preserved. A new
     file is created 0600 in a 0700 directory; an existing file's mode is kept.
   - **Refusals** (exit 1, file untouched, message names the reason): a non-array, non-string existing
     value; an existing file that is not valid TOML; an untrusted file. (`load_global_config` keeps
     ignoring such a file with a warning.)
   - **Verification.** Before the rename the writer re-parses the temp file through
     `parse_global_config` and aborts, leaving the original untouched, if the list differs from the
     intended one.
   - **Concurrency.** None: no lock. Two simultaneous `repos` invocations can lose one edit (last write
     wins); the atomic rename prevents a torn file. Accepted for a single-user CLI. A lock was
     considered and dropped (its fail-open and Windows cases cost more than the race).
   - **Windows** mirrors the reader (grilled 2026-10-17): the regular-file check only, no 0600/0700
     guarantee, a round-trip test on Windows CI, POSIX-only tests skipped there.
   - **Modules.** `shared/config_writer.py` (the writer) and `cli_repos.py` with
     `register_repos_commands(main)` (the `cli_install.py` pattern).
9. **Layer provenance** (grilled 2026-10-17). `repos` must say which layer is in force, and the
   loaders merge into `os.environ` first-writer-wins and record no source today.
   - **Contract.** `load_env_layers` returns `dict[str, str]`, key to layer (`env`, `.env`,
     `config.toml`); `main()` stores it in `ctx.obj["env_provenance"]` for the `repos` subcommand. The
     return value is optional, so the dashboard and smoke scripts are untouched. `repos` never re-reads
     a layer to decide which one is in force.
   - **Construction.** First snapshot the non-blank `PIPECAT_HUB_*` keys already in `os.environ` as
     layer `env`, because neither loader records a key it skips. Then each loader returns the keys it
     actually set, mirroring its own rule, which differ today: `load_cwd_dotenv` uses presence
     (`key not in os.environ`, so a blank real env var hides a `.env` value), `load_global_config` treats
     a blank as absent. So a blank real env var, a non-blank `.env` value and a `config.toml` value
     resolve to `config.toml`, and `repos add` does not refuse.
   - **Merge.** The key sets can overlap: `load_cwd_dotenv` writes an absent key as blank and
     `load_global_config` then sets it. The merge is `{**env, **dotenv, **global_config}`, so the loader
     that ran last, and determined the final `os.environ` value, wins.
   - **Order.** This is the first Phase 2 task after the parse extraction.
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
contains X, and `repos remove X` undoes it*, over every precedence-layer combination, with one
qualification: a successful add **with** `--config-only` under a shadowing layer puts X in
`config.toml` but not in `effective_repos`, and `repos list` names the shadowing layer. Each row of
the `repos` test table states which outcome it expects.

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
| `serve` (long-lived) | the index (reopened after a refresh), the catalog, `configured_repos` and `tainted_repos` (read at startup) | nothing | per tool call |
| one-shot query CLI | the index, the catalog, `configured_repos` and `tainted_repos` (for the hint and `status`) | nothing | per invocation |
| `catalog-health.yml` (weekly) | the catalog, the GitHub API | a report only | weekly |

The hint reads **indexed** state from the index, group membership from the catalog, and
**configured/tainted** state from the config as read at process start. A stale `serve` therefore hints
correctly after a `refresh`, but a repo added with `repos add` after startup is mis-sorted into
`add_command` (not yet configured) if its clone failed, until `serve` restarts. The catalog loader is
`shared/repo_catalog.py` and imports nothing from `cli`, `server` or `services`.

## Phases

1. **Catalog, generator, offline checks.** Schema and loader (`shared/repo_catalog.py`, including the
   cross-group invariants of decision 2); a `just catalog-regen` recipe with `--bump-verified` (the equality test's failure
   message names it, and it is the entry point for fixing any health-check finding; tests check that the
   recipe exists (the `test_audit_sync.py` style), that `--bump-verified` rewrites `verified` only,
   that plain regen leaves it, and that regen is idempotent); initial data
   from the 2026-10-16 audit: every non-default public slug in `.env.example` (the generator emits the default block from `SourceConfig.repos`; the equality test pins the count, so no number is hardcoded here); the renderers in `shared/repo_catalog_render.py` (imports `shared/config` for
   `SourceConfig.repos`; the equality test imports it, since tests import package modules only;
   `just catalog-regen` runs it as a module) that rewrite a delimited generated block inside the
   checked-in `.env.example` (header prose and non-repo settings stay hand-written outside the markers)
   and a generated catalog section in `docs/configuration.md`,
   separate from the env-var table that `TestConfigTomlExampleParity` pins. The equality test, the
   regenerated `.env.example` and the docs section land in **one commit**. Offline schema/uniqueness
   tests; wheel-contents test for the data file; the catalog test module joins the Windows file list in
   `.github/workflows/ci.yml` in this commit.
2. **`repos list|add|remove`.** In order: (a) add `tomlkit` to `pyproject.toml` and `uv.lock` as the
   first commit (so `pip-audit` and the SBOM see it before code imports it) and prove it resolves
   alongside `pipecat-ai[cli]` (`pipecat-ai` is not in `uv.lock`, so `uv lock` proves nothing): in a
   scratch venv, from the branch with `tomlkit` already in `pyproject.toml`, run
   `uv pip install --dry-run . "pipecat-ai[cli]"` (this includes the hub's own pins) and record the
   resolved versions in the PR; (b) in one commit: extract `read_trusted_config(path)` and `parse_global_config(path)` (decision 8) and make
   `load_global_config` wrap them, add the provenance map of decision 9 (loaders return it, `main()`
   stores it in `ctx.obj["env_provenance"]`, plus the `env` snapshot), and update the test that patches both loaders with
   None-returning lambdas (`tests/unit/test_config.py`, `test_load_env_layers_calls_both_loaders_in_order`);
   (c) the writer, decision 8, verifying through `parse_global_config`, with the writer test module added
   to the explicit file list of the Windows job in `.github/workflows/ci.yml` (the file must exist in
   the same commit, or pytest exits 4); (d) the commands (`cli_repos.py`)
   with the guard rails, `--group` semantics and `repos list` tags of decision 7, the `repos` test module added to the Windows list in this commit, `EXIT_INVALID_INPUT = 1` added to
   `shared/exit_codes.py` with `cli_query._EXIT_BAD_INPUT` migrated to it in the same commit
   (including the import at `tests/unit/test_cli_query.py:16`), docs in
   `docs/configuration.md`, and the `repos --help` bridge test.
3. **`get_hub_status` reports indexed repos**: `indexed_repos`, `configured_no_records`,
   `catalog_groups_not_indexed` (decision 10). In order: (a) `get_counts_by_repo` on the `ServingIndex`
   protocol and the `ReopeningIndexStore` delegate; (b) `shared/corpus_hint.py` with `indexed_repos` and
   `groups_not_indexed` (Phase 4 adds `corpus_hint` and `attach_corpus_hint` to the same module), its
   test module joining the Windows list in this commit; (c) the `configured_repos` and `tainted_repos` providers threaded
   through `handle_get_hub_status`, `create_server` and `cli_query._dispatch` in **one commit**, with a
   both-front-doors parity test.
4. **Corpus hints**: `corpus_hint`/`attach_corpus_hint` and both front-door call sites in one commit, stderr-hint
   suppression (CLI: compute before the warning, attach after `annotate_response`, as in decision 4), the server-instructions text, the *Candidate repos* field in `retrieval-quality.yml`,
   frozen real queries, a smoke-test item.
5. **Catalog health and curation scripts** plus the weekly `catalog-health.yml`.
6. **Dogfood migration**, in this order. The repo-root `.env` sets `PIPECAT_HUB_EXTRA_REPOS`, so every
   add needs `--config-only`. (i) Catalog slugs: `repos add --config-only <slug>` per slug (not `--group`, which skips
   `parseable = false` repos such as the two iOS demos the `.env` holds). (ii) The slugs that are not in
   the catalog (11 today; three are private: `nvidia-sgmaker`, `pipecat-krisp`, `hosted-smart-turn`,
   which clone with the maintainer's own credentials): derive them by diffing the `.env` list against
   the catalog, then `repos add --config-only --unlisted <slug>`; `repos list` afterwards must tag
   exactly those `unlisted`. (iii) Three slugs in the `.env` are `SourceConfig.repos`
   defaults (verified 2026-10-17); `repos` refuses them by design, so skip them and record the refusals. (iv) Target:
   `config.toml` holds the `.env` list minus the defaults (44 slugs today) and `effective_repos` is the
   defaults plus that list; confirm both counts rather than "the 47-repo list". Then delete the `.env`
   line, run `repos list`, then `refresh`. `.env.backup` exists. Record anything awkward.

## Test plan

- Catalog: schema, unique slugs, a `private`/`archived`/`status` key is a schema error (visibility
  itself is only caught by the health check), static cross-group invariants (alias collision, a slug
  in two groups, a default repo, bad `kind`/`parseable`/`verified`), a tainted slug in the environment
  does **not** break loading, a catalog that fails to load returns `None` and logs once across repeated calls, which disables
  hints without failing a tool call, leaves `catalog_groups_not_indexed == []` on both doors and still
  lets the CLI print its stderr hint;
  generated `.env.example` block and docs section equal the checked-in files, wheel contains the
  catalog, the `just catalog-regen` recipe exists.
- Classifier (offline, injected responses): ok, 404, archived, 301, a real rename (`full_name` differs
  beyond case, new name reported), case-only name difference; inconclusive for 403/429/5xx/timeout/
  network error (never labelled inaccessible); the 90-day advisory boundary; `GITHUB_TOKEN` sent as an
  `Authorization` header when set and omitted when not; a test that `catalog-health.yml` invokes the
  script (the `test_audit_sync.py` pattern); the frozen stale fixtures are the real 2026-10-16 slugs; the script's aggregate
  verdict and exit code over a mixed injected result set (ok with an old `verified` date exits 0;
  stale exits nonzero; inconclusive-only exits nonzero in its own section); `follow_redirects=False`
  asserted by routing a 301 through `httpx.MockTransport`; `curate_repo_catalog.py` leaves the catalog
  byte-identical.
- Provenance: per-key map for each layer, including a key set only in the real environment (layer
  `env`), a blank higher-layer value treated as absent (mirror `TestBlankHigherLayerValueDoesNotShadow`),
  plus the row blank real env var + non-blank `.env` + `config.toml` (provenance `config.toml`, no
  refusal), and the overlap row (a key set blank by `.env` and then by `config.toml`: the merged layer
  is `config.toml`). The `ctx.obj["env_provenance"]` carrier: through `main()` both loaders run once per `repos`
  invocation, and a pre-set provenance map alone drives the shadow decision (no layer is re-read).
- Writer: round trip through `parse_global_config` and `load_global_config` for no file, empty file,
  comments preserved, other keys byte-identical, CSV value, non-array/non-string value refused,
  symlinked config, `PIPECAT_HUB_CONFIG_FILE`, non-regular, world-writable and foreign-owned refusal,
  mode kept or created 0600, directory created 0700, and a failed re-parse leaving the original
  untouched, the containing-directory and symlink-hop refusals, an existing file that is not valid
  TOML refused with the file untouched, and an untrusted file refused. POSIX-only cases are skipped on Windows, where a round-trip test asserts the mirror-the-reader
  contract.
- `repos` table: layers (env var / cwd `.env` / blank `.env` / `config.toml` only / none) x (add /
  remove / list / add `--config-only`), asserting `SourceConfig.effective_repos`, the shadow refusal
  (exit 1), and a golden `repos list` per layer including the consulted cwd; the `--unlisted` syntax
  check, default and tainted refusal, idempotent add, a slug present only in a shadowing `.env` (add
  without `--config-only` exits 1, not 0), `remove` of an unconfigured slug in three rows (no other
  layer: exit 0; shadowing layer without `--config-only`: exit 1; with it: exit 0, names the layer),
  a table test for inputs that trigger two checks at once (default repo under a shadowing env,
  malformed `--unlisted` slug under a shadowing env), a case-variant add (no-op) and remove, a well-formed non-catalog slug without `--unlisted` (exit 1),
  `--unlisted` on a catalog slug (exit 1), `repos list` tags (`default`/`catalog`/`unlisted`), golden
  stdout for add/no-op/remove including the `refresh` reminder, `--group` (partial membership,
  `parseable = false` and tainted skipped and reported, an all-tainted group (exit 0, `Nothing to
  add`), unknown group, nothing to change, mutual
  exclusion with a slug and `--unlisted`, shadow with and without `--config-only`), refusals use
  `EXIT_INVALID_INPUT` (mirror `test_refresh_in_progress_exit_code_is_shared`), a cross-cwd case, each
  row stating whether `effective_repos` contains the slug afterwards, and `repos add` making no network
  call.
- Indexed state for `get_hub_status`: indexed, `configured_no_records`, not configured; case-variant
  slugs match; the synthetic `docs.pipecat.ai` key is dropped; `catalog_groups_not_indexed` for fully, partly and `parseable = false`-only groups;
  `get_counts_by_repo` raising yields no hint and no crash (mirror `test_store_errors_never_break_the_response`);
  the count read through `ReopeningIndexStore`, including after a reopen (a stale `serve` hints
  correctly after a `refresh`).
- Hints: frozen real queries, the four 2026-10-17 unindexed-vendor queries and the AWS Nova Sonic
  query, copied verbatim into the test file and reused for the new numbered smoke item (a vendor question with the repo unindexed must hint, the same with it
  indexed must not); a 0.95-confidence response still hints; alias negatives ("laws" must not match
  "aws"); `TTS + nvidia` and `TTS & nvidia`; an uppercase alias; `parseable = false` and tainted
  entries skipped (a tainted catalog repo appears in neither `repos_not_indexed` nor `add_command`, on
  both doors); a configured-but-empty repo lands in `repos_configured_no_records`; the
  `corpus_hint` key is absent (not null) when it does not apply; a golden `corpus_hint` object per
  door (`add_command` lists only unconfigured, untainted repos and is omitted when none; `refresh_command` appears
  only with a configured-but-empty repo; `report_issue_url` equals `RETRIEVAL_QUALITY_ISSUE_URL`);
  `configured_repos` reflects process start (a later `repos add` does not change a running hint); `get_code_snippet` hints from
  `symbol`/`intent` and not from `path` alone; a partially indexed group lists only the missing repos
  in `repos_not_indexed`; multi-group queries give catalog-ordered `matched_groups`; the hint coexists
  with the staleness footer; non-search tools never hint; both front doors give the same
  `corpus_hint`; the CLI stderr hint is suppressed when `corpus_hint` is present (the hint is computed
  before `_maybe_warn_poor_results` runs); `_SERVER_INSTRUCTIONS` pins the `corpus_hint`, `--unlisted` and draft-not-file wording (the
  `tests/unit/test_server.py` pattern) and `retrieval-quality.yml` carries the *Candidate repos* field.
- Mutation checks: for each of these, revert it and confirm its test fails: the shadow refusal, the
  default/tainted refusal, the check order (default/tainted before shadow before no-op), the writer's
  failed-re-parse abort, the alias word boundary, the
  `parseable = false` skip, the tainted skip in the hint, the catalog-load-failure fallback, the
  `--unlisted` rules, the casefold in slug matching, and the CLI stderr-hint suppression.
- Real data: run against a copy of `~/.pipecat-context-hub` and the maintainer's real `config.toml`;
  expected observations: `repos list` shows the effective list with every slug tagged, the catalog
  loads, and `status` reports `catalog_groups_not_indexed` consistent with the index.
- `repos --help` and `repos add --help` through the Typer bridge match the direct CLI, and a
  functional `pipecat context-hub repos list` plus `repos add --config-only` run through the bridge
  (guards a `ctx.obj["env_provenance"]` gap only on that door).
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

## Findings

### Review history

| Run | Findings | Notes |
|---|---|---|
| first `/review-plan` (2026-10-17) | 38 | folded into decisions 2-4, 6-10 |
| `20261016T231344` | 34 | writer and provenance seams, case-fold, `--group`, hint plumbing |
| `20261017T004623` | 38 | three contradictions grilled: Windows writer mirrors the reader; taint is a command-time and hint-time concern, not a loader invariant; `verified` is the last manual audit date |
| `20261017T025658` | 31 | taken at the recommended resolution without a grill: `read_trusted_config` owns open-and-trust; `--unlisted` on a catalog slug exits 1; the writer lock was later dropped in a consolidation pass |

No pass found a Critical finding. Most findings in each later pass were in edits from the pass before,
so decisions 4, 7, 8 and 9 were rewritten as contracts (2026-10-17) rather than patched again.

### Review Waivers

None. Every finding of the four passes was routed into the plan above, with the recommended resolution taken for each. The health-check recommendation was overridden by the maintainer (see decision 6).
