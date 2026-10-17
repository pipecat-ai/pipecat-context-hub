"""The one ``owner/repo`` slug grammar (dependency-free leaf module).

Shared by the ingester (``github_ingest.clone_or_fetch``), the repo catalog,
``repos`` and the config writer, so a slug any of them accepts can be cloned.
"""

from __future__ import annotations

import re

__all__ = ["REPO_SLUG_RE", "is_valid_repo_slug"]

# `owner/repo`, each part starting with a letter or digit, then letters,
# digits, `.`, `_` or `-`; it excludes `.`/`..` segments and a trailing
# newline. Self-anchoring (`\A`..`\Z`), so `.match()`, `.search()` and
# `.fullmatch()` agree: a consumer cannot weaken it by choosing the wrong method.
REPO_SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def is_valid_repo_slug(slug: str) -> bool:
    """True when ``slug`` is a well-formed ``owner/repo`` (:data:`REPO_SLUG_RE`)."""
    return REPO_SLUG_RE.fullmatch(slug) is not None
