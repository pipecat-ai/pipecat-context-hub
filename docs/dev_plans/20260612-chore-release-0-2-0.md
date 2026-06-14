# Release 0.2.0 — first PyPI publish

**Component**: release/ci
**Status**: Complete (v0.2.0) — published to PyPI 2026-06-12 (https://pypi.org/project/pipecat-ai-context-hub/)
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
- [x] **PyPI trusted publisher confirmed** under the `pipecat` org (owner:
      `pipecat-ai` GitHub org, repo: `pipecat-context-hub`, workflow:
      `release.yml`, env: `pypi`; project name `pipecat-ai-context-hub`).
      Confirmed by markbackman. The GH `pypi` environment also carries a
      required-reviewers gate (vr000m/markbackman/aconchillo), so the
      `publish-pypi` job pauses for manual approval before uploading.
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

- [x] `gh release create v0.2.0` (notes per CLAUDE.md "Release Notes Template").
      Tag-matches-version guard passed (tag `v0.2.0` == wheel `0.2.0`).
- [x] Release workflow green: `build` (sdist+wheel, twine check, tag guard,
      clean-venv smoke) → `publish-pypi` (OIDC, env `pypi`, after reviewer
      approval).
- [x] Project live: https://pypi.org/project/pipecat-ai-context-hub/ — 0.2.0,
      wheel + sdist, under the `pipecat` org.
- [x] Smoked the published artifact from a clean env: `uvx --refresh
      pipecat-ai-context-hub --help` resolved 110 deps from PyPI and ran (exit
      0). (Used `--help` rather than `check-deprecation`, which needs a built
      index.)

## Post-release

- [ ] **(fast-follow, open)** Register `pipecat-context-hub` short-name alias on
      PyPI (a metadata-only package depending on the real one, per
      `docs/CONTRIBUTING.md`) so the obvious wrong guess installs the right
      thing. Org-owner action; not blocking 0.2.0.
- [x] `release/0.2.0` branch deleted (on merge, `--delete-branch`).
- [x] Update `docs/dev_plans/README.md`: row marked `Complete (v0.2.0)`.
- [x] Dropped the `project_pypi_first_release_setup` memory follow-up (0.2.0 is
      live).
- [ ] **(optional, open)** Harden the GH `pypi` environment with a deployment
      tag policy (`v*`) on top of the reviewer gate.

## Versioning policy note

Pre-1.0 SemVer convention for this project: a minor bump (`0.1.x` → `0.2.0`) is
appropriate here because 0.2.0 adds new user-facing surfaces (CLI subcommands)
and the first distribution channel (PyPI), not just patches.
