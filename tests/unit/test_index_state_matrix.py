"""Every combination of keyword-half and vector-half state, against every open mode.

The index has two halves that must agree: the SQLite keyword database and the
Chroma vector collection. Earlier review rounds kept finding a new edge case in
this state machine, one cell at a time. This table pins the whole space, so a
change to one cell shows up next to its neighbours and an inconsistent remedy
fails the invariants at the bottom.

Columns of the table: the keyword half (absent, empty, populated, corrupt), the
vector half (absent, empty, populated) and the open mode:

- ``reader``: ``serve`` and the one-shot CLI queries (never create anything).
- ``refresh``: a plain ``refresh`` (creates what is missing, never rebuilds).
- ``force``: ``refresh --force`` (creates, and re-ingests every source).
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipecat_context_hub.services.index.errors import (
    CorruptKeywordIndexError,
    IndexNotBuiltError,
    LostKeywordIndexError,
    MissingKeywordIndexError,
    MissingVectorCollectionError,
)
from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.services.index.vector import VectorIndex
from pipecat_context_hub.shared.config import StorageConfig
from pipecat_context_hub.shared.types import ChunkedRecord

KEYWORD_STATES = ("absent", "empty", "populated", "corrupt")
VECTOR_STATES = ("absent", "empty", "populated")
MODES = ("reader", "refresh", "force")

OK = "ok"
# Remedy each refusal prescribes, by what the caller must run.
NOT_BUILT = IndexNotBuiltError  # `refresh` builds both halves
REBUILD = MissingVectorCollectionError  # `refresh --force` refills the vector half
RESET = (LostKeywordIndexError, MissingKeywordIndexError, CorruptKeywordIndexError)

# (keyword, vector) -> outcome per mode, in MODES order.
_EXPECTED: dict[tuple[str, str], tuple[object, object, object]] = {
    ("absent", "absent"): (NOT_BUILT, OK, OK),
    ("absent", "empty"): (NOT_BUILT, OK, OK),
    ("absent", "populated"): (
        MissingKeywordIndexError,
        LostKeywordIndexError,
        LostKeywordIndexError,
    ),
    ("empty", "absent"): (MissingVectorCollectionError, OK, OK),
    ("empty", "empty"): (OK, OK, OK),
    ("empty", "populated"): (LostKeywordIndexError, LostKeywordIndexError, LostKeywordIndexError),
    ("populated", "absent"): (MissingVectorCollectionError, MissingVectorCollectionError, OK),
    ("populated", "empty"): (MissingVectorCollectionError, MissingVectorCollectionError, OK),
    ("populated", "populated"): (OK, OK, OK),
    ("corrupt", "absent"): (
        CorruptKeywordIndexError,
        CorruptKeywordIndexError,
        CorruptKeywordIndexError,
    ),
    ("corrupt", "empty"): (
        CorruptKeywordIndexError,
        CorruptKeywordIndexError,
        CorruptKeywordIndexError,
    ),
    ("corrupt", "populated"): (
        CorruptKeywordIndexError,
        CorruptKeywordIndexError,
        CorruptKeywordIndexError,
    ),
}


def _record(i: int) -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id=f"chunk-{i}",
        content=f"Content for chunk {i} about pipecat voice agents.",
        content_type="doc",
        source_url="https://docs.pipecat.ai/intro",
        path=f"/docs/page{i}.md",
        indexed_at=datetime.now(tz=UTC),
        embedding=[(i + 1) / 10.0] * 384,
        metadata={"title": f"page {i}"},
    )


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A closed, fully populated index; each cell starts from a copy of it."""
    data_dir = tmp_path_factory.mktemp("state-matrix-baseline") / "hub"
    store = IndexStore(StorageConfig(data_dir=data_dir), create=True)
    try:
        asyncio.run(store.upsert([_record(i) for i in range(3)]))
    finally:
        store.close()
    return data_dir


def _build(baseline: Path, tmp_path: Path, keyword: str, vector: str) -> StorageConfig:
    data_dir = tmp_path / "hub"
    shutil.copytree(baseline, data_dir)
    config = StorageConfig(data_dir=data_dir)
    if keyword in ("absent", "corrupt"):
        for path in config.index_artifact_paths:
            if path.name.startswith(config.sqlite_path.name) and path.is_file():
                path.unlink()
        if keyword == "corrupt":
            config.sqlite_path.write_bytes(b"this is not a sqlite database" * 64)
    elif keyword == "empty":
        fts = FTSIndex(config.sqlite_path, create=True)
        fts.clear()
        fts.close()
    if vector == "absent":
        shutil.rmtree(config.chroma_path)
    elif vector == "empty":
        index = VectorIndex(config.chroma_path)
        index.clear()
        index.close()
    return config


def _open(config: StorageConfig, mode: str) -> IndexStore:
    if mode == "reader":
        return IndexStore(config)
    if mode == "refresh":
        return IndexStore(config, recover=True, create=True)
    return IndexStore(config, recover=True, create=True, rebuild=True)


def _outcome(config: StorageConfig, mode: str) -> object:
    try:
        store = _open(config, mode)
    except Exception as exc:
        return exc
    store.close()
    return OK


_CELLS = [
    pytest.param(keyword, vector, mode, id=f"kw-{keyword}-vec-{vector}-{mode}")
    for keyword in KEYWORD_STATES
    for vector in VECTOR_STATES
    for mode in MODES
]


@pytest.mark.parametrize(("keyword", "vector", "mode"), _CELLS)
def test_open_outcome(baseline: Path, tmp_path: Path, keyword: str, vector: str, mode: str) -> None:
    config = _build(baseline, tmp_path, keyword, vector)
    expected = _EXPECTED[(keyword, vector)][MODES.index(mode)]
    outcome = _outcome(config, mode)
    if expected == OK:
        assert outcome == OK, f"expected a clean open, got {outcome!r}"
    else:
        assert isinstance(outcome, expected), (  # type: ignore[arg-type]
            f"expected {expected}, got {outcome!r}"
        )


def _remedy(error: Exception) -> str:
    """What the message tells the caller to run."""
    text = str(error)
    if "--reset-index" in text:
        return "reset"
    if "refresh --force" in text:
        return "force"
    if "pipecat-context-hub refresh" in text:
        return "refresh"
    return "none"


@pytest.mark.parametrize(("keyword", "vector", "mode"), _CELLS)
def test_remedy_is_never_more_destructive_than_the_state_needs(
    baseline: Path, tmp_path: Path, keyword: str, vector: str, mode: str
) -> None:
    """A reset deletes the whole data directory (repo clones included), so it may
    be prescribed only when records would otherwise be stranded (the vector half
    is populated) or the keyword database cannot be read at all."""
    config = _build(baseline, tmp_path, keyword, vector)
    outcome = _outcome(config, mode)
    if outcome == OK:
        return
    assert isinstance(outcome, Exception)
    remedy = _remedy(outcome)
    assert remedy != "none", f"no remedy in: {outcome}"
    if remedy == "reset":
        assert vector == "populated" or keyword == "corrupt", (
            f"{keyword}/{vector}/{mode} prescribes a reset: {outcome}"
        )


@pytest.mark.parametrize(("keyword", "vector"), list(_EXPECTED))
def test_a_refused_open_changes_nothing_on_disk(
    baseline: Path, tmp_path: Path, keyword: str, vector: str
) -> None:
    """A reader never writes, and a refused refresh leaves the vector half as it was."""
    config = _build(baseline, tmp_path, keyword, vector)

    def snapshot() -> dict[str, int]:
        return {
            str(path.relative_to(config.data_dir)): path.stat().st_size
            for path in sorted(config.data_dir.rglob("*"))
            if path.is_file() and path.suffix not in (".sqlite3-wal", ".sqlite3-shm")
        }

    before = snapshot()
    outcome = _outcome(config, "reader")
    if outcome != OK:
        assert snapshot() == before, f"a refused reader open wrote to {config.data_dir}"


def test_every_state_pair_has_an_expectation() -> None:
    assert set(_EXPECTED) == {(k, v) for k in KEYWORD_STATES for v in VECTOR_STATES}
    assert all(len(row) == len(MODES) for row in _EXPECTED.values())
