"""Unit tests for the deprecation map builder and checker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipecat_context_hub.services.ingest.deprecation_map import (
    DeprecationEntry,
    DeprecationMap,
    build_deprecation_map_from_registry,
)


class TestDeprecationMapCheck:
    """Test the fuzzy matching in DeprecationMap.check()."""

    def _make_map(self) -> DeprecationMap:
        return DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                    kind="module",
                ),
                "pipecat.services.cartesia": DeprecationEntry(
                    old_path="pipecat.services.cartesia",
                    new_path="pipecat.services.cartesia.stt, pipecat.services.cartesia.tts",
                    kind="module",
                ),
            }
        )

    def test_exact_match(self) -> None:
        dm = self._make_map()
        entry = dm.check("pipecat.services.grok")
        assert entry is not None
        assert entry.new_path == "pipecat.services.xai.llm"

    def test_prefix_match_child(self) -> None:
        """'pipecat.services.grok.llm' should match 'pipecat.services.grok'."""
        dm = self._make_map()
        entry = dm.check("pipecat.services.grok.llm")
        assert entry is not None
        assert entry.old_path == "pipecat.services.grok"

    def test_ancestor_package_not_flagged(self) -> None:
        """A current ancestor package must NOT be flagged because a descendant
        module moved. ``pipecat.services.grok`` is deprecated, but the broad
        ``pipecat.services`` / ``pipecat`` packages are current."""
        dm = self._make_map()
        assert dm.check("pipecat.services") is None
        assert dm.check("pipecat") is None

    def test_no_match(self) -> None:
        dm = self._make_map()
        assert dm.check("pipecat.transports.daily") is None

    def test_bare_class_does_not_match_nested_member(self) -> None:
        """A current owner class must not inherit a deprecated nested member's
        verdict. ``GladiaSTTService.InputParams`` is deprecated, but
        ``GladiaSTTService`` itself is current."""
        dm = DeprecationMap(
            entries={
                "GladiaSTTService.InputParams": DeprecationEntry(
                    old_path="GladiaSTTService.InputParams",
                    new_path="GladiaInputParams",
                ),
            }
        )
        assert dm.check("GladiaSTTService.InputParams") is not None
        assert dm.check("GladiaSTTService") is None

    def test_current_module_with_deprecated_member_not_flagged(self) -> None:
        """A current module must not be flagged just because it contains a
        deprecated member. A deprecated parameter's fully-qualified key must not
        make its container module (or class) look deprecated."""
        dm = DeprecationMap(
            entries={
                # As produced by build_deprecation_map_from_registry: the bare
                # subject plus its fully-qualified alias, both kind="parameter".
                "OpenAILLMService.model": DeprecationEntry(
                    old_path="OpenAILLMService.model",
                    new_path="settings",
                    kind="parameter",
                ),
                "pipecat.services.openai.llm.OpenAILLMService.model": DeprecationEntry(
                    old_path="OpenAILLMService.model",
                    new_path="settings",
                    kind="parameter",
                ),
            }
        )
        # The current module and current class are NOT deprecated...
        assert dm.check("pipecat.services.openai.llm") is None
        assert dm.check("pipecat.services.openai.llm.OpenAILLMService") is None
        # ...but the deprecated parameter still resolves by both names.
        assert dm.check("OpenAILLMService.model") is not None
        assert dm.check("pipecat.services.openai.llm.OpenAILLMService.model") is not None

    def test_empty_map(self) -> None:
        dm = DeprecationMap()
        assert dm.check("anything") is None


class TestDeprecationMapSerialization:
    """Test save/load round-trip."""

    def test_round_trip(self, tmp_path: Path) -> None:
        original = DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                    deprecated_in="0.0.100",
                    note="Use xai.llm instead",
                    kind="module",
                    relation="move",
                    location="pipecat/services/grok/__init__.py:10",
                ),
            },
            pipecat_commit_sha="abc123",
        )
        path = tmp_path / "deprecation_map.json"
        original.save(path)

        loaded = DeprecationMap.load(path)
        assert loaded.pipecat_commit_sha == "abc123"
        assert "pipecat.services.grok" in loaded.entries
        entry = loaded.entries["pipecat.services.grok"]
        assert entry.new_path == "pipecat.services.xai.llm"
        assert entry.deprecated_in == "0.0.100"
        assert entry.note == "Use xai.llm instead"
        assert entry.kind == "module"
        assert entry.relation == "move"
        assert entry.location == "pipecat/services/grok/__init__.py:10"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        loaded = DeprecationMap.load(tmp_path / "nonexistent.json")
        assert loaded.entries == {}

    def test_to_dict_from_dict(self) -> None:
        dm = DeprecationMap(
            entries={
                "key": DeprecationEntry(
                    old_path="key",
                    new_path="new_key",
                    removed_in="0.0.110",
                    kind="class",
                    relation="rename",
                ),
            },
            pipecat_commit_sha="def456",
        )
        data = dm.to_dict()
        restored = DeprecationMap.from_dict(data)
        assert restored.pipecat_commit_sha == "def456"
        assert "key" in restored.entries
        assert restored.entries["key"].removed_in == "0.0.110"
        assert restored.entries["key"].kind == "class"
        assert restored.entries["key"].relation == "rename"


class TestBuildFromRegistry:
    """Test building the map from pipecat's deprecations.json registry."""

    def _write_registry(self, tmp_path: Path, records: list[dict[str, object]]) -> Path:
        path = tmp_path / "deprecations.json"
        path.write_text(
            json.dumps({"schema_version": 1, "deprecations": records}), encoding="utf-8"
        )
        return path

    def test_maps_record_fields(self, tmp_path: Path) -> None:
        path = self._write_registry(
            tmp_path,
            [
                {
                    "subject": "ResampyResampler",
                    "module": "pipecat.audio.resamplers.resampy_resampler",
                    "kind": "class",
                    "deprecated_in": "1.2.0",
                    "removed_in": "2.0.0",
                    "relation": "use_existing",
                    "replacement": "SOXRAudioResampler",
                    "message": "`ResampyResampler` is deprecated since 1.2.0 ...",
                    "location": "pipecat/audio/resamplers/resampy_resampler.py:42",
                }
            ],
        )
        dm = build_deprecation_map_from_registry(path, commit_sha="sha123")
        assert dm.pipecat_commit_sha == "sha123"
        entry = dm.check("ResampyResampler")
        assert entry is not None
        assert entry.new_path == "SOXRAudioResampler"
        assert entry.deprecated_in == "1.2.0"
        assert entry.removed_in == "2.0.0"
        assert entry.kind == "class"
        assert entry.relation == "use_existing"
        assert entry.note.startswith("`ResampyResampler` is deprecated")
        assert entry.location == "pipecat/audio/resamplers/resampy_resampler.py:42"

    def test_fully_qualified_alias_resolves(self, tmp_path: Path) -> None:
        """A non-module symbol resolves by both bare and fully-qualified path."""
        path = self._write_registry(
            tmp_path,
            [
                {
                    "subject": "ResampyResampler",
                    "module": "pipecat.audio.resamplers.resampy_resampler",
                    "kind": "class",
                    "deprecated_in": "1.2.0",
                    "removed_in": "2.0.0",
                    "relation": "use_existing",
                    "replacement": "SOXRAudioResampler",
                    "message": "...",
                }
            ],
        )
        dm = build_deprecation_map_from_registry(path)
        assert dm.check("ResampyResampler") is not None
        assert dm.check("pipecat.audio.resamplers.resampy_resampler.ResampyResampler") is not None

    def test_module_record_keyed_by_subject(self, tmp_path: Path) -> None:
        path = self._write_registry(
            tmp_path,
            [
                {
                    "subject": "pipecat.services.grok",
                    "module": "pipecat.services.grok",
                    "kind": "module",
                    "deprecated_in": "1.0.0",
                    "removed_in": "2.0.0",
                    "relation": "move",
                    "replacement": "pipecat.services.xai.llm",
                    "message": "...",
                }
            ],
        )
        dm = build_deprecation_map_from_registry(path)
        entry = dm.check("pipecat.services.grok.llm")
        assert entry is not None
        assert entry.new_path == "pipecat.services.xai.llm"
        assert entry.kind == "module"

    def test_no_replacement_yields_none(self, tmp_path: Path) -> None:
        path = self._write_registry(
            tmp_path,
            [
                {
                    "subject": "OldThing",
                    "module": "pipecat.x",
                    "kind": "class",
                    "deprecated_in": "1.0.0",
                    "removed_in": "2.0.0",
                    "relation": "none",
                    "replacement": "",
                    "message": "...",
                }
            ],
        )
        dm = build_deprecation_map_from_registry(path)
        entry = dm.check("OldThing")
        assert entry is not None
        assert entry.new_path is None

    def test_missing_registry_returns_empty(self, tmp_path: Path) -> None:
        dm = build_deprecation_map_from_registry(tmp_path / "nope.json", commit_sha="sha")
        assert dm.entries == {}
        assert dm.pipecat_commit_sha == "sha"

    def test_malformed_registry_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "deprecations.json"
        path.write_text("not json", encoding="utf-8")
        dm = build_deprecation_map_from_registry(path)
        assert dm.entries == {}

    def test_duplicate_bare_subject_warns_and_keeps_both_qualified(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two records colliding on the same bare subject log a warning; the
        last write wins the bare key, but both remain resolvable by full path."""
        import logging

        path = self._write_registry(
            tmp_path,
            [
                {
                    "subject": "Config",
                    "module": "pipecat.services.foo",
                    "kind": "class",
                    "replacement": "FooConfig",
                    "location": "pipecat/services/foo.py:1",
                },
                {
                    "subject": "Config",
                    "module": "pipecat.services.bar",
                    "kind": "class",
                    "replacement": "BarConfig",
                    "location": "pipecat/services/bar.py:2",
                },
            ],
        )
        with caplog.at_level(logging.WARNING):
            dm = build_deprecation_map_from_registry(path)
        assert "duplicate bare subject" in caplog.text

        # Last record wins the bare key...
        bare = dm.check("Config")
        assert bare is not None
        assert bare.new_path == "BarConfig"
        # ...but both remain resolvable by their fully-qualified path.
        foo = dm.check("pipecat.services.foo.Config")
        bar = dm.check("pipecat.services.bar.Config")
        assert foo is not None and bar is not None
        assert foo.new_path == "FooConfig"
        assert bar.new_path == "BarConfig"


class TestCheckDeprecationHandler:
    """Test the MCP tool handler for check_deprecation."""

    async def test_deprecated_symbol(self) -> None:
        from pipecat_context_hub.server.tools.check_deprecation import (
            handle_check_deprecation,
        )

        dm = DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                    deprecated_in="0.0.100",
                    kind="module",
                    relation="move",
                    location="pipecat/services/grok/__init__.py:10",
                ),
            }
        )
        result_json = await handle_check_deprecation({"symbol": "pipecat.services.grok.llm"}, dm)
        result = json.loads(result_json)
        assert result["deprecated"] is True
        assert result["replacement"] == "pipecat.services.xai.llm"
        assert result["kind"] == "module"
        assert result["relation"] == "move"
        assert result["location"] == "pipecat/services/grok/__init__.py:10"

    async def test_not_deprecated(self) -> None:
        from pipecat_context_hub.server.tools.check_deprecation import (
            handle_check_deprecation,
        )

        dm = DeprecationMap()
        result_json = await handle_check_deprecation({"symbol": "DailyTransport"}, dm)
        result = json.loads(result_json)
        assert result["deprecated"] is False

    async def test_no_map_available(self) -> None:
        from pipecat_context_hub.server.tools.check_deprecation import (
            handle_check_deprecation,
        )

        result_json = await handle_check_deprecation({"symbol": "pipecat.services.grok"}, None)
        result = json.loads(result_json)
        assert result["deprecated"] is False
        assert "not available" in (result.get("note") or "")
