"""Regress the persisted graph corruption observed during plugin exploration."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import struct
from pathlib import Path
from typing import Any
from unittest.mock import patch

import chromadb
import pytest

from pipecat_context_hub.services.index import chroma_schema, hnsw_validation
from pipecat_context_hub.services.index.errors import (
    CorruptHnswGraphError,
    IncompatibleIndexFormatError,
)
from pipecat_context_hub.services.index.hnsw_validation import (
    HnswWriteGuard,
    validate_persisted_hnsw,
)
from pipecat_context_hub.services.index.vector import (
    COLLECTION_NAME,
    COMPACT_BACKUP_NAME,
    COMPACT_STAGING_NAME,
    VectorIndex,
)

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


class TestBoundedRead:
    """The walk never reads more than the bounds it checked (hnsw_validation:100/:135)."""

    def test_absolute_ceiling_applies_whatever_the_header_says(
        self, persisted_segment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A damaged header can make its own bound huge; the ceiling still holds."""
        root, segment = persisted_segment
        (segment / "link_lists.bin").write_bytes(_links([1, 0, 1, 0]))  # valid, 152 bytes
        monkeypatch.setattr(hnsw_validation, "MAX_LINK_LISTS_BYTES", 100)
        with pytest.raises(CorruptHnswGraphError, match="100-byte ceiling"):
            validate_persisted_hnsw(root, "latest")

    def test_growth_after_the_size_check_is_not_read(
        self, persisted_segment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The size is re-checked on the open descriptor before any read, so a
        file that grew after the stat is rejected by the bound, not read to EOF."""
        root, segment = persisted_segment
        links = segment / "link_lists.bin"
        links.write_bytes(_links([0, 0, 0, 0]) + b"\0" * 5000)  # > 1664-byte header bound
        real_stat = Path.stat

        def stale_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            result = real_stat(path, *args, **kwargs)
            if path == links:
                values = list(result)
                values[6] = 16  # what the first check saw
                return os.stat_result(values)
            return result

        monkeypatch.setattr(Path, "stat", stale_stat)
        with pytest.raises(CorruptHnswGraphError, match="capacity bound"):
            validate_persisted_hnsw(root, "latest")


class TestChromadbVersionGate:
    """hnswlib's private on-disk format is parsed fail-closed only on verified releases."""

    def test_installed_chromadb_is_a_verified_release(self) -> None:
        """Fails on a chromadb bump: re-run this file against the new release
        (the native-graph tests persist a real graph) and extend
        VERIFIED_CHROMADB_RELEASES."""
        chroma_schema.chromadb_format_verified.cache_clear()
        assert chroma_schema.chromadb_format_verified()

    def test_native_header_parses_as_the_verified_layout(self, tmp_path: Path) -> None:
        root = tmp_path / "native"
        client: Any = chromadb.PersistentClient(path=str(root))
        collection = client.create_collection(
            "latest",
            embedding_function=None,
            configuration={"hnsw": {"sync_threshold": 100, "batch_size": 100}},
        )
        collection.add(
            ids=[str(i) for i in range(150)],
            embeddings=[[float(i), 1.0, 2.0] for i in range(150)],
        )
        client.close()
        headers = list(root.glob("*/header.bin"))
        assert headers
        for header in headers:
            fields = struct.unpack("<i6Qii3QdQ", header.read_bytes()[:100])
            assert fields[0] == 1 and fields[2] >= fields[3] == 150
            hnsw_validation.check_hnsw_segment_dir(root, header.parent)

    @pytest.mark.parametrize("verified", [True, False])
    def test_unverified_release_warns_instead_of_failing_closed(
        self, persisted_segment, monkeypatch: pytest.MonkeyPatch, verified: bool
    ) -> None:
        root, segment = persisted_segment
        monkeypatch.setattr(hnsw_validation, "chromadb_format_verified", lambda: verified)
        monkeypatch.setattr(hnsw_validation, "_unverified_warned", False)
        for header, links in [
            (_header(version=2), _links([0, 0, 0, 0])),  # unknown header layout
            (_header(), struct.pack("<I", 13) + b"\0" * 12),  # structural mismatch
        ]:
            (segment / "header.bin").write_bytes(header)
            (segment / "link_lists.bin").write_bytes(links)
            if verified:
                with pytest.raises(CorruptHnswGraphError):
                    validate_persisted_hnsw(root, "latest")
            else:
                validate_persisted_hnsw(root, "latest")

    def test_unverified_release_still_enforces_the_size_bound(
        self, persisted_segment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, segment = persisted_segment
        monkeypatch.setattr(hnsw_validation, "chromadb_format_verified", lambda: False)
        with (segment / "link_lists.bin").open("r+b") as f:
            f.truncate(50_000_000)
        with pytest.raises(CorruptHnswGraphError, match="capacity bound"):
            validate_persisted_hnsw(root, "latest")


class TestCompactSwap:
    """compact() must never leave the index without a ``latest`` collection."""

    def test_compaction_runs_the_write_guard_per_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (vector.py:498): staging writes are checked like upserts,
        and a tripped guard keeps the old collection."""
        from tests.unit.test_index_store import _make_records

        monkeypatch.setattr("pipecat_context_hub.services.index.vector._CHROMA_BATCH_SIZE", 2)
        index = VectorIndex(tmp_path / "chroma")
        try:
            index.upsert(_make_records(5))
            with patch.object(
                HnswWriteGuard,
                "check",
                side_effect=CorruptHnswGraphError(tmp_path, reason="corrupt persisted HNSW"),
            ) as check:
                with pytest.raises(CorruptHnswGraphError):
                    index.compact()
            check.assert_called_once()  # stopped after the first staging batch
            assert index._collection.count() == 5
            assert [c.name for c in index._client.list_collections()] == ["latest"]
        finally:
            index.close()

    def test_failed_promotion_keeps_the_old_index(self, tmp_path: Path) -> None:
        """Regression (vector.py:513): if renaming staging into place fails, the
        old collection is still ``latest`` (it used to be deleted first)."""
        from tests.unit.test_index_store import _make_records

        index = VectorIndex(tmp_path / "chroma")
        try:
            index.upsert(_make_records(30))
            real_modify = index._collection.__class__.modify

            def modify(self: Any, *args: Any, **kwargs: Any) -> Any:
                if self.name == COMPACT_STAGING_NAME:
                    raise RuntimeError("rename failed")
                return real_modify(self, *args, **kwargs)

            with patch.object(index._collection.__class__, "modify", modify):
                with pytest.raises(RuntimeError, match="rename failed"):
                    index.compact()
            assert index._collection.count() == 30
            assert [c.name for c in index._client.list_collections()] == ["latest"]
        finally:
            index.close()

    @pytest.mark.parametrize("listing_fails", [False, True])
    def test_transient_error_after_a_failed_copy_is_not_fatal(
        self, tmp_path: Path, listing_fails: bool
    ) -> None:
        """Regression (vector.py:585): any get_collection(latest) error after a
        failed pre-swap copy was taken to mean "no latest", so a transient
        error (SQLite "database is locked") became a fatal CompactionSwapError
        telling the user to restore an index that was intact."""
        from pipecat_context_hub.services.index.errors import CompactionSwapError
        from tests.unit.test_index_store import _make_records

        index = VectorIndex(tmp_path / "chroma", create=True)
        try:
            index.upsert(_make_records(5))
            locked = RuntimeError("database is locked")
            with (
                patch.object(HnswWriteGuard, "check", side_effect=RuntimeError("copy failed")),
                patch.object(index._client, "get_collection", side_effect=locked),
                patch.object(
                    index._client,
                    "list_collections",
                    side_effect=locked if listing_fails else index._client.list_collections,
                ),
                pytest.raises(RuntimeError, match="copy failed") as excinfo,
            ):
                index.compact()
            assert not isinstance(excinfo.value, CompactionSwapError)
            assert index._collection.count() == 5
            names = sorted(c.name for c in index._client.list_collections())
            # Unknown state (listing failed): nothing is deleted.
            assert names == (
                sorted([COLLECTION_NAME, COMPACT_STAGING_NAME])
                if listing_fails
                else [COLLECTION_NAME]
            )
        finally:
            index.close()

    def test_failed_promotion_and_rollback_is_fatal_and_recoverable(self, tmp_path: Path) -> None:
        """Regression (vector.py:549): when both the promotion and the rollback
        rename fail, compact() used to swallow the rollback failure, delete the
        staging copy and re-raise a plain error that refresh logs as "index
        valid" (exit 0). It must raise a FatalIndexError, destroy nothing, and
        leave a state readers reject and refresh's open restores."""
        from pipecat_context_hub.services.index.errors import (
            CompactionSwapError,
            InterruptedCompactionError,
        )
        from pipecat_context_hub.shared.interfaces import FatalIndexError
        from tests.unit.test_index_store import _make_records

        chroma = tmp_path / "chroma"
        index = VectorIndex(chroma)
        try:
            index.upsert(_make_records(30))
            real_modify = index._collection.__class__.modify

            def modify(self: Any, *args: Any, **kwargs: Any) -> Any:
                if kwargs.get("name") == COLLECTION_NAME:  # promotion and rollback
                    raise RuntimeError("disk full")
                return real_modify(self, *args, **kwargs)

            with patch.object(index._collection.__class__, "modify", modify):
                with pytest.raises(CompactionSwapError) as excinfo:
                    index.compact()
            assert isinstance(excinfo.value, FatalIndexError)
            assert sorted(c.name for c in index._client.list_collections()) == [
                COMPACT_STAGING_NAME,
                COMPACT_BACKUP_NAME,
            ], "the verified staging copy was destroyed mid-swap"
        finally:
            index.close()

        with pytest.raises(InterruptedCompactionError):
            VectorIndex(chroma)  # a reader
        index = VectorIndex(chroma, recover=True)  # how refresh opens it
        try:
            assert index._collection.count() == 30
            assert index._collection.name == COLLECTION_NAME
        finally:
            index.close()

    def test_parking_rename_that_commits_then_raises_is_fatal(self, tmp_path: Path) -> None:
        """Regression (vector.py:551): the fatal decision used to depend on which
        rename raised. If parking the old collection commits and then raises,
        compact() deleted the staging copy and re-raised a plain error that
        refresh reports as "index valid but larger" (exit 0) with no ``latest``."""
        from pipecat_context_hub.services.index.errors import CompactionSwapError
        from tests.unit.test_index_store import _make_records

        index = VectorIndex(tmp_path / "chroma")
        try:
            index.upsert(_make_records(30))
            real_modify = index._collection.__class__.modify

            def modify(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = real_modify(self, *args, **kwargs)
                if kwargs.get("name") == COMPACT_BACKUP_NAME:
                    raise RuntimeError("raised after commit")
                return result

            with patch.object(index._collection.__class__, "modify", modify):
                with pytest.raises(CompactionSwapError):
                    index.compact()
            assert sorted(c.name for c in index._client.list_collections()) == [
                COMPACT_STAGING_NAME,
                COMPACT_BACKUP_NAME,
            ], "the verified staging copy was destroyed mid-swap"
        finally:
            index.close()

    def test_crash_between_renames_is_recovered_on_open(self, tmp_path: Path) -> None:
        """A process killed after parking the old collection must not reopen
        as an empty ``latest`` while FTS still holds every record."""
        from tests.unit.test_index_store import _make_records

        chroma = tmp_path / "chroma"
        index = VectorIndex(chroma)
        index.upsert(_make_records(20))
        index._collection.modify(name=COMPACT_BACKUP_NAME)  # state after the first rename
        index.close()

        index = VectorIndex(chroma, recover=True)  # how refresh opens it
        try:
            assert index._collection.count() == 20
            assert [c.name for c in index._client.list_collections()] == ["latest"]
        finally:
            index.close()

    def test_reader_open_never_repairs_an_interrupted_compaction(self, tmp_path: Path) -> None:
        """Regression (vector.py:409, vector.py:443): readers hold the refresh
        lock only shared, so their open must not rename collections (two of
        them raced the rename). They get a typed "run refresh" error, the index
        is left as it was, and the half-opened client is released."""
        from chromadb.api.shared_system_client import SharedSystemClient

        from pipecat_context_hub.services.index.errors import InterruptedCompactionError
        from tests.unit.test_index_store import _make_records

        chroma = tmp_path / "chroma"
        index = VectorIndex(chroma)
        index.upsert(_make_records(20))
        index._collection.modify(name=COMPACT_BACKUP_NAME)  # state after the first rename
        index.close()

        with pytest.raises(InterruptedCompactionError, match="refresh"):
            VectorIndex(chroma)
        assert str(chroma) not in SharedSystemClient._identifier_to_refcount  # no leak
        client: Any = chromadb.PersistentClient(path=str(chroma))
        try:
            assert [c.name for c in client.list_collections()] == [COMPACT_BACKUP_NAME]
        finally:
            client.close()


class TestReaderNeverCreates:
    """Regression (vector.py:417): a reader open used get_or_create, so a lost
    ``latest`` came back as an empty collection beside a populated FTS index
    (vector search silently empty) and a missing index was created."""

    def test_reader_on_a_missing_index_creates_nothing(self, tmp_path: Path) -> None:
        from pipecat_context_hub.services.index.errors import MissingVectorCollectionError

        chroma = tmp_path / "chroma"
        with pytest.raises(MissingVectorCollectionError, match="refresh"):
            VectorIndex(chroma, create=False)
        assert not chroma.exists()

    def test_reader_on_a_lost_collection_reports_it(self, tmp_path: Path) -> None:
        from pipecat_context_hub.services.index.errors import MissingVectorCollectionError

        chroma = tmp_path / "chroma"
        VectorIndex(chroma).close()  # built ...
        client: Any = chromadb.PersistentClient(path=str(chroma))
        try:
            client.delete_collection(COLLECTION_NAME)  # ... then lost
        finally:
            client.close()

        with pytest.raises(MissingVectorCollectionError, match="refresh --force"):
            VectorIndex(chroma, create=False)
        client = chromadb.PersistentClient(path=str(chroma))
        try:
            assert client.list_collections() == []
        finally:
            client.close()
        VectorIndex(chroma).close()  # writers (refresh) still create it


_MASTER = "SELECT name FROM sqlite_master"


class TestQueryChromaSqlite:
    """The single reader of Chroma's private sqlite schema (chroma_schema.py)."""

    def test_missing_or_non_sqlite_db_reads_as_none(self, tmp_path: Path) -> None:
        assert chroma_schema.query_chroma_sqlite(tmp_path, _MASTER, immutable=True) is None
        (tmp_path / "chroma.sqlite3").write_bytes(b"not a database")
        for immutable in (True, False):
            assert chroma_schema.query_chroma_sqlite(tmp_path, _MASTER, immutable=immutable) is None

    def test_hot_journal_is_rolled_back_before_reading(self, tmp_path: Path) -> None:
        """Regression (chroma_schema.py:65): a writer killed mid-transaction
        leaves a hot rollback journal and uncommitted pages in the database.
        ``immutable=1`` ignored the journal, so the pre-open probes read a
        state Chroma never opens (it rolls the journal back first)."""
        import subprocess
        import sys

        db = tmp_path / "chroma.sqlite3"
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, x)")
            conn.executemany("INSERT INTO t VALUES (?, 'old')", [(i,) for i in range(5000)])
        writer = (
            "import os, sqlite3, sys\n"
            "c = sqlite3.connect(sys.argv[1])\n"
            "c.execute('PRAGMA cache_size=1')\n"  # spill dirty pages to the db file
            "c.execute('BEGIN')\n"
            "c.execute(\"UPDATE t SET x='new'\")\n"
            "os._exit(0)\n"  # die mid-transaction
        )
        subprocess.run([sys.executable, "-c", writer, str(db)], check=True)
        assert (tmp_path / "chroma.sqlite3-journal").exists()

        rows = chroma_schema.query_chroma_sqlite(
            tmp_path, "SELECT x, count(*) FROM t GROUP BY x", immutable=True
        )
        assert rows == [("old", 5000)]

    def test_relative_chroma_path_is_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Path.as_uri()`` rejects relative paths; orphan-segment cleanup used
        to raise ValueError for a relative chroma path."""
        with sqlite3.connect(tmp_path / "chroma.sqlite3") as conn:
            conn.execute("CREATE TABLE segments (id TEXT)")
            conn.execute("INSERT INTO segments VALUES ('x')")
        conn.close()
        monkeypatch.chdir(tmp_path.parent)
        rows = chroma_schema.query_chroma_sqlite(
            Path(tmp_path.name), "SELECT id FROM segments", immutable=False
        )
        assert rows == [("x",)]
