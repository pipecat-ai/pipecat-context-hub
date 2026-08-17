"""Tests for shared.versioning.strip_v_prefix."""

from __future__ import annotations

from pipecat_context_hub.shared.versioning import strip_v_prefix


class TestStripVPrefix:
    def test_strips_single_leading_v(self):
        assert strip_v_prefix("v1.0.0") == "1.0.0"

    def test_strips_only_one_leading_v(self):
        """The whole point of the fix: str.lstrip('v') strips a character set,
        collapsing 'vv1.0.0' to '1.0.0' — indistinguishable from 'v1.0.0'.
        """
        assert strip_v_prefix("vv1.0.0") == "v1.0.0"

    def test_no_v_prefix_unchanged(self):
        assert strip_v_prefix("1.0.0") == "1.0.0"

    def test_empty_string_unchanged(self):
        assert strip_v_prefix("") == ""
