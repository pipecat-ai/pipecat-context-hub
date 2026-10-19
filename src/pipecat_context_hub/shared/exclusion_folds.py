"""The one per-key entry identity for the exclusion lists (the tainted repos and refs).

Two entries of an exclusion key exclude the same thing when their folds are
equal. :func:`taint_matcher` is the one taint-matching rule built on it
(``SourceConfig.is_tainted`` and ``corpus_hint`` both call it), and the
env loader's shadowed-exclusion warning compares entries through it, and the
loader's set of exclusion keys is this map's keys, so a new exclusion key
cannot silently get a matching rule. Depends only on the two leaf modules
``env_keys`` and ``repo_slug``, so neither the model nor the loader imports
the other through it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType

from pipecat_context_hub.shared.env_keys import TAINTED_REFS_ENV, TAINTED_REPOS_ENV
from pipecat_context_hub.shared.repo_slug import slug_key

__all__ = ["EXCLUSION_FOLDS", "exclusion_fold", "taint_matcher"]


def _exact(entry: str) -> str:
    return entry


# Taints compare by `slug_key` (GitHub slugs are case-insensitive);
# `org/repo@ref` entries stay exact.
EXCLUSION_FOLDS: Mapping[str, Callable[[str], str]] = MappingProxyType(
    {TAINTED_REPOS_ENV: slug_key, TAINTED_REFS_ENV: _exact}
)


def exclusion_fold(key: str) -> Callable[[str], str]:
    """The entry fold for exclusion key ``key``; ``KeyError`` for any other key."""
    try:
        return EXCLUSION_FOLDS[key]
    except KeyError:
        raise KeyError(f"{key!r} is not an exclusion key; add it to EXCLUSION_FOLDS") from None


def taint_matcher(tainted: Iterable[str]) -> Callable[[str], bool]:
    """The one taint-matching rule over an explicit taint list.

    A slug is tainted when its ``TAINTED_REPOS_ENV`` fold equals an entry's.
    ``SourceConfig.is_tainted`` decides through it, and so does
    ``corpus_hint`` (the corpus hint and ``get_hub_status``), which takes the
    taint list as data.
    """
    fold = exclusion_fold(TAINTED_REPOS_ENV)
    keys = frozenset(fold(t) for t in tainted)
    return lambda slug: fold(slug) in keys
