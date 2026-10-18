# Repo catalog, `repos` command and corpus hints

**Branch:** `feature/repo-catalog-pr1` (PR 1; PR 2 gets its own branch) | **Review Gates:** full | **Component:** config | **Status:** In progress (design grilled 2026-10-16; signed off by `/review-plan` 2026-10-17, see Findings; delivered as two PRs: PR 1 merged (#144, `5733f06`), PR 2 built on `feature/repo-catalog-pr2`, awaiting merge)

**Delivery:** two PRs, each from its own conduct-format plan (see *Delivery*): PR 1 =
[`20261017-feature-repo-catalog-pr1-catalog-and-repos.md`](20261017-feature-repo-catalog-pr1-catalog-and-repos.md),
PR 2 = [`20261017-feature-repo-catalog-pr2-hints-and-health.md`](20261017-feature-repo-catalog-pr2-hints-and-health.md).
This file is the **design of record** (problem, decisions, contracts, test plan). It is not run through
`/conduct`: it has no `## Implementation Checklist` with `### Phase N:` headings, which is what
`/conduct` parses.

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
   and `catalog_groups_not_indexed` (no `corpus_hint`, an empty list) and never fails a tool call. `repos`
   does not ignore a `None` catalog: it cannot validate a slug, so it refuses to change anything (decision 7). The
   only default-overriding flag is `parseable = false` (the repo clones but yields no chunks, so hints
   skip it); personal repos carry `maintainer`. Community repos are grouped by **topic**, not
   by person (a person's name is not what a user types in a question). Phase 1 requires **one catalog group per
   vendor**, with no catch-all "other providers" group. AWS, Tavus, Murf and Supermemory each get their own. PR 2's
   frozen-query expectations rely on it: the AWS and Tavus queries hint, the Murf and Supermemory queries do not. `.env.example` and the docs
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
     `repos add <slug>`; `add_command` in the hint is always the `pipecat-context-hub ... --config-only --group` form (contract C1), which the
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
     configured nor tainted and is omitted when there are none; its exact format (`repos add --config-only --group <group>` per matched group, joined with `; `) is frozen in *Delivery*, contract C1. A group repo that is configured but has
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
   | 3 | the provenance map (decision 9) holds `PIPECAT_HUB_EXTRA_REPOS` from layer `env` or `.env` (so its final value is non-blank) and `--config-only` is absent | exit 1, naming the layer |
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
   - **Catalog failed to load** (`load_catalog()` returns `None`). Positional `add`/`remove` and `--group`
     exit 1, naming the catalog load failure, with the file untouched. `repos list` still prints the effective
     list and tags every non-default slug `unlisted`. It warns once, relying on `load_catalog`'s own log-once;
     `cli_repos` emits no second warning.
   - `repos add` is offline. Visibility is unknowable offline, so a bad or private `--unlisted` slug
     surfaces as a per-source clone error at the next `refresh`.
8. **`tomlkit` writer contract** (grilled 2026-10-17).
   - **Path.** `resolve_global_config_path()` (honours `PIPECAT_HUB_CONFIG_FILE`). If it returns `None` (no home, or a
     blank `PIPECAT_HUB_CONFIG_FILE`), the writer refuses with `ConfigWriteRefused`. For a symlink the
     temp file is written in the target's directory and renamed over the resolved target, so the link
     survives.
   - **Trust, checked once, in `env_loading`.** A new public `read_trusted_config(path) -> str | None`
     opens the file, applies every predicate `load_global_config` applies on the same open descriptor
     (non-regular, foreign-owned, world-writable, a foreign-owned or world-writable-without-sticky
     containing directory, a foreign-owned symlink hop), and returns the text (`None` for a missing
     file) or raises `ConfigNotTrusted(reason)`. Seam outcomes: a missing file gives `None`.
     Every trust predicate and every open, read or decode failure (EACCES, an embedded NUL) raises
     `ConfigNotTrusted`. Invalid TOML raises `tomllib.TOMLDecodeError` from `parse_global_config`, unwrapped; the writer
     wraps it into `ConfigWriteRefused`. The seam takes a path, so an unresolvable path stays outside it. The trust helpers stay private and the writer never
     reimplements them, so it cannot write a file the reader would ignore. A new
     `parse_global_config(path) -> dict[str, object]` is `read_trusted_config` plus `tomllib.loads`
     (raw values; raises on untrusted or invalid TOML). `load_global_config` becomes a wrapper that
     catches the raises, warns, coerces and merges; it stays unable to do the writer's job because it
     takes no path and sets only `os.environ` keys that are absent **or blank**. Both are extracted in
     Phase 2(b).
   - **Edit.** Only the top-level `PIPECAT_HUB_EXTRA_REPOS` key, as a native array of strings; a
     CSV-string value is read and rewritten as an array. Comments and other keys are preserved. A new
     file is created 0600 in a 0700 directory; an existing file's mode is kept.
   - **Refusals** (`repos add`/`remove` exit 1, file untouched, message names the reason): a non-array,
     non-string existing value; an existing file that is not valid TOML; an untrusted file; no resolvable config path.
     The writer raises `ConfigWriteRefused` (it wraps `TOMLDecodeError`); `ConfigNotTrusted` propagates unchanged.
     `cli_repos` maps all three errors to exit 1. `load_global_config` keeps ignoring such a file with a warning.
     `repos list` is read-only: on a bad `config.toml` it prints the effective list, names the ignored file and
     the reason, and exits 0.
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
     `config.toml`); `main()` always overwrites `ctx.obj["env_provenance"]` with it (no `setdefault`) for the `repos` subcommand. The
     return value is optional, so the dashboard and smoke scripts are untouched. `repos` never re-reads
     a layer to decide which one is in force.
   - **Meaning.** The map holds the layer that supplied the FINAL NON-BLANK value of each key. A key whose
     final merged value is blank or absent is omitted. "Blank" means empty after `.strip()`, as in
     `load_global_config`, so a whitespace-only value is blank. The shadow decision (check 3) reads only the map:
     a key present with layer `env` or `.env` means shadowed; layer `config.toml` or absent means not
     shadowed. Blankness is already encoded, so no code path consults `os.environ` for the decision.
   - **Construction.** First snapshot the non-blank `PIPECAT_HUB_*` keys already in `os.environ` as
     layer `env`, because neither loader records a key it skips. Then each loader returns the keys it
     actually set, mirroring its own rule, which differ today: `load_cwd_dotenv` uses presence
     (`key not in os.environ`, so a blank real env var hides a `.env` value), `load_global_config` treats
     a blank as absent. After the merge below, `load_env_layers` drops every key whose final value is
     blank. So a blank real env var, a non-blank `.env` value and a `config.toml` value resolve to
     `config.toml`, a blank `.env` value alone is omitted, and `repos add` refuses in neither case.
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

## Phases (index)

The phase text lives in the two child plans, so there is one copy to keep in step. Phase 6 (dogfood) is
in *Delivery* below because it needs both PRs. PR 2 runs in the order 3a, 3b, 3c, 4, 5, 4b: 4b is last because
it needs PR 1's real `repos` command (see the PR 2 gating procedure in *Delivery*).

| Phase | Where | PR |
|---|---|---|
| 1 Catalog, loader, renderers, generated sections | PR 1 plan, Phase 1 | PR 1 |
| 2 `repos list|add|remove` (2a `tomlkit`, 2b `env_loading` seam and provenance, 2c writer, 2d commands) | PR 1 plan, Phases 2a to 2d | PR 1 |
| 3 `get_hub_status` indexed repos (3a protocol, 3b predicates, 3c providers) | PR 2 plan, Phases 3a to 3c | PR 2 |
| 4 Corpus hints | PR 2 plan, Phase 4 | PR 2 |
| 5 Catalog health and curation scripts, weekly workflow | PR 2 plan, Phase 5 | PR 2 |
| 4b Seam test against the real `repos` (runs last in PR 2, after the rebase onto PR 1) | PR 2 plan, Phase 4b | PR 2 |
| 6 Dogfood migration | Delivery below | after both |
| (deferred) `search_repo_catalog` + `repos search` | Non-goals | none |

## Delivery

This section is the cold-start brief. A session that has lost its context reads this, then the child
plan for its worktree, then the parent decisions that plan names.

### Why two PRs, and the order

`repos` (PR 1) merges before hints (PR 2). The hint's `add_command` points users at `repos add`; if hints
reached `main` first, a release cut between the merges would ship a hint naming a command that does not
exist. In this order every state on `main` is coherent. PR 2 depends only on PR 1's catalog loader
(Phase 1) and, for the seam test, on the real `repos` command.

### Topology: separate top-level sessions, not nested subagents

Do **not** have this session spawn two `Agent` subagents that each run `/conduct`, `review-gauntlet` and
`/update-docs`. Those skills spawn their own subagents (conduct: implementer and test-writer; the
gauntlet: its `deep-review` lens fan-out, which runs "at its own top level" and forbids nesting). Under an
`Agent` subagent they would sit two or three levels deep and the lens fan-out would not be top-level.
`fan-out`'s own doctrine says the safe shape is a subprocess boundary: `fan-out -> (subprocess) -> conduct`.
So each worktree gets its **own top-level `claude` session**, started by the maintainer in a terminal (or by
a `claude -p` background process, which is unverified here: conduct hands back between phases unless
`--autonomous`, so check conduct's `SKILL.md` before relying on headless).

This session is the **monitor**: it creates the worktrees, reads each worktree's state files read-only,
helps resolve cross-PR questions, does the PR 2 rebase and the dogfood. It does not edit files in a
worktree while that worktree's conduct is running (conduct's rogue-commit check hard-stops on foreign
commits).

`/conduct` cannot run on this parent plan, and has no phase-range option (`--max-phases N` only caps a
count), which is why there are two child plans.

### Sign-off before any conduct run

Each child plan was derived from this reviewed parent but is itself unreviewed text, and a fresh
`/conduct` run hard-stops on a missing or stale review marker. Run `/skein:review-plan <child>` on each
(or `waive`, with the reason "derived from the signed-off parent; phase text only"). That is a
maintainer decision, not something a worker does.

### Branches and worktrees

| Name | Branch | Created from | Worktree dir |
|---|---|---|---|
| planning / monitor | `feature/repo-catalog` (this branch; holds only plan commits) | `main` | this checkout |
| PR 1 | `feature/repo-catalog-pr1` | `feature/repo-catalog` tip | `../pipecat-code-mcp-pr1` |
| PR 2 | `feature/repo-catalog-pr2` | PR 1's **Phase 1 commit** (`commit_sha` in `.conduct/state-*.json`) | `../pipecat-code-mcp-pr2` |

```
git worktree add ../pipecat-code-mcp-pr1 -b feature/repo-catalog-pr1 feature/repo-catalog
# later, once PR 1 reports Phase 1 complete:
git worktree add ../pipecat-code-mcp-pr2 -b feature/repo-catalog-pr2 <PR1 phase-1 commit_sha>
```

Each worktree needs its own `uv sync` (own `.venv`). The gitignored `.env` is not in a worktree, which is
what you want. Sibling worktrees resolve distinct conduct state files (`git rev-parse --show-toplevel`).
Never run `refresh` against the real `~/.pipecat-context-hub` from a worktree; real-data runs happen
after both PRs merge, against a copy.

No squash merges (maintainer rule): PR 2's branch shares PR 1's early commits and the plan commits as
ancestors, so after PR 1 merges (`gh pr merge --merge`), `git rebase main` on PR 2 drops the duplicates
cleanly.

### Runbook per worktree (the same sequence in each)

1. Start a top-level session in the worktree: `cd ../pipecat-code-mcp-pr1 && claude`.
2. `/skein:conduct docs/dev_plans/<child plan>` (flags worth setting: `--run-ci-parity` with `--ci-cmd`
   set to the AGENTS.md gate, `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
   && uv run mypy src/ tests/ && uv run pytest tests/ -q`; confirm the flag semantics in conduct's
   `SKILL.md` first). Conduct commits at each phase boundary and, because the plan says
   `Review Gates: full`, invokes `review-gauntlet` after the last phase.
3. After the gauntlet converges: `/update-docs` (nothing chains it; it also reconciles
   `docs/dev_plans/README.md` and the changelog).
4. `/code-review xhigh --fix` is human-typed only (the harness blocks Claude from invoking it). The
   standing preference is to run it before the gauntlet, but the gate auto-chains, so the practical order
   is: type it after the gauntlet converges, then re-run the full suite. A deviation to confirm per run.
5. Push the branch, open the PR (no attribution lines), run the AGENTS.md pre-merge live smoke items
   that apply to that PR, update the PR description with `gh pr edit`, merge with
   `gh pr merge --merge --delete-branch` only after the maintainer says so.

**PR 2 gating (Phase 4b needs PR 1's real `repos`).** PR 2 runs 3a, 3b, 3c, 4, 5, then 4b last.

1. Run `/skein:conduct <PR 2 plan> --autonomous --max-phases 5` so it walks Phases 3a to 5 and stops. conduct's
   `SKILL.md` (0.10.0) defines `--max-phases N` as a cap on total phases walked in autonomous mode and
   hard-stops when the completed count reaches `N`; it has no range or skip semantics, so it caps count only.
2. When it hands back, rebase `feature/repo-catalog-pr2` onto `feature/repo-catalog-pr1` once PR 1's Phase 2d
   commit exists (or onto `main` after PR 1 merges). Log it in the Handoff log table.
3. Run `/skein:conduct <PR 2 plan> --resume --run-ci-parity --ci-cmd '<gate>'` for Phase 4b, then the gauntlet
   auto-chain. `<gate>` is the AGENTS.md gate from the runbook. conduct's `SKILL.md` (0.10.0) lists both flags
   in one flat argument-hint beside `--resume` and gives no per-flag restriction. It does not persist them in
   state, so a legacy `--resume` run needs them again to get the CI-parity gate.

Verified in `SKILL.md`: `--resume` refreshes `state.resume_base_sha` to the current `HEAD`, and each
completed phase's `commit_sha` is immutable. So the rebase leaves those stored `commit_sha` values stale,
and the rogue-commit check uses the refreshed `resume_base_sha` instead. Not verified: whether conduct
reads the stale `commit_sha` values anywhere else after a rebase; check the first `--resume` hand-back.

If a session is interrupted: `/skein:conduct <child plan> --resume`; the gauntlet resumes from its
`.gauntlet/` ledger (`--last-decision`). Never restart either from scratch. State lives at
`<worktree>/.conduct/state-<plan-id>.json`; this session can read it with `jq` and must not write it.

### Frozen contracts between the PRs

These are the only two seams. Both PRs are written against them; change one only by editing this section
and both child plans in the same commit.

- **C1: `add_command`.** In PR 2's hint, `add_command` is a string made of one command per matched group
  that has at least one repo that is neither configured nor tainted: `pipecat-context-hub repos add
  --config-only --group <group-name>`, joined with `; `. Plain `repos add` is refused when a real env var or
  cwd `.env` sets `PIPECAT_HUB_EXTRA_REPOS` (Decision 7 check 3), and the hint must work for that user,
  the maintainer's own `.env`. `--group` skips repos already in `config.toml`, tainted repos and
  `parseable = false` repos, so the command is always accepted. It acts on the whole group, so it may add repos beyond the ones the
  hint named (for example an indexed repo that was removed from the config without `refresh --prune`).
  Catalog slugs use `--group`, never `--unlisted`; the `--unlisted` form appears only in the agent-assist
  text. (This format is chosen here, not earlier; the review of this section may revisit it.)
- **C2: configured state.** PR 2 reads configured and tainted repos only through
  `config.sources.effective_repos` and `config.sources.tainted_repos` at process start. It never reads
  `config.toml` directly. PR 1 guarantees that after `repos add X` (without `--config-only` under a
  shadowing layer) a new process's `effective_repos` contains X. PR 2's Phase 4b tests exactly this.
- **C3: catalog API.** `load_catalog() -> Catalog | None` and the group/repo fields of parent Decision 2,
  as implemented by PR 1's Phase 1. PR 1 also names the test seams: pure `parse_catalog(text) -> Catalog`
  (raises `CatalogError`) and module-level `_read_catalog_text() -> str`. `load_catalog` is `functools.cache`
  around try/except over both; tests call `load_catalog.cache_clear()`. PR 2's tests patch `_read_catalog_text`.
  PR 2 does not change the schema; if it needs a field, stop, amend
  PR 1's Phase 1 on its branch, and rebase PR 2.

### File ownership and expected conflicts

| File | PR 1 | PR 2 | Resolution |
|---|---|---|---|
| `shared/env_loading.py`, `shared/config_writer.py`, `cli_repos.py`, `shared/repo_catalog*.py`, `data/repo_catalog.toml` | owns | reads only | none |
| `shared/corpus_hint.py`, `server/tools/get_hub_status.py`, `shared/interfaces.py`, `services/index/reopening.py`, `scripts/*catalog*`, `catalog-health.yml`, `retrieval-quality.yml` | none | owns | none |
| `cli_query.py` | `_EXIT_BAD_INPUT` to `EXIT_INVALID_INPUT` (import and constant) | `_QueryRuntime`, hint call sites | different regions; PR 2 rebases onto PR 1 |
| `tests/unit/test_cli_query.py` | Phase 2d rewrites its line-16 `_EXIT_BAD_INPUT` import | Phases 3c and 4 extend the same module | keep both sides on rebase |
| `cli.py` | `register_repos_commands`, `ctx.obj` provenance | provider wiring in `serve` | PR 2 rebases |
| `.github/workflows/ci.yml` (Windows file list) | catalog, writer, repos test modules | `test_corpus_hint.py`, `test_check_repo_catalog.py` (`test_repos_hint_seam.py` is not listed: it opens a real `IndexStore`, which fails closed on Windows, and carries a POSIX `skipif`) | PR 2 rebases and keeps both |
| `docs/smoke-test.md` | Phase 2d adds numbered live-smoke items (53, and 54 from review) | Phase 4 adds another numbered after the highest existing item (expect 55) | keep both; renumber on rebase |
| `docs/configuration.md`, `AGENTS.md` layout, changelog, `justfile` | both edit | both edit | `/update-docs` per PR; PR 2 rebases |
| `pyproject.toml`, `uv.lock` | `tomlkit` | none | none |

### Integration (small, and inside PR 2)

There is no separate integration branch. When PR 2 is rebased onto `main` (PR 1 merged), the integration
checks are: the Phase 4b seam test (offline, deterministic); the shadow-layer rows of the `repos` table
still passing against merged code; the pre-merge live smoke at PR 2's creation (its numbered
`docs/smoke-test.md` item); one gauntlet pass over the merged diff, scoped to the seams in this
section. Findings from those that belong to PR 1 code are fixed as follow-up commits on PR 2, never by
reopening PR 1.

### After both PRs merge: Phase 6, dogfood migration

Done by the maintainer in the main checkout, in this order. The repo-root `.env` sets `PIPECAT_HUB_EXTRA_REPOS`, so every
   add needs `--config-only` (the hint's `add_command` carries it for the same reason, contract C1). (i) Catalog slugs: `repos add --config-only <slug>` per slug (not `--group`, which skips
   `parseable = false` repos such as the two iOS demos the `.env` holds). (ii) The slugs that are not in
   the catalog (11 today; three are private: `nvidia-sgmaker`, `pipecat-krisp`, `hosted-smart-turn`,
   which clone with the maintainer's own credentials): derive them by diffing the `.env` list against
   the catalog, then `repos add --config-only --unlisted <slug>`; `repos list` afterwards must tag
   exactly those `unlisted`. (iii) Three slugs in the `.env` are `SourceConfig.repos`
   defaults (verified 2026-10-17); `repos` refuses them by design, so skip them and record the refusals. (iv) Target:
   `config.toml` holds the `.env` list minus the defaults (44 slugs today) and `effective_repos` is the
   defaults plus that list; confirm both counts rather than "the 47-repo list". Then delete the `.env`
   line, run `repos list`, then `refresh`. `.env.backup` exists. Record anything awkward.

### Handoff log (fill in as work proceeds; this is what survives a cleared context)

| Item | Value |
|---|---|
| PR 1 plan signed off (`/review-plan` or waive) | 2026-10-17, `/review-plan` (marker in the PR 1 plan) |
| PR 2 plan signed off | |
| PR 1 worktree started / conduct state file | |
| PR 1 Phase 1 `commit_sha` (PR 2 branches from here) | |
| PR 2 worktree started / conduct state file | |
| PR 1 gauntlet converged / `/update-docs` done / PR number | |
| PR 1 merged (merge commit) | `5733f06` (PR #144) |
| PR 2 rebased onto PR 1 (before Phase 4b) / onto `main` | 2026-10-17, onto `main` (`5733f06`, PR #144 merged); run by the user (`git rebase origin/main`). Conflicts kept both sides: parent plan smoke note, `ci.yml` Windows file list, `test_cli_query.py`, `cli_query.py` (`attach_corpus_hint` + PR 1's `echo_json`), `docs/smoke-test.md` (PR 2 item renumbered 53 -> 55). Backup `backup/pr2-before-rebase-20261017` (`bcac0b9`); new tip `8f73c7f` |
| PR 2 gauntlet converged / `/update-docs` done / PR number | |
| PR 2 merged (merge commit) | |
| Dogfood migration done | |

## Test plan

- **[PR1]** Catalog: schema, unique slugs, a `private`/`archived`/`status` key is a schema error (visibility
  itself is only caught by the health check), static cross-group invariants (alias collision, a slug
  in two groups, a default repo, bad `kind`/`parseable`/`verified`), a tainted slug in the environment
  does **not** break loading, a catalog that fails to load returns `None` and logs once across repeated calls (PR 1 asserts only this
  loader contract; the consequences are **[PR2]** assertions: hints are disabled without failing a tool
  call, `catalog_groups_not_indexed == []` on both doors, and the CLI still prints its stderr hint);
  generated `.env.example` block and docs section equal the checked-in files, the `importlib.resources` catalog path
  is a file, one group per vendor (no catch-all group), the `just catalog-regen` recipe exists. Wheel inclusion is
  proven by Phase 1's validation command only, not by a pytest: an editable install resolves to the source tree.
- **[PR2]** Classifier (offline, injected responses): ok, 404, archived, 301, a real rename (`full_name` differs
  beyond case, new name reported), case-only name difference; inconclusive for 403/429/5xx/timeout/
  network error (never labelled inaccessible); the 90-day advisory boundary; `GITHUB_TOKEN` sent as an
  `Authorization` header when set and omitted when not; a test that `catalog-health.yml` invokes the
  script (the `test_audit_sync.py` pattern); the frozen stale fixtures are the real 2026-10-16 slugs; the script's aggregate
  verdict and exit code over a mixed injected result set (ok with an old `verified` date exits 0;
  stale exits nonzero; inconclusive-only exits nonzero in its own section); `follow_redirects=False`
  asserted by routing a 301 through `httpx.MockTransport`; `curate_repo_catalog.py` leaves the catalog
  byte-identical.
- **[PR1]** Provenance: per-key map (the layer of the final non-blank value; blank or absent keys omitted)
  for each layer, including a key set only in the real environment (layer
  `env`), a blank higher-layer value treated as absent (mirror `TestBlankHigherLayerValueDoesNotShadow`),
  plus the row blank real env var + non-blank `.env` + `config.toml` (provenance `config.toml`, no
  refusal), a whitespace-only real env var alone (key omitted, no refusal), a blank `.env` value alone
  (key omitted, no refusal), and the overlap row (a key set blank by `.env` and then by `config.toml`:
  the merged layer is `config.toml`). Blank means empty after `.strip()`. Shadow-decision tests build
  the map directly or patch `env_loading.load_env_layers`, never `os.environ`. The `ctx.obj["env_provenance"]` carrier: through `main()` both loaders run once per `repos`
  invocation. A second test patches `env_loading.load_env_layers` to return a controlled map and asserts neither
  loader is called: that map alone drives the shadow decision (no layer is re-read).
- **[PR1]** Writer: round trip through `parse_global_config` and `load_global_config` for no file, empty file,
  comments preserved, other keys byte-identical, CSV value, non-array/non-string value refused,
  symlinked config, `PIPECAT_HUB_CONFIG_FILE`, non-regular, world-writable and foreign-owned refusal,
  mode kept or created 0600, directory created 0700, and a failed re-parse leaving the original
  untouched, the containing-directory and symlink-hop refusals, an existing file that is not valid
  TOML refused with the file untouched, an untrusted file refused, and no resolvable config path refused. Each ConfigNotTrusted predicate is also run through
  the reader and the writer (one shared case tuple). POSIX-only cases are skipped on Windows, where a round-trip test asserts the mirror-the-reader
  contract.
- **[PR1]** `repos` table: layers (env var / cwd `.env` / blank `.env` / `config.toml` only / none) x (add /
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
  call. Also: `load_catalog()` returning `None` (positional `add`/`remove` and `--group` exit 1, file untouched;
  `repos list` tags non-default slugs `unlisted` and logs one warning), and `repos list` on a bad `config.toml`
  (untrusted, invalid TOML, no resolvable path: prints the effective list, exits 0).
- **[PR2]** Indexed state for `get_hub_status`: indexed, `configured_no_records`, not configured; case-variant
  slugs match; the synthetic `docs.pipecat.ai` key is dropped; `catalog_groups_not_indexed` for fully, partly and `parseable = false`-only groups;
  `get_counts_by_repo` raising yields no hint and no crash (mirror `test_store_errors_never_break_the_response`);
  the count read through `ReopeningIndexStore`, including after a reopen (a stale `serve` hints
  correctly after a `refresh`). `indexed_repos` returns `None` on an unreadable count,
  `groups_not_indexed(catalog, None)` returns `[]`, and `corpus_hint` returns `None` (no false hint), per
  Decision 10.
- **[PR2]** Hints: frozen real queries, the four 2026-10-17 unindexed-vendor queries and the AWS Nova Sonic
  query, copied verbatim into the test file and reused for the new numbered smoke item (a vendor question with the repo unindexed must hint, the same with it
  indexed must not); a 0.95-confidence response still hints; alias negatives ("laws" must not match
  "aws"); `TTS + nvidia` and `TTS & nvidia`; an uppercase alias; `parseable = false` and tainted
  entries skipped (a tainted catalog repo appears in neither `repos_not_indexed` nor `add_command`, on
  both doors); a configured-but-empty repo lands in `repos_configured_no_records`; the
  `corpus_hint` key is absent (not null) when it does not apply; a golden `corpus_hint` object per
  door (`add_command` is one `pipecat-context-hub repos add --config-only --group <group>` per matched group with an unconfigured, untainted repo, joined with `; `, and is omitted when none; `refresh_command` appears
  only with a configured-but-empty repo; `report_issue_url` equals `RETRIEVAL_QUALITY_ISSUE_URL`);
  `configured_repos` reflects process start (a later `repos add` does not change a running hint); `get_code_snippet` hints from
  `symbol`/`intent` and not from `path` alone; a partially indexed group lists only the missing repos
  in `repos_not_indexed`; multi-group queries give catalog-ordered `matched_groups`; the hint coexists
  with the staleness footer; non-search tools never hint; both front doors give the same
  `corpus_hint`; the CLI stderr hint is suppressed when `corpus_hint` is present (the hint is computed
  before `_maybe_warn_poor_results` runs); `_SERVER_INSTRUCTIONS` pins the `corpus_hint`, `--unlisted` and draft-not-file wording (the
  `tests/unit/test_server.py` pattern) and `retrieval-quality.yml` carries the *Candidate repos* field.
- **[PR1]** Mutation checks: for each of these, revert it and confirm its test fails: the shadow refusal,
  the default/tainted refusal, the check order (default/tainted before shadow before no-op), the writer's
  failed-re-parse abort, the `--unlisted` rules, the casefold in the `repos add` no-op, and the
  provenance overlap merge.
- **[PR2]** Mutation checks: the alias word boundary, the `parseable = false` skip, the tainted skip in
  the hint, the catalog-load-failure fallback, the casefold in `indexed_repos`, the CLI stderr-hint
  suppression, and the `add_command` format (contract C1, including `--config-only`).
- **Mutation-check assignment:** each [PR1] and [PR2] mutation check above is assigned to a phase in the
  child plans; see PR 1 and PR 2 plans for the per-phase list.
- **[after both]** Real data: run against a copy of `~/.pipecat-context-hub` and the maintainer's real `config.toml`;
  expected observations: `repos list` shows the effective list with every slug tagged, the catalog
  loads, and `status` reports `catalog_groups_not_indexed` consistent with the index.
- **[PR1]** `repos --help` and `repos add --help` through the Typer bridge match the direct CLI, and a
  functional `pipecat context-hub repos list` plus `repos add --config-only` run through the bridge
  (guards a `ctx.obj["env_provenance"]` gap only on that door).
- **[PR1] and [PR2]** Live MCP smoke at each PR's creation (AGENTS.md pre-merge checklist), each with its own numbered item: PR 1 covers `repos`, PR 2 covers the hint on the live index. The end-to-end scenario (`repos add`, `refresh`, hint disappears) is run once after PR 2 merges.

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

<!-- reviewed: 2026-10-17 @ 6a383e6a8d24be2081e355278f5190224a3c1d6a -->

## Progress

Conduct tracks phase progress in each child plan's own `## Progress`. This list tracks the PRs.

- [ ] Child plans signed off (PR 1: 2026-10-17; PR 2: pending)
- [ ] PR 1 merged (catalog, generator, `repos`)
- [ ] PR 2 merged (indexed-repo status, hints, health check)
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
