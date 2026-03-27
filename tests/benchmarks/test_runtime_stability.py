"""Opt-in runtime stability benchmark for refresh, serve, and retrieval paths.

Run with:
  PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK=1 \
    uv run pytest tests/benchmarks/test_runtime_stability.py -m benchmark -v -s

Optional JSON report:
  PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK=1 \
  PIPECAT_HUB_STABILITY_OUTPUT=artifacts/benchmarks/runtime-stability.json \
    uv run pytest tests/benchmarks/test_runtime_stability.py -m benchmark -v -s
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import resource
import sys
import threading
from collections.abc import Generator
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from pipecat_context_hub.cli import main
from pipecat_context_hub.shared.config import HubConfig, RerankerConfig, StorageConfig
from pipecat_context_hub.shared.types import IngestResult, SearchDocsInput

_ENABLE_ENV = "PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK"
_OUTPUT_ENV = "PIPECAT_HUB_STABILITY_OUTPUT"
_SCHEMA_VERSION = 1

_REFRESH_CYCLES = 6
_SERVE_CYCLES = 6
_CONCURRENCY = 12
_CONCURRENCY_ROUNDS = 4

_MAX_STEADY_STATE_RSS_DELTA_BYTES = 64 * 1024 * 1024
_MAX_STEADY_STATE_THREAD_DELTA = 4
_MAX_STEADY_STATE_FD_DELTA = 8


@dataclass(frozen=True)
class ResourceSnapshot:
    rss_bytes: int
    threads: int
    open_fds: int


def _require_opt_in() -> None:
    if os.environ.get(_ENABLE_ENV, "").strip() not in {"1", "true", "yes"}:
        pytest.skip(
            f"Set {_ENABLE_ENV}=1 to run runtime stability benchmarks.",
            allow_module_level=True,
        )


def _rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports KiB.
    return int(rss if sys.platform == "darwin" else rss * 1024)


def _open_fd_count() -> int:
    for candidate in (Path("/dev/fd"), Path("/proc/self/fd")):
        if candidate.exists():
            return len(list(candidate.iterdir()))
    return -1


def _snapshot() -> ResourceSnapshot:
    gc.collect()
    return ResourceSnapshot(
        rss_bytes=_rss_bytes(),
        threads=threading.active_count(),
        open_fds=_open_fd_count(),
    )


def _delta(previous: ResourceSnapshot, current: ResourceSnapshot) -> ResourceSnapshot:
    return ResourceSnapshot(
        rss_bytes=current.rss_bytes - previous.rss_bytes,
        threads=current.threads - previous.threads,
        open_fds=current.open_fds - previous.open_fds,
    )


def _assert_steady_state_growth(
    label: str,
    warm_snapshot: ResourceSnapshot,
    final_snapshot: ResourceSnapshot,
) -> ResourceSnapshot:
    delta = _delta(warm_snapshot, final_snapshot)
    assert delta.rss_bytes <= _MAX_STEADY_STATE_RSS_DELTA_BYTES, (
        f"{label} RSS grew by {delta.rss_bytes} bytes after warmup"
    )
    assert delta.threads <= _MAX_STEADY_STATE_THREAD_DELTA, (
        f"{label} thread count grew by {delta.threads} after warmup"
    )
    if warm_snapshot.open_fds >= 0 and final_snapshot.open_fds >= 0:
        assert delta.open_fds <= _MAX_STEADY_STATE_FD_DELTA, (
            f"{label} open-fd count grew by {delta.open_fds} after warmup"
        )
    return delta


def _write_report(report: dict[str, object]) -> None:
    output_path = os.environ.get(_OUTPUT_ENV, "").strip()
    if not output_path:
        return

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nWrote runtime-stability report to {path}")


class _FakeDocsCrawler:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def fetch_llms_txt(self) -> str:
        return "# Benchmark Page\nSource: https://example.com\nContent here"

    async def ingest(self, prefetched_text: str | None = None) -> IngestResult:
        assert prefetched_text is not None
        return IngestResult(
            source="docs",
            records_upserted=1,
            errors=[],
            duration_seconds=0.001,
        )

    async def close(self) -> None:
        return None


class _FakeGitHubRepoIngester:
    def __init__(self, config: HubConfig, *_args, **_kwargs) -> None:
        self._repos_dir = config.storage.data_dir / "repos"

    def clone_or_fetch(self, repo_slug: str, checkout: bool = True) -> tuple[Path, str]:
        del checkout
        repo_path = self._repos_dir / repo_slug.replace("/", "_")
        repo_path.mkdir(parents=True, exist_ok=True)
        return repo_path, "bench-sha"

    def checkout_commit(self, repo_path: Path, commit_sha: str) -> None:
        del repo_path, commit_sha

    async def ingest(
        self,
        repos: list[str] | None = None,
        prefetched: dict[str, tuple[Path, str]] | None = None,
    ) -> IngestResult:
        del prefetched
        repo_count = len(repos) if repos is not None else 0
        return IngestResult(
            source="github",
            records_upserted=max(repo_count, 1),
            errors=[],
            duration_seconds=0.001,
        )


class _FakeSourceIngester:
    def __init__(self, _config: HubConfig, _writer: object, repo_slug: str) -> None:
        self._repo_slug = repo_slug

    async def ingest(self) -> IngestResult:
        return IngestResult(
            source=f"source:{self._repo_slug}",
            records_upserted=1,
            errors=[],
            duration_seconds=0.001,
        )


def _make_config(data_dir: Path) -> HubConfig:
    return HubConfig(
        storage=StorageConfig(data_dir=data_dir),
        reranker=RerankerConfig(enabled=False),
    )


def _invoke_refresh(data_dir: Path) -> None:
    runner = CliRunner()
    config = _make_config(data_dir)
    with (
        patch("pipecat_context_hub.cli.HubConfig", return_value=config),
        patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler", _FakeDocsCrawler),
        patch(
            "pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester",
            _FakeGitHubRepoIngester,
        ),
        patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted", return_value=False),
        patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester", _FakeSourceIngester),
    ):
        result = runner.invoke(main, ["refresh", "--force"])
    assert result.exit_code == 0, result.output


def _invoke_serve(data_dir: Path) -> None:
    runner = CliRunner()
    config = _make_config(data_dir)
    with (
        patch("pipecat_context_hub.cli.HubConfig", return_value=config),
        patch("pipecat_context_hub.server.transport.serve_stdio", return_value=None),
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output


@pytest.fixture(scope="module")
def stability_context(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[dict[str, object], None, None]:
    _require_opt_in()

    base_dir = tmp_path_factory.mktemp("runtime-stability")
    report: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "thresholds": {
            "steady_state_rss_delta_bytes": _MAX_STEADY_STATE_RSS_DELTA_BYTES,
            "steady_state_thread_delta": _MAX_STEADY_STATE_THREAD_DELTA,
            "steady_state_fd_delta": _MAX_STEADY_STATE_FD_DELTA,
        },
        "config": {
            "refresh_cycles": _REFRESH_CYCLES,
            "serve_cycles": _SERVE_CYCLES,
            "concurrency": _CONCURRENCY,
            "concurrency_rounds": _CONCURRENCY_ROUNDS,
        },
    }

    context = {"base_dir": base_dir, "report": report}
    yield context
    _write_report(report)


@pytest.mark.benchmark
class TestRuntimeStability:
    def test_refresh_and_serve_cycles(self, stability_context: dict[str, object]) -> None:
        base_dir = stability_context["base_dir"]
        assert isinstance(base_dir, Path)
        report = stability_context["report"]
        assert isinstance(report, dict)

        refresh_dir = base_dir / "refresh-data"
        refresh_snapshots: list[ResourceSnapshot] = []
        for _ in range(_REFRESH_CYCLES):
            _invoke_refresh(refresh_dir)
            refresh_snapshots.append(_snapshot())

        refresh_delta = _assert_steady_state_growth(
            "refresh cycles",
            refresh_snapshots[0],
            refresh_snapshots[-1],
        )

        serve_dir = base_dir / "serve-data"
        serve_snapshots: list[ResourceSnapshot] = []
        for _ in range(_SERVE_CYCLES):
            _invoke_serve(serve_dir)
            serve_snapshots.append(_snapshot())

        serve_delta = _assert_steady_state_growth(
            "serve cycles",
            serve_snapshots[0],
            serve_snapshots[-1],
        )

        report["refresh"] = {
            "snapshots": [asdict(snapshot) for snapshot in refresh_snapshots],
            "steady_state_delta": asdict(refresh_delta),
        }
        report["serve"] = {
            "snapshots": [asdict(snapshot) for snapshot in serve_snapshots],
            "steady_state_delta": asdict(serve_delta),
        }

    @pytest.mark.asyncio
    async def test_concurrent_search_rounds(
        self,
        stability_context: dict[str, object],
        bench_retriever,
    ) -> None:
        report = stability_context["report"]
        assert isinstance(report, dict)

        query = SearchDocsInput(query="TTS + STT + pipeline", limit=5)
        snapshots: list[ResourceSnapshot] = []
        for _ in range(_CONCURRENCY_ROUNDS):
            await asyncio.gather(*[
                bench_retriever.search_docs(query)
                for _ in range(_CONCURRENCY)
            ])
            snapshots.append(_snapshot())

        concurrency_delta = _assert_steady_state_growth(
            "concurrent retrieval rounds",
            snapshots[0],
            snapshots[-1],
        )

        report["concurrency"] = {
            "snapshots": [asdict(snapshot) for snapshot in snapshots],
            "steady_state_delta": asdict(concurrency_delta),
        }
