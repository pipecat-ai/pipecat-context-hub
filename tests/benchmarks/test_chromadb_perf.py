"""Opt-in ChromaDB performance benchmark for version-migration comparison.

Captures five metrics that a chromadb-version migration (0.6 baseline vs 1.x)
can compare across two runs:

  - index_build_seconds       wall-clock of a full ``refresh --force --reset-index``
  - refresh_peak_rss_bytes    peak RSS of the refresh process tree
  - query_p50_ms / query_p95_ms   raw vector_search latency percentiles
  - dashboard_peak_rss_bytes  peak RSS of the ``just dashboard-build`` process tree

Build time is embedding-compute dominated (the embedding model is identical
across chromadb versions), so the chromadb-discriminating signals are query
latency and dashboard RSS.

Run with:
  PIPECAT_HUB_ENABLE_PERF_BENCHMARK=1 \
    uv run pytest tests/benchmarks/test_chromadb_perf.py -m benchmark -v -s

Optional JSON report:
  PIPECAT_HUB_ENABLE_PERF_BENCHMARK=1 \
  PIPECAT_HUB_PERF_OUTPUT=artifacts/benchmarks/chromadb-perf.json \
  PIPECAT_HUB_PERF_DATA_DIR=/tmp/pch-perf \
    uv run pytest tests/benchmarks/test_chromadb_perf.py -m benchmark -v -s

The refresh subprocess sets ``PIPECAT_HUB_DATA_DIR`` to an isolated data dir so
it builds a throwaway index and never touches ``~/.pipecat-context-hub``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pipecat_context_hub.services.embedding import EmbeddingService
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.shared.config import EmbeddingConfig, StorageConfig
from pipecat_context_hub.shared.types import IndexQuery

_ENABLE_ENV = "PIPECAT_HUB_ENABLE_PERF_BENCHMARK"
_OUTPUT_ENV = "PIPECAT_HUB_PERF_OUTPUT"
_DATA_DIR_ENV = "PIPECAT_HUB_PERF_DATA_DIR"
_SCHEMA_VERSION = 1

# Env var the refresh subprocess reads to pick its (isolated) data dir.
_HUB_DATA_DIR_ENV = "PIPECAT_HUB_DATA_DIR"
# Live default data dir whose repos/ clones we pre-seed to avoid re-cloning.
_LIVE_DATA_DIR = Path.home() / ".pipecat-context-hub"

# Fixed, representative query set for latency measurement.
_QUERIES: tuple[str, ...] = (
    "TTS pipeline",
    "STT Deepgram",
    "function calling",
    "WebRTC transport",
    "interruption handling",
    "ElevenLabs voice",
    "OpenAI LLM",
    "frame processor",
    "idle timeout",
    "RTVI client",
    "Daily transport",
    "serverless deploy",
)
_ITERATIONS = 5  # timed reps per query (one extra warmup rep is discarded)

# How often the RSS sampler polls the process tree, in seconds.
_RSS_POLL_INTERVAL = 0.5


@dataclass
class PerfReport:
    schema_version: int = _SCHEMA_VERSION
    metrics: dict[str, object] = field(default_factory=dict)
    config: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: str = (
        "build time is embedding-compute dominated (model identical across "
        "chromadb versions); query latency + dashboard RSS are the "
        "chromadb-discriminating signals. dashboard RSS is UMAP-dominated and "
        "measured against the live index (dashboard scripts do not yet honor "
        "PIPECAT_HUB_DATA_DIR)."
    )


def _require_opt_in() -> None:
    if os.environ.get(_ENABLE_ENV, "").strip() not in {"1", "true", "yes"}:
        pytest.skip(
            f"Set {_ENABLE_ENV}=1 to run chromadb performance benchmarks.",
            allow_module_level=True,
        )


def _write_report(report: PerfReport) -> None:
    output_path = os.environ.get(_OUTPUT_ENV, "").strip()
    if not output_path:
        return
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nWrote chromadb-perf report to {path}")


# ── RSS sampling ─────────────────────────────────────────────────────────


def _process_tree_rss_bytes(root_pid: int) -> int:
    """Sum the RSS (bytes) of ``root_pid`` and all its descendants.

    Parses ``ps -axo pid=,ppid=,rss=`` once and walks ppid links from the
    root. macOS ``ps`` reports RSS in KiB, so we multiply by 1024. Pids that
    vanish mid-sample are simply absent from the snapshot and skipped.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return 0

    rss_by_pid: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss_kib = int(parts[2])
        except ValueError:
            continue
        rss_by_pid[pid] = rss_kib * 1024
        children.setdefault(ppid, []).append(pid)

    total = 0
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss_by_pid.get(pid, 0)
        stack.extend(children.get(pid, []))
    return total


def peak_rss_bytes_during(popen_proc: subprocess.Popen[Any]) -> int:
    """Poll the process tree's RSS while ``popen_proc`` runs; return peak bytes.

    Sampling runs on a background thread that stops once the process exits.
    The root pid's own RSS is included along with every descendant. The
    caller is responsible for waiting on ``popen_proc`` after this returns.
    """
    peak = 0
    stop = threading.Event()

    def _sample() -> None:
        nonlocal peak
        while not stop.is_set():
            current = _process_tree_rss_bytes(popen_proc.pid)
            if current > peak:
                peak = current
            stop.wait(_RSS_POLL_INTERVAL)

    sampler = threading.Thread(target=_sample, daemon=True)
    sampler.start()
    try:
        popen_proc.wait()
    finally:
        stop.set()
        sampler.join()
    # One final sample is unnecessary (process has exited), but take a last
    # in-loop reading guarantee via the loop above.
    return peak


# ── percentile helper ────────────────────────────────────────────────────


def percentile(data: list[float], pct: float) -> float:
    """Return the ``pct`` percentile (0..100) of ``data`` via linear interp.

    Uses ``statistics.quantiles(n=100)`` cut points. For a single sample the
    value itself is returned; an empty list raises ValueError.
    """
    if not data:
        raise ValueError("percentile() requires a non-empty data list")
    if len(data) == 1:
        return float(data[0])
    cut_points = statistics.quantiles(data, n=100, method="inclusive")
    # quantiles(n=100) returns 99 cut points: index i-1 == i-th percentile.
    idx = int(round(pct)) - 1
    idx = max(0, min(idx, len(cut_points) - 1))
    return float(cut_points[idx])


# ── benchmark fixture ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def perf_context(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[dict[str, object], None, None]:
    _require_opt_in()

    override = os.environ.get(_DATA_DIR_ENV, "").strip()
    perf_dir = Path(override).expanduser() if override else tmp_path_factory.mktemp("chromadb-perf")
    perf_dir.mkdir(parents=True, exist_ok=True)

    report = PerfReport()
    context: dict[str, object] = {"perf_dir": perf_dir, "report": report}
    yield context
    _write_report(report)


@pytest.mark.benchmark
class TestChromaDBPerf:
    def test_capture_metrics(self, perf_context: dict[str, object]) -> None:
        perf_dir = perf_context["perf_dir"]
        assert isinstance(perf_dir, Path)
        report = perf_context["report"]
        assert isinstance(report, PerfReport)

        # 1+2. Index build time + refresh peak RSS (isolated data dir).
        # Pre-seed repos/ from the live clone OUTSIDE the timed region so the
        # refresh reuses clones instead of re-cloning from the network.
        live_repos = _LIVE_DATA_DIR / "repos"
        perf_repos = perf_dir / "repos"
        if live_repos.exists() and not perf_repos.exists():
            shutil.copytree(live_repos, perf_repos)

        refresh_env = {**os.environ, _HUB_DATA_DIR_ENV: str(perf_dir)}
        start = time.perf_counter()
        proc = subprocess.Popen(
            ["uv", "run", "pipecat-context-hub", "refresh", "--force", "--reset-index"],
            env=refresh_env,
        )
        refresh_peak_rss = peak_rss_bytes_during(proc)
        build_seconds = time.perf_counter() - start
        assert proc.returncode == 0, f"refresh failed (exit {proc.returncode})"

        report.metrics["index_build_seconds"] = build_seconds
        report.metrics["refresh_peak_rss_bytes"] = refresh_peak_rss

        # 3. Query p50/p95 against the freshly built isolated index.
        store = IndexStore(StorageConfig(data_dir=perf_dir))
        try:
            record_count = int(store.get_index_stats().get("total", 0))
            embedder = EmbeddingService(EmbeddingConfig())
            latencies_ms = asyncio.run(self._collect_latencies(store, embedder))
        finally:
            store.close()

        report.metrics["query_p50_ms"] = percentile(latencies_ms, 50)
        report.metrics["query_p95_ms"] = percentile(latencies_ms, 95)

        # 4. Dashboard build peak RSS (live index; UMAP-dominated).
        report.metrics["dashboard_peak_rss_bytes"] = self._dashboard_peak_rss(report)

        report.config = {
            "query_count": len(_QUERIES),
            "iterations": _ITERATIONS,
            "record_count": record_count,
        }

        # Invariant: all five metric keys are populated.
        for key in (
            "index_build_seconds",
            "refresh_peak_rss_bytes",
            "query_p50_ms",
            "query_p95_ms",
            "dashboard_peak_rss_bytes",
        ):
            assert key in report.metrics, f"missing metric {key}"

    @staticmethod
    async def _collect_latencies(store: IndexStore, embedder: EmbeddingService) -> list[float]:
        latencies_ms: list[float] = []
        for query_text in _QUERIES:
            embedding = embedder.embed_query(query_text)
            base = IndexQuery(query_text=query_text, query_embedding=embedding, limit=10)
            # Warmup rep (discarded) then timed reps.
            for rep in range(_ITERATIONS + 1):
                start = time.perf_counter()
                await store.vector_search(base)
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                if rep > 0:
                    latencies_ms.append(elapsed_ms)
        return latencies_ms

    @staticmethod
    def _dashboard_peak_rss(report: PerfReport) -> int | None:
        if shutil.which("just") is None:
            report.warnings.append("dashboard skipped: 'just' not found on PATH")
            return None
        try:
            proc = subprocess.Popen(["just", "dashboard-build"], env={**os.environ})
        except OSError as exc:
            report.warnings.append(f"dashboard skipped: failed to launch ({exc})")
            return None
        peak = peak_rss_bytes_during(proc)
        if proc.returncode != 0:
            report.warnings.append(
                f"dashboard-build exited non-zero ({proc.returncode}); RSS may be partial"
            )
        return peak


# ── self-tests (always run; no opt-in, no real refresh) ───────────────────


@pytest.mark.parametrize(
    ("pct", "expected"),
    [(50, 50.0), (95, 95.0)],
)
def test_percentile_known_distribution(pct: float, expected: float) -> None:
    data = [float(i) for i in range(1, 101)]  # 1..100 inclusive
    result = percentile(data, pct)
    # Inclusive method on 1..100 lands within 1 unit of the nominal percentile.
    assert abs(result - expected) <= 1.0, f"p{pct}={result}, expected ~{expected}"


def test_percentile_single_sample() -> None:
    assert percentile([42.0], 95) == 42.0


def test_peak_rss_sampler_detects_allocation() -> None:
    # Allocate ~50MB and hold it for ~2s so the 0.5s sampler catches the peak.
    proc = subprocess.Popen(
        [sys.executable, "-c", "x = bytearray(50_000_000); import time; time.sleep(2)"]
    )
    peak = peak_rss_bytes_during(proc)
    assert proc.returncode == 0
    assert peak > 10 * 1024 * 1024, f"peak RSS implausibly low: {peak} bytes"
