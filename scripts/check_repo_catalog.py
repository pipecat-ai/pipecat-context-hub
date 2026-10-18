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
                    other unexpected answer, including a 200 that is not a fully
                    valid repository object. Reported in its own section, never
                    labelled inaccessible or private.

Contract: an entry is ``ok`` only when its 200 body was fully validated (a
JSON object with a non-empty string ``full_name`` and boolean ``private`` and
``archived``); any 200 the script cannot fully validate is ``inconclusive``.
Every body is read through ``catalog_script_common.validated_body``.

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
from catalog_script_common import (
    API_BASE,
    EXIT_CATALOG,
    github_headers,
    is_bool,
    load_catalog_arg,
    non_empty_str,
    validated_body,
)

from pipecat_context_hub.shared.repo_catalog import Catalog
from pipecat_context_hub.shared.repo_slug import slug_key

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
# EXIT_CATALOG (3, the catalog cannot be loaded) comes from catalog_script_common.

_STALE_STATUSES = frozenset({STATUS_INACCESSIBLE, STATUS_ARCHIVED, STATUS_RENAMED})

# Every field the classifier reads from a ``/repos/{slug}`` 200 body.
REPO_FIELDS = {"full_name": non_empty_str, "private": is_bool, "archived": is_bool}


@dataclass(frozen=True)
class EntryResult:
    """The classified outcome for one catalog slug."""

    slug: str
    status: str
    detail: str = ""
    new_name: str | None = None


def build_headers(token: str | None) -> dict[str, str]:
    """Request headers; ``Authorization`` is present only when a token is given."""
    return github_headers("pipecat-context-hub-catalog-health", token)


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
    body, _ = validated_body(second, {"full_name": non_empty_str})
    return str(body["full_name"]) if body is not None else None


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
            body, _ = validated_body(response, {"url": non_empty_str}, status=None)
            if body is not None:
                new_name = _name_from_api_url(str(body["url"]))
        return EntryResult(slug, STATUS_RENAMED, f"HTTP {code} redirect", new_name)
    if code == 404:
        return EntryResult(slug, STATUS_INACCESSIBLE, "HTTP 404 (missing or private)")
    if code == 200:
        body, problem = validated_body(response, REPO_FIELDS)
        if body is None:
            return EntryResult(slug, STATUS_INCONCLUSIVE, f"200 not a valid repository: {problem}")
        if body["private"] is True:
            return EntryResult(slug, STATUS_INACCESSIBLE, "repository is private")
        if body["archived"] is True:
            return EntryResult(slug, STATUS_ARCHIVED, "repository is archived")
        full_name = str(body["full_name"])
        if slug_key(full_name) != slug_key(slug):
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
    catalog = load_catalog_arg(args.catalog)
    if catalog is None:
        return EXIT_CATALOG

    token = env.get("GITHUB_TOKEN") or None
    with make_client(token, transport) as client:
        results = check_slugs(client, catalog_slugs(catalog))
    print(render_report(results, catalog.verified, today or date.today()))
    return aggregate_verdict(results)


if __name__ == "__main__":
    sys.exit(main())
