# Release 0.2.0 — first PyPI publish

**Component**: release/ci
**Status**: Not Started
**Type**: chore
**Created**: 2026-06-12
**Branch**: `release/0.2.0` (to be cut from `main` after PR #76 merges)
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

- [ ] PR #76 is merged to `main`.
- [ ] PyPI pending publisher registered under the `pipecat` org (owner:
      `pipecat-ai` GitHub org, repo: `pipecat-context-hub`, workflow:
      `release.yml`, env: `pypi`; project name `pipecat-ai-context-hub`).
- [ ] *(optional, recommended)* Dry-run the publish pipeline: trigger the
      Release workflow via `workflow_dispatch` once — it publishes to **TestPyPI**
      and validates the OIDC wiring end-to-end without touching prod PyPI.
      (Requires the matching `testpypi` pending publisher.)

## Release steps (on `release/0.2.0`, branched from updated `main`)

- [ ] Branch: `git checkout main && git pull && git checkout -b release/0.2.0`
- [ ] Bump version in **both** locations (enforced by
      `tests/unit/test_server.py::TestVersionConsistency`):
  - [ ] `pyproject.toml` → `[project].version = "0.2.0"`
  - [ ] `src/pipecat_context_hub/server/main.py` → `_SERVER_VERSION = "0.2.0"`
- [ ] `uv lock` (refresh `uv.lock` to `0.2.0`)
- [ ] `CHANGELOG.md`: convert `## [Unreleased]` → `## [0.2.0] - 2026-06-XX`,
      keeping the Added/Changed/Fixed structure; add a fresh empty
      `## [Unreleased]` above it.
- [ ] Run `uv run pytest tests/ -q`, `uv run ruff format`, `uv run ruff check`,
      `uv run mypy src/ tests/`.
- [ ] PR `release/0.2.0` → `main`; wait for green CI; merge (regular merge, no
      squash).

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
