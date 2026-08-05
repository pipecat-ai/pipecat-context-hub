"""Unit tests for shared/paths.py home-redaction helpers."""

from __future__ import annotations

from pathlib import Path

from pipecat_context_hub.shared.paths import redact_home_in_text


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
        assert "https://github.com" in result
        assert "https:~/" not in result

    def test_empty_home_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: "")
        text = "https://example.com/issues/new"
        assert redact_home_in_text(text) == text

    def test_no_home_occurrence_is_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        text = "Nothing home-related here."
        assert redact_home_in_text(text) == text
