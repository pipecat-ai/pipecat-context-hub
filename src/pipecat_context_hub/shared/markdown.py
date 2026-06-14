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

import re

_FENCE_RE = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def fenced_ranges(markdown: str) -> list[tuple[int, int]]:
    """Return (start, end) character ranges of fenced code blocks.

    A fence opens on a line starting with three or more backticks or tildes and
    closes on the next line starting with at least as many of the same fence
    character. An unclosed fence extends to the end of the document.
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
    """Check if a position falls inside any fenced code block."""
    for start, end in ranges:
        if start <= pos < end:
            return True
    return False


def iter_headings(content: str) -> list[tuple[int, str, int]]:
    """Return ``(level, title, line_index)`` for each real markdown heading.

    Headings inside fenced code blocks are skipped, and only valid ATX syntax
    (``#{1,6}`` + whitespace + title) is matched. ``line_index`` is the 0-based
    index of the heading line in ``content.splitlines()``.
    """
    fences = fenced_ranges(content)
    headings: list[tuple[int, str, int]] = []
    for match in _HEADING_RE.finditer(content):
        if inside_fence(match.start(), fences):
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        line_index = content.count("\n", 0, match.start())
        headings.append((level, title, line_index))
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
