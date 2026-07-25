# Task: Expose the hub as an external API and mount it in the Pipecat CLI

**Status**: Phases 1-3 complete (hub side done); pipecat-side work pending
**Component**: cli, index metadata, packaging
**Assigned to**: markbackman
**Priority**: High
**Branch**: `feature/indexed-framework-version`
**Created**: 2026-07-25
**Completed**: —

## Objective

Make the hub consumable by external tooling, then mount it in the Pipecat CLI as
`pipecat mcp`, so that installing and keeping the index current is part of the
normal Pipecat workflow rather than a separate thing a developer must remember.

## Context

A coding agent citing a stale index is indistinguishable, to the developer, from
a coding agent citing the truth — the failure only surfaces when the generated
code is wrong. Two gaps make that failure mode likely:

1. Nothing checks index freshness. The Pipecat CLI writes `AGENTS.md` telling the
   agent to refresh when stale, and then never verifies it happened.
2. Until Phase 1, the index recorded nothing about which pipecat release it was
   built from. `framework_version` holds an operator's explicit
   `--framework-version` pin and is deleted on any unpinned refresh, so the
   common case carried no version signal at all — there was nothing to compare a
   project's installed `pipecat-ai` against.

The hub stays a separate package. Its retrieval stack is 488 locked packages and
~1.1 GB installed and caps Python at `<3.15`, where `pipecat-ai` is unbounded;
merging it into the framework repo is not viable. The Pipecat CLI already has an
entry-point plugin mechanism (`pipecat_cli.extensions`, used by `pipecatcloud`),
which is the seam this work targets.

## Requirements

**R1 — Observed framework provenance.** Every refresh records the pipecat
revision the index was built from, independent of whether an operator pinned a
tag. Surfaced via `get_hub_status`.

**R2 — Floor vs identity.** An unpinned refresh tracks the default branch, where
the nearest tag is a floor, not an identity (an index 55 commits past `v1.6.0`
still describes as `1.6.0`). The recorded provenance must let a consumer tell the
two apart, or version comparison will fire on every core developer.

**R3 — Provenance never lies.** A run in which the framework repo is not cloned
must leave the existing stamp alone rather than erase or overwrite it; a run that
drops the framework repo from the configured sources must clear it.

**R4 — Cheap external freshness read.** An external process must be able to
answer "how old is this index, and what pipecat version is it for" without
importing the hub. (`IndexStore` construction opens chromadb even for
`needs_embeddings=False` commands, so shelling out to `status` is not viable on a
per-invocation basis.)

**R5 — Mounted command surface.** `pipecat mcp` exposes the hub's commands
through the Pipecat CLI without duplicating command or option definitions.

## Conformance Matrix

| Req | Phase | Implementation | Verification |
|---|---|---|---|
| R1 | 1 | `describe_framework_checkout` (`github_ingest.py`); `indexed_framework_version` written in `cli.py` refresh metadata pass | `TestDescribeFrameworkCheckout`; `TestHandleGetHubStatus`; AGENTS.md smoke 38a |
| R2 | 1 | `indexed_framework_commits_ahead` from `git describe --tags --long` | `test_exact_tag_reports_zero_commits_ahead`, `test_commits_past_tag` |
| R3 | 1 | Write guarded on `framework_checkout is not None`; cleanup on de-configuration | `test_indexed_framework_version_absent_by_default` |
| R4 | 2 | `METADATA_CONTRACT_VERSION` (`fts.py`) stamped on refresh; "Index Metadata Contract" in `docs/README.md` | `TestRefreshProvenanceMetadata`; `TestVersionConsistency` |
| R5 | 3 | `plugin.py` passthrough bridge + `[project.entry-points."pipecat_cli.extensions"]` | `tests/unit/test_plugin.py`; AGENTS.md CLI smoke 6a |

## Phase 1 — Framework provenance in the index (complete)

`git describe --tags --long` always renders as `<tag>-<commits>-g<sha>`, so one
call yields both the nearest tag and the distance to it; splitting from the right
keeps hyphenated tags (`v1.0.0-rc1`) intact. `_get_framework_version` was left
alone — it feeds per-chunk `pipecat_version_pin` and uses `--abbrev=0`, a
different question from index provenance.

- `services/ingest/github_ingest.py` — add `describe_framework_checkout`
- `cli.py` — capture the framework checkout during refresh; write
  `indexed_framework_version` and `indexed_framework_commits_ahead`; clear both
  when the framework repo is de-configured
- `shared/types.py`, `server/tools/get_hub_status.py` — surface both fields

## Phase 2 — External read contract (complete)

`index_metadata` (key, value, updated_at) is documented as a stable, versioned
contract that external tooling may read directly, read-only, with stdlib
`sqlite3`. Verified against the live index: the database is WAL, `mode=ro`
rejects writes, a read during an open write transaction completes in ~0.1 ms with
`timeout=0`, and readers see only committed state — so no locking or sidecar file
is warranted.

This was chosen over publishing a separate `status.json`. A sidecar has the same
"absent until the next refresh" upgrade profile as a new metadata key, but adds a
desync mode (index committed, sidecar write fails) and buys no decoupling, since
the key names would have to be documented either way. Reading the table directly
also means the staleness half of a freshness check works against indexes that
already exist, because `last_refresh_at` predates the contract.

- `services/index/fts.py` — `METADATA_CONTRACT_VERSION`, beside the DDL it versions
- `cli.py` — stamp it on every refresh
- `docs/README.md` — "Index Metadata Contract": the reader snippet, contracted
  keys, `PIPECAT_HUB_DATA_DIR` requirement, and compatibility rules. Adding a key
  is backwards compatible; only a shape or meaning change bumps the version
- `__init__.py` — `__version__` was hard-coded at `0.1.0`, three releases stale;
  now from `importlib.metadata`, pinned by a test

## Phase 3 — Typer bridge, entry point, and install (complete)

Two bridge designs were built and measured against the real CLI before choosing
(`pipecat mcp <cmd> --help`, unknown option, unknown subcommand, invalid input,
empty index, group options, bare invocation). Both passed every case, so the
choice rested on what each depends on rather than on a defect:

- **Passthrough (chosen).** One generated Typer command per click command, raw
  argv handed to the click group. Click only ever sees its own classes.
- **Delegating `TyperGroup`.** Overrides `get_command`/`list_commands` to return
  real `click.Command` objects into typer's dispatch. Works on typer 0.26.2, but
  correctness depends on typer tolerating objects from the other class hierarchy
  — typer vendors click as `typer._click`, where `typer._click.exceptions.Exit is
  click.exceptions.Exit` is `False`. Pipecat allows `typer<1` in a long-lived
  global tool, so that is an implicit dependency on unspecified behavior.

The planned `init_context` extraction turned out to be unnecessary: the
passthrough re-enters the click group, so its callback runs normally and there is
nothing to mirror.

- `plugin.py` — the bridge. `help_option_names: []` is load-bearing; without it
  subcommand `--help` renders the empty stub
- `cli.py` — `start` alias for `serve`
- `cli_install.py` — `install`; registers via each client's own CLI, prints
  (never writes) config for hand-edited clients, and points clients at this
  package directly so serving does not load the Pipecat CLI. `--with` exposes
  only the host package's scripts, so the co-install case falls back to
  `sys.executable -m pipecat_context_hub`, which also pins the client to the
  installed version instead of whatever `uvx` would resolve at each start
- `pyproject.toml` — entry point; `typer` in the `dev` extra only, as a peer
  dependency, so the bridge is tested rather than skipped

Client config templates were left in `config/clients/` rather than packaged:
`install` generates the block from the resolved server command, which the static
files cannot express since it varies by install method.

## Remaining: pipecat side

Not required for the feature to work — discovery is dynamic and shipped in
`pipecat-ai` 1.6.0, so `pipecat mcp` works once this package is published. What
remains there is discoverability and the freshness warning:

- `_KNOWN_EXTENSIONS["mcp"]` so the command appears in `pipecat --help` before
  the plugin is installed. **Must not ship before this package is on PyPI**, or
  the hint points at a package that registers nothing
- Guard `ep.load()` in `_build_app`: it is unwrapped inside a list comprehension,
  so any plugin import error takes down the whole CLI, `pipecat init` included
- `_enable_hint` emits `uv tool install "pipecat-ai[cli]" --with <pkg>`, which
  *replaces* the tool environment. With two official plugins, following one hint
  uninstalls the other; it needs to name every installed plugin
- The freshness warning, reading the Phase 2 contract
