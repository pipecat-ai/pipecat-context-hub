# chromadb 1.x migration + Python 3.14 support

**Component**: services/index (vector store)
**Status**: Not Started
**Type**: chore
**Created**: 2026-05-29
**Branch**: `chore/chromadb-1x-python-314`
**Target version**: TBD — likely `v0.1.0` (on-disk format break warrants a minor bump, not a patch)

## Why

`v0.0.20` capped `requires-python` to `<3.14` (PR #65) because two upstream blockers prevent Python 3.14 support today:

1. **chromadb 0.6.x's pydantic-v1 `BaseSettings` shim** crashes at import on Python 3.14 (`typing.get_type_hints` changes).
2. **torch (via `sentence-transformers` / `transformers`) and onnxruntime** have no cp314 wheels yet — resolver fails or import fails.

(1) is unblocked: chromadb shipped 1.0.0 on 2025-04-03 and is now on 1.5.9 (2026-05-05). The library has had 14 months of patch releases and is pydantic-v2 native, which fixes the 3.14 import crash.

(2) is still upstream — track PyTorch + onnxruntime cp314 wheel availability separately. We can't lift the cap until both ship, but we can do the chromadb migration now and lift the ceiling as a follow-up release the moment torch publishes cp314.

## Scope

### In-scope

- Bump `chromadb>=0.6,<1.0` → `chromadb>=1.0,<2.0` in `pyproject.toml`
- Update chromadb usage in `src/pipecat_context_hub/services/index/vector.py` (the only file that imports chromadb):
  - `chromadb.PersistentClient(...)` kwargs — re-verify `Settings` field surface against 1.x
  - `from chromadb.config import Settings as ChromaSettings` — verify still exported
  - `from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent` — module reorganized in 1.x; replace with the 1.x opt-out config flag or update the no-op stub
- Tighten `Collection.query()` typing if needed (1.x returns more strictly-typed `QueryResult`)
- Re-lock `uv.lock`
- Run the full test suite + `tests/integration/test_end_to_end.py` + `dashboard-refresh` benchmark to catch performance regressions

### Migration impact (user-facing)

- **On-disk format is NOT backward-compatible** between chromadb 0.6 and 1.x. Users upgrading must run `refresh --force --reset-index`.
- Add a startup detection in `serve` that recognises a 0.6-format index and exits with a clear remediation message (or auto-resets if the user opts in).
- Document the upgrade clearly in release notes — this is the user-visible cost of the bump.

### Out-of-scope for this plan

- **Python 3.14 cap lift** — gated on torch + onnxruntime cp314 wheels. File a follow-up ticket to monitor; ship the lift as a separate point release once both wheels land.
- **chromadb-server** (HTTP mode) — we use `PersistentClient` only.

## Files to Modify

- `pyproject.toml` — chromadb pin
- `src/pipecat_context_hub/services/index/vector.py` — import + API updates
- `src/pipecat_context_hub/server/transport.py` (or `main.py`) — startup format detection
- `uv.lock` — re-lock
- `CHANGELOG.md` — Changed entry documenting the format break + upgrade command
- `CLAUDE.md` — update `Vector store:` line if anything material changes
- `docs/README.md` — same

## Verification

- [ ] `uv run pytest tests/ -v` — full suite passes (1021+ tests today)
- [ ] `uv run pipecat-context-hub refresh --force --reset-index` — clean rebuild works on 1.x
- [ ] `uv run pipecat-context-hub serve` — boots, embedding model pre-warms, MCP queries return expected shape
- [ ] `just benchmark-stability` — no regression vs v0.0.20 baseline
- [ ] Manual check: `get_hub_status` reports expected counts after re-index
- [ ] CI matrix — once chromadb 1.x is in, expand the Python matrix to include 3.13 if it isn't already

## Sequencing

1. Land this branch as `v0.1.0` (or `v0.0.21` if we treat the format break as patch-acceptable with clear release notes — TBD)
2. Track torch/onnxruntime cp314 wheel availability separately
3. When both ship, lift `requires-python` to `<3.15` in a follow-up patch release

## Notes

- chromadb 1.x release date: 2025-04-03 (1.0.0). Current: 1.5.9 (2026-05-05).
- The lift condition was documented in `pyproject.toml`'s `requires-python` comment and v0.0.20's CHANGELOG entry. Keep both in sync when the ceiling is lifted.
