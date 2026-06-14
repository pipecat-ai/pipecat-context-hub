"""Fence-aware markdown heading utilities.

Shared by the docs ingester and the retrieval layer so that heading detection
is consistent everywhere: headings inside fenced code blocks (``` or ~~~) are
never mistaken for real document headings, and only valid ATX heading syntax
(``#{1,6}`` followed by whitespace) counts.

Layering note: this lives in ``shared/`` so both ``services.ingest`` and
``services.retrieval`` may depend on it (``retrieval`` must not import from
``ingest``).
"""

from __future__ import annotations

import bisect
import re

_FENCE_RE = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def fenced_ranges(markdown: str) -> list[tuple[int, int]]:
    """Return (start, end) character ranges of fenced code blocks.

    A fence opens on a line starting with three or more backticks or tildes and
    closes on the next line starting with at least as many of the same fence
    character. An unclosed fence extends to the end of the document.

    The returned ranges are sorted by start offset and non-overlapping (an
    unclosed fence, if any, is always the last range) — :func:`inside_fence`
    relies on this for its binary search.
    """
    ranges: list[tuple[int, int]] = []
    it = _FENCE_RE.finditer(markdown)
    for open_match in it:
        fence_char = open_match.group(1)[0]
        fence_len = len(open_match.group(1))
        closed = False
        for close_match in it:
            if close_match.group(1)[0] == fence_char and len(close_match.group(1)) >= fence_len:
                ranges.append((open_match.start(), close_match.end()))
                closed = True
                break
        if not closed:
            # Unclosed fence runs to end of document.
            ranges.append((open_match.start(), len(markdown)))
    return ranges


def inside_fence(pos: int, ranges: list[tuple[int, int]]) -> bool:
    """Check if a position falls inside any fenced code block.

    ``ranges`` must be the output of :func:`fenced_ranges` (sorted by start,
    non-overlapping). Binary-searches for the last range starting at or before
    ``pos`` — O(log n) per call, so callers scanning every heading position stay
    O(n log n) rather than O(n*m).
    """
    # Largest range whose start <= pos. ``(pos, inf)`` sorts after every
    # ``(start, end)`` with start == pos, so bisect_right-1 lands on it.
    idx = bisect.bisect_right(ranges, (pos, float("inf"))) - 1
    return idx >= 0 and ranges[idx][1] > pos


def iter_headings(content: str) -> list[tuple[int, str, int]]:
    """Return ``(level, title, line_index)`` for each real markdown heading.

    Headings inside fenced code blocks are skipped, and only valid ATX syntax
    (``#{1,6}`` + whitespace + title) is matched. ``line_index`` is the 0-based
    index of the heading line in ``content.split("\n")`` (newline count before
    the heading) — callers slicing by it must split on ``"\n"`` too, not
    ``str.splitlines`` (which breaks on additional Unicode separators).
    """
    fences = fenced_ranges(content)
    headings: list[tuple[int, str, int]] = []
    # Count newlines incrementally as the regex walks forward (matches are in
    # increasing position order) instead of rescanning from offset 0 per heading
    # — keeps the whole pass O(n log m) rather than O(n*m) on heading-dense docs.
    nl_count = 0
    last_pos = 0
    for match in _HEADING_RE.finditer(content):
        start = match.start()
        nl_count += content.count("\n", last_pos, start)
        last_pos = start
        if inside_fence(start, fences):
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        headings.append((level, title, nl_count))
    return headings


def heading_titles(content: str) -> list[str]:
    """List a page's heading titles in document order, dedup case-insensitively.

    Skips headings inside fenced code blocks. Duplicate titles that would
    resolve to the same section (case-insensitive) are collapsed to their first
    occurrence, producing a stable table of contents rather than a raw dump.
    """
    titles: list[str] = []
    seen: set[str] = set()
    for _level, title, _line_index in iter_headings(content):
        if not title:
            continue
        key = title.lower()
        if key not in seen:
            seen.add(key)
            titles.append(title)
    return titles
