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
