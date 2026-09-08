"""Per-file ingest filters shared by ``source_ingest.py`` and ``github_ingest.py``.

The two ingesters walk the *same* cloned TypeScript/JavaScript trees
independently and for different corpora: ``source_ingest.py`` builds the
symbol-level ``content_type="source"`` chunks behind ``search_api`` /
``get_code_snippet``; ``github_ingest.py`` builds the raw-text
``content_type="code"`` chunks behind ``search_examples``. A per-file
exclusion or dedup rule added to only one silently leaves the same noise in
the other's results.

That's exactly what happened: Storybook-fixture exclusion and
byte-identical-file dedup (``.stories.ts(x)`` files, and a shadcn-style
registry component vendored byte-for-byte into a demo app under the same
repo) were added to ``source_ingest.py`` only. A live end-to-end refresh
against ``pipecat-ai/pipecat-ui`` after it went public found both problems
still present in ``search_examples`` results — the fixture file and the
vendored duplicate were never filtered from ``github_ingest.py``'s
independent file walk. These two helpers are the single source of truth for
both ingesters going forward, so a future fix here can't drift the same way.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Storybook Component Story Format files. Fixture/demo code co-located next
# to the real component they document, not excludable by directory the way
# tests/examples dirs are.
_STORYBOOK_SUFFIXES: tuple[str, ...] = (".stories.ts", ".stories.tsx")


def is_storybook_file(path: Path) -> bool:
    """True for a Storybook CSF fixture (``*.stories.ts`` / ``*.stories.tsx``)."""
    return path.name.endswith(_STORYBOOK_SUFFIXES)


def hash_source(text: str) -> str:
    """Content hash used to detect byte-identical files vendored at multiple
    paths within the same repo (e.g. a shadcn-style registry component also
    copied into a demo app).

    Hashed on raw file text, before any chunk rendering: a rendered chunk's
    ``content`` typically embeds the file's own path (e.g. a "Module: <path>"
    header), so it differs even between byte-identical files and can't be
    used for this comparison.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
