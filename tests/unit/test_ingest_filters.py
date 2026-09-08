"""Tests for ingest_filters: the per-file filters shared by SourceIngester
and GitHubRepoIngester (see the module docstring for why they're shared)."""

from __future__ import annotations

from pathlib import Path

from pipecat_context_hub.services.ingest.ingest_filters import (
    hash_source,
    is_storybook_file,
)


class TestHashSource:
    """Tests for hash_source."""

    def test_identical_text_hashes_equal(self):
        assert hash_source("export const x = 1;\n") == hash_source("export const x = 1;\n")

    def test_different_text_hashes_differ(self):
        assert hash_source("export const x = 1;\n") != hash_source("export const x = 2;\n")


class TestIsStorybookFile:
    """Tests for is_storybook_file."""

    def test_stories_tsx_is_storybook(self):
        assert is_storybook_file(Path("connect-button.stories.tsx"))

    def test_stories_ts_is_storybook(self):
        assert is_storybook_file(Path("index.stories.ts"))

    def test_regular_tsx_is_not_storybook(self):
        assert not is_storybook_file(Path("connect-button.tsx"))

    def test_test_file_is_not_storybook(self):
        assert not is_storybook_file(Path("connect-button.test.tsx"))
