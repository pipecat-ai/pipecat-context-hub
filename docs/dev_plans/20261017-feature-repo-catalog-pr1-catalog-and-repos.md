# Repo catalog PR 1: catalog, generator and the `repos` command

**Branch:** `feature/repo-catalog-pr1` | **Review Gates:** full | **Component:** config | **Status:** Planned (derived from the reviewed parent plan; needs its own `/review-plan` sign-off before `/conduct`)

## Objective

Land the first of two PRs from the parent design
[`20261016-feature-repo-catalog-and-repos-command.md`](20261016-feature-repo-catalog-and-repos-command.md):
the checked-in repo **catalog** with its generated `.env.example` block and docs section, and the
`pipecat-context-hub repos list|add|remove` command that edits `~/.config/pipecat-context-hub/config.toml`.
PR 2 (hints, `get_hub_status` fields, health check) is a separate plan and merges after this one.

## Context

**Required reading before any phase, in this order:** the parent plan's *Delivery* section (the
runbook, the frozen contracts and the file ownership), then its Decisions 1, 2, 7, 8, 9 and 10, its
*Test plan* bullets labelled `[PR1]`, and its *Facts resolved* list. The parent is the single source for
behaviour; this file does not restate it. If this file and the parent disagree, stop and hand back, do
not pick one.

Facts a phase worker needs and cannot cheaply rediscover:

- Config layering is `PIPECAT_HUB_*` real env var > cwd `.env` > `~/.config/pipecat-context-hub/config.toml`
  > field defaults, first-writer-wins per key, via `shared/env_loading.py:load_env_layers()`. Do not
  hand-replicate the two loader calls in a new entry point (a source-parity test in
  `tests/unit/test_config.py` fails if you do).
- `SourceConfig.repos` holds the 8 default repos; `config.sources.effective_repos` is defaults plus
  `PIPECAT_HUB_EXTRA_REPOS` minus tainted repos; `config.sources.tainted_repos` is a property.
- `tomlkit` is not in `uv.lock`. `shared/exit_codes.py` has no `EXIT_INVALID_INPUT`; `cli_query._EXIT_BAD_INPUT = 1`
  exists and `tests/unit/test_cli_query.py:16` imports it.
- Tests import package modules only (no `scripts/` imports). The Windows CI job lists test files by name
  in `.github/workflows/ci.yml`; a listed file that does not exist makes pytest exit 4, so a test module
  joins that list in the same commit that creates it.
- Never `git add -A`; stage explicit paths. Work only on this branch.

## Requirements

1. The catalog, loader, renderers and equality test of parent Decision 2 and Phase-1 material below.
2. `tomlkit` as a runtime dependency that resolves alongside `pipecat-ai[cli]`.
3. The `env_loading` seam of parent Decisions 8 and 9: `read_trusted_config`, `parse_global_config`,
   `ConfigNotTrusted`, and the layer provenance map exposed through `load_env_layers` and
   `ctx.obj["env_provenance"]`.
4. The writer (parent Decision 8) and the `repos` command (parent Decision 7), with
   `EXIT_INVALID_INPUT` shared in `shared/exit_codes.py`.
5. Every parent *Test plan* bullet labelled `[PR1]` has tests; every mutation check labelled `[PR1]` is
   run (revert it, confirm its test fails).

## Review Focus

- `shared/env_loading.py` is security-sensitive (PR #121 closed a cwd-module-shadowing RCE and a cwd
  `.env` key-allowlist hole here). The writer must not reimplement a trust check the reader applies.
- The provenance map must mirror each loader's *own* blank rule (parent Decision 9); the overlap row is
  the likely bug.
- The `repos` check order (parent Decision 7 table) is an invariant across `add`, `remove` and `--group`.

## Implementation Checklist

### Phase 1: Catalog, loader, renderers and generated sections

**Goal:** one catalog file is the single source for `.env.example`, a docs section and (later) hints; a test fails when they drift.
**Impl files:** `src/pipecat_context_hub/shared/repo_catalog.py`, `src/pipecat_context_hub/shared/repo_catalog_render.py`, `src/pipecat_context_hub/data/repo_catalog.toml`, `.env.example`, `docs/configuration.md`, `justfile`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_repo_catalog.py`
**Test command:** `uv run pytest tests/unit/test_repo_catalog.py tests/unit/test_config.py -v`

- Schema and `load_catalog() -> Catalog | None` per parent Decision 2 (static cross-group invariants,
  `private`/`archived`/`status` keys rejected, failure returns a cached `None` and logs once).
  `shared/repo_catalog.py` imports nothing from `cli`, `server` or `services`.
- Initial data: every non-default public slug in the current `.env.example` (the equality test pins the
  count; hardcode no number). Group by vendor or topic as in parent Decision 2.
- `shared/repo_catalog_render.py` rewrites a delimited generated block inside `.env.example` (header
  prose and non-repo settings stay outside the markers) and a generated catalog section in
  `docs/configuration.md`, separate from the env-var table that `TestConfigTomlExampleParity` pins.
- `just catalog-regen` (runs the renderer as a module) with `--bump-verified`.
- The equality test, the regenerated `.env.example` and the docs section land in this one commit; the
  catalog test module joins the Windows file list in `ci.yml` here.

### Phase 2a: `tomlkit` dependency

**Goal:** `pip-audit` and the SBOM see `tomlkit` before any code imports it.
**Impl files:** `pyproject.toml`, `uv.lock`
**Test files:** none (dependency change)
**Test command:** `uv lock --check && uv run python -c "import tomlkit"`
**Validation cmd:** `uv pip install --dry-run . "pipecat-ai[cli]"`

- Add `tomlkit` to `[project.dependencies]` as the first commit of the repos work. Record the resolved
  versions from the validation command in the PR description. `pipecat-ai` is not in `uv.lock`, so
  `uv lock` alone proves nothing.

### Phase 2b: trusted-config read, parse seam and provenance

**Goal:** the writer and the reader share one trust check, and `repos` can tell which layer is in force.
**Impl files:** `src/pipecat_context_hub/shared/env_loading.py`, `src/pipecat_context_hub/cli.py`
**Test files:** `tests/unit/test_env_loading.py`, `tests/unit/test_config.py`
**Test command:** `uv run pytest tests/unit/test_env_loading.py tests/unit/test_config.py -v`

- Extract public `read_trusted_config(path) -> str | None` and `parse_global_config(path) -> dict[str, object]`,
  and `ConfigNotTrusted(reason)`; `load_global_config` becomes a wrapper (parent Decision 8). The trust
  helpers stay private.
- Add the provenance map (parent Decision 9): `env` snapshot, per-loader returned keys mirroring each
  loader's own blank rule, merge `{**env, **dotenv, **global_config}`; `main()` stores it in
  `ctx.obj["env_provenance"]`. The return value is optional so dashboard and smoke scripts are untouched.
- Update `test_load_env_layers_calls_both_loaders_in_order` (it patches both loaders with None-returning
  lambdas).

### Phase 2c: the writer

**Goal:** an atomic, comment-preserving edit of the one key that cannot write a file the reader would ignore.
**Impl files:** `src/pipecat_context_hub/shared/config_writer.py`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_config_writer.py`
**Test command:** `uv run pytest tests/unit/test_config_writer.py -v`

- Implement parent Decision 8 in full: path resolution and symlink handling, `read_trusted_config` for
  the existing file, edit only `PIPECAT_HUB_EXTRA_REPOS` as a native array, refusals, temp-file
  re-parse through `parse_global_config`, atomic rename, modes 0600/0700, no lock.
- Add the writer test module to the Windows file list in `ci.yml` in this commit. Windows mirrors the
  reader; POSIX-only tests are skipped there and a round-trip test runs on Windows.

### Phase 2d: the `repos` commands

**Goal:** `repos add X` then `repos list` shows X, `effective_repos` contains X, and `repos remove X` undoes it, across every layer combination (parent standing contract, with its `--config-only` qualification).
**Impl files:** `src/pipecat_context_hub/cli_repos.py`, `src/pipecat_context_hub/cli.py`, `src/pipecat_context_hub/shared/exit_codes.py`, `src/pipecat_context_hub/cli_query.py`, `docs/configuration.md`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_cli_repos.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_plugin.py`
**Test command:** `uv run pytest tests/unit/test_cli_repos.py tests/unit/test_cli_query.py tests/unit/test_plugin.py -v`

- `cli_repos.py` with `register_repos_commands(main)` (the `cli_install.py` pattern); the parent
  Decision 7 check-order table, `--group`, `--unlisted`, `--config-only`, `repos list` tags and the
  consulted-cwd line.
- Add `EXIT_INVALID_INPUT = 1` to `shared/exit_codes.py` and migrate `cli_query._EXIT_BAD_INPUT` to it in
  this commit, including the import at `tests/unit/test_cli_query.py:16`; add a shared-constant pin test
  (mirror `test_refresh_in_progress_exit_code_is_shared`).
- Docs in `docs/configuration.md`; a `repos --help` Typer-bridge test plus a functional
  `pipecat context-hub repos list` and `repos add --config-only` run through the bridge.
- The `repos` test module joins the Windows file list in `ci.yml` in this commit.

## Progress

- [ ] Phase 1: Catalog, loader, renderers and generated sections
- [ ] Phase 2a: `tomlkit` dependency
- [ ] Phase 2b: trusted-config read, parse seam and provenance
- [ ] Phase 2c: the writer
- [ ] Phase 2d: the `repos` commands

## Findings

### Review Waivers

_(none yet; this plan has not been through `/review-plan`)_
