"""Unit tests for release notes deprecation parsing."""

from __future__ import annotations

from unittest.mock import patch

from pipecat_context_hub.services.ingest.deprecation_map import (
    DeprecationEntry,
    DeprecationMap,
    _parse_release_body,
    build_deprecation_map_from_releases,
)


class TestParseReleaseBody:
    """Test _parse_release_body for full release note parsing."""

    def test_deprecated_module_path(self) -> None:
        body = (
            "### Deprecated\n"
            "- `pipecat.services.grok.llm`, `pipecat.services.grok.realtime.llm`, and\n"
            "  `pipecat.services.grok.realtime.events` are deprecated. The old import paths\n"
            "  still work but emit a `DeprecationWarning`; use `pipecat.services.xai.llm`,\n"
            "  `pipecat.services.xai.realtime.llm`, and\n"
            "  `pipecat.services.xai.realtime.events` instead.\n"
            "  (PR [#4142](https://github.com/pipecat-ai/pipecat/pull/4142))\n"
        )
        entries = _parse_release_body("0.0.108", body)
        # Should have 3 deprecated entries (grok.llm, grok.realtime.llm, grok.realtime.events)
        deprecated = [e for e in entries if e.deprecated_in == "0.0.108"]
        assert len(deprecated) >= 3
        paths = {e.old_path for e in deprecated}
        assert "pipecat.services.grok.llm" in paths
        assert "pipecat.services.grok.realtime.llm" in paths
        assert "pipecat.services.grok.realtime.events" in paths
        # Each should have replacement
        for e in deprecated:
            if "grok.llm" in e.old_path and "realtime" not in e.old_path:
                assert e.new_path is not None
                assert "xai" in e.new_path

    def test_removed_section(self) -> None:
        body = (
            "### Removed\n"
            "- Removed `SambaNovaSTTService`. SambaNova no longer offers speech-to-text.\n"
        )
        entries = _parse_release_body("0.0.108", body)
        assert len(entries) >= 1
        removed = [e for e in entries if e.removed_in == "0.0.108"]
        assert len(removed) >= 1
        assert removed[0].old_path == "SambaNovaSTTService"

    def test_class_name_extraction(self) -> None:
        body = (
            "### Deprecated\n"
            "- Deprecated `FalSmartTurnAnalyzer` and `LocalSmartTurnAnalyzer`. "
            "Use `LocalSmartTurnAnalyzerV3` instead.\n"
        )
        entries = _parse_release_body("0.0.98", body)
        names = {e.old_path for e in entries}
        assert "FalSmartTurnAnalyzer" in names
        assert "LocalSmartTurnAnalyzer" in names

    def test_dotted_symbol_extraction(self) -> None:
        """Dotted identifiers like SimliVideoService.InputParams are stored as real keys."""
        body = (
            "### Deprecated\n"
            "- Deprecated `SimliVideoService.InputParams`. Use the new params API.\n"
        )
        entries = _parse_release_body("0.0.110", body)
        names = {e.old_path for e in entries}
        assert "SimliVideoService.InputParams" in names

    def test_dotted_symbol_queryable(self) -> None:
        """Dotted symbol entries can be found via DeprecationMap.check()."""
        dm = DeprecationMap(
            entries={
                "SimliVideoService.InputParams": DeprecationEntry(
                    old_path="SimliVideoService.InputParams",
                    deprecated_in="0.0.110",
                ),
            }
        )
        result = dm.check("SimliVideoService.InputParams")
        assert result is not None
        assert result.deprecated_in == "0.0.110"

    def test_no_deprecated_sections(self) -> None:
        body = "### Added\n- New feature.\n### Fixed\n- Bugfix.\n"
        entries = _parse_release_body("0.0.107", body)
        assert entries == []

    def test_mixed_sections(self) -> None:
        body = (
            "### Added\n"
            "- New feature.\n"
            "### Deprecated\n"
            "- `pipecat.turns.mute` is deprecated. Use `pipecat.turns.user_mute` instead.\n"
            "### Fixed\n"
            "- Bugfix.\n"
            "### Removed\n"
            "- Removed the deprecated VLLM-based Ultravox STT service.\n"
        )
        entries = _parse_release_body("0.0.99", body)
        deprecated = [e for e in entries if e.deprecated_in]
        removed = [e for e in entries if e.removed_in]
        assert len(deprecated) >= 1
        assert len(removed) >= 1
        assert deprecated[0].old_path == "pipecat.turns.mute"
        assert deprecated[0].new_path is not None
        assert "user_mute" in deprecated[0].new_path

    def test_multiline_item(self) -> None:
        """Items that span multiple lines are joined."""
        body = (
            "### Deprecated\n"
            "- Deprecated `pipecat.services.google.llm_vertex`,\n"
            "  `pipecat.services.google.llm_openai`, and\n"
            "  `pipecat.services.google.gemini_live.llm_vertex` modules.\n"
            "  Use `pipecat.services.google.vertex.llm` instead.\n"
            "  (PR [#3980](https://github.com/pipecat-ai/pipecat/pull/3980))\n"
        )
        entries = _parse_release_body("0.0.105", body)
        paths = {e.old_path for e in entries}
        assert "pipecat.services.google.llm_vertex" in paths
        assert "pipecat.services.google.llm_openai" in paths
        assert "pipecat.services.google.gemini_live.llm_vertex" in paths

    def test_parameter_deprecation(self) -> None:
        """Parameter-level deprecations without module paths use class name."""
        body = (
            "### Deprecated\n"
            "- `SimliVideoService.InputParams` is deprecated. "
            "Use the direct constructor parameters instead.\n"
        )
        entries = _parse_release_body("0.0.106", body)
        # Should extract the dotted reference
        assert len(entries) >= 1


class TestBuildFromReleases:
    """Test build_deprecation_map_from_releases."""

    def test_populates_from_mock_releases(self) -> None:
        mock_releases = [
            (
                "0.0.108",
                (
                    "### Deprecated\n"
                    "- `pipecat.services.grok.llm` is deprecated. "
                    "Use `pipecat.services.xai.llm` instead.\n"
                    "### Removed\n"
                    "- Removed `SambaNovaSTTService`.\n"
                ),
            ),
            (
                "0.0.106",
                (
                    "### Deprecated\n"
                    "- Deprecated `WakeCheckFilter` in favor of "
                    "`WakePhraseUserTurnStartStrategy`.\n"
                ),
            ),
        ]
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=mock_releases,
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat")

        assert len(dm.entries) >= 3
        grok = dm.check("pipecat.services.grok.llm")
        assert grok is not None
        assert grok.deprecated_in == "0.0.108"
        assert grok.new_path is not None
        assert "xai" in grok.new_path

        samba = dm.check("SambaNovaSTTService")
        assert samba is not None
        assert samba.removed_in == "0.0.108"

    def test_does_not_overwrite_existing(self) -> None:
        """Release entries don't overwrite source-derived entries."""
        existing = DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                    note="From DeprecatedModuleProxy",
                ),
            }
        )
        mock_releases = [
            ("0.0.108", ("### Deprecated\n- `pipecat.services.grok` is deprecated.\n")),
        ]
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=mock_releases,
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat", existing)

        # Should keep the original entry's note, not overwrite
        assert dm.entries["pipecat.services.grok"].note == "From DeprecatedModuleProxy"
        # But should merge the missing deprecated_in field
        assert dm.entries["pipecat.services.grok"].deprecated_in == "0.0.108"

    def test_merge_missing_lifecycle_fields(self) -> None:
        """Release data merges deprecated_in/removed_in into existing entries."""
        existing = DeprecationMap(
            entries={
                "pipecat.services.old": DeprecationEntry(
                    old_path="pipecat.services.old",
                    new_path="pipecat.services.new",
                    note="From source",
                ),
            }
        )
        mock_releases = [
            (
                "0.0.105",
                (
                    "### Deprecated\n"
                    "- `pipecat.services.old` is deprecated. Use `pipecat.services.new`.\n"
                ),
            ),
            ("0.0.110", ("### Removed\n- `pipecat.services.old` has been removed.\n")),
        ]
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=mock_releases,
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat", existing)

        entry = dm.entries["pipecat.services.old"]
        assert entry.note == "From source"  # not overwritten
        assert entry.deprecated_in == "0.0.105"  # merged
        assert entry.removed_in == "0.0.110"  # merged

    def test_oldest_first_deprecated_in_wins(self) -> None:
        """When a symbol appears in multiple releases, earliest deprecated_in wins."""
        # Feed releases in reverse-chronological order (as gh returns them)
        mock_releases = [
            ("0.0.110", ("### Deprecated\n- `pipecat.services.old.thing` is deprecated.\n")),
            ("0.0.105", ("### Deprecated\n- `pipecat.services.old.thing` is deprecated.\n")),
        ]
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=mock_releases,
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat")

        entry = dm.entries["pipecat.services.old.thing"]
        # Should be 0.0.105 (earliest), not 0.0.110
        assert entry.deprecated_in == "0.0.105"

    def test_synthetic_keys_go_to_notes(self) -> None:
        """Unmatched prose entries go to changelog_notes, not entries."""
        mock_releases = [
            (
                "0.0.108",
                ("### Deprecated\n- Some general deprecation notice without backtick paths.\n"),
            ),
        ]
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=mock_releases,
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat")

        # No synthetic release:... keys in entries
        assert not any(k.startswith("release:") for k in dm.entries)
        # But it should be in changelog_notes
        assert len(dm.changelog_notes) >= 1

    def test_gh_not_available(self) -> None:
        """Gracefully returns empty when gh CLI is not available."""
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=[],
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat")
        assert len(dm.entries) == 0


class TestRealReleaseNotes:
    """Smoke tests against real GitHub releases (requires gh CLI)."""

    def test_real_pipecat_releases(self) -> None:
        """Fetch real release notes and verify parsing produces entries."""
        from pipecat_context_hub.services.ingest.deprecation_map import (
            _fetch_release_notes,
        )

        releases = _fetch_release_notes("pipecat-ai/pipecat", limit=5)
        if not releases:
            return  # gh not available or not authenticated

        dm = build_deprecation_map_from_releases("pipecat-ai/pipecat", limit=5)
        # v0.0.104-108 all have deprecations, so we should get entries
        assert len(dm.entries) >= 3, (
            f"Expected >= 3 entries from real releases, got {len(dm.entries)}"
        )

        # Verify a known deprecation from v0.0.108
        grok = dm.check("pipecat.services.grok.llm")
        if grok:
            assert grok.deprecated_in == "0.0.108"
            assert grok.new_path is not None
            assert "xai" in grok.new_path


class TestRenamedToBullets:
    """Regression: pipecat 1.3.0 rename bullets, which the old parser misread.

    The old/new split keyed on the literal word "use"; bullets phrased as
    "renamed to" / "Import X from Y" got every token keyed as deprecated
    (false positives on the *replacements* — pipecat.pipeline.worker,
    pipecat.workers.runner) while the deprecated class names (PipelineTask,
    PipelineRunner) were never keyed at all (false negatives). Texts below
    are the real v1.3.0 release bullets.
    """

    _TASK_BULLET = (
        "`PipelineTask`, `PipelineTaskParams`, and the `pipecat.pipeline.task` "
        "module have been renamed to `PipelineWorker`, `WorkerParams`, and "
        "`pipecat.pipeline.worker`. The old names still resolve (the module "
        "re-exports the new symbols) but constructing `PipelineTask` / "
        "`PipelineTaskParams` emits a `DeprecationWarning`; they will be "
        "removed in a future release."
    )

    _RUNNER_BULLET = (
        "`PipelineRunner` has been renamed to `WorkerRunner` and moved to "
        "`pipecat.workers.runner`, since the runner now runs workers (of which "
        "`PipelineWorker` is one kind), not just pipelines. Import "
        "`WorkerRunner` from `pipecat.workers.runner`. The old "
        "`pipecat.pipeline.runner` module still re-exports both names, and "
        "`PipelineRunner` still works as a subclass alias, but it emits a "
        "`DeprecationWarning` and will be removed in a future release."
    )

    _FRAME_BULLET = "`BotInterruptionFrame` is now deprecated, use `InterruptionTaskFrame` instead."

    def _entries(self, bullet: str) -> dict[str, DeprecationEntry]:
        body = f"### Deprecated\n- {bullet}\n"
        return {e.old_path: e for e in _parse_release_body("1.3.0", body)}

    def test_renamed_to_keys_old_names_not_new(self) -> None:
        entries = self._entries(self._TASK_BULLET)
        # Deprecated names (class names AND module path) are keyed.
        assert "PipelineTask" in entries
        assert "PipelineTaskParams" in entries
        assert "pipecat.pipeline.task" in entries
        # Replacements must NOT be keyed — a deprecated verdict on the
        # current API misdirects agents away from the right answer.
        assert "PipelineWorker" not in entries
        assert "WorkerParams" not in entries
        assert "pipecat.pipeline.worker" not in entries
        # Prose identifiers never become keys.
        assert "DeprecationWarning" not in entries
        # The replacement is recorded.
        assert "pipecat.pipeline.worker" in (entries["PipelineTask"].new_path or "")

    def test_old_marked_module_after_boundary_is_deprecated(self) -> None:
        entries = self._entries(self._RUNNER_BULLET)
        assert "PipelineRunner" in entries
        # "The old `pipecat.pipeline.runner` module" appears *after* the
        # boundary but is rescued by the old/legacy marker.
        assert "pipecat.pipeline.runner" in entries
        assert "WorkerRunner" not in entries
        assert "pipecat.workers.runner" not in entries
        # The replacement is recorded under the deprecated class, not dropped.
        assert "WorkerRunner" in (entries["PipelineRunner"].new_path or "")

    def test_use_instead_keys_only_the_deprecated_frame(self) -> None:
        entries = self._entries(self._FRAME_BULLET)
        assert "BotInterruptionFrame" in entries
        # The replacement frame must not be keyed (the old parser keyed both).
        assert "InterruptionTaskFrame" not in entries
        assert entries["BotInterruptionFrame"].new_path == "InterruptionTaskFrame"

    def test_renamed_from_rescues_deprecated_source(self) -> None:
        # Target-first phrasing: the source after "from" is the deprecation,
        # the leading token is the replacement. The owner-context skip (which
        # fires on any token following a preposition) must NOT drop the source,
        # and position must NOT key the leading replacement. PR #78 follow-up.
        entries = self._entries("`WorkerRunner` was renamed from `PipelineRunner`.")
        assert "PipelineRunner" in entries
        assert "WorkerRunner" not in entries

    def test_migrate_from_to_keys_only_the_source(self) -> None:
        # "migrate from X to Y" has no boundary phrase, so both tokens follow a
        # preposition and would be dropped by the owner skip. The rename-source
        # rescue keeps X (the deprecation); Y is simply not keyed (not a false
        # positive on the current API).
        entries = self._entries("Migrate from `pipecat.old.thing` to `pipecat.new.thing`.")
        assert "pipecat.old.thing" in entries
        assert "pipecat.new.thing" not in entries

    def test_check_resolves_renamed_class(self) -> None:
        body = f"### Deprecated\n- {self._TASK_BULLET}\n"
        dm = DeprecationMap()
        for e in _parse_release_body("1.3.0", body):
            dm.entries[e.old_path] = e
        hit = dm.check("PipelineTask")
        assert hit is not None and hit.deprecated_in == "1.3.0"
        assert dm.check("PipelineWorker") is None


class TestNewestReleaseWins:
    """Re-mentioned symbols take the newest release's entry as primary.

    Regression: releases were sorted oldest-first (and lexicographically, so
    "0.0.9" sorted after "0.0.108"), letting a misparsed 0.0.58 bullet that
    mentioned `PipelineTask` in passing shadow the real 1.3.0 rename entry.
    """

    def test_newest_entry_is_primary(self) -> None:
        old_body = (
            "### Deprecated\n"
            "- `PipelineParams.observers` is now deprecated, you the new "
            "`PipelineTask` parameter `observers`.\n"
        )
        new_body = (
            "### Deprecated\n"
            "- `PipelineTask`, `PipelineTaskParams`, and the "
            "`pipecat.pipeline.task` module have been renamed to "
            "`PipelineWorker`, `WorkerParams`, and `pipecat.pipeline.worker`.\n"
        )
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=[("0.0.58", old_body), ("1.3.0", new_body)],
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat")
        entry = dm.entries["PipelineTask"]
        assert entry.deprecated_in == "1.3.0"
        assert "renamed to" in entry.note
        assert "PipelineWorker" in (entry.new_path or "")

    def test_version_sort_is_numeric(self) -> None:
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=[
                ("0.0.9", "### Deprecated\n- `OldThingService` is deprecated.\n"),
                (
                    "0.0.108",
                    "### Deprecated\n- `OldThingService` is deprecated, use "
                    "`NewThingService` instead.\n",
                ),
            ],
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat")
        # 0.0.108 is numerically newer than 0.0.9 (a lexicographic sort gets
        # this backwards), so its guidance (new_path) is primary — while
        # deprecated_in is the earliest announcement.
        assert dm.entries["OldThingService"].deprecated_in == "0.0.9"
        assert dm.entries["OldThingService"].new_path == "NewThingService"


class TestOwnerContextNotKeyed:
    """Member-deprecation bullets must not key the owning class.

    Regression: `PipelineTask` reported `deprecated_in: 0.0.86` and
    `removed_in: 1.0.0` because historical bullets deprecating its *events*
    keyed the class itself, and the earliest-mention lifecycle merge then
    inherited those versions. Texts are the real v0.0.86 / v1.0.0 bullets.
    """

    _EVENTS_DEPRECATED_BULLET = (
        "`PipelineTask` events `on_pipeline_stopped`, `on_pipeline_ended` and "
        "`on_pipeline_cancelled` are now deprecated. Use "
        "`on_pipeline_finished` instead."
    )

    _EVENTS_REMOVED_BULLET = (
        "⚠️ Removed deprecated `on_pipeline_ended`, `on_pipeline_cancelled`, "
        "and `on_pipeline_stopped` events from `PipelineTask`. Use "
        "`on_pipeline_finished` instead."
    )

    _OBSERVERS_REMOVED_BULLET = (
        "⚠️ Removed deprecated `observers` field from `PipelineParams`. Pass "
        "observers directly to `PipelineTask` constructor instead."
    )

    def test_owner_before_member_nouns_is_skipped(self) -> None:
        body = f"### Deprecated\n- {self._EVENTS_DEPRECATED_BULLET}\n"
        keys = {e.old_path for e in _parse_release_body("0.0.86", body)}
        assert "PipelineTask" not in keys

    def test_owner_after_from_is_skipped(self) -> None:
        body = f"### Removed\n- {self._EVENTS_REMOVED_BULLET}\n"
        keys = {e.old_path for e in _parse_release_body("1.0.0", body)}
        assert "PipelineTask" not in keys

    def test_owner_of_field_and_constructor_target_are_skipped(self) -> None:
        body = f"### Removed\n- {self._OBSERVERS_REMOVED_BULLET}\n"
        keys = {e.old_path for e in _parse_release_body("1.0.0", body)}
        assert "PipelineParams" not in keys
        assert "PipelineTask" not in keys

    def test_lifecycle_not_corrupted_by_member_bullets(self) -> None:
        """The full history: member bullets must not pollute the class's
        deprecated_in/removed_in once the real 1.3.0 rename keys it."""
        rename_bullet = (
            "`PipelineTask`, `PipelineTaskParams`, and the "
            "`pipecat.pipeline.task` module have been renamed to "
            "`PipelineWorker`, `WorkerParams`, and `pipecat.pipeline.worker`."
        )
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=[
                ("0.0.86", f"### Deprecated\n- {self._EVENTS_DEPRECATED_BULLET}\n"),
                (
                    "1.0.0",
                    f"### Removed\n- {self._EVENTS_REMOVED_BULLET}\n"
                    f"- {self._OBSERVERS_REMOVED_BULLET}\n",
                ),
                ("1.3.0", f"### Deprecated\n- {rename_bullet}\n"),
            ],
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat")
        entry = dm.entries["PipelineTask"]
        assert entry.deprecated_in == "1.3.0"
        assert entry.removed_in is None
        assert "PipelineWorker" in (entry.new_path or "")


class TestInconsistentHeadingLevels:
    """Hand-written release bodies mix heading levels (real v0.0.87 layout).

    Regression: the section tracker only exited on `### `, so an `## Fixed`
    (h2) heading left the Deprecated section open and every Fixed bullet was
    parsed as a deprecation ("Fixed a `PipelineTask` issue …" keyed
    PipelineTask as deprecated in 0.0.87).
    """

    def test_h2_heading_ends_deprecated_section(self) -> None:
        body = (
            "### Deprecated\n"
            "- `GladiaSTTService` arg is deprecated.\n"
            "## Fixed\n"
            "- Fixed a `PipelineTask` issue that could prevent the "
            "application to exit if `task.cancel()` was called when the "
            "task was already finished.\n"
        )
        keys = {e.old_path for e in _parse_release_body("0.0.87", body)}
        assert "PipelineTask" not in keys

    def test_h2_deprecated_section_is_accepted(self) -> None:
        body = "## Deprecated\n- `OldNameService` is deprecated, use `NewNameService` instead.\n"
        entries = {e.old_path: e for e in _parse_release_body("0.0.99", body)}
        assert "OldNameService" in entries
        assert entries["OldNameService"].new_path == "NewNameService"


class TestHeadingDecorations:
    """Hand-finished markdown decorates headings; parsing must tolerate it.

    pipecat's real CHANGELOG has '## [0.0.96] - 2025-11-26 🦃 "Happy
    Thanksgiving!" 🦃' and v0.0.96's release body opens with an h2 banner.
    """

    def test_decorated_section_heading_is_parsed(self) -> None:
        body = "### ⚠️ Deprecated\n- `OldThingService` is deprecated.\n"
        entries = {e.old_path for e in _parse_release_body("1.0.0", body)}
        assert "OldThingService" in entries

    def test_changelog_decorated_version_and_h2_section(self, tmp_path) -> None:
        from pipecat_context_hub.services.ingest.deprecation_map import (
            build_deprecation_map_from_changelog,
        )

        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            '## [0.0.96] - 2025-11-26 🦃 "Happy Thanksgiving!" 🦃\n'
            "\n"
            "## Deprecated\n"
            "- `OldThingService` is deprecated.\n"
            "## Fixed\n"
            "- Fixed `CurrentThingService`.\n",
            encoding="utf-8",
        )
        dm = build_deprecation_map_from_changelog(changelog)
        notes = {(n.deprecated_in, n.note) for n in dm.changelog_notes}
        # The decorated version heading still yields 0.0.96; the h2 section
        # is accepted; the h2 Fixed heading ends it (no Fixed bleed).
        assert ("0.0.96", "`OldThingService` is deprecated.") in notes
        assert not any("CurrentThingService" in n.note for n in dm.changelog_notes)


class TestMalformedHeadingResilience:
    """A human step in pipecat's changelog can slip the heading level or typo
    the word. Recognized levels are parsed; unrecognizable headings warn
    loudly instead of silently dropping a whole Deprecated section.
    """

    def test_h1_deprecated_section_captured(self) -> None:
        entries = {
            e.old_path
            for e in _parse_release_body("1.0.0", "# Deprecated\n- `OldH1Service` is deprecated.\n")
        }
        assert "OldH1Service" in entries

    def test_h5_deprecated_section_captured(self) -> None:
        entries = {
            e.old_path
            for e in _parse_release_body(
                "1.0.0", "##### Deprecated\n- `OldH5Service` is deprecated.\n"
            )
        }
        assert "OldH5Service" in entries

    def test_missing_space_heading_captured(self) -> None:
        entries = {
            e.old_path
            for e in _parse_release_body(
                "1.0.0", "###Deprecated\n- `OldNoSpaceService` is deprecated.\n"
            )
        }
        assert "OldNoSpaceService" in entries

    def test_unparsable_deprecation_heading_warns(self, caplog) -> None:
        import logging

        body = "### Deprecations\n- `OldPluralService` is deprecated.\n"
        with caplog.at_level(logging.WARNING):
            entries = {e.old_path for e in _parse_release_body("9.9.9", body)}
        # The plural heading is not a recognized section, so the entry is not
        # captured — but the parser warns loudly rather than dropping silently.
        assert "OldPluralService" not in entries
        assert any(
            "malformed header" in r.getMessage().lower() and "9.9.9" in r.getMessage()
            for r in caplog.records
        )

    def test_normal_fixed_heading_does_not_warn(self, caplog) -> None:
        import logging

        # A plain non-deprecation heading must not trip the malformed warning.
        body = (
            "### Deprecated\n- `OldService` is deprecated.\n## Fixed\n- Fixed `CurrentService`.\n"
        )
        with caplog.at_level(logging.WARNING):
            _parse_release_body("1.0.0", body)
        assert not any("malformed header" in r.getMessage().lower() for r in caplog.records)
