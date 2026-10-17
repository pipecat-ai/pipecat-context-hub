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
*Test plan* bullets labelled `[PR1]`, and its *Facts resolved during the interview* list. The parent is
the single source for behaviour; this file does not restate it. If this file and the parent disagree,
stop and hand back, do not pick one.

Facts a phase worker needs and cannot cheaply rediscover:

- Config layering is `PIPECAT_HUB_*` real env var > cwd `.env` > `~/.config/pipecat-context-hub/config.toml`
  > field defaults, first-writer-wins per key, via `shared/env_loading.py:load_env_layers()`. Do not
  hand-replicate the two loader calls in a new entry point. The source-parity test in
  `tests/unit/test_config.py` scans only `dashboard/scripts` and `scripts`, so it does not guard
  `src/` modules; the renderer and `cli_repos.py` need their own tests.
- `SourceConfig.repos` holds the 8 default repos; `config.sources.effective_repos` is defaults plus
  `PIPECAT_HUB_EXTRA_REPOS` minus tainted repos; `config.sources.tainted_repos` is a property.
- `tomlkit` is not in `uv.lock`. `shared/exit_codes.py` has no `EXIT_INVALID_INPUT`; `cli_query._EXIT_BAD_INPUT = 1`
  exists and `tests/unit/test_cli_query.py:16` imports it.
- Rule for NEW test modules: import package modules, never `scripts/` or `sys.path` hacks.
  `tests/unit/test_smoke_scaffold.py` imports `scripts.check_pipecat_drift` today; that is a known exception.
- The Windows CI job lists test files by name in `.github/workflows/ci.yml`; a listed file that does not
  exist makes pytest exit 4, so a test module joins that list in the same commit that creates it.
  `test_cli.py`, `test_env_loading.py` and `test_config.py` are already listed.
- Never `git add -A`; stage explicit paths. Work only on this branch.

## Requirements

1. The catalog, loader, renderers and equality test of parent Decision 2 and Phase-1 material below.
2. `tomlkit` as a runtime dependency that resolves alongside `pipecat-ai[cli]`.
3. The `env_loading` seam of parent Decisions 8 and 9: `read_trusted_config`, `parse_global_config`,
   `ConfigNotTrusted`, and the layer provenance map exposed through `load_env_layers` and
   `ctx.obj["env_provenance"]`.
4. The writer (parent Decision 8) and the `repos` command (parent Decision 7), with
   `EXIT_INVALID_INPUT` shared in `shared/exit_codes.py`.
5. Every parent *Test plan* bullet labelled `[PR1]` has tests. Every `[PR1]` mutation check is run
   (revert it, confirm its test fails) in the phase that owns it: overlap merge in 2b, failed-re-parse
   abort in 2c, the other five in 2d. Record each result in the PR description.

## Review Focus

- `shared/env_loading.py` is security-sensitive (PR #121 closed a cwd-module-shadowing RCE and a cwd
  `.env` key-allowlist hole here). The writer must not reimplement a trust check the reader applies.
- The provenance map must mirror each loader's *own* blank rule (parent Decision 9); the overlap row is
  the likely bug.
- The `repos` check order (parent Decision 7 table) is an invariant for a positional slug. Checks 1 and 2
  apply to a positional slug only. `--group` skips tainted and `parseable = false` members and applies
  checks 3 to 5 in order.

## Implementation Checklist

### Phase 1: Catalog, loader, renderers and generated sections

**Goal:** one catalog file is the single source for `.env.example`, a docs section and (later) hints; a test fails when they drift.
**Impl files:** `src/pipecat_context_hub/shared/repo_catalog.py`, `src/pipecat_context_hub/shared/repo_catalog_render.py`, `src/pipecat_context_hub/data/repo_catalog.toml`, `.env.example`, `docs/configuration.md`, `justfile`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_repo_catalog.py`
**Test command:** `uv run pytest tests/unit/test_repo_catalog.py tests/unit/test_config.py -v`
**Validation cmd:** `d=$(mktemp -d) && uv build --wheel -o "$d" && unzip -l "$d"/*.whl | grep data/repo_catalog.toml`

- Schema and `load_catalog() -> Catalog | None` per parent Decision 2 (static cross-group invariants,
  `private`/`archived`/`status` keys rejected, failure returns a cached `None` and logs once).
  PR 1 asserts only this loader contract; the hint-side clauses of load failure are PR 2's.
- Import boundary: `shared/repo_catalog.py` imports nothing from `cli`, `server` or `services`. A test
  imports it in a subprocess (`python -I -c`) and asserts no such `pipecat_context_hub` key in `sys.modules`.
- `src/pipecat_context_hub/data/` is a new directory. The hatchling wheel must contain the `.toml`:
  test `importlib.resources.files(...)/"data/repo_catalog.toml"` is a file, and run the validation command.
- Initial data: every non-default public slug in the current `.env.example`. Four slugs there are
  already defaults; drop those duplicates. The equality test subtracts the live default list
  (`SourceConfig.model_fields['repos'].default`) before counting; hardcode no number. Group as in parent Decision 2.
- `shared/repo_catalog_render.py` stays pure functions: catalog to block text, and splice of text to
  text. Paths are resolved only at its `python -m` entry. It rewrites a delimited generated block inside
  `.env.example` (header prose and non-repo settings stay outside the markers) and a generated catalog
  section in `docs/configuration.md`, separate from the env-var table that `TestConfigTomlExampleParity` pins.
- The renderer reads defaults from `SourceConfig.model_fields['repos'].default`, never `effective_repos`,
  and never calls `load_env_layers()`. A test sets `PIPECAT_HUB_EXTRA_REPOS` and asserts unchanged output.
- `just catalog-regen` runs the renderer as a module, with `--bump-verified`. The bump is a line-anchored
  text rewrite of the single top-level `verified = <date>` line, re-validated through `load_catalog`.
  The renderer imports no third-party TOML library; `tomlkit` lands in 2a.
- Catalog tests also cover: a recipe-exists test mirroring `tests/unit/test_audit_sync.py`; a tainted
  env slug (`PIPECAT_HUB_TAINTED_REPOS` set to a catalog slug) does not break loading; a caplog test
  calls `load_catalog()` three times on a broken catalog and asserts one log record.
- The equality test, the regenerated `.env.example` and the docs section land in this one commit.
  `test_repo_catalog.py` is Windows-eligible (pure parsing, no permission bits) and joins the
  `ci.yml` Windows file list here; the subprocess and wheel checks carry a POSIX skip if they fail there.

### Phase 2a: `tomlkit` dependency

**Goal:** `tomlkit` is locked, resolves alongside `pipecat-ai[cli]`, and passes the dependency audit before any code imports it.
**Impl files:** `pyproject.toml`, `uv.lock`
**Test command:** `uv lock --check && uv run python -c "import tomlkit"`
**Validation cmd:** `uv pip install --dry-run . "pipecat-ai[cli]" && just audit`

- Add `tomlkit` to `[project.dependencies]` as the first commit of the repos work. Record the resolved
  versions from the validation command in the PR description. `pipecat-ai` is not in `uv.lock`, so
  `uv lock` alone proves nothing.

### Phase 2b: trusted-config read, parse seam and provenance

**Goal:** the writer and the reader share one trust check, and `repos` can tell which layer is in force.
**Impl files:** `src/pipecat_context_hub/shared/env_loading.py`, `src/pipecat_context_hub/cli.py`
**Test files:** `tests/unit/test_env_loading.py`, `tests/unit/test_config.py`, `tests/unit/test_cli.py`
**Test command:** `uv run pytest tests/unit/test_env_loading.py tests/unit/test_config.py tests/unit/test_cli.py -v`

- Extract public `read_trusted_config(path) -> str | None` and `parse_global_config(path) -> dict[str, object]`,
  and `ConfigNotTrusted(reason)`; `load_global_config` becomes a wrapper (parent Decision 8). The trust
  helpers stay private. Add direct tests for all three.
- Parametrized reader-vs-seam equivalence test over every trust predicate: non-regular, foreign-owned,
  world-writable, untrusted parent dir, foreign symlink hop, invalid TOML, missing file.
  `load_global_config` warns and ignores exactly when the seam raises `ConfigNotTrusted` (or returns
  `None` for missing). POSIX-only cases are skipped on Windows.
- Add the provenance map (parent Decision 9): `env` snapshot, per-loader returned keys mirroring each
  loader's own blank rule, merge `{**env, **dotenv, **global_config}`. `load_env_layers` returns it;
  the return value is optional so dashboard and smoke scripts are untouched.
- `main()` stores the map in `ctx.obj["env_provenance"]`. `test_cli.py` asserts the key is set and that
  both loaders run once per invocation. Phase 2d tests consume the carrier and assert no layer is re-read.
- Update `test_load_env_layers_calls_both_loaders_in_order`: its patched loaders return `None` today;
  they now return dicts.
- Mutation check: break the overlap merge (a key set blank by `.env`, then by `config.toml`); the
  overlap test must fail. Record the result in the PR description.
- Windows: the three test files are already listed. POSIX-only cases skip there.

### Phase 2c: the writer

**Goal:** an atomic, comment-preserving edit of the one key that cannot write a file the reader would ignore.
**Impl files:** `src/pipecat_context_hub/shared/config_writer.py`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_config_writer.py`
**Test command:** `uv run pytest tests/unit/test_config_writer.py -v`

- Implement parent Decision 8 in full: path resolution and symlink handling, `read_trusted_config` for
  the existing file, edit only `PIPECAT_HUB_EXTRA_REPOS` as a native array, temp-file re-parse
  through `parse_global_config`, atomic write via `os.replace` (not rename, which fails on Windows
  over an existing file), modes 0600/0700, no lock.
- Refusals raise `ConfigWriteRefused(reason)`, defined in `shared/config_writer.py`: a non-array,
  non-string value, invalid TOML, and a failed re-parse. `ConfigNotTrusted` propagates unchanged.
  The module never exits; `cli_repos.py` maps both errors (Phase 2d).
- Mutation check: remove the failed-re-parse abort; its test must fail. Record it in the PR description.
- `test_config_writer.py` joins the `ci.yml` Windows file list in this commit. POSIX-only tests skip
  there (`os.name != "posix"`); a round-trip test that writes twice runs on Windows.

### Phase 2d: the `repos` commands

**Goal:** `repos add X` then `repos list` shows X, `effective_repos` contains X, and `repos remove X` undoes it, across every layer combination (parent standing contract, with its `--config-only` qualification).
**Impl files:** `src/pipecat_context_hub/cli_repos.py`, `src/pipecat_context_hub/cli.py`, `src/pipecat_context_hub/shared/exit_codes.py`, `src/pipecat_context_hub/cli_query.py`, `docs/configuration.md`, `docs/smoke-test.md`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_cli_repos.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_plugin.py`
**Test command:** `uv run pytest tests/unit/test_cli_repos.py tests/unit/test_cli_query.py tests/unit/test_plugin.py -v`

- `cli_repos.py` with `register_repos_commands(main)` (the `cli_install.py` pattern); the parent
  Decision 7 check-order table, `--group`, `--unlisted`, `--config-only`, `repos list` tags and the
  consulted-cwd line.
- Membership for check 4 and the `repos list` shadow line comes only from `parse_global_config`
  (CSV or array, casefolded). Provenance decides the layer. `os.environ` is never consulted for
  membership, and `cli_repos` never raw-parses `config.toml`.
- `cli_repos.py` is the only place that maps `ConfigWriteRefused` and `ConfigNotTrusted` to `EXIT_INVALID_INPUT`.
- Add `EXIT_INVALID_INPUT = 1` to `shared/exit_codes.py` and migrate `cli_query._EXIT_BAD_INPUT` to it in
  this commit, including the import at `tests/unit/test_cli_query.py:16`; add a shared-constant pin test
  (mirror `test_refresh_in_progress_exit_code_is_shared`).
- Carrier tests: through `main`, with both loaders patched, each runs once per `repos` invocation. With
  `ctx.obj["env_provenance"]` pre-set and the loaders patched to fail, the shadow decision reads only the map.
- A "`repos add` makes no network call" test: a fixture patches `socket.socket` and `subprocess.run`
  (the `gh` path) to raise, applied to every `repos add` row.
- Docs in `docs/configuration.md`. A Typer-bridge test covers `repos --help` and `repos add --help`
  (matching the direct CLI) plus a functional `pipecat context-hub repos list` and `repos add --config-only`.
- Add a numbered live-smoke item to `docs/smoke-test.md`: `repos list`, `repos add --config-only` and
  `repos remove` against a scratch `PIPECAT_HUB_CONFIG_FILE`.
- Mutation checks (five), each reverted with its test failing, results in the PR description: the shadow
  refusal, the default/tainted refusal, the check order, the `--unlisted` rules, the casefold in the add no-op.
- `test_cli_repos.py` joins the `ci.yml` Windows file list in this commit. Goldens build the
  consulted-cwd line at runtime (via `Path`), and uid/mode rows skip when `os.name != "posix"`.
  `test_cli_query.py` and `test_plugin.py` are not Windows-listed and stay so.

<!-- reviewed: YYYY-MM-DD @ <hash> -->

## Progress

- [ ] Phase 1: Catalog, loader, renderers and generated sections
- [ ] Phase 2a: `tomlkit` dependency
- [ ] Phase 2b: trusted-config read, parse seam and provenance
- [ ] Phase 2c: the writer
- [ ] Phase 2d: the `repos` commands

## Findings

### Review Waivers

_(none yet; this plan has not been through `/review-plan`)_
