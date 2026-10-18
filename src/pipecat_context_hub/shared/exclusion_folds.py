"""The one per-key entry identity for the exclusion lists (the tainted repos and refs).

Two entries of an exclusion key exclude the same thing when their folds are
equal. ``SourceConfig`` (``shared/config.py``) matches taints through it and the
env loader's shadowed-exclusion warning compares entries through it, and the
loader's set of exclusion keys is this map's keys, so a new exclusion key
cannot silently get a matching rule. Depends only on the two leaf modules
``env_keys`` and ``repo_slug``, so neither the model nor the loader imports
the other through it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from pipecat_context_hub.shared.env_keys import TAINTED_REFS_ENV, TAINTED_REPOS_ENV
from pipecat_context_hub.shared.repo_slug import slug_key

__all__ = ["EXCLUSION_FOLDS", "exclusion_fold"]


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
