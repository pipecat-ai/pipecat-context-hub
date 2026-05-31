"""Tests for the pre-1.0 ChromaDB on-disk format detection probe.

The probe (`_detect_incompatible_format`) must reject a chromadb 0.6-era index
with a typed, actionable error BEFORE `PersistentClient` touches it, and must do
so without mutating the directory (Phase 6 of the chromadb 1.x migration asserts
the 0.6 snapshot is byte-identical after a failed open).

A minimal synthetic ``chroma.sqlite3`` (a ``migrations`` table with a chosen max
sysdb version) stands in for a real 0.6 directory so the test is self-contained.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pipecat_context_hub.services.index import (
    RESET_INDEX_REMEDIATION,
    IncompatibleIndexFormatError,
)
from pipecat_context_hub.services.index.vector import (
    VectorIndex,
    _detect_incompatible_format,
)


def _write_chroma_db(chroma_dir: Path, *, sysdb_version: int | None) -> Path:
    """Create a minimal chroma.sqlite3. ``sysdb_version=None`` omits migrations."""
    chroma_dir.mkdir(parents=True, exist_ok=True)
    db = chroma_dir / "chroma.sqlite3"
    conn = sqlite3.connect(db)
    try:
        if sysdb_version is not None:
            conn.execute("CREATE TABLE migrations (dir TEXT, version INTEGER, filename TEXT)")
            # A couple of sysdb rows + an unrelated dir, to exercise MAX(... dir='sysdb').
            conn.executemany(
                "INSERT INTO migrations (dir, version, filename) VALUES (?, ?, ?)",
                [
                    ("sysdb", sysdb_version - 1, "prev.sql"),
                    ("sysdb", sysdb_version, "head.sql"),
                    ("metadb", 99, "unrelated.sql"),
                ],
            )
        else:
            conn.execute("CREATE TABLE not_chroma (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    return db


class TestDetectIncompatibleFormat:
    def test_missing_dir_passes(self, tmp_path: Path):
        _detect_incompatible_format(tmp_path / "does-not-exist")  # no raise

    def test_dir_without_sqlite_passes(self, tmp_path: Path):
        (tmp_path / "chroma").mkdir()
        _detect_incompatible_format(tmp_path / "chroma")  # no raise

    def test_non_chroma_sqlite_is_silent(self, tmp_path: Path):
        # A sqlite file with no migrations table — let chromadb surface its own error.
        _write_chroma_db(tmp_path / "chroma", sysdb_version=None)
        _detect_incompatible_format(tmp_path / "chroma")  # no raise

    def test_supported_format_passes(self, tmp_path: Path):
        _write_chroma_db(tmp_path / "chroma", sysdb_version=10)
        _detect_incompatible_format(tmp_path / "chroma")  # no raise

    def test_future_format_passes(self, tmp_path: Path):
        # A later 1.x minor may add migration 11+ — must still be accepted.
        _write_chroma_db(tmp_path / "chroma", sysdb_version=15)
        _detect_incompatible_format(tmp_path / "chroma")  # no raise

    def test_old_format_raises(self, tmp_path: Path):
        _write_chroma_db(tmp_path / "chroma", sysdb_version=9)
        with pytest.raises(IncompatibleIndexFormatError) as exc_info:
            _detect_incompatible_format(tmp_path / "chroma")
        assert exc_info.value.detected_sysdb_migration == 9

    def test_error_message_contract(self, tmp_path: Path):
        _write_chroma_db(tmp_path / "chroma", sysdb_version=9)
        with pytest.raises(IncompatibleIndexFormatError) as exc_info:
            _detect_incompatible_format(tmp_path / "chroma")
        msg = str(exc_info.value)
        # Phase 6 assertions: format-specific wording + literal remediation command.
        assert "refresh --force --reset-index" in msg
        assert "ncompatible" in msg and "format" in msg
        assert "0.6" in msg
        assert RESET_INDEX_REMEDIATION in msg

    def test_probe_is_non_mutating(self, tmp_path: Path):
        db = _write_chroma_db(tmp_path / "chroma", sysdb_version=9)
        before = db.read_bytes()
        with pytest.raises(IncompatibleIndexFormatError):
            _detect_incompatible_format(tmp_path / "chroma")
        assert db.read_bytes() == before
        # No WAL/SHM sidecars created by the read-only immutable open.
        assert not (tmp_path / "chroma" / "chroma.sqlite3-wal").exists()
        assert not (tmp_path / "chroma" / "chroma.sqlite3-shm").exists()

    def test_vectorindex_rejects_old_format(self, tmp_path: Path):
        # End-to-end: VectorIndex construction runs the probe before PersistentClient.
        _write_chroma_db(tmp_path / "chroma", sysdb_version=9)
        with pytest.raises(IncompatibleIndexFormatError):
            VectorIndex(tmp_path / "chroma")


class _ConfigRaisingClient:
    """Stub PersistentClient whose collection open raises a config-parse error.

    Models the real failure: when an older chromadb (0.6) has written to a 1.x
    directory, chromadb's ``get_or_create_collection`` loads the existing
    ``collections`` row and ``CollectionConfigurationInternal.from_json`` raises
    ``KeyError: '_type'`` (config has no ``_type`` discriminator) — or a
    ``ValueError`` when ``_type`` is present but unrecognized. The exact chromadb
    routing that reaches ``from_json`` depends on internal sysdb state a 0.6
    write leaves behind and is not reproducible from a clean 1.x index, so the
    translation contract is exercised by injecting the error chromadb raises.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_or_create_collection(self, *args: object, **kwargs: object) -> object:
        raise self._exc

    def close(self) -> None:  # pragma: no cover - cleanup only
        pass


class TestPoisonedCollectionConfig:
    """``_open_client`` must translate chromadb's opaque config-parse error into
    the typed, actionable :class:`IncompatibleIndexFormatError`.

    The pre-open sysdb-migration probe cannot catch this case: the ``migrations``
    table is still 1.x and only the ``collections`` row is damaged.
    """

    @pytest.mark.parametrize(
        "exc",
        [KeyError("_type"), ValueError("unknown config type 'hnsw_configuration'")],
        ids=["missing-_type", "unknown-type"],
    )
    def test_translates_config_parse_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ):
        # vector.py calls ``chromadb.PersistentClient(...)``; patch the module
        # attribute so the open returns a client whose collection load fails.
        monkeypatch.setattr("chromadb.PersistentClient", lambda **kwargs: _ConfigRaisingClient(exc))
        with pytest.raises(IncompatibleIndexFormatError) as exc_info:
            VectorIndex(tmp_path / "chroma")
        msg = str(exc_info.value)
        assert "refresh --force --reset-index" in msg
        assert RESET_INDEX_REMEDIATION in msg
        assert "configuration is unreadable" in msg
        # Reason path, not the sysdb-migration path.
        assert exc_info.value.detected_sysdb_migration is None
        # Original chromadb error is chained for debuggability.
        assert exc_info.value.__cause__ is exc

    def test_freshly_created_index_is_not_misflagged(self, tmp_path: Path):
        # The translation must only fire on a genuine config-parse failure — a
        # fresh/empty directory creates its collection without parsing one, so a
        # real VectorIndex over an empty dir must open cleanly.
        vi = VectorIndex(tmp_path / "chroma")  # no raise
        try:
            assert vi._collection.count() == 0
        finally:
            vi.close()
