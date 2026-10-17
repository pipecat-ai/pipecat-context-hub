"""Which catalog repos are indexed, and which catalog groups are not.

One predicate, :func:`groups_not_indexed`, decides "this group is not indexed"
for both the corpus hint and ``get_hub_status``. "Indexed" means a repo has at
least one record. Slugs are casefolded on both sides of every comparison:
GitHub slugs are case-insensitive while index keys are the configured slug
verbatim.

``shared/`` imports nothing from ``services/`` at runtime; the index store is
passed in and typed against a narrow protocol exposing only ``get_counts_by_repo``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Set
from typing import Protocol

from pipecat_context_hub.shared.repo_catalog import Catalog, CatalogGroup

logger = logging.getLogger(__name__)

# Synthetic key `get_counts_by_repo` injects for crawled docs; not a repo.
_DOCS_KEY = "docs.pipecat.ai"


class _CountsSource(Protocol):
    """The one capability :func:`indexed_repos` needs from a serving index."""

    def get_counts_by_repo(self) -> dict[str, int]: ...


def indexed_repos(index_store: _CountsSource) -> frozenset[str] | None:
    """Casefolded slugs of repos with at least one record, or ``None``.

    ``None`` means the count was unreadable (missing method, any raise, or a
    non-``Mapping`` result such as a ``MagicMock``). It is never an empty set
    for that case: an empty set would make every catalog group look unindexed.
    """
    try:
        counts = index_store.get_counts_by_repo()
    except Exception as exc:  # noqa: BLE001 - any failure means "unreadable"
        logger.warning("Per-repo record counts unreadable (%s: %s)", type(exc).__name__, exc)
        return None
    if not isinstance(counts, Mapping):
        logger.warning(
            "Per-repo record counts unreadable (got %s, expected a mapping)",
            type(counts).__name__,
        )
        return None
    return frozenset(
        str(slug).casefold()
        for slug, count in counts.items()
        if str(slug).casefold() != _DOCS_KEY and isinstance(count, int) and count > 0
    )


def configured_no_records(configured: Iterable[str], indexed: Set[str] | None) -> list[str]:
    """Configured slugs (original case) with no indexed records; ``[]`` if unreadable."""
    if indexed is None:
        return []
    return sorted(slug for slug in configured if slug.casefold() not in indexed)


def missing_repos(group: CatalogGroup, indexed: Set[str]) -> list[str]:
    """Parseable catalog slugs of ``group`` that have no indexed records."""
    return [r.slug for r in group.repos if r.parseable and r.slug.casefold() not in indexed]


def groups_not_indexed(catalog: Catalog | None, indexed: Set[str] | None) -> list[str]:
    """Names of catalog groups with at least one parseable repo not indexed.

    Catalog order. A group whose repos are all ``parseable = false`` never
    fires. ``[]`` when there is no catalog or the count was unreadable.
    """
    if catalog is None or indexed is None:
        return []
    return [g.name for g in catalog.groups if missing_repos(g, indexed)]
