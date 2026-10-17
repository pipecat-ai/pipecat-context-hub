"""Render the repo catalog into ``.env.example`` and ``docs/configuration.md``.

Pure functions (catalog to block text, text to text splice) plus a ``python -m``
entry that resolves paths. Defaults come from ``SourceConfig.model_fields``,
never the env-aware ``effective_repos``, and no env layer is loaded, so output is
identical on every machine.

    python -m pipecat_context_hub.shared.repo_catalog_render [--bump-verified] [--check]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

from pipecat_context_hub.shared.config import EXTRA_REPOS_ENV
from pipecat_context_hub.shared.repo_catalog import (
    Catalog,
    CatalogGroup,
    CatalogRepo,
    default_repos,
    parse_catalog,
)

ENV_BEGIN = "# >>> BEGIN GENERATED: repo catalog (edit data/repo_catalog.toml, run `just catalog-regen`) >>>"
ENV_END = "# <<< END GENERATED: repo catalog <<<"
DOCS_BEGIN = (
    "<!-- BEGIN GENERATED: repo catalog (edit data/repo_catalog.toml, run `just catalog-regen`) -->"
)
DOCS_END = "<!-- END GENERATED: repo catalog -->"

_ENV_KEY = EXTRA_REPOS_ENV
_RULE = "# " + "-" * 74
_VERIFIED_LINE_RE = re.compile(r"^(verified[ \t]*=[ \t]*)\d{4}-\d{2}-\d{2}([ \t]*(?:#.*)?)$", re.M)


class SpliceError(ValueError):
    """The generated-block markers are missing, duplicated or out of order."""


def _extras(group: CatalogGroup) -> list[CatalogRepo]:
    defaults = {d.casefold() for d in default_repos()}
    return [r for r in group.repos if r.slug.casefold() not in defaults]


def _kind_label(group: CatalogGroup) -> str:
    return "Vendor" if group.kind == "vendor" else "Topic"


def render_env_block(catalog: Catalog) -> str:
    """The text between (and excluding) the ``.env.example`` markers."""
    lines: list[str] = [
        _RULE,
        f"# Recommended extras from the repo catalog (last audited {catalog.verified.isoformat()})",
        "# Copy/uncomment what you need. Only public, non-archived repos are listed.",
        _RULE,
    ]
    everything: list[str] = []
    for group in catalog.groups:
        repos = _extras(group)
        if not repos:
            continue
        lines.append("")
        lines.append(f"# {_kind_label(group)}: {group.description}")
        for repo in repos:
            if repo.note:
                lines.append(f"#   {repo.slug}: {repo.note}")
        unparsed = [r.slug for r in repos if not r.parseable]
        if unparsed:
            lines.append(f"#   (clone but yield no source chunks: {', '.join(unparsed)})")
        slugs = [r.slug for r in repos]
        everything.extend(slugs)
        lines.append(f'# {_ENV_KEY}="{",".join(slugs)}"')
    lines.append("")
    lines.append(_RULE)
    lines.append("# All catalog repos in one line (copy and uncomment):")
    lines.append(f'# {_ENV_KEY}="{",".join(everything)}"')
    lines.append(_RULE)
    return "\n".join(lines)


def render_docs_block(catalog: Catalog) -> str:
    """The text between (and excluding) the ``docs/configuration.md`` markers."""
    lines: list[str] = [
        f"Last audited {catalog.verified.isoformat()}. Public repos only. "
        "A repo marked *no source chunks* clones but yields nothing to search "
        "(Swift/Kotlin/C++ are not parsed).",
        "",
        "| Group | Kind | Description | Repos |",
        "|-------|------|-------------|-------|",
    ]
    for group in catalog.groups:
        repos = _extras(group)
        if not repos:
            continue
        cells: list[str] = []
        for repo in repos:
            cell = f"`{repo.slug}`"
            if not repo.parseable:
                cell += " *(no source chunks)*"
            cells.append(cell)
        lines.append(
            f"| `{group.name}` | {group.kind} | {group.description} | {'<br>'.join(cells)} |"
        )
    return "\n".join(lines)


def splice(text: str, begin: str, end: str, body: str) -> str:
    """Replace the text between the single ``begin``/``end`` marker lines.

    Everything outside the markers is returned byte-identical. Missing,
    duplicated or reversed markers raise :class:`SpliceError`.
    """
    if text.count(begin) != 1 or text.count(end) != 1:
        raise SpliceError(
            f"expected exactly one begin and one end marker, found "
            f"{text.count(begin)} begin and {text.count(end)} end"
        )
    start = text.index(begin)
    stop = text.index(end)
    if stop < start:
        raise SpliceError("end marker precedes begin marker")
    return text[: start + len(begin)] + "\n" + body + "\n" + text[stop:]


def render_env_example(text: str, catalog: Catalog) -> str:
    return splice(text, ENV_BEGIN, ENV_END, render_env_block(catalog))


def render_docs(text: str, catalog: Catalog) -> str:
    return splice(text, DOCS_BEGIN, DOCS_END, render_docs_block(catalog))


def bump_verified(text: str, today: date) -> str:
    """Rewrite the single top-level ``verified = <date>`` line to ``today``.

    Line-anchored, so an indented ``verified`` key inside a group is untouched.
    The result is re-validated with :func:`parse_catalog`.
    """
    matches = _VERIFIED_LINE_RE.findall(text)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one top-level verified line, found {len(matches)}")
    bumped = _VERIFIED_LINE_RE.sub(
        lambda m: f"{m.group(1)}{today.isoformat()}{m.group(2)}", text, count=1
    )
    parse_catalog(bumped)
    return bumped


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repo_catalog_render", description=__doc__)
    parser.add_argument(
        "--bump-verified",
        action="store_true",
        help="set the catalog's verified date to today (after a manual audit)",
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if a generated file is out of date"
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    catalog_path = root / "src" / "pipecat_context_hub" / "data" / "repo_catalog.toml"
    env_path = root / ".env.example"
    docs_path = root / "docs" / "configuration.md"

    original_catalog = catalog_path.read_text(encoding="utf-8")
    catalog_text = original_catalog
    if args.bump_verified:
        catalog_text = bump_verified(catalog_text, date.today())
    catalog = parse_catalog(catalog_text)

    # Render everything before writing anything, so a failing render (say, a
    # missing marker) leaves every file, the catalog included, as it was.
    outputs: list[tuple[Path, str]] = []
    if catalog_text != original_catalog:
        outputs.append((catalog_path, catalog_text))
    stale = False
    for path, render in ((env_path, render_env_example), (docs_path, render_docs)):
        current = path.read_text(encoding="utf-8")
        updated = render(current, catalog)
        if updated != current:
            stale = True
            outputs.append((path, updated))
    if not args.check:
        for path, text in outputs:
            path.write_text(text, encoding="utf-8")
            print(f"updated {path.relative_to(root)}")
    if args.check and stale:
        print("generated catalog sections are out of date; run `just catalog-regen`")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
