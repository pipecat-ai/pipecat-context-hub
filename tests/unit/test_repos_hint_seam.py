"""Seam test: what ``repos add`` writes is what the corpus hint reads (PR2 Phase 4b).

Offline and end to end across the two PRs' halves: the real ``repos`` command
(PR 1) writes ``config.toml``; a fresh ``_query_runtime`` over a real on-disk
``IndexStore`` reads it back as ``configured_repos``; ``corpus_hint`` (PR 2)
turns that into a hint whose ``add_command`` is then executed on the real
``repos`` command.

The command layer exercised here is the click group (``main``) with argv as
``add_command`` prints it minus the leading ``pipecat-context-hub``. The shell,
the console script and the ``pipecat context-hub`` Typer bridge are not in play.

POSIX only: a real ``IndexStore`` fails closed on Windows.
"""

from __future__ import annotations

import asyncio
import os
import random
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from pipecat_context_hub.cli import main
from pipecat_context_hub.cli_query import _query_runtime
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.shared import env_loading, repo_catalog
from pipecat_context_hub.shared.config import HubConfig, StorageConfig
from pipecat_context_hub.shared.corpus_hint import corpus_hint
from pipecat_context_hub.shared.repo_catalog import load_catalog
from pipecat_context_hub.shared.types import ChunkedRecord

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="a real IndexStore fails closed on Windows"
)

KEY = "PIPECAT_HUB_EXTRA_REPOS"
EMBEDDING_DIM = 384

CATALOG_TEXT = """\
verified = 2026-10-16

[groups.vend]
kind = "vendor"
description = "Two-repo vendor"
aliases = ["vend"]
  [[groups.vend.repos]]
  slug = "acme/vend-a"
  [[groups.vend.repos]]
  slug = "acme/vend-b"

[groups.solo]
kind = "vendor"
description = "Single-repo vendor"
aliases = ["solo"]
  [[groups.solo.repos]]
  slug = "acme/solo-one"

[groups.mix]
kind = "vendor"
description = "One normal repo, one we taint"
aliases = ["mix"]
  [[groups.mix.repos]]
  slug = "acme/mix-ok"
  [[groups.mix.repos]]
  slug = "acme/mix-taint"

[groups.tonly]
kind = "vendor"
description = "Only repo is tainted"
aliases = ["tonly"]
  [[groups.tonly.repos]]
  slug = "acme/tonly-one"
"""

VEND_A, VEND_B, SOLO = "acme/vend-a", "acme/vend-b", "acme/solo-one"
MIX_OK, MIX_TAINT, TONLY = "acme/mix-ok", "acme/mix-taint", "acme/tonly-one"


@pytest.fixture(autouse=True)
def _catalog(monkeypatch: pytest.MonkeyPatch):
    load_catalog.cache_clear()
    monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: CATALOG_TEXT)
    yield
    load_catalog.cache_clear()


def _record(slug: str, n: int = 0) -> ChunkedRecord:
    rng = random.Random(hash((slug, n)) % 10000)
    return ChunkedRecord(
        chunk_id=f"{slug}-{n}",
        content=f"Content of {slug} chunk {n}.",
        content_type="code",
        source_url=f"https://github.com/{slug}",
        repo=slug,
        path=f"/src/mod{n}.py",
        indexed_at=datetime.now(tz=UTC),
        embedding=[rng.gauss(0, 1) for _ in range(EMBEDDING_DIM)],
        metadata={},
    )


class Layout:
    """A ``config.toml``-only layout plus a real on-disk index, in a scratch tree."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.cwd = tmp_path / "work"
        self.cwd.mkdir()
        monkeypatch.chdir(self.cwd)
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        cfg_dir.chmod(0o700)
        self.config = cfg_dir / "config.toml"
        self.data_dir = tmp_path / "data"
        self.data_dir.mkdir()
        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(self.config))
        monkeypatch.setenv("PIPECAT_HUB_DATA_DIR", str(self.data_dir))
        self._baseline = {k: v for k, v in os.environ.items() if k.startswith("PIPECAT_HUB_")}
        self.runner = CliRunner()

    def env(self, **values: str) -> None:
        for k, v in values.items():
            self.monkeypatch.setenv(k, v)
            self._baseline[k] = v

    def fresh_process_env(self) -> None:
        """The loaders write ``os.environ``; reset to the arranged state, as a new process would."""
        for k in [k for k in os.environ if k.startswith("PIPECAT_HUB_")]:
            del os.environ[k]
        os.environ.update(self._baseline)

    def index(self, *slugs: str) -> None:
        """Build the on-disk index: two records per slug (and a placeholder when empty)."""
        store = IndexStore(StorageConfig(data_dir=self.data_dir), create=True)
        try:
            records = [_record(s, n) for s in slugs for n in range(2)]
            records.append(_record("pipecat-ai/pipecat"))
            asyncio.run(store.upsert(records))
        finally:
            store.close()

    def invoke(self, argv: list[str]) -> Result:
        self.fresh_process_env()
        try:
            return self.runner.invoke(main, argv)
        finally:
            self.fresh_process_env()

    def repos(self, *args: str) -> Result:
        return self.invoke(["repos", *args])

    def run_add_command(self, add_command: str) -> list[Result]:
        """Run each ``; ``-joined part of ``add_command`` on the real ``repos`` command."""
        results = []
        for part in add_command.split("; "):
            argv = shlex.split(part)
            assert argv[0] == "pipecat-context-hub", part
            results.append(self.invoke(argv[1:]))
        return results

    def runtime_and_hint(self, query: str) -> tuple[frozenset[str], dict[str, Any] | None]:
        """A new ``_query_runtime`` (as a fresh process builds it) and the hint it yields."""
        self.fresh_process_env()
        try:
            env_loading.load_env_layers()
            config = HubConfig()
            with _query_runtime(config, needs_embeddings=False) as rt:
                hint = corpus_hint(
                    rt.index_store,
                    tool="search_docs",
                    args={"query": query},
                    configured_repos=rt.configured_repos,
                    tainted_repos=rt.tainted_repos,
                )
                return rt.configured_repos, hint
        finally:
            self.fresh_process_env()

    def written(self) -> set[str]:
        """The ``config.toml`` slug set, read back through ``load_global_config``."""
        self.fresh_process_env()
        try:
            env_loading.load_global_config()
            raw = os.environ.get(KEY, "")
        finally:
            self.fresh_process_env()
        return {s.strip() for s in raw.split(",") if s.strip()}

    def effective(self) -> list[str]:
        self.fresh_process_env()
        try:
            env_loading.load_env_layers()
            return list(HubConfig().sources.effective_repos)
        finally:
            self.fresh_process_env()


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    return Layout(tmp_path, monkeypatch)


class TestAddThenHint:
    def test_added_slug_reaches_configured_repos(self, layout: Layout):
        layout.index()
        added = layout.repos("add", VEND_A)
        assert added.exit_code == 0, added.output
        configured, _ = layout.runtime_and_hint("vend")
        assert VEND_A in configured

    def test_tainted_slug_yields_neither_field(self, layout: Layout):
        layout.env(PIPECAT_HUB_TAINTED_REPOS=MIX_TAINT)
        layout.index()
        refused = layout.repos("add", MIX_TAINT)
        assert refused.exit_code == 1
        configured, hint = layout.runtime_and_hint("mix")
        assert MIX_TAINT not in configured
        assert hint is not None
        assert MIX_TAINT not in hint["repos_not_indexed"]
        assert MIX_TAINT not in hint["repos_configured_no_records"]
        assert hint["repos_not_indexed"] == [MIX_OK]
        # add_command names only the group, so the seam is what running it writes:
        # the group's untainted repo, never the tainted one.
        for result in layout.run_add_command(hint["add_command"]):
            assert result.exit_code == 0, result.output
        assert layout.written() == {MIX_OK}

    def test_group_of_only_tainted_repos_has_no_hint(self, layout: Layout):
        layout.env(PIPECAT_HUB_TAINTED_REPOS=TONLY)
        layout.index()
        _, hint = layout.runtime_and_hint("tonly")
        assert hint is None


class TestGroupLevelExpectations:
    def test_two_repo_group_with_one_added_lists_the_other(self, layout: Layout):
        layout.index(VEND_A)  # the added repo has records
        assert layout.repos("add", VEND_A).exit_code == 0
        _, hint = layout.runtime_and_hint("vend")
        assert hint is not None
        assert hint["repos_not_indexed"] == [VEND_B]
        assert hint["repos_configured_no_records"] == []
        assert hint["add_command"] == "pipecat-context-hub repos add --config-only --group vend"
        assert "refresh_command" not in hint

    def test_single_repo_group_added_without_records_asks_for_refresh(self, layout: Layout):
        layout.index()
        assert layout.repos("add", SOLO).exit_code == 0
        _, hint = layout.runtime_and_hint("solo")
        assert hint is not None
        assert hint["repos_not_indexed"] == []
        assert hint["repos_configured_no_records"] == [SOLO]
        assert hint["refresh_command"] == "pipecat-context-hub refresh"
        assert "add_command" not in hint

    def test_single_repo_group_added_with_records_has_no_hint(self, layout: Layout):
        layout.index(SOLO)
        assert layout.repos("add", SOLO).exit_code == 0
        _, hint = layout.runtime_and_hint("solo")
        assert hint is None


class TestAddCommandIsAcceptedByRepos:
    # Layer under test: the click group, argv = add_command minus the leading
    # `pipecat-context-hub`. Not the shell, the console script or the Typer bridge.
    @pytest.mark.parametrize(
        ("query", "groups", "expected"),
        [
            ("vend", ["vend"], {VEND_A, VEND_B}),
            ("vend + solo", ["vend", "solo"], {VEND_A, VEND_B, SOLO}),
        ],
    )
    def test_exact_add_command_writes_the_missing_repos(
        self, layout: Layout, query: str, groups: list[str], expected: set[str]
    ):
        layout.index()
        _, hint = layout.runtime_and_hint(query)
        assert hint is not None
        assert hint["matched_groups"] == groups
        assert hint["add_command"] == "; ".join(
            f"pipecat-context-hub repos add --config-only --group {g}" for g in groups
        )

        for result in layout.run_add_command(hint["add_command"]):
            assert result.exit_code == 0, result.output

        assert layout.written() == set(hint["repos_not_indexed"]) == expected
        _, after = layout.runtime_and_hint(query)
        assert after is not None
        assert "add_command" not in after
        assert after["repos_not_indexed"] == []
        assert set(after["repos_configured_no_records"]) == expected


class TestShadowingDotenv:
    def test_config_only_is_accepted_and_plain_add_is_refused(self, layout: Layout):
        layout.index()
        (layout.cwd / ".env").write_text(f"{KEY}=acme/other\n")

        _, hint = layout.runtime_and_hint("vend")
        assert hint is not None
        add_command = hint["add_command"]
        assert "--config-only" in add_command

        plain = layout.repos("add", "--group", "vend")
        assert plain.exit_code == 1, plain.output
        assert layout.written() == set()

        # The exact add_command, under the shadowing layer: this is the mutation
        # check (drop --config-only from the C1 format and this assertion fails).
        for result in layout.run_add_command(add_command):
            assert result.exit_code == 0, result.output

        assert layout.written() == {VEND_A, VEND_B}
        assert not {VEND_A, VEND_B} & set(layout.effective())
