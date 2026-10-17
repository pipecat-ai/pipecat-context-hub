"""Which catalog repos are indexed, and which catalog groups are not.

One predicate, :func:`groups_not_indexed`, decides "this group is not indexed"
for both the corpus hint and ``get_hub_status``. "Indexed" means a repo has at
least one record. Slugs are casefolded on both sides of every comparison:
GitHub slugs are case-insensitive while index keys are the configured slug
verbatim.

``shared/`` imports nothing from ``services/`` at runtime; the index store is
passed in and typed against a narrow protocol exposing only ``get_counts_by_repo``.

:func:`corpus_hint` / :func:`attach_corpus_hint` are the corpus hint itself: a
vendor question whose catalog repos are not indexed carries one compact,
deterministic ``corpus_hint`` object. No network, no embeddings, and no
dependence on result confidence (a 0.95 answer can still be a miss).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Set
from typing import Any, Protocol

from pipecat_context_hub.shared.interfaces import IndexUnavailableError
from pipecat_context_hub.shared.repo_catalog import Catalog, CatalogGroup, load_catalog
from pipecat_context_hub.shared.support_links import RETRIEVAL_QUALITY_ISSUE_URL

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
    except IndexUnavailableError:
        # A refresh started or a reopen is pending: an expected race, not a failure.
        logger.debug("Per-repo record counts unavailable: index unavailable", exc_info=True)
        return None
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


def _fold(slugs: Iterable[str]) -> frozenset[str]:
    return frozenset(s.casefold() for s in slugs)


def missing_repos(
    group: CatalogGroup, indexed: Set[str], *, tainted: Set[str] = frozenset()
) -> list[str]:
    """Parseable, untainted catalog slugs of ``group`` that have no indexed records.

    ``tainted`` is casefolded here, so callers may pass raw slugs.
    """
    skip = _fold(tainted)
    return [
        r.slug
        for r in group.repos
        if r.parseable and (key := r.slug.casefold()) not in indexed and key not in skip
    ]


def groups_not_indexed(
    catalog: Catalog | None,
    indexed: Set[str] | None,
    *,
    tainted: Set[str] = frozenset(),
) -> list[str]:
    """Names of catalog groups with at least one parseable, untainted repo not indexed.

    Catalog order. A group whose repos are all ``parseable = false`` or tainted
    never fires. ``[]`` when there is no catalog or the count was unreadable.
    """
    if catalog is None or indexed is None:
        return []
    return [g.name for g in catalog.groups if missing_repos(g, indexed, tainted=tainted)]


# Which tool arguments carry the user's topic text, per tool. A path-only
# ``get_code_snippet`` lookup never hints. Pinned to the four search tools by a
# drift-guard test; deliberately not derived from the input models.
_HINT_TEXT_ARGS: dict[str, tuple[str, ...]] = {
    "search_docs": ("query",),
    "search_examples": ("query",),
    "search_api": ("query",),
    "get_code_snippet": ("symbol", "intent"),
}

_ADD_COMMAND = "pipecat-context-hub repos add --config-only --group {group}"
_REFRESH_COMMAND = "pipecat-context-hub refresh"


def _query_text(tool: str, args: Mapping[str, Any]) -> str | None:
    keys = _HINT_TEXT_ARGS.get(tool)
    if keys is None:
        return None
    parts = [v for k in keys if isinstance(v := args.get(k), str) and v.strip()]
    return " ".join(parts) if parts else None


def _alias_matches(alias: str, text: str) -> bool:
    pattern = rf"(?<!\w){re.escape(alias.casefold())}(?!\w)"
    return re.search(pattern, text) is not None


def corpus_hint(
    index_store: _CountsSource,
    *,
    tool: str,
    args: Mapping[str, Any],
    configured_repos: Set[str],
    tainted_repos: Set[str],
) -> dict[str, Any] | None:
    """The ``corpus_hint`` object for one search call, or ``None`` when none applies.

    Fires when a catalog group alias appears in the tool's query text (word
    boundary, case-insensitive, over the whole text including every ``+`` / ``&``
    concept) and that group has a parseable, untainted repo with no indexed
    records. ``None`` (no key at all) for non-search tools, no alias match, no
    catalog, or an unreadable count. Never raises.

    ``repos_not_indexed`` lists missing repos that are not configured;
    ``repos_configured_no_records`` lists missing repos that are configured (a
    failed clone), so the two never overlap. ``add_command`` covers only groups
    with a repo that is neither configured nor tainted.
    """
    try:
        text = _query_text(tool, args)
        if text is None:
            return None
        catalog = load_catalog()
        if catalog is None:
            return None
        folded = text.casefold()
        matched = [g for g in catalog.groups if any(_alias_matches(a, folded) for a in g.aliases)]
        if not matched:
            return None
        indexed = indexed_repos(index_store)
        if indexed is None:
            return None
        tainted = _fold(tainted_repos)
        configured = _fold(configured_repos) - tainted

        groups: list[str] = []
        not_indexed: list[str] = []
        no_records: list[str] = []
        add_commands: list[str] = []
        for group in matched:
            missing = missing_repos(group, indexed, tainted=tainted)
            if not missing:
                continue
            groups.append(group.name)
            fresh = [s for s in missing if s.casefold() not in configured]
            not_indexed.extend(fresh)
            no_records.extend(s for s in missing if s.casefold() in configured)
            if fresh:
                add_commands.append(_ADD_COMMAND.format(group=group.name))
        if not groups:
            return None

        hint: dict[str, Any] = {
            "matched_groups": groups,
            "repos_not_indexed": not_indexed,
            "repos_configured_no_records": no_records,
        }
        if add_commands:
            hint["add_command"] = "; ".join(add_commands)
        if no_records:
            hint["refresh_command"] = _REFRESH_COMMAND
        hint["report_issue_url"] = RETRIEVAL_QUALITY_ISSUE_URL
        return hint
    except Exception:  # noqa: BLE001 - the hint must never cost a response
        logger.exception("Failed to compute the corpus hint")
        return None


def attach_corpus_hint(result_json: str, hint: dict[str, Any] | None) -> str:
    """Add ``corpus_hint`` to a tool's JSON response; unchanged when there is no hint.

    Best-effort like ``annotate_response``: a non-object payload or any failure
    returns the input untouched.
    """
    if hint is None:
        return result_json
    try:
        payload = json.loads(result_json)
        if not isinstance(payload, dict):
            return result_json
        payload["corpus_hint"] = hint
        return json.dumps(payload)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to attach the corpus hint")
        return result_json
