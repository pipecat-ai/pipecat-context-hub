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
  `src/` modules. That is fine: the renderer's own test asserts it loads no env layer (Phase 1), and
  `cli_repos.py` runs under `main()`, which already calls `load_env_layers()` (carrier test, 2d).
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
- The provenance map records the layer that supplied the FINAL NON-BLANK value of each key; a key whose
  final value is blank or absent is omitted (parent Decision 9). The overlap row is the likely bug.
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
- Test seams (new named API; PR 2 patches the same symbol): pure `parse_catalog(text: str) -> Catalog` (raises `CatalogError`) and
  module-level `_read_catalog_text() -> str`; `load_catalog` is `functools.cache` around
  try/except over both. Tests and PR 2 patch `_read_catalog_text` and call `load_catalog.cache_clear()`
  in a fixture.
- Import boundary: `shared/repo_catalog.py` imports nothing from `cli`, `server` or `services`. A test
  imports it in a subprocess (`python -I -c`) and asserts no such `pipecat_context_hub` key in `sys.modules`.
- `src/pipecat_context_hub/data/` is a new directory. The hatchling wheel must contain the `.toml`.
  Wheel inclusion is proven by the validation command only (an editable-install pytest resolves to the
  source tree, so a pytest check would prove nothing); the test only asserts
  `importlib.resources.files(...)/"data/repo_catalog.toml"` is a file (resource path is correct).
- Initial data: every non-default public slug in the current `.env.example`. Four slugs there are
  already defaults; drop those duplicates. The equality test subtracts the live default list
  (`SourceConfig.model_fields['repos'].default`) before counting; hardcode no number. Group as in parent Decision 2, with ONE GROUP PER VENDOR (AWS, Tavus, Murf, Supermemory
  each their own group; no catch-all "other providers" group): PR 2's frozen-query expectations depend on it
  (Murf and Supermemory queries must not hint while Tavus and AWS do).
- `shared/repo_catalog_render.py` stays pure functions: catalog to block text, and splice of text to
  text, including `bump_verified(text, today) -> str`. Paths are resolved only at its `python -m` entry. It rewrites a delimited generated block inside
  `.env.example` (header prose and non-repo settings stay outside the markers) and a generated catalog
  section in `docs/configuration.md`, separate from the env-var table that `TestConfigTomlExampleParity` pins.
- The renderer reads defaults from `SourceConfig.model_fields['repos'].default`, never `effective_repos`,
  and never calls `load_env_layers()`. A test sets `PIPECAT_HUB_EXTRA_REPOS` and asserts unchanged output;
  another patches `env_loading.load_env_layers` to raise and still renders.
- `just catalog-regen *args` runs the renderer as a module and passes extra args through;
  `--bump-verified` is opt-in (`just catalog-regen --bump-verified`), never in the recipe body (parent
  Decision 2: `verified` is the last manual audit date). The bump is a line-anchored text rewrite of the
  single top-level `verified = <date>` line, re-validated with `parse_catalog` on the rewritten text
  (not the cached `load_catalog`). The renderer imports no third-party TOML library; `tomlkit` lands in 2a.
- Catalog tests also cover: a recipe-exists test mirroring `tests/unit/test_audit_sync.py` that also
  asserts the recipe body does not contain `--bump-verified`; a tainted
  env slug (`PIPECAT_HUB_TAINTED_REPOS` set to a catalog slug) does not break loading; a caplog test
  (patched `_read_catalog_text` returning a broken catalog, `cache_clear()` first) calls `load_catalog()`
  three times and asserts one log record; `bump_verified` on a catalog copy changes only the
  top-level `verified` line to today's ISO date, leaves an indented `verified` key inside a group
  untouched, and the result passes `parse_catalog`; splice on a synthetic file with changed catalog text
  leaves everything outside the markers byte-identical, and missing or duplicated markers raise.
- The equality test, the regenerated `.env.example` and the docs section land in this one commit.
  `test_repo_catalog.py` joins the `ci.yml` Windows file list here; its `python -I` subprocess test is
  marked `skipif(os.name != "posix")` unconditionally (a worker cannot observe Windows before the commit).
- Catalog test pinning the vendor rule: no group mixes vendors under an "other"/"misc"-style name, and each
  vendor named above resolves to its own group.

### Phase 2a: `tomlkit` dependency

**Goal:** `tomlkit` is locked, resolves alongside `pipecat-ai[cli]`, and passes the dependency audit before any code imports it.
**Impl files:** `pyproject.toml`, `uv.lock`
**Test command:** `uv lock --check && uv run python -c "import tomlkit"`
**Validation cmd:** `uv pip install --dry-run . "pipecat-ai[cli]" && just audit`

- Add `tomlkit` to `[project.dependencies]` as the first commit of the repos work, with an explicit
  floor at the version the validation command resolves. Record the resolved versions in the PR
  description, and that `.` (not PyPI) satisfied `pipecat-ai[cli]`'s own pin on this package
  (`pipecat-ai-context-hub<1,>=0.7.0`; a failure outside that range is unrelated to `tomlkit`).
  `pipecat-ai` is not in `uv.lock`, so `uv lock` alone proves nothing.

### Phase 2b: trusted-config read, parse seam and provenance

**Goal:** the writer and the reader share one trust check, and `repos` can tell which layer is in force.
**Impl files:** `src/pipecat_context_hub/shared/env_loading.py`, `src/pipecat_context_hub/cli.py`
**Test files:** `tests/unit/test_env_loading.py`, `tests/unit/test_config.py`, `tests/unit/test_cli.py`, `tests/unit/_config_trust_cases.py`
**Test command:** `uv run pytest tests/unit/test_env_loading.py tests/unit/test_config.py tests/unit/test_cli.py -v`

- Extract public `read_trusted_config(path) -> str | None` and `parse_global_config(path) -> dict[str, object]`,
  and `ConfigNotTrusted(reason)`; `load_global_config` becomes a wrapper (parent Decision 8). The trust
  helpers stay private. Add direct tests for all three.
- Seam outcomes (the parent leaves the exception types open; fixed here): missing file -> `None`;
  every trust predicate and every open/read/decode failure (`OSError`/`ValueError` at open, `UnicodeError`
  or `OSError` mid-read, e.g. EACCES, embedded NUL) -> `ConfigNotTrusted(reason)`; invalid TOML ->
  `tomllib.TOMLDecodeError` from `parse_global_config`, not wrapped. The seam takes a path, so a `None`
  from `resolve_global_config_path()` (no home, blank `PIPECAT_HUB_CONFIG_FILE`) stays outside it:
  the reader loads nothing and callers decide (2c, 2d). The resolver itself warns for a blank override.
- Parametrized reader-vs-seam equivalence test over the shared `TRUST_REFUSAL_CASES` (defined here in 2b in `tests/unit/_config_trust_cases.py`; 2c imports it) plus invalid
  TOML, missing file, unresolvable path. The helper is a shared module with no tests of its own.
  The unresolvable fixture is a blank `PIPECAT_HUB_CONFIG_FILE`. `load_global_config` warns and ignores
  exactly when the seam raises `ConfigNotTrusted` or `TOMLDecodeError`. For a missing file it returns
  silently. For the unresolvable fixture it loads nothing; the only log is the resolver's own warning
  (`resolve_global_config_path`, `env_loading.py`), which the test allows.
  POSIX-only cases are skipped on Windows.
- Add the provenance map (parent Decision 9): the layer that supplied the FINAL NON-BLANK value of each
  key; a key whose final merged value is blank or absent is omitted. Build it in `load_env_layers`: the
  non-blank `env` snapshot, the keys each loader actually set (each by its own rule), merge
  `{**env, **dotenv, **global_config}`, then drop every key whose final value is blank. "Blank" means
  empty after `.strip()`, as in `load_global_config` (`env_loading.py`, ~line 895). It applies to the
  `env` snapshot, the final drop and every provenance row. Blankness is
  encoded in the map, so no later code consults `os.environ`. `load_env_layers` returns it; the return
  value is optional so dashboard and smoke scripts are untouched.
- `main()` always overwrites `ctx.obj["env_provenance"]` with the map `load_env_layers` returns (no
  `setdefault`); downstream the shadow decision reads only that map and never re-reads a layer.
  `test_cli.py` asserts the key is set and that both loaders run once per invocation. Phase 2d tests
  consume the carrier by patching `env_loading.load_env_layers` to return a controlled map.
- Provenance test rows (parent Test plan, `[PR1]` Provenance bullet), one test id per row. The blank
  rows mirror `TestBlankHigherLayerValueDoesNotShadow` (`tests/unit/test_env_loading.py:1418`):

  | Layers holding the key | Expected map entry |
  | --- | --- |
  | real env, non-blank | `env` |
  | real env, blank, alone | omitted |
  | real env, whitespace-only, alone | omitted (no refusal) |
  | `.env`, non-blank | `.env` |
  | `.env`, blank, alone | omitted |
  | `.env` blank + `config.toml` non-blank (the overlap row) | `config.toml` |
  | real env blank + `.env` non-blank + `config.toml` non-blank | `config.toml` |
  | `config.toml`, alone | `config.toml` |
  | none | empty map |
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
  non-string value, invalid TOML (the writer wraps `TOMLDecodeError` from the seam and from the temp-file
  re-parse), a failed re-parse, and no resolvable config path (`resolve_global_config_path()` is `None`;
  test with `PIPECAT_HUB_CONFIG_FILE` blank). `ConfigNotTrusted` propagates unchanged.
  The module never exits; `cli_repos.py` maps the errors (Phase 2d).
- Test cases: one test id per clause of the parent Test plan `[PR1]` Writer bullet, checked off in the
  PR description; plus a writer test parametrized over `TRUST_REFUSAL_CASES`: the ONE shared tuple of the
  2b predicates that raise `ConfigNotTrusted` (non-regular, foreign-owned, world-writable, untrusted parent
  dir, foreign symlink hop, unreadable, undecodable, embedded-NUL path; excludes invalid TOML, missing,
  unresolvable), created in 2b at `tests/unit/_config_trust_cases.py`. The 2b equivalence test and this test both
  import it, so a new predicate is exercised on reader and writer. Writer asserts the file byte-identical.
- Mutation check: remove the failed-re-parse abort; its test must fail. Record it in the PR description.
- `test_config_writer.py` joins the `ci.yml` Windows file list in this commit. POSIX-only tests skip
  there (`os.name != "posix"`); a Windows round-trip test writes, then asserts both `parse_global_config`
  and `load_global_config` return the intended list.

### Phase 2d: the `repos` commands

**Goal:** `repos add X` then `repos list` shows X, `effective_repos` contains X, and `repos remove X` undoes it, across every layer combination (parent standing contract, with its `--config-only` qualification).
**Impl files:** `src/pipecat_context_hub/cli_repos.py`, `src/pipecat_context_hub/cli.py`, `src/pipecat_context_hub/shared/exit_codes.py`, `src/pipecat_context_hub/cli_query.py`, `docs/configuration.md`, `docs/smoke-test.md`, `.github/workflows/ci.yml`
**Test files:** `tests/unit/test_cli_repos.py`, `tests/unit/test_cli_query.py`, `tests/unit/test_plugin.py`, `tests/unit/test_config.py`
**Test command:** `uv run pytest tests/unit/test_cli_repos.py tests/unit/test_cli_query.py tests/unit/test_plugin.py tests/unit/test_config.py -v`

- `cli_repos.py` with `register_repos_commands(main)` (mirror `register_install_command(group)` in
  `cli_install.py`); the parent
  Decision 7 check-order table, `--group`, `--unlisted`, `--config-only`, `repos list` tags and the
  consulted-cwd line. Import `shared/config_writer.py` (hence `tomlkit`) inside the add/remove bodies,
  not at module top; a `python -I` subprocess test (`skipif(os.name != "posix")`) asserts `tomlkit` is
  absent from `sys.modules` after `import pipecat_context_hub.cli`.
- Test cases: one test id per clause of the parent Test plan `[PR1]` `repos` table bullet, checked off
  in the PR description.
- `load_catalog()` returning `None` (gap in the parent, which defines `None` only for PR 2 hints; this
  plan fills it): positional `add`/`remove` and `--group` exit `EXIT_INVALID_INPUT` naming the catalog
  load failure, file untouched; `repos list` still prints the effective list, tags non-default slugs
  `unlisted`, and warns once (relying on `load_catalog`'s own log-once; `cli_repos` emits no second warning, asserted
  by the test). One test row per command.
- Membership for check 4 and the `repos list` shadow line comes only from `parse_global_config`
  (CSV or array, casefolded). The shadow decision reads only the provenance map: the key present with
  layer `env` or `.env` means shadowed; layer `config.toml` or absent means not shadowed. The map already
  omits blank values, so no code path consults `os.environ` for the decision, and `cli_repos` never
  raw-parses `config.toml`.
- `cli_repos.py` is the only place that maps `ConfigWriteRefused`, `ConfigNotTrusted` and `TOMLDecodeError`
  to `EXIT_INVALID_INPUT`, for `add`/`remove` only. `repos list` catches all three, prints the effective
  list, names the ignored `config.toml` and the reason (the reader's posture), and exits 0; it also
  reports "no config.toml location" when `resolve_global_config_path()` is `None`. One test row each.
- Add `EXIT_INVALID_INPUT = 1` to `shared/exit_codes.py` and migrate `cli_query._EXIT_BAD_INPUT` to it in
  this commit, including the import at `tests/unit/test_cli_query.py:16`; add a shared-constant pin test
  (mirror `test_refresh_in_progress_exit_code_is_shared`).
- Carrier tests: through `main`, with both loaders patched, each runs once per `repos` invocation. Second
  test: patch `env_loading.load_env_layers` to return a controlled provenance map and assert
  `load_cwd_dotenv`/`load_global_config` are never called; the shadow decision reads only the map.
- Shadow-decision rows build the map directly or patch `env_loading.load_env_layers`; none patches
  `os.environ`. Rows: blank real env var + non-blank `.env` + `config.toml` (map says `config.toml`):
  `repos add` succeeds, no refusal. Blank `.env` only (key omitted): no refusal. Blank `.env` +
  `config.toml` key (map says `config.toml`): no refusal. Whitespace-only real env var (key omitted):
  no refusal. Non-blank `.env` or real env var: refusal.
- A "`repos add` makes no network call" test: a fixture patches `socket.socket` and `subprocess.run`
  (the `gh` path) to raise, applied to every `repos add` row.
- Docs in `docs/configuration.md`. A Typer-bridge test covers `repos --help` and `repos add --help`
  (matching the direct CLI) plus a functional `pipecat context-hub repos list` and `repos add --config-only`,
  asserting the named shadow layer, not only the exit code. Labels are only `env`, `.env`, `config.toml`
  (parent Decision 9); no fourth label. The host may pre-load a cwd `.env` into `os.environ`, so the
  bridge test pins the label only for a real-env-var shadow (label `env`, no cwd `.env`), and for a
  cwd-`.env` key asserts only the refusal and that the label is `env` or `.env`.
- Add a numbered live-smoke item to `docs/smoke-test.md`: `repos list`, `repos add --config-only` and
  `repos remove` against a scratch `PIPECAT_HUB_CONFIG_FILE`.
- Mutation checks (five), each reverted with its test failing, results in the PR description: the shadow
  refusal, the default/tainted refusal, the check order, the `--unlisted` rules, the casefold in the add no-op.
- `test_cli_repos.py` joins the `ci.yml` Windows file list in this commit. Goldens build the
  consulted-cwd line at runtime (via `Path`), and uid/mode rows skip when `os.name != "posix"`.
  `test_cli_query.py` and `test_plugin.py` are not Windows-listed and stay so.

<!-- reviewed: 2026-10-17 @ 88af6c3e0054de59ee837720caae74dc86674f2f -->

## Progress

- [x] Phase 1: Catalog, loader, renderers and generated sections
- [x] Phase 2a: `tomlkit` dependency
- [x] Phase 2b: trusted-config read, parse seam and provenance
- [x] Phase 2c: the writer
- [ ] Phase 2d: the `repos` commands

## Findings

### Review Waivers

_(none yet; this plan has not been through `/review-plan`)_
