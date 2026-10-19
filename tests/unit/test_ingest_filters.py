"""Tests for ingest_filters: the per-file filters shared by SourceIngester
and GitHubRepoIngester (see the module docstring for why they're shared)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pipecat_context_hub.services.ingest.ingest_filters import (
    hash_source,
    is_plain_dir,
    is_plain_file,
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


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
class TestPlainPathPredicates:
    """``is_plain_dir`` / ``is_plain_file``: real entries only, never raising."""

    def test_real_entries(self, tmp_path: Path):
        (tmp_path / "d").mkdir()
        (tmp_path / "f.py").write_text("x = 1\n")

        assert is_plain_dir(tmp_path / "d") and not is_plain_file(tmp_path / "d")
        assert is_plain_file(tmp_path / "f.py") and not is_plain_dir(tmp_path / "f.py")

    def test_links_are_neither(self, tmp_path: Path):
        (tmp_path / "real").mkdir()
        (tmp_path / "real.py").write_text("x = 1\n")
        (tmp_path / "dir_link").symlink_to(tmp_path / "real")
        (tmp_path / "file_link").symlink_to(tmp_path / "real.py")
        (tmp_path / "dangling").symlink_to("/nonexistent/target")

        for name in ("dir_link", "file_link", "dangling"):
            assert not is_plain_dir(tmp_path / name)
            assert not is_plain_file(tmp_path / name)

    def test_missing_path_is_neither(self, tmp_path: Path):
        assert not is_plain_dir(tmp_path / "nope")
        assert not is_plain_file(tmp_path / "nope")

    def test_link_into_an_untraversable_directory_does_not_raise(self, tmp_path: Path):
        """``Path.is_dir()`` / ``is_file()`` follow the link and raise ``PermissionError`` on 3.11-3.13.

        3.14 returns False, so this guards the older interpreters.
        """
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root can traverse any directory")
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "inner").mkdir()
        (tmp_path / "link").symlink_to(locked / "inner")
        locked.chmod(0o000)
        try:
            assert not is_plain_dir(tmp_path / "link")
            assert not is_plain_file(tmp_path / "link")
        finally:
            locked.chmod(0o700)
