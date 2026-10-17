# Repo catalog, `repos` command and corpus hints

**Branch:** `feature/repo-catalog` (not yet cut) | **Review Gates:** full | **Component:** config | **Status:** Planned (design agreed in conversation 2026-10-16; open questions below must be settled before code)

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
3. **Hints**: when a result is low-confidence or empty and the query matches a catalog entry whose
   repos are not indexed, say so and name the command.
4. **Catalog health check** that catches private/archived/renamed entries (this would have caught the
   eleven stale entries removed on `docs/env-example-repo-coverage`).

## Non-goals

- No network access during tool calls (the docs promise none). Hints come from the static catalog.
- No automatic indexing by an agent. The agent proposes; the user runs or approves the command.
- No change to the default repo list, and no support for non-Python/TS/RST parsing.

## Design decisions (settled)

1. **Catalog is data, not prose.** Shipped inside the package (so `uv tool` installs have it).
   `.env.example`'s bundles and the docs table are *generated* from it; a test fails if the generated
   output differs from the checked-in file.
2. **`config.toml` is the write target** for `repos add/remove`. A cwd `.env` is never edited.
   `repos list` shows the **effective** list and, per repo, which layer supplied it, and warns when a
   higher layer's `PIPECAT_HUB_EXTRA_REPOS` shadows the file it just edited.
3. **Agent proposes, user approves.** `repos add` is the approval point and is interactive or takes
   an explicit `--yes`; the server instructions tell agents to *suggest* the command, never to run it.
   Reason: a repo that is added is cloned and indexed, and the index feeds later answers, so a
   prompt-injected page steering an agent into adding an attacker's repo is an index-poisoning path.
   Same trust posture as the PR #121 restriction of cwd `.env` to `PIPECAT_HUB_*` keys.
4. **Catalog entries only by default.** Arbitrary `org/repo` slugs need an explicit flag, are
   syntax-validated, and trigger the same warnings as above. Entries carry `private`, `archived`,
   `last_verified`, and a `parseable` flag (Swift/Kotlin/C++ repos yield zero chunks).
5. **Design-first, not point patches** (see the 2026-10-16 PR #139 lesson): each phase states its
   contract and verifies the whole state space, not the reported case. The contract here is
   *`repos add X` then `repos list` shows X, `effective_repos` contains X, and `repos remove X` undoes
   it*, over every precedence layer combination.

## Open questions (settle with `/skein:grill` before coding)

- **Writing TOML.** The code reads with `tomllib` (read-only). There is no TOML writer in
  `pyproject.toml`. Options: add `tomlkit` (preserves the user's comments, a new dependency, and the
  dependency review gate), or own a clearly marked managed block in `config.toml`. Decide.
- **Catalog format and location**: TOML vs JSON, `src/pipecat_context_hub/data/`, how it is packaged
  with hatchling, and how it is validated at import.
- **Hint trigger.** Keyword/alias match on vendor and topic names (cheap, predictable, can miss) vs.
  embedding similarity to catalog descriptions (more recall, more surprise). Needs real queries
  frozen into tests and a smoke-test item (AGENTS.md: freeze live failures twice).
- **Hint placement**: MCP `evidence` field, the response footer next to `index_staleness`, or both;
  and CLI parity (`cli_query.py` already adds a report hint for low-confidence/empty results).
- **`repos` as a click group vs the Typer bridge.** `plugin.py` mounts one passthrough command per
  top-level click command and `test_every_click_command_is_bridged` compares names. Confirm a group
  with subcommands passes raw argv through correctly and that `--help` stays the real one.
- **Should `repos add` run `refresh`?** Probably print the command and let the user run it
  (refresh takes the exclusive lock and can take minutes).
- **Health-check home.** Extend `.github/workflows/smoke-drift.yml` (live network, every 5 days) or a
  separate job; needs `gh`/token, and must not make PR CI depend on GitHub availability.

## Phases

1. **Catalog + generator + health check.** Schema, loader, validator; initial data from the
   2026-10-16 audit (51 verified slugs); generator for `.env.example`; staleness job.
2. **`repos list|add|remove`.** `config.toml` read/modify/write (atomic, comment-preserving per the
   decision above), effective-list view with layer attribution, shadowing warning, exit codes per
   `shared/exit_codes.py`, `--vendor`/`--topic` selection. Docs in `docs/configuration.md`.
3. **`get_hub_status` reports indexed repos** and which catalog repos are not indexed.
4. **Corpus hints** on low-confidence/empty results, MCP and CLI, plus the server-instructions text.
5. **Dogfood migration**: move the maintainer's `.env` (`.env.backup` already taken 2026-10-16) into
   `config.toml` using the new commands; record anything that was awkward.

## Test plan

- Catalog: schema, unique slugs, no `private` entry reachable from a public bundle, generated
  `.env.example` equals the checked-in file.
- `repos` round-trip table over layers (env var set / cwd `.env` set / `config.toml` only / none) x
  (add / remove / list), asserting `SourceConfig.effective_repos` and the shadowing warning.
- Mutation checks: revert each fix and confirm its test fails.
- Real data: run against a copy of `~/.pipecat-context-hub` and the maintainer's real `config.toml`.
- Hints: frozen real queries (a vendor question with the vendor repo unindexed must hint; the same
  query with it indexed must not; an unrelated empty result must not).
- Live MCP smoke at PR creation (AGENTS.md pre-merge checklist), with a new numbered item.

## Already done (this branch, before the plan)

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
