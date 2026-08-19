"""Tests for server.tools.check_deprecation.resolve_framework_version."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pipecat_context_hub.server.tools.check_deprecation import resolve_framework_version


def _index_store(metadata: dict[str, object]) -> MagicMock:
    store = MagicMock()
    store.get_all_metadata.return_value = metadata
    return store


class TestResolveFrameworkVersion:
    def test_no_index_store_returns_none(self):
        assert resolve_framework_version(None) is None

    def test_indexed_version_preferred_even_when_pin_is_latest(self):
        """indexed_framework_version is the concrete resolved revision; it must
        win even though framework_version still holds the unresolved 'latest'
        sentinel.
        """
        store = _index_store(
            {
                "framework_version": "latest",
                "indexed_framework_version": "1.10.0",
                "indexed_framework_commits_ahead": "0",
            }
        )
        assert resolve_framework_version(store) == "1.10.0"

    def test_floor_version_with_commit_distance_is_not_exact(self):
        """A nearest tag on the default branch must not drive version-aware status."""
        store = _index_store(
            {
                "framework_version": "latest",
                "indexed_framework_version": "1.5.0",
                "indexed_framework_commits_ahead": "80",
            }
        )
        assert resolve_framework_version(store) is None

    def test_falls_back_to_framework_version_when_indexed_unset(self):
        store = _index_store({"framework_version": "v0.0.96"})
        assert resolve_framework_version(store) == "v0.0.96"

    def test_neither_set_returns_none(self):
        store = _index_store({})
        assert resolve_framework_version(store) is None

    @pytest.mark.parametrize("pin", ["latest", "  LATEST  ", "Latest"])
    def test_latest_pin_is_never_returned_as_a_version(self, pin: str):
        """Metadata contract v2 permits ``framework_version == "latest"``. The
        sentinel is a pin, not a version — returning it verbatim hands a
        version-shaped non-version to the deprecation handler.
        """
        store = _index_store({"framework_version": pin})
        assert resolve_framework_version(store) is None

    def test_map_provenance_mismatch_falls_back_to_none(self):
        """Round 10 Finding #1 regression: if the on-disk deprecation map's
        commit SHA doesn't match the metadata's `deprecation_map_commit_sha`
        stamp (a crash between the two writes left them describing different
        revisions), don't assert version-exactness against a map that may not
        match — fall back to None.
        """
        store = _index_store(
            {
                "indexed_framework_version": "1.2.0",
                "indexed_framework_commits_ahead": "0",
                "deprecation_map_commit_sha": "abc123",
            }
        )
        dep_map = MagicMock()
        dep_map.pipecat_commit_sha = "def456"
        assert resolve_framework_version(store, dep_map) is None

    def test_map_provenance_match_returns_version(self):
        store = _index_store(
            {
                "indexed_framework_version": "1.2.0",
                "indexed_framework_commits_ahead": "0",
                "deprecation_map_commit_sha": "abc123",
            }
        )
        dep_map = MagicMock()
        dep_map.pipecat_commit_sha = "abc123"
        assert resolve_framework_version(store, dep_map) == "1.2.0"

    def test_missing_map_commit_sha_stamp_preserves_old_behavior(self):
        """A pre-fix index has no `deprecation_map_commit_sha` key at all —
        the cross-check must skip (fail open), not force a None regression
        for existing indexes built before this fix shipped.
        """
        store = _index_store(
            {
                "indexed_framework_version": "1.2.0",
                "indexed_framework_commits_ahead": "0",
            }
        )
        dep_map = MagicMock()
        dep_map.pipecat_commit_sha = "def456"
        assert resolve_framework_version(store, dep_map) == "1.2.0"
