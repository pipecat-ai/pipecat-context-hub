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

The same module owns ``is_plain_file`` / ``is_plain_dir``, the one rule for which
filesystem entries an example walk may follow (real files and directories, never
a symlink or junction). ``github_ingest.py``, ``taxonomy.py`` and
``source_ingest.py`` all apply it to the entries they discover.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from pipecat_context_hub.shared.paths import is_reparse_link

# Storybook Component Story Format files. Fixture/demo code co-located next
# to the real component they document, not excludable by directory the way
# tests/examples dirs are.
_STORYBOOK_SUFFIXES: tuple[str, ...] = (".stories.ts", ".stories.tsx")


def is_plain_file(path: Path) -> bool:
    """True for a regular file that is not a link. Never raises.

    One ``lstat`` (does not follow links), so it can neither be fooled by nor
    trip over a link's target. ``Path.is_file()`` follows the link first and
    raises ``PermissionError`` for a link into a directory the user cannot
    traverse, which aborts the whole refresh just as a dangling link did.
    """
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except (OSError, ValueError):
        return False


def is_plain_dir(path: Path) -> bool:
    """True for a real directory that is not a symlink or Windows junction. Never raises.

    The rule for every directory an ingester treats as an example (or as the
    ``examples/`` tree holding them). A committed directory symlink can point
    outside the clone, so following it reads files that were never part of the
    repo; ``github_ingest`` and the taxonomy builder must refuse the same ones, or
    one indexes what the other skipped.

    Only the final path component is inspected: a real directory reached through
    a linked *parent* still passes, so a caller must validate each ancestor it
    does not already trust (``examples/`` before ``examples/foundational``).
    """
    try:
        return not is_reparse_link(path) and stat.S_ISDIR(path.lstat().st_mode)
    except (OSError, ValueError):
        return False


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
