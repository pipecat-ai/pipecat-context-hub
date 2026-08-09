"""Unit tests for shared/paths.py home-redaction helpers and path primitives."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from pipecat_context_hub.shared.paths import redact_home_in_text, resolution_chain


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
class TestResolutionChainMatchesKernelOrdering:
    """Round-6 finding #1: the walk seeded itself from ``os.path.abspath``,
    which collapses ``..`` *lexically* — before any symlink is expanded. The
    kernel does the opposite: it expands each component in order, so a ``..``
    that follows a symlink pops out of the link's *target* directory, not out
    of the link's own parent. Seeding from a pre-normalized path therefore
    produced a chain describing locations the lookup never visits, silently
    voiding every guard built on it.
    """

    def test_pardir_after_symlink_records_the_traversed_link(self, tmp_path: Path):
        real = tmp_path / "real"
        (real / "inner").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            (outside / "link").symlink_to(real / "inner", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        chain = resolution_chain(outside / "link" / ".." / "config.toml")

        # The symlink itself is a real directory entry the lookup depends on.
        assert outside / "link" in chain
        # `..` applies to the link's *target*, so the walk lands in `real`.
        assert real / "config.toml" in chain
        # ...and never in the lexically-collapsed location the kernel skips.
        assert outside / "config.toml" not in chain

    def test_chain_endpoint_agrees_with_realpath(self, tmp_path: Path):
        """Differential floor: the last location the walk stands on must be
        what ``os.path.realpath`` reports for the same input."""
        real = tmp_path / "real"
        (real / "inner").mkdir(parents=True)
        (real / "config.toml").write_text("")
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            (outside / "link").symlink_to(real / "inner", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/permissions")

        target = outside / "link" / ".." / "config.toml"
        chain = resolution_chain(target)
        assert str(chain[-1]) == os.path.realpath(target)

    def test_relative_input_is_absolutised_against_cwd(self, tmp_path: Path, monkeypatch):
        (tmp_path / "sub").mkdir()
        monkeypatch.chdir(tmp_path)
        chain = resolution_chain(Path("sub") / "config.toml")
        assert tmp_path / "sub" / "config.toml" in chain


class TestRedactHomeInText:
    def test_redacts_embedded_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        embedded = tmp_path / "chroma.sqlite3"
        text = f"Error: unable to open database file: {embedded}"
        result = redact_home_in_text(text)
        assert str(tmp_path) not in result
        assert "~" in result

    def test_bare_home_occurrence_is_redacted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert redact_home_in_text(str(tmp_path)) == "~"

    def test_root_home_does_not_corrupt_https_urls(self, monkeypatch):
        """Regression test: a degenerate ``HOME=/`` (root/minimal-container
        environments) previously made ``home + os.sep`` a bare ``"//"``,
        which occurs inside every ``https://`` URL — including this
        function's own report-hint callers. A naive ``text.replace("//",
        "~/")`` mangled ``https://`` into ``https:~/``, corrupting the URL
        the message was trying to point the operator at.
        """
        monkeypatch.setattr(Path, "home", lambda: "/")
        text = (
            "Warning: results were poor or missing. Retry with fewer filters; "
            "if poor or missing results persist, file at "
            "https://github.com/pipecat-ai/pipecat-context-hub/issues/new?"
            "template=retrieval-quality.yml."
        )
        result = redact_home_in_text(text)
        assert result == text
        url_token = next(part for part in result.split() if part.startswith("https://"))
        parsed = urlparse(url_token)
        assert parsed.scheme == "https"
        assert parsed.hostname == "github.com"
        assert "https:~/" not in result

    def test_empty_home_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: "")
        text = "https://example.com/issues/new"
        assert redact_home_in_text(text) == text

    def test_no_home_occurrence_is_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        text = "Nothing home-related here."
        assert redact_home_in_text(text) == text
