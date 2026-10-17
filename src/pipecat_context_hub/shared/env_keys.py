"""Dependency-free definitions shared by the config model, the env loader and their consumers.

A leaf module (standard library only, imports nothing from this package), so
``shared/config.py`` (the model) and ``shared/env_loading.py`` (the loader) can
both import it without either importing the other, and the catalog renderer
can name the key without loading the env loader.
"""

from __future__ import annotations

__all__ = [
    "EXTRA_REPOS_ENV",
    "TAINTED_REFS_ENV",
    "TAINTED_REPOS_ENV",
    "split_csv",
    "taint_key",
]

# The single definitions of the list-valued key literals.
EXTRA_REPOS_ENV = "PIPECAT_HUB_EXTRA_REPOS"
# Repos skipped entirely by local policy (comma-separated `owner/repo`).
TAINTED_REPOS_ENV = "PIPECAT_HUB_TAINTED_REPOS"
# Specific upstream refs skipped (`org/repo@ref,org/repo@other-ref`).
TAINTED_REFS_ENV = "PIPECAT_HUB_TAINTED_REFS"


def split_csv(raw: str) -> list[str]:
    """Split a comma-separated value into trimmed non-empty entries.

    The one CSV rule for every list-valued ``PIPECAT_HUB_*`` setting
    (``shared/config.py`` reads ``os.environ`` through it; the loader's
    ``config_list_value`` applies it to a ``config.toml`` value, and its
    shadowed-taint warning to the winning layer's value).
    """
    return [part.strip() for part in raw.split(",") if part.strip()]


def taint_key(slug: str) -> str:
    """The one taint-matching fold for ``PIPECAT_HUB_TAINTED_REPOS`` entries.

    Casefolded, because GitHub slugs are case-insensitive: ``Foo/Bar`` and
    ``foo/bar`` name the same repo. ``SourceConfig.is_tainted`` matches through
    it, and the loader's shadowed-taint warning compares through it, so the
    warning never reports an entry as lost that ``is_tainted`` still honours.
    (``PIPECAT_HUB_TAINTED_REFS`` matching stays exact.)
    """
    return slug.casefold()
