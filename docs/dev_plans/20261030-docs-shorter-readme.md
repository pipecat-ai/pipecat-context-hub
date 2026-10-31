# Shorter Package README

**Status:** Complete (documentation changes verified locally)
**Component:** Documentation
**Branch:** `docs/shorter-readme`

## Decision

The user approved a shorter `docs/README.md`, retaining it as the PyPI readme
source. Keep purpose, quick start, example usage, platform support and absolute
GitHub documentation links. Preserve removed reference material in linked guides.
No package version or runtime changes.

## Scope

- Setup alternatives and registration: `docs/setup/README.md`.
- Tool reference and CLI equivalents: `docs/tools.md`, `docs/cli.md`.
- Sources and repo commands: `docs/configuration.md`.
- Catalog maintenance: `docs/CONTRIBUTING.md`.
- Detailed platform contract: `docs/troubleshooting.md`.
- Plugin prerequisites and qualification remain owned by its existing README
  and evaluation report; setup links to them instead of duplicating history.

## Verification

- [x] Reference sections relocated and linked; package readme path unchanged.
- [x] Validated 54 repository link targets/anchors, absolute README links and
  unchanged PyPI readme source; `git diff --check` passed.
- [x] CLI help examples checked; `tests/unit/test_cli_query.py`: 96 passed.
- [x] Reviewed relocation diff; README reduced from 1,707 to 294 words.

Full runtime suite not run for this documentation-only change; no PR opened.

Sibling plans mentioning the README/catalog/plugin record historical delivery
and unchanged runtime decisions; no corrections are needed for this relocation.
