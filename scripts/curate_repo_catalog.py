#!/usr/bin/env python3
"""Report-only curation helper for the repo catalog.

Reproduces the manual 2026-10-16 analysis for a human to review: per catalog
repo the source-file count (.py/.ts/.tsx), last push and archived flag, plus
vendor discovery by GitHub code search (repos importing ``pipecat.services``
that are not in the catalog). It NEVER edits the catalog: the catalog is only
read, and a test asserts the file is byte-identical after a run.

A GitHub token is required (code search rejects unauthenticated requests):
``GITHUB_TOKEN``, else ``gh auth token``; exits nonzero with a message when
neither exists. Exits 2 (report still printed) when code search failed,
answered malformed, flagged its results incomplete or had more results than
the page cap reads, or when a catalog repo's metadata or file tree could not
be read in full (a truncated tree included): the report is then missing data,
so "none found" or a count is not a clean result. Exits 3 when the catalog
cannot be loaded.

Usage::

    uv run python scripts/curate_repo_catalog.py
"""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import quote

import httpx

from pipecat_context_hub.shared.config import SourceConfig
from pipecat_context_hub.shared.repo_catalog import (
    CatalogError,
    load_catalog,
    parse_catalog,
)
from pipecat_context_hub.shared.repo_slug import slug_key

API_BASE = "https://api.github.com"
SOURCE_SUFFIXES = (".py", ".ts", ".tsx")
SEARCH_QUERY = "pipecat.services extension:py"
SEARCH_PAGES = 3
SEARCH_PER_PAGE = 100

EXIT_OK = 0
EXIT_NO_TOKEN = 1
# The report printed, but code search failed, answered malformed, flagged
# incomplete results or stopped at the page cap, or a catalog repo's stats
# could not be read in full: the report is missing data, so "none found" or a
# count is not a clean result.
EXIT_REPORT_INCOMPLETE = 2
EXIT_CATALOG = 3


def token_from_gh() -> str | None:
    """``gh auth token``, or ``None`` when gh is missing or not logged in."""
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed argv
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


def resolve_token(
    environ: dict[str, str] | None = None,
    gh_token: Callable[[], str | None] = token_from_gh,
) -> str | None:
    """``GITHUB_TOKEN`` first, else ``gh auth token``, else ``None``."""
    env = os.environ if environ is None else environ
    return env.get("GITHUB_TOKEN") or gh_token()


def make_client(token: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pipecat-context-hub-catalog-curate",
            "Authorization": f"Bearer {token}",
        },
        follow_redirects=True,
        timeout=20.0,
        transport=transport,
    )


def count_source_files(tree: object) -> int:
    """Count .py/.ts/.tsx blobs in a git-tree API payload."""
    if not isinstance(tree, dict):
        return 0
    entries = tree.get("tree")
    if not isinstance(entries, list):
        return 0
    return sum(
        1
        for e in entries
        if isinstance(e, dict)
        and e.get("type") == "blob"
        and str(e.get("path", "")).endswith(SOURCE_SUFFIXES)
    )


def _tree_stats(client: httpx.Client, slug: str, branch: object) -> dict[str, object]:
    """``source_files``/``truncated`` for a repo's tree, or a ``tree_error``."""
    if not isinstance(branch, str) or not branch:
        return {"tree_error": "no default branch"}
    try:
        # The branch is one path segment: quote "/", "#" and "?" so it cannot
        # reshape the URL.
        tree = client.get(
            f"/repos/{slug}/git/trees/{quote(branch, safe='')}", params={"recursive": "1"}
        )
        if tree.status_code != 200:
            return {"tree_error": f"HTTP {tree.status_code}"}
        payload = tree.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"tree_error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
        return {"tree_error": "unexpected response body (no tree list)"}
    return {
        "source_files": count_source_files(payload),
        "truncated": payload.get("truncated") is True,
    }


def repo_stats(client: httpx.Client, slug: str) -> dict[str, object]:
    """Metadata and source-file count for one repo; errors are recorded, not raised.

    A failed metadata lookup yields an ``error`` row; a failed tree lookup keeps
    the metadata and records ``tree_error`` in place of a file count.
    """
    try:
        meta = client.get(f"/repos/{slug}")
        if meta.status_code != 200:
            return {"slug": slug, "error": f"HTTP {meta.status_code}"}
        body = meta.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"slug": slug, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(body, dict):
        return {"slug": slug, "error": "unexpected response body (not a JSON object)"}
    return {
        "slug": slug,
        "archived": bool(body.get("archived")),
        "pushed_at": body.get("pushed_at"),
        "stars": body.get("stargazers_count"),
        **_tree_stats(client, slug, body.get("default_branch")),
    }


def stats_incomplete(stats: Sequence[dict[str, object]]) -> bool:
    """True when any repo's metadata or file tree could not be read in full.

    A truncated tree (GitHub's recursive-tree size limit) undercounts the
    source files, so it is incomplete just like a failed tree.
    """
    return any("error" in s or "tree_error" in s or s.get("truncated") is True for s in stats)


def discover_vendors(
    client: httpx.Client, known: set[str], problems: list[str] | None = None
) -> Counter[str]:
    """Repos whose code matches the search and are not in ``known`` (``slug_key`` form).

    A failed request (network error, timeout, any non-200 such as a 403 or a
    rate limit) or a malformed response is recorded in ``problems`` (when
    given), never raised: it ends discovery early, so it must not read as
    "no candidates". A page GitHub flags ``incomplete_results`` (its search
    timed out), an item without a string ``full_name``, and a search that
    still has unread results after the last of ``SEARCH_PAGES`` pages are
    recorded too, since the candidate set may then be partial.

    Candidates are counted by ``slug_key`` (the one slug identity), shown in
    the first spelling seen, so ``Owner/Repo`` and ``owner/repo`` are one row.
    """
    if problems is None:
        problems = []
    found: Counter[str] = Counter()
    spelling: dict[str, str] = {}
    read = 0
    for page in range(1, SEARCH_PAGES + 1):
        try:
            resp = client.get(
                "/search/code",
                params={"q": SEARCH_QUERY, "per_page": SEARCH_PER_PAGE, "page": page},
            )
        except httpx.HTTPError as exc:
            problems.append(f"page {page}: {type(exc).__name__}: {exc}")
            break
        if resp.status_code != 200:
            problems.append(f"page {page}: HTTP {resp.status_code}")
            break
        try:
            payload = resp.json()
        except ValueError as exc:
            problems.append(f"page {page}: {type(exc).__name__}: {exc}")
            break
        if not isinstance(payload, dict):
            problems.append(f"page {page}: unexpected response body (not a JSON object)")
            break
        if payload.get("incomplete_results") is True:
            # GitHub answers 200 with ``incomplete_results: true`` when the search
            # timed out, so this page may be missing matches.
            problems.append(f"page {page}: incomplete_results (search may have missed repos)")
        items = payload.get("items", [])
        if not isinstance(items, list):
            problems.append(f"page {page}: unexpected items (not a list)")
            break
        if not items:
            break
        read += len(items)
        malformed = 0
        for item in items:
            repo = item.get("repository") if isinstance(item, dict) else None
            name = repo.get("full_name") if isinstance(repo, dict) else None
            if not isinstance(name, str) or not name:
                malformed += 1
                continue
            key = slug_key(name)
            if key not in known:
                found[spelling.setdefault(key, name)] += 1
        if malformed:
            problems.append(f"page {page}: {malformed} item(s) without a repository full_name")
    else:
        # Every capped page came back non-empty: results may remain unread unless
        # GitHub's total_count says this was all of them.
        total = payload.get("total_count")
        if not (isinstance(total, int) and not isinstance(total, bool) and total <= read):
            shown_total = total if isinstance(total, int) and not isinstance(total, bool) else "?"
            problems.append(
                f"stopped at the {SEARCH_PAGES}-page cap: read {read} of {shown_total} "
                "results (search may have missed repos)"
            )
    return found


def render_report(
    stats: Sequence[dict[str, object]],
    vendors: Counter[str],
    problems: Sequence[str] = (),
) -> str:
    lines = ["# Repo catalog curation report (read-only)", "", "## Catalog repos", ""]
    for s in stats:
        if "error" in s:
            lines.append(f"- `{s['slug']}`: {s['error']}")
        else:
            if "tree_error" in s:
                files = f"source files unknown (tree: {s['tree_error']})"
            else:
                files = (
                    f"{s['source_files']}{' (truncated)' if s.get('truncated') else ''} "
                    "source files"
                )
            lines.append(
                f"- `{s['slug']}`: {files}, "
                f"pushed {s['pushed_at']}, stars {s['stars']}, archived {s['archived']}"
            )
    lines += ["", "## Candidate repos not in the catalog (code-search hits)", ""]
    if vendors:
        lines += [f"- `{name}`: {n} matching files" for name, n in vendors.most_common(30)]
    elif problems:
        lines.append("- none found (code search incomplete, see below)")
    else:
        lines.append("- none found")
    if problems:
        lines += ["", "## Code-search problems", ""]
        lines += [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"


def _catalog_failure(message: str) -> None:
    """One line on stdout too, so the workflow's empty-report guard files an issue."""
    print(message)
    print(message, file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    environ: dict[str, str] | None = None,
    gh_token: Callable[[], str | None] = token_from_gh,
) -> int:
    parser = argparse.ArgumentParser(description="Report-only repo catalog curation helper")
    parser.add_argument("--catalog", type=Path, help="catalog TOML (default: the packaged one)")
    args = parser.parse_args(argv)

    token = resolve_token(environ, gh_token)
    if not token:
        print(
            "curate needs a GitHub token (code search rejects unauthenticated requests): "
            "set GITHUB_TOKEN or run `gh auth login`.",
            file=sys.stderr,
        )
        return EXIT_NO_TOKEN
    try:
        if args.catalog is not None:
            catalog = parse_catalog(args.catalog.read_text(encoding="utf-8"))
        else:
            catalog = load_catalog()
    except (CatalogError, OSError, UnicodeDecodeError) as exc:
        _catalog_failure(f"cannot load the catalog: {exc}")
        return EXIT_CATALOG
    if catalog is None:
        _catalog_failure("cannot load the catalog (see log)")
        return EXIT_CATALOG

    slugs = [r.slug for g in catalog.groups for r in g.repos]
    # The shipped default repos are always indexed and never in the catalog (the
    # parser rejects them), yet they match the code search heavily.
    known = {slug_key(s) for s in [*slugs, *SourceConfig().repos]}
    problems: list[str] = []
    with make_client(token, transport) as client:
        stats = [repo_stats(client, slug) for slug in slugs]
        vendors = discover_vendors(client, known, problems)
    print(render_report(stats, vendors, problems))
    return EXIT_REPORT_INCOMPLETE if problems or stats_incomplete(stats) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
