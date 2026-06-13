# Release 0.2.0 — first PyPI publish

**Component**: release/ci
**Status**: In Progress — release prep merged (PR #82); only the PyPI trusted-publisher confirmation + `gh release create v0.2.0` remain
**Type**: chore
**Created**: 2026-06-12
**Branch**: `release/0.2.0` (cut from `main` after PR #76 merged; prep landed via PR #82)
**Target version**: `v0.2.0`

## Why

`v0.2.0` is the **first version published to PyPI**. It bundles the two
agent-bootstrappability features so the first `uvx`-able release already has
them:

- **#75** — one-shot CLI query subcommands (every MCP tool as a shell command). **Merged.**
- **#76** — PyPI trusted-publishing release workflow + dist rename to `pipecat-ai-context-hub`. **Open, ready to merge.**

The distribution is published under the **`pipecat` PyPI org**
(https://pypi.org/org/pipecat/). The command, MCP server name, data dir, and
env vars all stay `pipecat-context-hub`; only the PyPI distribution name carries
the `pipecat-ai-` family prefix (decision confirmed 2026-06-12 — keep, matches
sibling `pipecat-ai*` packages).

## Hard dependency / sequencing

This release **cannot be cut until PR #76 is merged to `main`** — the
`.github/workflows/release.yml` that does the publish lives in #76. Cutting
0.2.0 before #76 lands would produce a tag with no publish pipeline.

Order of operations:
1. Merge #76 to `main`.
2. (Independent, any time before step 6) Register the PyPI trusted publisher —
   see [[project_pypi_first_release_setup]] / `docs/CONTRIBUTING.md` "Maintainer
   Setup". Owner of the `pipecat` PyPI org adds a **pending publisher** from the
   org settings (project does not exist yet). Without this, the publish job
   fails on OIDC.
3. Cut this release (steps below).
4. `gh release create v0.2.0` → triggers `release.yml` → first PyPI publish.

## Pre-flight checks (before tagging)

- [x] PR #76 is merged to `main` (commit b198be6).
- [ ] **PyPI trusted publisher confirmed** under the `pipecat` org (owner:
      `pipecat-ai` GitHub org, repo: `pipecat-context-hub`, workflow:
      `release.yml`, env: `pypi`; project name `pipecat-ai-context-hub`).
      **Status: project name reserved under the `pipecat` org, but the
      trusted-publisher config is UNCONFIRMED** — settings page is Owner-only
      and vr000m is a project Maintainer (403). Needs an org/project Owner to
      confirm or add the publisher before publish, or the `publish-pypi` job
      fails on OIDC (recoverable: re-run after the publisher is added; no
      re-tag needed).
- [ ] *(optional, recommended)* Dry-run the publish pipeline: trigger the
      Release workflow via `workflow_dispatch` once — it publishes to **TestPyPI**
      and validates the OIDC wiring end-to-end without touching prod PyPI.
      (Requires the matching `testpypi` pending publisher.)

## Release steps (on `release/0.2.0`, branched from updated `main`)

- [x] Branch: `release/0.2.0` cut from updated `main`.
- [x] Bump version in **both** locations (enforced by
      `tests/unit/test_server.py::TestVersionConsistency`):
  - [x] `pyproject.toml` → `[project].version = "0.2.0"`
  - [x] `src/pipecat_context_hub/server/main.py` → `_SERVER_VERSION = "0.2.0"`
- [x] `uv lock` (refreshed `uv.lock` to `0.2.0`).
- [x] `CHANGELOG.md`: `## [Unreleased]` cut to `## [0.2.0] - 2026-06-12`; fresh
      empty `## [Unreleased]` added above it.
- [x] Verified: `ruff check` clean, `mypy` clean (99 files), `pytest` 1139
      passed / 6 skipped. (`ruff format` deliberately skipped — locked ruff
      0.15.1 reformats 34 unrelated files; CI gates on `ruff check` only.)
- [x] PR #82 `release/0.2.0` → `main`; CI green; merged (regular merge).

## Publish

- [ ] `gh release create v0.2.0` with notes per CLAUDE.md "Release Notes
      Template" (theme: first PyPI release; CLI subcommands + trusted-publishing
      pipeline). The **tag must be exactly `v0.2.0`** — `release.yml`'s
      tag-matches-version guard fails the build if the tag ≠ the wheel version.
- [ ] Watch the Release workflow: `build` (sdist+wheel, twine check, tag guard,
      clean-venv smoke) → `publish-pypi` (OIDC, env `pypi`).
- [ ] Verify the project is live and org-owned:
      https://pypi.org/project/pipecat-ai-context-hub/ (under the `pipecat` org).
- [ ] Smoke the published artifact:
      `uvx pipecat-ai-context-hub check-deprecation PipelineTask` from a clean
      machine/container.

## Post-release

- [ ] Confirm `pipecat-context-hub` short-name handling (a metadata-only alias
      package depending on the real one, per `docs/CONTRIBUTING.md`) — register
      it on PyPI so the obvious wrong guess installs the right thing. *(Can be a
      fast-follow; not blocking for 0.2.0.)*
- [ ] Delete the `release/0.2.0` branch.
- [ ] Update `docs/dev_plans/README.md`: mark this row `Complete (v0.2.0)`.
- [ ] Drop the `project_pypi_first_release_setup` memory follow-up once 0.2.0 is
      live.

## Versioning policy note

Pre-1.0 SemVer convention for this project: a minor bump (`0.1.x` → `0.2.0`) is
appropriate here because 0.2.0 adds new user-facing surfaces (CLI subcommands)
and the first distribution channel (PyPI), not just patches.
