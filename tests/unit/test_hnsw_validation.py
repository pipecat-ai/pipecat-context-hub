"""Regress the persisted graph corruption observed during plugin exploration."""

from __future__ import annotations

import os
import sqlite3
import struct
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import chromadb
import pytest

from pipecat_context_hub.services.index.errors import IncompatibleIndexFormatError
from pipecat_context_hub.services.index.hnsw_validation import (
    _open_segment_file,
    _open_segment_file_without_dir_fd,
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
    (segment / "link_lists.bin").write_bytes(b"\0" * 1000)
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


def test_dir_fd_unavailable_uses_safe_path_fallback(
    persisted_segment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows' missing dir_fd support must not reject healthy active segments."""
    root, segment = persisted_segment
    real_open = os.open

    def no_dir_fd_open(path, flags, *args, **kwargs):
        if "dir_fd" in kwargs:
            raise NotImplementedError("dir_fd is unavailable")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        "pipecat_context_hub.services.index.hnsw_validation.os.open", no_dir_fd_open
    )
    fd, info = _open_segment_file(root, segment.name, "link_lists.bin")
    try:
        assert info.st_size == 1000
        assert os.fstat(fd).st_size == 1000
    finally:
        os.close(fd)


def test_windows_path_fallback_accepts_safe_regular_files(
    persisted_segment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, segment = persisted_segment
    monkeypatch.setattr(os, "name", "nt")
    fd, info = _open_segment_file_without_dir_fd(root, segment.name, "link_lists.bin")
    try:
        assert info.st_size == 1000
        assert os.fstat(fd).st_size == 1000
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement fixture")
def test_path_fallback_rechecks_parent_identity_after_open(
    persisted_segment: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A segment-directory swap must not redirect the Windows-compatible fallback."""
    root, segment = persisted_segment
    outside = tmp_path / "outside-segment"
    outside.mkdir()
    (outside / "link_lists.bin").write_bytes(b"outside")
    (outside / "header.bin").write_bytes((segment / "header.bin").read_bytes())
    real_open = os.open
    swapped = False

    def swap_parent(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == segment / "link_lists.bin" and not swapped:
            swapped = True
            moved = root / f"{segment.name}.moved"
            segment.rename(moved)
            (root / segment.name).symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("pipecat_context_hub.services.index.hnsw_validation.os.open", swap_parent)
    with pytest.raises(OSError, match="regular file"):
        _open_segment_file_without_dir_fd(root, segment.name, "link_lists.bin")


def test_malformed_header_closes_links_descriptor(
    persisted_segment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejected headers must not leak one links descriptor per probe."""
    root, segment = persisted_segment
    (segment / "header.bin").write_bytes(_header(version=2))
    before = len(os.listdir("/proc/self/fd")) if Path("/proc/self/fd").is_dir() else None
    for _ in range(20):
        with pytest.raises(IncompatibleIndexFormatError):
            validate_persisted_hnsw(root, "latest")
    if before is not None:
        after = len(os.listdir("/proc/self/fd"))
        assert after <= before + 1


def test_new_index_without_persisted_graph_is_allowed(tmp_path: Path) -> None:
    validate_persisted_hnsw(tmp_path / "absent", "latest")


def test_wal_presence_fails_closed_before_stale_probe(
    persisted_segment: tuple[Path, Path],
) -> None:
    """The immutable probe must not ignore an uncheckpointed Chroma WAL."""
    root, _ = persisted_segment
    (root / "chroma.sqlite3-wal").write_bytes(b"uncheckpointed state")
    with pytest.raises(IncompatibleIndexFormatError, match="WAL"):
        validate_persisted_hnsw(root, "latest")


def test_unreadable_links_file_uses_recovery_error(
    persisted_segment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, segment = persisted_segment
    real_stat = Path.stat

    def fail_links(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == segment / "link_lists.bin":
            raise OSError("permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_links)
    with pytest.raises(IncompatibleIndexFormatError, match="unreadable persisted HNSW links"):
        validate_persisted_hnsw(root, "latest")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
@pytest.mark.parametrize("name", ["header.bin", "link_lists.bin"])
def test_symlinked_hnsw_files_are_rejected(
    persisted_segment: tuple[Path, Path], name: str, tmp_path: Path
) -> None:
    root, segment = persisted_segment
    target = tmp_path / f"outside-{name}"
    target.write_bytes((segment / name).read_bytes())
    (segment / name).unlink()
    (segment / name).symlink_to(target)
    with pytest.raises(IncompatibleIndexFormatError, match="unsafe persisted HNSW"):
        validate_persisted_hnsw(root, "latest")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlinked_hnsw_segment_directory_is_rejected(
    persisted_segment: tuple[Path, Path], tmp_path: Path
) -> None:
    root, segment = persisted_segment
    outside = tmp_path / "outside-segment"
    outside.mkdir()
    (outside / "header.bin").write_bytes((segment / "header.bin").read_bytes())
    (outside / "link_lists.bin").write_bytes((segment / "link_lists.bin").read_bytes())
    segment_name = segment.name
    segment.rename(root / f"{segment_name}.real")
    (root / segment_name).symlink_to(outside, target_is_directory=True)
    with pytest.raises(IncompatibleIndexFormatError, match="unsafe persisted HNSW links"):
        validate_persisted_hnsw(root, "latest")


@pytest.mark.parametrize("field", ["capacity", "max_level", "max_m"])
def test_implausible_hnsw_header_values_are_rejected(
    persisted_segment: tuple[Path, Path], field: str
) -> None:
    root, segment = persisted_segment
    values = [1, 0, 8, 4, 152, 144, 132, 2, 0, 16, 32, 16, 0.36, 100]
    values[{"capacity": 2, "max_level": 7, "max_m": 9}[field]] = {
        "capacity": 100_000_001,
        "max_level": 65,
        "max_m": 1025,
    }[field]
    (segment / "header.bin").write_bytes(struct.pack("<i6Qii3QdQ", *values))
    with pytest.raises(IncompatibleIndexFormatError, match="unsupported persisted HNSW header"):
        validate_persisted_hnsw(root, "latest")


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


def test_chromadb_dependency_stays_within_validated_hnsw_format_line() -> None:
    """Future persisted formats must not bypass the pre-open validation boundary."""
    from packaging.specifiers import SpecifierSet

    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    with (root / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)

    declared = next(
        dependency for dependency in project["dependencies"] if dependency.startswith("chromadb")
    )
    assert declared == "chromadb>=1.5,<1.6"
    locked = next(package for package in lock["package"] if package["name"] == "chromadb")
    assert locked["version"] in SpecifierSet(">=1.5,<1.6")
