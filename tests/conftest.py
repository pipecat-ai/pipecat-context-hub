"""Shared test fixtures for the Pipecat Context Hub test suite."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipecat_context_hub.shared.types import (
    CapabilityTag,
    ChunkedRecord,
    Citation,
    EvidenceReport,
    IndexQuery,
    IndexResult,
    KnownItem,
    TaxonomyEntry,
    UnknownItem,
)

# Complete set of opt-in, output, and data/reference controls read directly
# from the real environment by the four opt-in benchmark modules (never via
# config.toml — those modules don't call the loader). Allowlisted here so a
# developer running e.g. `PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK=1 pytest ...`
# still has that var visible during the test, instead of it being wiped by
# this fixture's own PIPECAT_HUB_* setup-time delete below. Independently
# re-verified against each file's actual line numbers, not guessed:
# PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK / PIPECAT_HUB_STABILITY_OUTPUT
# (tests/benchmarks/test_runtime_stability.py:34-35),
# PIPECAT_HUB_ENABLE_PERF_BENCHMARK / PIPECAT_HUB_PERF_OUTPUT /
# PIPECAT_HUB_PERF_DATA_DIR (tests/benchmarks/test_chromadb_perf.py:52-54),
# PIPECAT_HUB_ENABLE_QUALITY_BENCHMARK / PIPECAT_HUB_BENCHMARK_OUTPUT
# (tests/benchmarks/test_retrieval_quality.py:43-44), and
# PIPECAT_HUB_PARITY_REFERENCE (tests/benchmarks/test_chromadb_parity.py:55).
# This is orthogonal to shared/env_loading.py's `_INVOCATION_SCOPED_KEYS`:
# that one stops config.toml from setting these vars; this one protects the
# real shell-invocation env var a developer sets from being wiped here.
_FIXTURE_PASSTHROUGH_KEYS = frozenset(
    {
        "PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK",
        "PIPECAT_HUB_STABILITY_OUTPUT",
        "PIPECAT_HUB_ENABLE_PERF_BENCHMARK",
        "PIPECAT_HUB_PERF_OUTPUT",
        "PIPECAT_HUB_PERF_DATA_DIR",
        "PIPECAT_HUB_ENABLE_QUALITY_BENCHMARK",
        "PIPECAT_HUB_BENCHMARK_OUTPUT",
        "PIPECAT_HUB_PARITY_REFERENCE",
    }
)


@pytest.fixture(autouse=True)
def _isolate_env_vars(tmp_path: Path):
    """Full env-var + config-path reset around every test.

    Two independent problems, one rewritten fixture (dev plan
    docs/dev_plans/20260807-feature-global-config-toml.md, Phase 1
    Hermeticity):

    1. (pre-existing) Functions like ``load_cwd_dotenv()``/``load_global_config()``
       write directly to ``os.environ``, bypassing ``monkeypatch``. Without
       teardown cleanup, env vars set in one test leak into subsequent tests
       in the same process. Fixed by sweeping every key *added* during the
       test at teardown, **regardless of prefix** — unchanged from the
       original fixture, and still required by
       ``tests/unit/test_env_loading.py::TestLoadCwdDotenv``, which sets
       plain unprefixed keys (``FOO``, ``KEY``, ...) directly.
    2. (new) A real, pre-existing ``PIPECAT_HUB_*`` shell export (e.g. from
       the operator's ``~/.zshrc``) was, before this rewrite, visible in
       ``os.environ`` for the *entire duration* of every test run on the
       operator's own machine — not just leaking *across* tests, but present
       *during* each one. Fixed by deleting the full ``PIPECAT_HUB_*`` set at
       *setup* (not only cleaning up at teardown), except for the benchmark
       passthrough allowlist above, and by defaulting
       ``PIPECAT_HUB_CONFIG_FILE`` to a guaranteed-nonexistent sentinel path
       so no test ever falls through ``load_global_config()``'s lookup-path
       branch to the developer's real
       ``~/.config/pipecat-context-hub/config.toml``.
    """
    # Setup: snapshot the full real PIPECAT_HUB_* set, then strip it from
    # os.environ (except the passthrough allowlist) so it's absent for the
    # duration of the test body, not merely restored afterward.
    pre_existing_hub_vars = {k: v for k, v in os.environ.items() if k.startswith("PIPECAT_HUB_")}
    for key in pre_existing_hub_vars:
        if key not in _FIXTURE_PASSTHROUGH_KEYS:
            del os.environ[key]
    # A tmp_path-rooted path that is never created — guaranteed nonexistent,
    # so load_global_config()'s missing-file-silent branch always runs
    # unless a test explicitly overrides PIPECAT_HUB_CONFIG_FILE itself.
    sentinel_config_path = tmp_path / "no-such-global-config-dir" / "config.toml"
    os.environ["PIPECAT_HUB_CONFIG_FILE"] = str(sentinel_config_path)

    before = set(os.environ)
    yield
    # Teardown, part 1: today's existing any-prefix added-key cleanup,
    # unchanged — this is what TestLoadCwdDotenv's unprefixed FOO/KEY vars
    # depend on.
    for key in set(os.environ) - before:
        del os.environ[key]
    # Teardown, part 2: restore the setup-time snapshot exactly. Clear every
    # PIPECAT_HUB_* key first (including the sentinel PIPECAT_HUB_CONFIG_FILE
    # and any passthrough var a test mutated in place, which part 1's
    # added-key sweep wouldn't catch since the key itself isn't new), then
    # put back only what was really there before this test started.
    for key in [k for k in os.environ if k.startswith("PIPECAT_HUB_")]:
        del os.environ[key]
    os.environ.update(pre_existing_hub_vars)


@pytest.fixture
def sample_citation() -> Citation:
    return Citation(
        source_url="https://docs.pipecat.ai/guides/getting-started",
        repo="pipecat-ai/pipecat",
        path="docs/guides/getting-started.md",
        commit_sha="abc1234",
        section="Installation",
        indexed_at=datetime(2026, 2, 18, tzinfo=UTC),
    )


@pytest.fixture
def sample_chunked_record() -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id="doc-getting-started-001",
        content="# Getting Started\n\nInstall pipecat with `pip install pipecat-ai`.",
        content_type="doc",
        source_url="https://docs.pipecat.ai/guides/getting-started",
        repo=None,
        path="guides/getting-started",
        commit_sha=None,
        indexed_at=datetime(2026, 2, 18, tzinfo=UTC),
    )


@pytest.fixture
def sample_code_record() -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id="code-pipecat-bot-001",
        content="from pipecat.pipeline import Pipeline\n\nasync def main():\n    pipeline = Pipeline()\n    await pipeline.run()",
        content_type="code",
        source_url="https://github.com/pipecat-ai/pipecat/blob/main/examples/foundational/01-say-one-thing.py",
        repo="pipecat-ai/pipecat",
        path="examples/foundational/01-say-one-thing.py",
        commit_sha="def5678",
        indexed_at=datetime(2026, 2, 18, tzinfo=UTC),
    )


@pytest.fixture
def sample_taxonomy_entry() -> TaxonomyEntry:
    return TaxonomyEntry(
        example_id="foundational-01-say-one-thing",
        repo="pipecat-ai/pipecat",
        path="examples/foundational/01-say-one-thing.py",
        foundational_class="01-say-one-thing",
        capabilities=[
            CapabilityTag(name="tts", confidence=0.95, source="code"),
            CapabilityTag(name="pipeline", confidence=1.0, source="directory"),
        ],
        key_files=["01-say-one-thing.py"],
        summary="Minimal example: say a single phrase via TTS.",
    )


@pytest.fixture
def sample_evidence_report(sample_citation: Citation) -> EvidenceReport:
    return EvidenceReport(
        known=[
            KnownItem(
                statement="Pipecat supports ElevenLabs TTS.",
                citations=[sample_citation],
                confidence=0.95,
            ),
        ],
        unknown=[
            UnknownItem(
                question="Does Pipecat support real-time screen sharing?",
                reason="No matching documentation found.",
                suggested_queries=["pipecat screen share", "pipecat RTVI video"],
            ),
        ],
        confidence=0.7,
        confidence_rationale="Partial match: TTS confirmed, screen share unknown.",
        next_retrieval_queries=["pipecat screen share example", "RTVI frontend integration"],
    )


@pytest.fixture
def sample_index_result(sample_chunked_record: ChunkedRecord) -> IndexResult:
    return IndexResult(
        chunk=sample_chunked_record,
        score=0.85,
        match_type="vector",
    )


@pytest.fixture
def sample_index_query() -> IndexQuery:
    return IndexQuery(
        query_text="how to create a pipecat bot",
        filters={"content_type": "doc"},
        limit=5,
    )
