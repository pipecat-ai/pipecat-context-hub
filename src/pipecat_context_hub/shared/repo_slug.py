"""The one ``owner/repo`` slug grammar and slug identity (dependency-free leaf module).

Shared by the ingester (``github_ingest.clone_or_fetch``), the repo catalog,
``repos`` and the config writer, so a slug any of them accepts can be cloned,
and every one of them compares two slugs through :func:`slug_key`.
"""

from __future__ import annotations

import re

__all__ = ["REPO_SLUG_RE", "is_valid_repo_slug", "slug_key"]

# `owner/repo`, each part starting with a letter or digit, then letters,
# digits, `.`, `_` or `-`; it excludes `.`/`..` segments and a trailing
# newline. Self-anchoring (`\A`..`\Z`), so `.match()`, `.search()` and
# `.fullmatch()` agree: a consumer cannot weaken it by choosing the wrong method.
REPO_SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def is_valid_repo_slug(slug: str) -> bool:
    """True when ``slug`` is a well-formed ``owner/repo`` (:data:`REPO_SLUG_RE`)."""
    return REPO_SLUG_RE.fullmatch(slug) is not None


def slug_key(slug: str) -> str:
    """The one slug identity: two slugs name the same repo when their keys are equal.

    Casefolded, because GitHub slugs are case-insensitive (``Foo/Bar`` and
    ``foo/bar`` are one repo). Every slug comparison goes through it: the
    default and taint predicates (``SourceConfig.is_default`` /
    ``is_tainted``), the ``effective_repos`` dedupe, the config writer, the
    catalog and ``repos``. Index records are a different thing: they are keyed
    by the exact spelling refresh ingested, so refresh's cleanup compares those
    exactly (see ``cli.py``). ``PIPECAT_HUB_TAINTED_REFS`` matching stays exact.
    """
    return slug.casefold()
