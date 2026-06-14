"""Unit tests for fence-aware markdown heading utilities."""

from __future__ import annotations

from pipecat_context_hub.shared.markdown import (
    fenced_ranges,
    heading_titles,
    inside_fence,
    iter_headings,
)


class TestFencedRanges:
    def test_backtick_fence(self):
        md = "before\n```\ncode\n```\nafter"
        ranges = fenced_ranges(md)
        assert len(ranges) == 1
        start, end = ranges[0]
        assert md[start : start + 3] == "```"
        assert not inside_fence(0, ranges)
        assert inside_fence(md.index("code"), ranges)

    def test_tilde_fence(self):
        md = "~~~\ncode\n~~~"
        assert len(fenced_ranges(md)) == 1

    def test_unclosed_fence_runs_to_end(self):
        md = "## Real\n\n```\n# not a heading\nmore"
        ranges = fenced_ranges(md)
        assert len(ranges) == 1
        assert ranges[0][1] == len(md)

    def test_inside_fence_binary_search_multiple_ranges(self):
        # inside_fence binary-searches sorted, non-overlapping ranges; verify it
        # is correct across several blocks and at range boundaries (half-open).
        md = "a\n```\nx\n```\nb\n~~~\ny\n~~~\nc"
        ranges = fenced_ranges(md)
        assert len(ranges) == 2
        for start, end in ranges:
            assert not inside_fence(start - 1, ranges)  # char before open fence
            assert inside_fence(start, ranges)  # start is inside (half-open)
            assert inside_fence(end - 1, ranges)  # last char inside
            assert not inside_fence(end, ranges)  # end is exclusive
        assert not inside_fence(len(md) - 1, ranges)  # trailing 'c' between/after


class TestIterHeadings:
    def test_levels_and_titles(self):
        md = "# A\n\ntext\n\n## B\n\n### C"
        headings = iter_headings(md)
        assert [(lvl, title) for lvl, title, _ in headings] == [
            (1, "A"),
            (2, "B"),
            (3, "C"),
        ]

    def test_skips_fenced_headings(self):
        md = "# Real\n\n```bash\n# install deps\nuv sync\n```\n\n## Next"
        titles = [t for _, t, _ in iter_headings(md)]
        assert titles == ["Real", "Next"]
        assert "install deps" not in titles

    def test_requires_whitespace_after_hashes(self):
        # No space → not a heading; 7+ hashes exceeds ATX max.
        md = "#nospace\n\n####### toolevel\n\n## Valid"
        titles = [t for _, t, _ in iter_headings(md)]
        assert titles == ["Valid"]

    def test_line_index_is_zero_based(self):
        md = "intro\n\n## Heading\n\nbody"
        _level, title, line_index = iter_headings(md)[0]
        assert title == "Heading"
        assert md.splitlines()[line_index] == "## Heading"


class TestHeadingTitles:
    def test_document_order(self):
        md = "## First\n\n## Second\n\n## Third"
        assert heading_titles(md) == ["First", "Second", "Third"]

    def test_case_insensitive_dedup_keeps_first(self):
        md = "## Setup\n\n## setup\n\n## Done"
        assert heading_titles(md) == ["Setup", "Done"]

    def test_skips_fenced_comments(self):
        md = "## Install\n\n```\n# install deps\n```\n\n## Usage"
        assert heading_titles(md) == ["Install", "Usage"]
