"""Unit tests for fence-aware markdown heading utilities."""

from __future__ import annotations

from pipecat_context_hub.shared.markdown import (
    extract_section,
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
        _level, title, line_index = next(iter(iter_headings(md)))
        assert title == "Heading"
        assert md.split("\n")[line_index] == "## Heading"

    def test_iter_headings_is_lazy_generator(self):
        from collections.abc import Iterator

        result = iter_headings("## A\n\n## B")
        assert isinstance(result, Iterator)
        assert [t for _, t, _ in result] == ["A", "B"]
        # Generator is exhausted after one pass.
        assert list(result) == []


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


class TestExtractSection:
    """Tests for extract_section — must stay in lockstep with heading_titles."""

    def test_extract_skips_fenced_comment(self):
        content = "## Install\n\nRun:\n\n```bash\n# install deps\nuv sync\n```\n\n## Usage\n\nGo.\n"
        # A comment inside a fence is not a selectable section.
        assert extract_section(content, "install deps") is None
        # The real section still extracts, fenced block preserved verbatim.
        install = extract_section(content, "Install")
        assert install is not None
        assert "# install deps" in install
        assert "## Usage" not in install

    def test_titles_round_trip_through_extract(self):
        content = (
            "## Events Overview\n\nIntro.\n\n"
            "```python\n# example handler\npass\n```\n\n"
            "## Multiple Handlers\n\nBody.\n"
        )
        for title in heading_titles(content):
            extracted = extract_section(content, title)
            assert extracted is not None
            assert extracted.split("\n")[0].lstrip("#").strip() == title

    def test_extract_returns_none_for_missing_section(self):
        assert extract_section("## A\n\nbody\n", "Nonexistent") is None

    def test_extract_round_trip_with_non_newline_line_separators(self):
        # iter_headings indexes lines by counting "\n"; extract_section must
        # split on "\n" too (not str.splitlines, which also breaks on \f, \x85,
        # \x1c-\x1e, \x85, \u2028, \u2029, ...). A body carrying those chars
        # would otherwise drift the slice boundary and return the wrong line.
        for sep in ("\x0c", "\x85", "\u2028", "\u2029"):
            content = f"## Alpha\n\nbefore{sep}after\n\n## Beta\n\nbody\n"
            for title in heading_titles(content):
                extracted = extract_section(content, title)
                assert extracted is not None
                # The extracted block must START at the requested heading.
                assert extracted.split("\n")[0].lstrip("#").strip() == title, (
                    sep,
                    title,
                    extracted,
                )
