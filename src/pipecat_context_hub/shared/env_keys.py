"""Dependency-free definitions shared by the config model, the env loader and their consumers.

A leaf module (standard library only, imports nothing from this package), so
``shared/config.py`` (the model) and ``shared/env_loading.py`` (the loader) can
both import it without either importing the other, and the catalog renderer
can name the key without loading the env loader.
"""

from __future__ import annotations

__all__ = ["EXTRA_REPOS_ENV", "split_csv"]

# The single definition of the extra-repos key literal.
EXTRA_REPOS_ENV = "PIPECAT_HUB_EXTRA_REPOS"


def split_csv(raw: str) -> list[str]:
    """Split a comma-separated value into trimmed non-empty entries.

    The one CSV rule for every list-valued ``PIPECAT_HUB_*`` setting
    (``shared/config.py`` reads ``os.environ`` through it; the loader's
    ``config_list_value`` applies it to a ``config.toml`` value).
    """
    return [part.strip() for part in raw.split(",") if part.strip()]
