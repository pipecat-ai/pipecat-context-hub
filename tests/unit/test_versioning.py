"""Tests for the shared version-tag string helpers."""

from __future__ import annotations

import pytest
from packaging.version import InvalidVersion, Version

from pipecat_context_hub.shared.versioning import (
    DEFAULT_FRAMEWORK_PIN,
    HEAD_SENTINEL,
    LATEST_SENTINEL,
    canonicalize_framework_pin,
    exact_release_version,
    is_head_sentinel,
    is_latest_sentinel,
    parse_release_version,
    strip_v_prefix,
)


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


class TestIsLatestSentinel:
    @pytest.mark.parametrize("value", ["latest", "LATEST", "Latest", "  latest  ", "\tLaTeSt\n"])
    def test_accepts_any_casing_or_surrounding_whitespace(self, value: str):
        assert is_latest_sentinel(value) is True

    @pytest.mark.parametrize("value", [None, "", "v1.2.0", "late", "latest-rc", "la test"])
    def test_rejects_everything_else(self, value: str | None):
        assert is_latest_sentinel(value) is False


class TestIsHeadSentinel:
    @pytest.mark.parametrize("value", ["head", "HEAD", "main", "  Main  ", "\tHeAd\n"])
    def test_accepts_either_spelling_in_any_casing(self, value: str):
        assert is_head_sentinel(value) is True

    @pytest.mark.parametrize("value", [None, "", "v1.2.0", "latest", "heads", "mainline"])
    def test_rejects_everything_else(self, value: str | None):
        assert is_head_sentinel(value) is False


class TestDefaultFrameworkPin:
    def test_default_is_the_latest_sentinel(self):
        """An operator who names no pin gets the newest release, not the default branch."""
        assert DEFAULT_FRAMEWORK_PIN == LATEST_SENTINEL
        assert is_latest_sentinel(DEFAULT_FRAMEWORK_PIN) is True


class TestCanonicalizeFrameworkPin:
    @pytest.mark.parametrize("value", ["latest", "  LATEST  ", "Latest"])
    def test_sentinel_collapses_to_canonical_spelling(self, value: str):
        assert canonicalize_framework_pin(value) == LATEST_SENTINEL

    @pytest.mark.parametrize("value", ["head", "  MAIN  ", "Main"])
    def test_head_spellings_collapse_to_one(self, value: str):
        assert canonicalize_framework_pin(value) == HEAD_SENTINEL

    @pytest.mark.parametrize("value", ["v0.0.96", " v0.0.96 ", "some-feature-tag", ""])
    def test_non_sentinel_returned_verbatim(self, value: str):
        """Only the sentinel is normalised — a real pin is handed to git as typed."""
        assert canonicalize_framework_pin(value) == value


class TestParseReleaseVersion:
    def test_parses_v_prefixed_tag(self):
        assert parse_release_version("v1.10.0") == Version("1.10.0")

    def test_parses_bare_version(self):
        assert parse_release_version("1.10.0") == Version("1.10.0")

    def test_parses_uppercase_v_prefix(self):
        """The strip is case-insensitive, so a repo whose newest release is
        tagged `V2.0.0` is not silently dropped from `latest` candidacy."""
        assert parse_release_version("V2.0.0") == Version("2.0.0")

    @pytest.mark.parametrize("value", ["vv1.0.0", "vV1.0.0", "Vv1.0.0", "VV1.0.0"])
    def test_rejects_doubled_prefix_in_any_casing(self, value: str):
        """`Version()` alone would normalise "v1.0.0" (what a single strip leaves
        behind) straight back to 1.0.0, erasing the single-prefix guarantee. The
        guard is symmetric with the strip, so no casing sneaks a second prefix
        through."""
        with pytest.raises(InvalidVersion):
            parse_release_version(value)

    @pytest.mark.parametrize("value", ["", "nightly", "some-feature-tag", "main"])
    def test_rejects_non_versions(self, value: str):
        with pytest.raises(InvalidVersion):
            parse_release_version(value)

    def test_prerelease_parses(self):
        assert parse_release_version("v2.0.0rc1").is_prerelease


class TestExactReleaseVersion:
    @pytest.mark.parametrize(
        ("tag", "expected"),
        [("v1.10.0", "1.10.0"), ("1.10.0", "1.10.0"), ("V2.0.0", "2.0.0"), ("  v1.0.0  ", "1.0.0")],
    )
    def test_release_tags_normalize(self, tag: str, expected: str):
        assert exact_release_version(tag) == expected

    @pytest.mark.parametrize("tag", [None, "some-feature-tag", "main", "nightly", "vv1.0.0", ""])
    def test_non_releases_are_none(self, tag: str | None):
        """A tag with no version identity must not be mistaken for an exact
        release — the caller has to fall back to git-describe floor semantics."""
        assert exact_release_version(tag) is None

    def test_local_version_segment_is_not_a_bare_release(self):
        """`v1.10.0+cu121` parses, and normalizing it keeps the local segment —
        so it never silently reads as the plain 1.10.0 release."""
        assert exact_release_version("v1.10.0+cu121") == "1.10.0+cu121"
