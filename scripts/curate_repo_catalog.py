#!/usr/bin/env python3
"""Report-only curation helper for the repo catalog.

Reproduces the manual 2026-10-16 analysis for a human to review: per catalog
repo the source-file count (.py/.ts/.tsx), last push and archived flag, plus
vendor discovery by GitHub code search (repos importing ``pipecat.services``
that are not in the catalog). It NEVER edits the catalog: the catalog is only
read, and a test asserts the file is byte-identical after a run.

A GitHub token is required (code search rejects unauthenticated requests):
``GITHUB_TOKEN``, else ``gh auth token``; exits nonzero with a message when
neither exists. Exits 3 when the catalog cannot be loaded.

Contract: the report is complete (exit 0) only when every GitHub response it
used was fully validated and nothing was left unread. Every response goes
through ``_get`` (``catalog_script_common.validated_body``), which hands back
data only when the status is 200, the body is a JSON object and every field
the caller reads is present with the right type. Anything else (a failed
request, a non-200, a wrong shape, a missing or mistyped field, a truncated
tree, ``incomplete_results``, a search item without a repository name, or
fewer results read than ``total_count`` reports, whether the search stopped
at a short page, an empty page or the page cap) is recorded and exits 2 with
the report still printed: "none found" or a count is then not a clean result.

Usage::

    uv run python scripts/curate_repo_catalog.py
"""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import quote

import httpx
from catalog_script_common import (
    API_BASE,
    EXIT_CATALOG,
    FieldCheck,
    github_headers,
    is_bool,
    is_list,
    load_catalog_arg,
    non_empty_str,
    plain_int,
    validated_body,
)

from pipecat_context_hub.shared.config import SourceConfig
from pipecat_context_hub.shared.repo_slug import slug_key

SOURCE_SUFFIXES = (".py", ".ts", ".tsx")
SEARCH_QUERY = "pipecat.services extension:py"
SEARCH_PAGES = 3
SEARCH_PER_PAGE = 100

EXIT_OK = 0
EXIT_NO_TOKEN = 1
# The report printed, but a response was not fully valid or results were left
# unread (see the module docstring): "none found" or a count is not clean.
EXIT_REPORT_INCOMPLETE = 2
# EXIT_CATALOG (3, the catalog cannot be loaded) comes from catalog_script_common.


def _tree_entries(value: object) -> bool:
    """A git-tree list whose every entry has a string ``type`` and ``path``."""
    return isinstance(value, list) and all(
        isinstance(e, dict) and isinstance(e.get("type"), str) and isinstance(e.get("path"), str)
        for e in value
    )


# Every field each caller reads, per endpoint. ``default_branch`` is checked in
# ``_tree_stats`` (with ``non_empty_str``) so the metadata survives a repo
# without one.
_REPO_FIELDS: dict[str, FieldCheck] = {
    "archived": is_bool,
    "pushed_at": non_empty_str,
    "stargazers_count": plain_int,
}
_TREE_FIELDS: dict[str, FieldCheck] = {"tree": _tree_entries, "truncated": is_bool}
_SEARCH_FIELDS: dict[str, FieldCheck] = {
    "total_count": plain_int,
    "incomplete_results": is_bool,
    "items": is_list,
}


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
        headers=github_headers("pipecat-context-hub-catalog-curate", token),
        follow_redirects=True,
        timeout=20.0,
        transport=transport,
    )


def _get(
    client: httpx.Client,
    path: str,
    fields: Mapping[str, FieldCheck],
    params: Mapping[str, str | int] | None = None,
) -> tuple[dict[str, object] | None, str]:
    """The one GitHub read: ``(body, "")`` when fully valid, else ``(None, reason)``."""
    try:
        response = client.get(path, params=params)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return validated_body(response, fields)


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
    # The branch is one path segment: quote "/", "#" and "?" so it cannot
    # reshape the URL.
    payload, problem = _get(
        client,
        f"/repos/{slug}/git/trees/{quote(branch, safe='')}",
        _TREE_FIELDS,
        params={"recursive": "1"},
    )
    if payload is None:
        return {"tree_error": problem}
    return {
        "source_files": count_source_files(payload),
        "truncated": payload["truncated"] is True,
    }


def repo_stats(client: httpx.Client, slug: str) -> dict[str, object]:
    """Metadata and source-file count for one repo; errors are recorded, not raised.

    A failed metadata lookup yields an ``error`` row; a failed tree lookup keeps
    the metadata and records ``tree_error`` in place of a file count.
    """
    body, problem = _get(client, f"/repos/{slug}", _REPO_FIELDS)
    if body is None:
        return {"slug": slug, "error": problem}
    return {
        "slug": slug,
        "archived": body["archived"],
        "pushed_at": body["pushed_at"],
        "stars": body["stargazers_count"],
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

    Problems are recorded in ``problems`` (when given), never raised, since the
    candidate set may then be partial: a page that is not fully valid (it ends
    discovery), a page GitHub flags ``incomplete_results`` (its search timed
    out), an item without a string repository ``full_name``, and fewer results
    read than the last page's ``total_count``, however the search stopped (a
    short page, an empty page or the ``SEARCH_PAGES`` cap).

    Candidates are counted by ``slug_key`` (the one slug identity), shown in
    the first spelling seen, so ``Owner/Repo`` and ``owner/repo`` are one row.
    """
    if problems is None:
        problems = []
    found: Counter[str] = Counter()
    spelling: dict[str, str] = {}
    read = 0
    total = 0
    for page in range(1, SEARCH_PAGES + 1):
        payload, problem = _get(
            client,
            "/search/code",
            _SEARCH_FIELDS,
            params={"q": SEARCH_QUERY, "per_page": SEARCH_PER_PAGE, "page": page},
        )
        if payload is None:
            problems.append(f"page {page}: {problem}")
            return found
        if payload["incomplete_results"] is True:
            # GitHub answers 200 with ``incomplete_results: true`` when the search
            # timed out, so this page may be missing matches.
            problems.append(f"page {page}: incomplete_results (search may have missed repos)")
        # _SEARCH_FIELDS validated both: a list and a non-negative int.
        items = cast("list[object]", payload["items"])
        total = cast(int, payload["total_count"])
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
        if read >= total or len(items) < SEARCH_PER_PAGE:
            break  # all read, or a short/empty page: GitHub has no further page
    if read < total:
        where = (
            f"stopped at the {SEARCH_PAGES}-page cap"
            if page == SEARCH_PAGES and len(items) == SEARCH_PER_PAGE
            else f"page {page} came back short"
        )
        problems.append(f"{where}: read {read} of {total} results (search may have missed repos)")
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
    catalog = load_catalog_arg(args.catalog)
    if catalog is None:
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
