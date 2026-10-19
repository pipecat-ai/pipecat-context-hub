"""Shared pieces of the two repo catalog scripts (``check_repo_catalog.py`` and
``curate_repo_catalog.py``).

``scripts/`` is not a package: each script imports this module as a sibling
(``python scripts/<name>.py`` puts ``scripts/`` on ``sys.path``), and the tests
load it by path before the scripts.

:func:`validated_body` is the one place either script turns a GitHub response
into data. A caller names every field it will read with a check, and gets the
body back only when the status, the JSON object and every named field pass;
otherwise it gets a reason and no data, so no caller reads a field unvalidated.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from pipecat_context_hub.shared.repo_catalog import (
    Catalog,
    CatalogError,
    load_catalog,
    parse_catalog,
)

API_BASE = "https://api.github.com"

# Both scripts: the catalog could not be loaded.
EXIT_CATALOG = 3

FieldCheck = Callable[[object], bool]


def non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def plain_int(value: object) -> bool:
    """A non-negative ``int`` that is not a ``bool`` (``True`` is an ``int`` in Python)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_bool(value: object) -> bool:
    return isinstance(value, bool)


def is_list(value: object) -> bool:
    return isinstance(value, list)


def validated_body(
    response: httpx.Response,
    fields: Mapping[str, FieldCheck],
    *,
    status: int | None = 200,
) -> tuple[dict[str, object] | None, str]:
    """``(body, "")`` when the response is fully valid, else ``(None, reason)``.

    Valid means: the status is ``status`` (any status when ``None``), the body
    is a JSON object, and every field in ``fields`` is present and passes its
    check. A caller reads only the fields it named.
    """
    if status is not None and response.status_code != status:
        return None, f"HTTP {response.status_code}"
    try:
        data = response.json()
    except ValueError:
        return None, "body is not JSON"
    if not isinstance(data, dict):
        return None, "body is not a JSON object"
    for name, check in fields.items():
        if name not in data or not check(data[name]):
            return None, f"field {name!r} missing or malformed"
    return data, ""


def github_headers(user_agent: str, token: str | None) -> dict[str, str]:
    """GitHub REST headers; ``Authorization`` is present only when a token is given."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": user_agent,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def load_catalog_arg(path: Path | None) -> Catalog | None:
    """The catalog at ``path`` (the packaged one when ``None``), or ``None`` on failure.

    A failure prints one line on stdout as well as stderr, so the report is
    never empty: the health workflow files an issue from stdout, and a curate
    run shows why it stopped.
    """
    try:
        catalog = (
            parse_catalog(path.read_text(encoding="utf-8")) if path is not None else load_catalog()
        )
    except (CatalogError, OSError, UnicodeDecodeError) as exc:
        _catalog_failure(f"cannot load the catalog: {exc}")
        return None
    if catalog is None:
        _catalog_failure("cannot load the catalog (see log)")
    return catalog


def catalog_slugs(catalog: Catalog) -> list[str]:
    """Every catalog repo slug, in catalog order (both scripts walk the same list)."""
    return [repo.slug for group in catalog.groups for repo in group.repos]


def _catalog_failure(message: str) -> None:
    print(message)
    print(message, file=sys.stderr)
