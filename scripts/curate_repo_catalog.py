#!/usr/bin/env python3
"""Report-only curation helper for the repo catalog.

Reproduces the manual 2026-10-16 analysis for a human to review: per catalog
repo the source-file count (.py/.ts/.tsx), last push and archived flag, plus
vendor discovery by GitHub code search (repos importing ``pipecat.services``
that are not in the catalog). It NEVER edits the catalog: the catalog is only
read, and a test asserts the file is byte-identical after a run.

A GitHub token is required (code search rejects unauthenticated requests):
``GITHUB_TOKEN``, else ``gh auth token``; exits nonzero with a message when
neither exists. Exits 2 (report still printed) when code search failed or
answered malformed, since its candidate list may then be incomplete.

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

EXIT_OK = 0
EXIT_NO_TOKEN = 1
# The report printed, but code search failed or answered malformed: the
# candidate list may be missing repos, so "none found" is not a clean result.
EXIT_SEARCH_INCOMPLETE = 2
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


def repo_stats(client: httpx.Client, slug: str) -> dict[str, object]:
    """Metadata and source-file count for one repo; errors are recorded, not raised."""
    try:
        meta = client.get(f"/repos/{slug}")
        if meta.status_code != 200:
            return {"slug": slug, "error": f"HTTP {meta.status_code}"}
        body = meta.json()
        if not isinstance(body, dict):
            return {"slug": slug, "error": "unexpected response body (not a JSON object)"}
        branch = body.get("default_branch", "main")
        tree = client.get(f"/repos/{slug}/git/trees/{branch}", params={"recursive": "1"})
        files = None
        truncated = False
        if tree.status_code == 200:
            payload = tree.json()
            files = count_source_files(payload)
            truncated = isinstance(payload, dict) and payload.get("truncated") is True
        return {
            "slug": slug,
            "archived": bool(body.get("archived")),
            "pushed_at": body.get("pushed_at"),
            "stars": body.get("stargazers_count"),
            "source_files": files,
            "truncated": truncated,
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"slug": slug, "error": f"{type(exc).__name__}: {exc}"}


def discover_vendors(
    client: httpx.Client, known: set[str], problems: list[str] | None = None
) -> Counter[str]:
    """Repos whose code matches the search and are not in ``known`` (``slug_key`` form).

    A failed request (network error, timeout, any non-200 such as a 403 or a
    rate limit) or a malformed response is recorded in ``problems`` (when
    given), never raised: it ends discovery early, so it must not read as
    "no candidates".
    """
    if problems is None:
        problems = []
    found: Counter[str] = Counter()
    for page in range(1, SEARCH_PAGES + 1):
        try:
            resp = client.get(
                "/search/code", params={"q": SEARCH_QUERY, "per_page": 100, "page": page}
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
        items = payload.get("items", [])
        if not isinstance(items, list):
            problems.append(f"page {page}: unexpected items (not a list)")
            break
        if not items:
            break
        malformed = 0
        for item in items:
            repo = item.get("repository") if isinstance(item, dict) else None
            if not isinstance(repo, dict):
                malformed += 1
                continue
            name = repo.get("full_name")
            if isinstance(name, str) and slug_key(name) not in known:
                found[name] += 1
        if malformed:
            problems.append(f"page {page}: {malformed} item(s) without a repository object")
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
            lines.append(
                f"- `{s['slug']}`: {s['source_files']}{' (truncated)' if s.get('truncated') else ''} "
                f"source files, "
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
    except (CatalogError, OSError) as exc:
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
    return EXIT_SEARCH_INCOMPLETE if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
