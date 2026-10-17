"""Checked-in catalog of known public repos, grouped by vendor or topic.

The catalog (``data/repo_catalog.toml``) is the single source for the generated
block in ``.env.example``, the generated section in ``docs/configuration.md`` and
(later) corpus hints. This module imports nothing from ``cli``, ``server`` or
``services``.

Test seams: :func:`parse_catalog` is pure (text in, :class:`Catalog` out, raises
:class:`CatalogError`); :func:`_read_catalog_text` is the only I/O.
:func:`load_catalog` is ``functools.cache`` around both and returns ``None`` on
any failure, logging once at the first load.
"""

from __future__ import annotations

import functools
import logging
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from importlib import resources

from pipecat_context_hub.shared.config import SourceConfig

logger = logging.getLogger(__name__)

CATALOG_RESOURCE = "data/repo_catalog.toml"
VALID_KINDS = frozenset({"vendor", "topic"})

# The one repo-slug grammar: `owner/repo`, each part starting with a letter or
# digit, then letters, digits, `.`, `_` or `-`. It is the grammar the ingester
# accepts (`github_ingest.clone_or_fetch`), so a slug the catalog, `repos` or
# the writer accepts can always be cloned; it also excludes `.`/`..` segments
# and a trailing newline. Self-anchoring (`\A`..`\Z`), so `.match()`, `.search()` and
# `.fullmatch()` agree: a consumer cannot weaken it by choosing the wrong method.
REPO_SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_GROUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TOP_KEYS = frozenset({"verified", "groups"})
_GROUP_KEYS = frozenset({"kind", "description", "aliases", "repos"})
_REPO_KEYS = frozenset({"slug", "note", "parseable", "maintainer"})
# Visibility is unknowable offline; the health check owns it. The schema simply
# has no such fields, and names them in the error so the reason is obvious.
_FORBIDDEN_KEYS = frozenset({"private", "archived", "status"})


class CatalogError(ValueError):
    """The catalog text violates the schema or a static invariant."""


@dataclass(frozen=True)
class CatalogRepo:
    slug: str
    note: str = ""
    parseable: bool = True
    maintainer: bool = False


@dataclass(frozen=True)
class CatalogGroup:
    name: str
    kind: str
    description: str
    aliases: tuple[str, ...]
    repos: tuple[CatalogRepo, ...]


@dataclass(frozen=True)
class Catalog:
    verified: date
    groups: tuple[CatalogGroup, ...]

    def find_repo(self, slug: str) -> CatalogRepo | None:
        """The catalog entry for ``slug`` (casefolded match), or ``None``."""
        folded = slug.casefold()
        for group in self.groups:
            for repo in group.repos:
                if repo.slug.casefold() == folded:
                    return repo
        return None

    def find_group(self, name: str) -> CatalogGroup | None:
        """The group called ``name`` (casefolded match), or ``None``."""
        folded = name.casefold()
        return next((g for g in self.groups if g.name.casefold() == folded), None)

    def slugs(self) -> frozenset[str]:
        """Every catalog slug, casefolded."""
        return frozenset(r.slug.casefold() for g in self.groups for r in g.repos)


def is_valid_repo_slug(slug: str) -> bool:
    """True when ``slug`` is a well-formed ``owner/repo`` (:data:`REPO_SLUG_RE`)."""
    return REPO_SLUG_RE.fullmatch(slug) is not None


def default_repos() -> list[str]:
    """The built-in default repo list (never the env-aware ``effective_repos``)."""
    default = SourceConfig.model_fields["repos"].default
    return list(default)


def _check_keys(where: str, table: dict[str, object], allowed: frozenset[str]) -> None:
    for key in table:
        if key in _FORBIDDEN_KEYS:
            raise CatalogError(
                f"{where}: key {key!r} is not allowed (the catalog is public-only; "
                "the health check, not the schema, detects private or archived repos)"
            )
        if key not in allowed:
            raise CatalogError(f"{where}: unknown key {key!r}")


def _expect_str(where: str, table: dict[str, object], key: str, *, required: bool) -> str:
    if key not in table:
        if required:
            raise CatalogError(f"{where}: missing {key!r}")
        return ""
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{where}: {key!r} must be a non-empty string")
    return value


def _expect_bool(where: str, table: dict[str, object], key: str, *, default: bool) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise CatalogError(f"{where}: {key!r} must be a bool")
    return value


def _parse_repo(where: str, raw: object) -> CatalogRepo:
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: each repo must be a table")
    _check_keys(where, raw, _REPO_KEYS)
    slug = _expect_str(where, raw, "slug", required=True)
    if not is_valid_repo_slug(slug):
        raise CatalogError(f"{where}: malformed slug {slug!r} (expected org/repo)")
    return CatalogRepo(
        slug=slug,
        note=_expect_str(where, raw, "note", required=False),
        parseable=_expect_bool(where, raw, "parseable", default=True),
        maintainer=_expect_bool(where, raw, "maintainer", default=False),
    )


def _parse_group(name: str, raw: object) -> CatalogGroup:
    where = f"groups.{name}"
    if not _GROUP_NAME_RE.match(name):
        raise CatalogError(f"{where}: group name must be lowercase letters, digits and '-'")
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: must be a table")
    _check_keys(where, raw, _GROUP_KEYS)
    kind = _expect_str(where, raw, "kind", required=True)
    if kind not in VALID_KINDS:
        raise CatalogError(f"{where}: kind {kind!r} must be one of {sorted(VALID_KINDS)}")
    description = _expect_str(where, raw, "description", required=True)
    aliases_raw = raw.get("aliases")
    if (
        not isinstance(aliases_raw, list)
        or not aliases_raw
        or not all(isinstance(a, str) and a.strip() for a in aliases_raw)
    ):
        raise CatalogError(f"{where}: 'aliases' must be a non-empty list of non-empty strings")
    repos_raw = raw.get("repos")
    if not isinstance(repos_raw, list) or not repos_raw:
        raise CatalogError(f"{where}: needs at least one [[repos]] entry")
    repos = tuple(_parse_repo(f"{where}.repos[{i}]", r) for i, r in enumerate(repos_raw))
    return CatalogGroup(
        name=name,
        kind=kind,
        description=description,
        aliases=tuple(str(a) for a in aliases_raw),
        repos=repos,
    )


def parse_catalog(text: str) -> Catalog:
    """Parse and validate catalog text. Raises :class:`CatalogError`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"invalid TOML: {exc}") from exc
    _check_keys("catalog", data, _TOP_KEYS)
    verified = data.get("verified")
    # tomllib yields datetime for a timestamp; only a plain date is valid.
    if type(verified) is not date:
        raise CatalogError("catalog: 'verified' must be an ISO date (YYYY-MM-DD)")
    groups_raw = data.get("groups")
    if not isinstance(groups_raw, dict) or not groups_raw:
        raise CatalogError("catalog: needs at least one [groups.<name>] table")
    groups = tuple(_parse_group(name, raw) for name, raw in groups_raw.items())

    defaults = {d.casefold() for d in default_repos()}
    seen_slugs: dict[str, str] = {}
    seen_aliases: dict[str, str] = {}
    for group in groups:
        for repo in group.repos:
            key = repo.slug.casefold()
            if key in defaults:
                raise CatalogError(f"groups.{group.name}: {repo.slug!r} is a default repo")
            if key in seen_slugs:
                raise CatalogError(
                    f"groups.{group.name}: slug {repo.slug!r} already in group {seen_slugs[key]!r}"
                )
            seen_slugs[key] = group.name
        for alias in group.aliases:
            akey = alias.casefold()
            if akey in seen_aliases:
                raise CatalogError(
                    f"groups.{group.name}: alias {alias!r} already used by "
                    f"group {seen_aliases[akey]!r}"
                )
            seen_aliases[akey] = group.name
    return Catalog(verified=verified, groups=groups)


def _read_catalog_text() -> str:
    resource = resources.files("pipecat_context_hub").joinpath(CATALOG_RESOURCE)
    return resource.read_text(encoding="utf-8")


@functools.cache
def load_catalog() -> Catalog | None:
    """Load the shipped catalog; ``None`` (cached, logged once) on any failure."""
    try:
        return parse_catalog(_read_catalog_text())
    except Exception as exc:  # never raise: a bad catalog must not fail a tool call
        logger.warning("repo catalog failed to load; catalog features disabled: %s", exc)
        return None
