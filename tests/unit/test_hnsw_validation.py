"""Regress the persisted graph corruption observed during plugin exploration."""

from __future__ import annotations

import os
import sqlite3
import struct
from pathlib import Path
from typing import Any
from unittest.mock import patch

import chromadb
import pytest

from pipecat_context_hub.services.index.errors import IncompatibleIndexFormatError
from pipecat_context_hub.services.index.hnsw_validation import (
    HnswWriteGuard,
    validate_persisted_hnsw,
)
from pipecat_context_hub.services.index.vector import VectorIndex

SEGMENT_ID = "6719df4e-c43a-4cd8-b8ce-b9b5c9fb4400"
# Frozen header bytes and logical link-list size from the actual failed index.
FIELD_HEADER = bytes.fromhex(
    "0100000000000000000000000000100000000000256c0800000000008c06000000000000"
    "84060000000000008400000000000000030000004e050000100000000000000020000000"
    "000000001000000000000000fe822b654715d73f6400000000000000"
)
FIELD_LINK_SIZE = 12_090_482_427_024


def _header(version: int = 1) -> bytes:
    return struct.pack("<i6Qii3QdQ", version, 0, 8, 4, 152, 144, 132, 2, 0, 16, 32, 16, 0.36, 100)


def _links(levels: list[int], *, neighbours: int = 1, max_m: int = 16) -> bytes:
    """A structurally valid link_lists.bin: one record per element.

    ``levels[i]`` is element i's number of upper levels (0 = empty record).
    Each level block is ``max_m*4+4`` bytes: uint32 neighbour count, neighbour
    ids, zero padding.
    """
    out = b""
    for n in levels:
        if n == 0:
            out += struct.pack("<I", 0)
            continue
        block = struct.pack("<I", neighbours) + struct.pack("<I", 0) * neighbours
        block += b"\0" * (max_m * 4 + 4 - len(block))
        out += struct.pack("<I", len(block) * n) + block * n
    return out


@pytest.fixture()
def persisted_segment(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "chroma"
    root.mkdir()
    with sqlite3.connect(root / "chroma.sqlite3") as conn:
        conn.execute("CREATE TABLE collections (id TEXT, name TEXT)")
        conn.execute("INSERT INTO collections VALUES ('collection', 'latest')")
        conn.execute("CREATE TABLE segments (id TEXT, type TEXT, collection TEXT)")
        conn.execute(
            "INSERT INTO segments VALUES (?, ?, 'collection')",
            (SEGMENT_ID, "urn:chroma:segment/vector/hnsw-local-persisted"),
        )
    conn.close()
    segment = root / SEGMENT_ID
    segment.mkdir()
    (segment / "header.bin").write_bytes(_header())
    (segment / "link_lists.bin").write_bytes(_links([0, 0, 0, 0]))
    return root, segment


def test_field_corruption_rejected_before_native_open(
    persisted_segment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, segment = persisted_segment
    (segment / "header.bin").write_bytes(FIELD_HEADER)
    # Do not allocate a 12 TB test file, particularly on non-sparse filesystems.
    real_stat = Path.stat

    def field_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        if path == segment / "link_lists.bin":
            values = list(result)
            values[6] = FIELD_LINK_SIZE
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", field_stat)
    before = {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with patch("pipecat_context_hub.services.index.vector.chromadb.PersistentClient") as native:
        with pytest.raises(IncompatibleIndexFormatError, match="corrupt persisted HNSW") as error:
            VectorIndex(root)
        native.assert_not_called()
    assert str(FIELD_LINK_SIZE) in str(error.value)
    assert "289406976" in str(error.value)  # capacity-based conservative bound
    assert "refresh --force --reset-index" in str(error.value)
    assert {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert not (root / "chroma.sqlite3-wal").exists()
    assert not (root / "chroma.sqlite3-shm").exists()


def test_capacity_and_orphan_segments_are_allowed(persisted_segment: tuple[Path, Path]) -> None:
    root, _ = persisted_segment
    orphan = root / "00000000-0000-0000-0000-000000000000"
    orphan.mkdir()
    (orphan / "header.bin").write_bytes(b"invalid")
    (orphan / "link_lists.bin").write_bytes(b"invalid")
    validate_persisted_hnsw(root, "latest")
    # No active matching collection: a stale graph must not block another one.
    validate_persisted_hnsw(root, "another")


@pytest.mark.parametrize("header", [b"truncated", _header(version=2)])
def test_unreadable_header_refuses_native_open(
    persisted_segment: tuple[Path, Path], header: bytes
) -> None:
    root, segment = persisted_segment
    (segment / "header.bin").write_bytes(header)
    with patch("pipecat_context_hub.services.index.vector.chromadb.PersistentClient") as native:
        with pytest.raises(IncompatibleIndexFormatError, match="persisted HNSW header"):
            VectorIndex(root)
        native.assert_not_called()


def test_new_index_without_persisted_graph_is_allowed(tmp_path: Path) -> None:
    validate_persisted_hnsw(tmp_path / "absent", "latest")


def test_healthy_native_graph_reopens_and_queries(tmp_path: Path) -> None:
    """Verify compatibility against actual Chroma persistence, not a mocked header."""
    root = tmp_path / "native"
    # Chroma's ClientAPI stub omits the runtime close() API, as in VectorIndex.
    client: Any = chromadb.PersistentClient(path=str(root))
    collection = client.create_collection(
        "latest",
        embedding_function=None,
        configuration={"hnsw": {"sync_threshold": 100, "batch_size": 100}},
    )
    collection.add(
        ids=[str(i) for i in range(110)],
        embeddings=[[float(i), 1.0, 2.0] for i in range(110)],
    )
    client.close()
    assert list(root.glob("*/header.bin")), "must exercise an actually persisted HNSW graph"
    index = VectorIndex(root)
    try:
        assert index._collection.count() == 110
        assert index._collection.query(query_embeddings=[[0.0, 1.0, 2.0]], n_results=1)["ids"] == [
            ["0"]
        ]
    finally:
        index.close()


class TestHnswWriteGuard:
    """The guard re-checks segments touched since it was created."""

    def test_healthy_segment_passes(self, persisted_segment: tuple[Path, Path]) -> None:
        root, _ = persisted_segment
        HnswWriteGuard(root).check()

    def test_runaway_segment_written_after_guard_creation_fails(
        self, persisted_segment: tuple[Path, Path]
    ) -> None:
        root, segment = persisted_segment
        guard = HnswWriteGuard(root)
        with (segment / "link_lists.bin").open("r+b") as f:
            f.truncate(50_000_000)  # sparse; far beyond the capacity-8 bound
        with pytest.raises(IncompatibleIndexFormatError, match="corrupt persisted HNSW"):
            guard.check()

    def test_old_oversized_orphan_is_ignored(self, persisted_segment: tuple[Path, Path]) -> None:
        root, segment = persisted_segment
        with (segment / "link_lists.bin").open("r+b") as f:
            f.truncate(50_000_000)
        os.utime(segment / "link_lists.bin", (1, 1))  # untouched since long before the guard
        HnswWriteGuard(root).check()

    def test_non_segment_entries_are_ignored(self, persisted_segment: tuple[Path, Path]) -> None:
        root, _ = persisted_segment
        (root / "not-a-uuid").mkdir()
        HnswWriteGuard(root).check()

    def test_missing_chroma_dir_is_ignored(self, tmp_path: Path) -> None:
        HnswWriteGuard(tmp_path / "absent").check()


def test_vector_upsert_runs_write_guard(tmp_path: Path) -> None:
    """A guard failure must surface from VectorIndex.upsert, not be swallowed."""
    from tests.unit.test_index_store import _make_record

    index = VectorIndex(tmp_path / "chroma")
    try:
        with patch.object(
            HnswWriteGuard,
            "check",
            side_effect=IncompatibleIndexFormatError(tmp_path, reason="corrupt persisted HNSW"),
        ) as check:
            with pytest.raises(IncompatibleIndexFormatError, match="corrupt persisted HNSW"):
                index.upsert([_make_record()])
        check.assert_called_once()
    finally:
        index.close()


def test_vector_upsert_checks_guard_after_every_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One large upsert must not keep growing a runaway file past the first bad batch."""
    from tests.unit.test_index_store import _make_records

    monkeypatch.setattr("pipecat_context_hub.services.index.vector._CHROMA_BATCH_SIZE", 2)
    index = VectorIndex(tmp_path / "chroma")
    try:
        with patch.object(HnswWriteGuard, "check") as check:
            index.upsert(_make_records(5))
        assert check.call_count == 3  # ceil(5 / 2) batches

        with patch.object(
            HnswWriteGuard,
            "check",
            side_effect=IncompatibleIndexFormatError(tmp_path, reason="corrupt persisted HNSW"),
        ) as failing:
            with pytest.raises(IncompatibleIndexFormatError):
                index.upsert(_make_records(10)[5:])  # 5 new ids, batches of 2
        assert failing.call_count == 1
        assert index._collection.count() == 5 + 2  # aborted after the first new batch
    finally:
        index.close()


class TestStructuralWalk:
    """A within-bound but inconsistent graph (two racing writers) must be rejected.

    Spike, 2026-10-15: after racing refreshes the file had the healthy size and
    header, yet the next load SIGSEGVed or ran away. A walk of the records
    found the damage before that load.
    """

    def test_valid_records_with_upper_levels_pass(self, persisted_segment) -> None:
        root, segment = persisted_segment
        (segment / "link_lists.bin").write_bytes(_links([0, 1, 2, 0]))
        validate_persisted_hnsw(root, "latest")

    @pytest.mark.parametrize(
        ("mutate", "match"),
        [
            (lambda d: d + b"\0\0\0\0", "trailing bytes"),
            (lambda d: d[:-2], "truncated|past the end"),
            (lambda d: struct.pack("<I", 13) + d[4:], "not a multiple"),
            (lambda d: struct.pack("<I", 68 * 9) + d[4:], "not a multiple|levels"),
        ],
    )
    def test_structural_damage_is_rejected(self, persisted_segment, mutate, match) -> None:
        root, segment = persisted_segment
        (segment / "link_lists.bin").write_bytes(mutate(_links([1, 0, 1, 0])))
        with pytest.raises(
            IncompatibleIndexFormatError, match=f"corrupt persisted HNSW.*({match})"
        ):
            validate_persisted_hnsw(root, "latest")

    def test_neighbour_id_out_of_range_is_rejected(self, persisted_segment) -> None:
        root, segment = persisted_segment
        data = bytearray(_links([1, 0, 0, 0]))
        struct.pack_into("<I", data, 8, 4)  # element 0, level 1, neighbour 0 -> id 4 (count is 4)
        (segment / "link_lists.bin").write_bytes(bytes(data))
        with pytest.raises(IncompatibleIndexFormatError, match="neighbour 4"):
            validate_persisted_hnsw(root, "latest")

    def test_neighbour_count_over_max_m_is_rejected(self, persisted_segment) -> None:
        root, segment = persisted_segment
        data = bytearray(_links([1, 0, 0, 0]))
        struct.pack_into("<I", data, 4, 17)  # 17 > maxM 16
        (segment / "link_lists.bin").write_bytes(bytes(data))
        with pytest.raises(IncompatibleIndexFormatError, match="17 neighbours"):
            validate_persisted_hnsw(root, "latest")

    def test_write_guard_runs_the_walk(self, persisted_segment) -> None:
        root, segment = persisted_segment
        guard = HnswWriteGuard(root)
        (segment / "link_lists.bin").write_bytes(struct.pack("<I", 13) + b"\0" * 12)
        with pytest.raises(IncompatibleIndexFormatError, match="not a multiple"):
            guard.check()


class TestCompact:
    """`refresh --force` delete + re-add bloats the graph; compact() rebuilds it."""

    @staticmethod
    def _graph_count(chroma: Path) -> int:
        seg = next(d for d in chroma.iterdir() if d.is_dir() and (d / "header.bin").exists())
        return int(struct.unpack("<i6Qii3QdQ", (seg / "header.bin").read_bytes()[:100])[3])

    def test_compact_removes_dead_elements_and_keeps_records(self, tmp_path: Path) -> None:
        from tests.unit.test_index_store import _make_records

        chroma = tmp_path / "chroma"
        index = VectorIndex(chroma)
        records = _make_records(1200)
        index.upsert(records)
        for _ in range(3):  # what --force does: delete everything, add it back
            index.delete_by_content_type("doc")
            index.upsert(records)
        index.close()
        index = VectorIndex(chroma)
        index.compact()
        index.close()

        index = VectorIndex(chroma)
        try:
            assert index._collection.count() == 1200
            got = index._collection.get(ids=["chunk-7"], include=["documents", "embeddings"])
            assert got["documents"][0] == records[7].content
            assert list(got["embeddings"][0]) == pytest.approx(records[7].embedding, abs=1e-5)
        finally:
            index.close()
        assert self._graph_count(chroma) == 1200
        validate_persisted_hnsw(chroma.resolve(), "latest")
        segments = [d for d in chroma.iterdir() if d.is_dir()]
        assert len(segments) == 1, "orphaned segment dir of the old collection was not removed"

    def test_failed_copy_keeps_the_old_index(self, tmp_path: Path) -> None:
        from tests.unit.test_index_store import _make_records

        index = VectorIndex(tmp_path / "chroma")
        try:
            index.upsert(_make_records(50))
            with patch.object(index._collection.__class__, "get", side_effect=RuntimeError("boom")):
                with pytest.raises(RuntimeError, match="boom"):
                    index.compact()
            assert index._collection.count() == 50
            assert [c.name for c in index._client.list_collections()] == ["latest"]
        finally:
            index.close()
