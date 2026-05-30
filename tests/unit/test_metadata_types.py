"""Enumerate the ChromaDB metadata fields we write, and pin their types.

chromadb 1.x is stricter than 0.6: metadata values must be str | int | float |
bool, and ``None`` is rejected outright (``ValueError: Expected metadata value
to be a str, int, float or bool, got None``). ``_record_to_metadata`` already
guards this by adding each field only when its source value is not None, but
that invariant is easy to break. This test ingests a fully-populated record and
asserts every produced field's concrete type, so a future field that leaks a
None / list / dict into Chroma metadata fails here instead of at upsert time.

The same field->type map is mirrored as a comment beside the upsert() call site.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pipecat_context_hub.services.index.vector import _record_to_metadata
from pipecat_context_hub.shared.types import ChunkedRecord

# Field -> expected ChromaDB-stored Python type. The single source of truth for
# what 1.x's stricter typing constrains; keep in sync with _record_to_metadata.
EXPECTED_METADATA_TYPES: dict[str, type] = {
    # Always present
    "source_url": str,
    "content_type": str,
    "path": str,
    "indexed_at": str,
    # Optional top-level record fields
    "repo": str,
    "commit_sha": str,
    # Derived / extra metadata
    "capability_tags": str,  # list -> comma-joined
    "foundational_class": str,
    "language": str,
    "domain": str,
    "execution_mode": str,
    "line_start": int,
    "line_end": int,
    "pipecat_version_pin": str,
    "module_path": str,
    "class_name": str,
    "chunk_type": str,
    "method_name": str,
    "method_signature": str,
    "return_type": str,
    "is_dataclass": bool,
    "is_abstract": bool,
    "base_classes": str,  # list -> JSON string
    "imports": str,
    "yields": str,
    "calls": str,
    "fields": str,
    "rst_refs": str,
    "related_types": str,
}

_ALLOWED_CHROMA_TYPES = (str, int, float, bool)


def _fully_populated_record() -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id="meta-types-1",
        content="class Foo:\n    pass",
        content_type="source",
        source_url="https://github.com/pipecat-ai/pipecat/blob/main/foo.py",
        repo="pipecat-ai/pipecat",
        path="src/foo.py",
        commit_sha="abc1234",
        indexed_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
        embedding=[0.1] * 384,
        metadata={
            "capability_tags": ["tts", "stt"],
            "foundational_class": "01-say-one-thing",
            "language": "python",
            "domain": "backend",
            "execution_mode": "local",
            "line_start": 1,
            "line_end": 42,
            "pipecat_version_pin": "0.0.95",
            "module_path": "pipecat.foo",
            "class_name": "Foo",
            "chunk_type": "class_overview",
            "method_name": "run",
            "method_signature": "def run(self) -> None",
            "return_type": "None",
            "is_dataclass": True,
            "is_abstract": False,
            "base_classes": ["Base", "Mixin"],
            "imports": ["os", "sys"],
            "yields": ["TextFrame"],
            "calls": ["super().__init__"],
            "fields": ["x: int"],
            "rst_refs": [":class:`Foo`"],
            "related_types": ["Bar"],
        },
    )


class TestMetadataTypes:
    def test_all_fields_have_expected_concrete_type(self):
        meta = _record_to_metadata(_fully_populated_record())
        # Every field we expect is present with the right concrete type.
        for field, expected_type in EXPECTED_METADATA_TYPES.items():
            assert field in meta, f"metadata field missing: {field}"
            # bool is a subclass of int — match exactly, not via isinstance.
            assert type(meta[field]) is expected_type, (
                f"{field}: expected {expected_type.__name__}, got {type(meta[field]).__name__}"
            )

    def test_no_unexpected_fields(self):
        meta = _record_to_metadata(_fully_populated_record())
        unexpected = set(meta) - set(EXPECTED_METADATA_TYPES)
        assert not unexpected, f"unmapped metadata fields (update the map): {unexpected}"

    def test_every_value_is_chroma_storable(self):
        # chromadb 1.x rejects None and non-scalar values — none may leak through.
        meta = _record_to_metadata(_fully_populated_record())
        for field, value in meta.items():
            assert value is not None, f"{field} is None (1.x rejects None metadata)"
            assert isinstance(value, _ALLOWED_CHROMA_TYPES), (
                f"{field} is {type(value).__name__}; chromadb only stores str/int/float/bool"
            )

    def test_minimal_record_emits_only_required_fields(self):
        # A record with no extra metadata must still produce only scalar values
        # (no None leakage for absent optional fields).
        record = ChunkedRecord(
            chunk_id="meta-min-1",
            content="doc body",
            content_type="doc",
            source_url="https://docs.pipecat.ai/x",
            path="/x",
            indexed_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
            embedding=[0.1] * 384,
        )
        meta = _record_to_metadata(record)
        assert set(meta) == {"source_url", "content_type", "path", "indexed_at"}
        for value in meta.values():
            assert isinstance(value, _ALLOWED_CHROMA_TYPES)
