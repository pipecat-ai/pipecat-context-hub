#!/usr/bin/env python3
"""Weekly health check for the curated repo catalog (existence first).

For every catalog entry, ask the GitHub REST API about ``/repos/{slug}`` and
classify the answer:

- ``ok``            public, not archived, same name
- ``inaccessible``  404 (missing or private; indistinguishable) or a 200 with
                    ``private: true``. The catalog is public-only, so both fail.
- ``archived``      200 with ``archived: true``
- ``renamed``       a 301/308 redirect, or a 200 whose ``full_name`` differs from
                    the slug beyond case. The new name is reported; GitHub's real
                    redirect target is ``/repositories/{id}``, so one more
                    non-following GET resolves it (failure keeps ``renamed``
                    with no new name).
- ``inconclusive``  403/429 (rate limit), 5xx, timeout, network error, or any
                    other unexpected answer, including a 200 that is not a repository
                    object (no string ``full_name``). Reported in its own section, never
                    labelled inaccessible or private.

Exit codes (``aggregate_verdict``): 0 only when every entry is ok (a stale
``verified`` date is advisory and does not fail); 1 when any entry is
inaccessible, archived or renamed; 2 when the only problems are inconclusive
(re-run the job); 3 when the catalog cannot be loaded.

``follow_redirects`` is False so a 301 is observable. ``GITHUB_TOKEN`` is sent
as a bearer token when set and omitted otherwise.

Usage::

    uv run python scripts/check_repo_catalog.py
    uv run python scripts/check_repo_catalog.py --catalog path/to/repo_catalog.toml
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from pipecat_context_hub.shared.repo_catalog import (
    Catalog,
    CatalogError,
    load_catalog,
    parse_catalog,
)

API_BASE = "https://api.github.com"
ADVISORY_DAYS = 90
TIMEOUT_SECONDS = 15.0

STATUS_OK = "ok"
STATUS_INACCESSIBLE = "inaccessible"
STATUS_ARCHIVED = "archived"
STATUS_RENAMED = "renamed"
STATUS_INCONCLUSIVE = "inconclusive"

EXIT_OK = 0
EXIT_STALE = 1
EXIT_INCONCLUSIVE = 2
EXIT_CATALOG = 3

_STALE_STATUSES = frozenset({STATUS_INACCESSIBLE, STATUS_ARCHIVED, STATUS_RENAMED})


@dataclass(frozen=True)
class EntryResult:
    """The classified outcome for one catalog slug."""

    slug: str
    status: str
    detail: str = ""
    new_name: str | None = None


def build_headers(token: str | None) -> dict[str, str]:
    """Request headers; ``Authorization`` is present only when a token is given."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pipecat-context-hub-catalog-health",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def make_client(token: str | None, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An ``httpx`` client that never follows redirects, so a 301 is observable."""
    return httpx.Client(
        base_url=API_BASE,
        headers=build_headers(token),
        follow_redirects=False,
        timeout=TIMEOUT_SECONDS,
        transport=transport,
    )


def _name_from_api_url(url: str) -> str | None:
    """``owner/repo`` from ``.../repos/owner/repo`` (or ``.../repositories/{id}``)."""
    parts = [unquote(p) for p in urlparse(url).path.split("/") if p]
    if "repos" in parts:
        tail = parts[parts.index("repos") + 1 :]
        if len(tail) >= 2:
            return f"{tail[0]}/{tail[1]}"
    return None


def _json_object(response: httpx.Response) -> dict[str, object] | None:
    """The body as a JSON object, or ``None`` when it is not valid JSON or not an object."""
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _json_body(response: httpx.Response) -> dict[str, object]:
    return _json_object(response) or {}


def _is_repositories_target(url: str) -> bool:
    """True for ``https://api.github.com/repositories/{id}`` (GitHub's rename target)."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return (
        parsed.netloc == urlparse(API_BASE).netloc
        and len(parts) == 2
        and parts[0] == "repositories"
        and parts[1].isdigit()
    )


def resolve_repositories_redirect(client: httpx.Client, response: httpx.Response) -> str | None:
    """New ``full_name`` behind a 301/308 to ``/repositories/{id}``, else ``None``.

    One extra GET that does not follow redirects (the client never does). Any
    failure (non-200, non-JSON, no string ``full_name``, httpx error) is ``None``.
    """
    location = response.headers.get("location", "")
    if not _is_repositories_target(location):
        return None
    try:
        second = client.get(urlparse(location).path)
    except httpx.HTTPError:
        return None
    if second.status_code != 200:
        return None
    full_name = (_json_object(second) or {}).get("full_name")
    return full_name if isinstance(full_name, str) and full_name else None


def classify_response(
    slug: str, response: httpx.Response, resolved_name: str | None = None
) -> EntryResult:
    """Classify one ``/repos/{slug}`` response. Pure: no network, no clock.

    ``resolved_name`` is the new name ``check_slug`` found by following a
    ``/repositories/{id}`` redirect target one hop (``None`` when it did not or could not).
    """
    code = response.status_code
    if code in (301, 308):
        new_name = _name_from_api_url(response.headers.get("location", ""))
        if new_name is None:
            new_name = resolved_name
        if new_name is None:
            body_url = _json_body(response).get("url")
            if isinstance(body_url, str):
                new_name = _name_from_api_url(body_url)
        return EntryResult(slug, STATUS_RENAMED, f"HTTP {code} redirect", new_name)
    if code == 404:
        return EntryResult(slug, STATUS_INACCESSIBLE, "HTTP 404 (missing or private)")
    if code == 200:
        body = _json_object(response)
        if body is None:
            return EntryResult(slug, STATUS_INCONCLUSIVE, "200 body is not a JSON object")
        if body.get("private") is True:
            return EntryResult(slug, STATUS_INACCESSIBLE, "repository is private")
        if body.get("archived") is True:
            return EntryResult(slug, STATUS_ARCHIVED, "repository is archived")
        full_name = body.get("full_name")
        if not isinstance(full_name, str):
            return EntryResult(slug, STATUS_INCONCLUSIVE, "200 body has no string full_name")
        if full_name.casefold() != slug.casefold():
            return EntryResult(slug, STATUS_RENAMED, "full_name differs", full_name)
        return EntryResult(slug, STATUS_OK)
    if code in (403, 429) or code >= 500:
        return EntryResult(slug, STATUS_INCONCLUSIVE, f"HTTP {code}")
    return EntryResult(slug, STATUS_INCONCLUSIVE, f"unexpected HTTP {code}")


def classify_error(slug: str, exc: Exception) -> EntryResult:
    """A timeout or network error is inconclusive, never inaccessible."""
    return EntryResult(slug, STATUS_INCONCLUSIVE, f"{type(exc).__name__}: {exc}")


def check_slug(client: httpx.Client, slug: str) -> EntryResult:
    """Fetch and classify one slug; any ``httpx`` failure is inconclusive."""
    try:
        response = client.get(f"/repos/{slug}")
        resolved = (
            resolve_repositories_redirect(client, response)
            if response.status_code in (301, 308)
            else None
        )
    except httpx.HTTPError as exc:
        return classify_error(slug, exc)
    return classify_response(slug, response, resolved)


def check_slugs(client: httpx.Client, slugs: Sequence[str]) -> list[EntryResult]:
    return [check_slug(client, slug) for slug in slugs]


def catalog_slugs(catalog: Catalog) -> list[str]:
    return [repo.slug for group in catalog.groups for repo in group.repos]


def verified_age_days(verified: date, today: date) -> int:
    return (today - verified).days


def is_verified_stale(verified: date, today: date) -> bool:
    """Advisory only: True when ``verified`` is strictly more than 90 days old."""
    return verified_age_days(verified, today) > ADVISORY_DAYS


def aggregate_verdict(results: Sequence[EntryResult]) -> int:
    """Exit code over all results; the ``verified`` advisory never affects it."""
    if any(r.status in _STALE_STATUSES for r in results):
        return EXIT_STALE
    if any(r.status == STATUS_INCONCLUSIVE for r in results):
        return EXIT_INCONCLUSIVE
    return EXIT_OK


def render_report(results: Sequence[EntryResult], verified: date, today: date) -> str:
    """Markdown report: stale entries, inconclusive entries, then the advisory."""
    lines = ["# Repo catalog health", ""]
    stale = [r for r in results if r.status in _STALE_STATUSES]
    inconclusive = [r for r in results if r.status == STATUS_INCONCLUSIVE]
    ok_count = sum(1 for r in results if r.status == STATUS_OK)
    lines.append(f"Checked {len(results)} entries: {ok_count} ok.")
    lines.append("")
    if stale:
        lines += ["## Stale entries (fix the catalog)", ""]
        for r in stale:
            extra = f" -> {r.new_name}" if r.new_name else ""
            lines.append(f"- `{r.slug}`: {r.status}{extra} ({r.detail})")
        lines.append("")
    if inconclusive:
        lines += ["## Inconclusive (re-run the job; not a verdict on the repo)", ""]
        for r in inconclusive:
            lines.append(f"- `{r.slug}`: {r.detail}")
        lines.append("")
    if is_verified_stale(verified, today):
        age = verified_age_days(verified, today)
        lines += [
            "## Advisory",
            "",
            f"`verified = {verified}` is {age} days old (limit {ADVISORY_DAYS}). "
            "Audit the catalog, then run `just catalog-regen --bump-verified`.",
            "",
        ]
    return "\n".join(lines)


def _catalog_failure(message: str) -> None:
    """One line on stdout too, so the workflow's empty-report guard files an issue."""
    print(message)
    print(message, file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    today: date | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument("--catalog", type=Path, help="catalog TOML (default: the packaged one)")
    args = parser.parse_args(argv)

    env = os.environ if environ is None else environ
    try:
        if args.catalog is not None:
            catalog = parse_catalog(args.catalog.read_text(encoding="utf-8"))
        else:
            catalog = load_catalog()
    except (CatalogError, OSError) as exc:
        _catalog_failure(f"cannot load the catalog: {exc}")
        return EXIT_CATALOG
    if catalog is None:
        _catalog_failure("cannot load the catalog (see log)")
        return EXIT_CATALOG

    token = env.get("GITHUB_TOKEN") or None
    with make_client(token, transport) as client:
        results = check_slugs(client, catalog_slugs(catalog))
    print(render_report(results, catalog.verified, today or date.today()))
    return aggregate_verdict(results)


if __name__ == "__main__":
    sys.exit(main())
