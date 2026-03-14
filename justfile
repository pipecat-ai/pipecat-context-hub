# Pipecat Context Hub — task runner
# https://github.com/casey/just

set dotenv-load

# List available recipes
default:
    @just --list

# ── Dev ──────────────────────────────────────────────

# Run the full test suite
test *args:
    uv run pytest tests/ -v {{args}}

# Lint with ruff
lint:
    uv run ruff check src/ tests/

# Format check with ruff
fmt-check:
    uv run ruff format --check src/ tests/

# Auto-format with ruff
fmt:
    uv run ruff format src/ tests/

# Type check with mypy
typecheck:
    uv run mypy src/ tests/

# Run lint + format check + type check
check: lint fmt-check typecheck

# ── Server ───────────────────────────────────────────

# Start the MCP server
serve:
    uv run pipecat-context-hub serve

# Refresh the index (pass --force for full re-ingest)
refresh *args:
    uv run pipecat-context-hub refresh {{args}}

# ── Dashboard ────────────────────────────────────────

# Refresh index + rebuild all dashboard data
dashboard-refresh *args: (refresh args)
    @echo ""
    @echo "=== Extracting embeddings + UMAP 3D projection ==="
    uv run python dashboard/scripts/extract_embeddings.py
    @echo ""
    @echo "=== Computing K-means clusters ==="
    uv run python dashboard/scripts/compute_clusters.py
    @echo ""
    @echo "=== Extracting dashboard stats ==="
    uv run python dashboard/scripts/extract_dashboard.py
    @echo ""
    @echo "Done. Run 'just dashboard-serve' to view."

# Rebuild dashboard data without refreshing the index
dashboard-build:
    uv run python dashboard/scripts/extract_embeddings.py
    uv run python dashboard/scripts/compute_clusters.py
    uv run python dashboard/scripts/extract_dashboard.py

# Serve the dashboard on localhost:8765
dashboard-serve port="8765":
    python3 -m http.server {{port}} -d dashboard/public/
