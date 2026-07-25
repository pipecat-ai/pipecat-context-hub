# Task: Expose the hub as an external API and mount it in the Pipecat CLI

**Status**: Phase 1 complete; Phases 2-3 not started
**Component**: cli, index metadata, packaging
**Assigned to**: markbackman
**Priority**: High
**Branch**: `feature/indexed-framework-version` (Phase 1)
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
| R4 | 2 | Documented read contract over `index_metadata` | — |
| R5 | 3 | Typer bridge + `[project.entry-points."pipecat_cli.extensions"]` | — |

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

## Phase 2 — External read contract

Document `index_metadata` (key, value, updated_at) as a stable, versioned
contract external tooling may read directly, read-only, with stdlib `sqlite3`.
The DB is WAL, so readers neither block nor are blocked by a concurrent refresh
or a running `serve`. Add a contract-version key so a consumer can bail out on a
future incompatible change, and document that consumers must honour
`PIPECAT_HUB_DATA_DIR`. Also fix `__init__.py`'s `__version__`, stale at `0.1.0`
against a `0.2.1` package — any external consumer will reach for it.

## Phase 3 — Typer bridge and entry point

The Pipecat CLI's loader requires a `typer.Typer`; this CLI is click. Rather than
mirror ten commands and their options by hand, bridge the existing click group so
there is one definition of every command. Extract the `@click.group` body
(`_load_dotenv`, logging, `ctx.obj["config"]`) into a reusable initializer shared
by both front doors, add `plugin.py`, and register
`mcp = "pipecat_context_hub.plugin:..."` under `pipecat_cli.extensions`.

Verify the leaf paths, not just dispatch: `pipecat mcp <cmd> --help` must render
the hub's help (not the bridge's), a bad option must produce click's usage error
with exit 2, and `serve`'s exit-2-on-unbuilt-index must propagate.

Because plugin discovery is dynamic and already shipped in `pipecat-ai` 1.6.0,
publishing this phase makes `pipecat mcp` work against Pipecat CLI installs that
already exist — no coordinated release required. The corresponding pipecat-side
work (discoverability stub, freshness warning) must not ship before this phase is
on PyPI, or its install hint points at a package that registers nothing.
