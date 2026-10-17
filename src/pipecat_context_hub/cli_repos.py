"""The ``repos`` command group: list, add and remove extra repos in ``config.toml``.

``repos add/remove`` edit only ``PIPECAT_HUB_EXTRA_REPOS`` in the machine-global
``config.toml`` (see ``shared/config_writer.py``). The layer that is actually in
force comes from the provenance map ``main()`` stores in
``ctx.obj["env_provenance"]``; nothing here re-reads a layer or consults
``os.environ`` to decide that.

Checks run in a fixed order for a positional slug (the first that applies
decides): (1) default or tainted repo, (2) slug syntax and catalog membership,
(3) a shadowing ``env`` / ``.env`` layer without ``--config-only``, (4) no-op,
(5) write. ``--group`` skips tainted and ``parseable = false`` members and
applies checks 3 to 5.

``shared/config_writer`` (hence ``tomlkit``) is imported inside the add/remove
bodies only, so ``import pipecat_context_hub.cli`` never loads it.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

import click

from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.env_loading import (
    ConfigNotTrusted,
    parse_global_config,
    resolve_global_config_path,
)
from pipecat_context_hub.shared.exit_codes import EXIT_INVALID_INPUT
from pipecat_context_hub.shared.paths import redact_home, redact_home_in_text
from pipecat_context_hub.shared.repo_catalog import Catalog, CatalogGroup, load_catalog

_KEY = "PIPECAT_HUB_EXTRA_REPOS"
_SHADOWING_LAYERS = frozenset({"env", ".env"})
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")

_REFRESH_REMINDER = "Run `pipecat-context-hub refresh` to index it. A running `serve` reopens the rebuilt index itself."
_REMOVE_REMINDER = (
    "Run `pipecat-context-hub refresh --prune` to drop its indexed data. "
    "A running `serve` reopens the rebuilt index itself."
)


def _fail(message: str) -> NoReturn:
    click.echo(f"Error: {message}", err=True)
    raise SystemExit(EXIT_INVALID_INPUT)


def _layer_phrase(layer: str) -> str:
    where = "the real environment" if layer == "env" else "the `.env` in the current directory"
    return f"layer `{layer}` ({where})"


def _shadowing_layer(ctx: click.Context) -> str | None:
    """The higher layer holding ``PIPECAT_HUB_EXTRA_REPOS``, from the provenance map only."""
    provenance = (ctx.obj or {}).get("env_provenance") or {}
    layer = provenance.get(_KEY)
    return layer if layer in _SHADOWING_LAYERS else None


def _config(ctx: click.Context) -> HubConfig:
    config: HubConfig = ctx.obj["config"]
    return config


def _catalog_or_fail() -> Catalog:
    catalog = load_catalog()
    if catalog is None:
        _fail(
            "the repo catalog failed to load, so slugs cannot be validated; "
            "nothing was changed (see the warning above)."
        )
    return catalog


def _catalog_slug(catalog: Catalog, slug: str) -> str | None:
    """The catalog's spelling of ``slug`` (casefolded match), or ``None``."""
    folded = slug.casefold()
    for group in catalog.groups:
        for repo in group.repos:
            if repo.slug.casefold() == folded:
                return repo.slug
    return None


def _resolve_positional(ctx: click.Context, slug: str, *, unlisted: bool) -> str:
    """Checks 1 and 2 for a positional slug; returns the slug to act on."""
    config = _config(ctx)
    folded = slug.casefold()
    # 1. default or tainted repo
    if folded in {s.casefold() for s in config.sources.repos}:
        _fail(f"{slug} is a default repo; it is always indexed and cannot be added or removed.")
    if folded in {s.casefold() for s in config.sources.tainted_repos}:
        _fail(f"{slug} is tainted (PIPECAT_HUB_TAINTED_REPOS) and is never indexed.")
    # 2. syntax and catalog membership
    parts = slug.split("/")
    if not _SLUG_RE.match(slug) or any(p in {".", ".."} for p in parts):
        _fail(f"{slug!r} is not a valid repo slug; expected owner/repo.")
    catalog = _catalog_or_fail()
    listed = _catalog_slug(catalog, slug)
    if listed is None:
        if not unlisted:
            _fail(
                f"{slug} is not in the repo catalog. Pass --unlisted to add a repo "
                "that is not listed there."
            )
        return slug
    if unlisted:
        _fail(f"{listed} is in the repo catalog; drop --unlisted (the catalog spelling is used).")
    return listed


def _resolve_group(catalog: Catalog, name: str) -> CatalogGroup:
    for group in catalog.groups:
        if group.name.casefold() == name.casefold():
            return group
    valid = ", ".join(g.name for g in catalog.groups)
    _fail(f"unknown group {name!r}. Valid groups: {valid}")


def _apply(*, add: Sequence[str] = (), remove: Sequence[str] = ()) -> tuple[list[str], bool]:
    """Run the writer; returns ``(before, changed)``. Maps writer refusals to exit 1."""
    from pipecat_context_hub.shared import config_writer

    try:
        result = config_writer.update_extra_repos(add=add, remove=remove)
    except config_writer.ConfigWriteRefused as exc:
        _fail(f"config.toml was not changed: {exc.reason}")
    except ConfigNotTrusted as exc:
        _fail(f"config.toml was not changed: it is not trusted ({exc.reason}).")
    except tomllib.TOMLDecodeError as exc:
        _fail(f"config.toml was not changed: not valid TOML ({redact_home_in_text(str(exc))}).")
    return result.before, result.changed


def _shadow_note(layer: str | None) -> str:
    if layer is None:
        return ""
    return (
        f" (not in effect: {_layer_phrase(layer)} overrides {_KEY}, so this only edits config.toml)"
    )


def _refuse_if_shadowed(ctx: click.Context, config_only: bool) -> str | None:
    """Check 3. Returns the shadowing layer when ``--config-only`` lets it through."""
    layer = _shadowing_layer(ctx)
    if layer is not None and not config_only:
        _fail(
            f"{_KEY} is set by {_layer_phrase(layer)}, which outranks config.toml, so this "
            "change would have no effect. Pass --config-only to edit config.toml anyway."
        )
    return layer


def _validate_selector(slug: str | None, group: str | None, unlisted: bool) -> None:
    if slug is not None and group is not None:
        _fail("give either a SLUG or --group, not both.")
    if group is not None and unlisted:
        _fail("--unlisted applies to a single SLUG, not --group.")
    if slug is None and group is None:
        _fail("give a SLUG (owner/repo) or --group NAME.")


def _skipped_lines(group: CatalogGroup, tainted: set[str]) -> tuple[list[str], list[str]]:
    """``(actionable, skipped_lines)`` for a group's members."""
    actionable: list[str] = []
    skipped: list[str] = []
    for repo in group.repos:
        if repo.slug.casefold() in tainted:
            skipped.append(f"Skipped (tainted): {repo.slug}")
        elif not repo.parseable:
            skipped.append(f"Skipped (parseable = false): {repo.slug}")
        else:
            actionable.append(repo.slug)
    return actionable, skipped


def _run_group(ctx: click.Context, name: str, *, config_only: bool, adding: bool) -> None:
    catalog = _catalog_or_fail()
    group = _resolve_group(catalog, name)
    tainted = {s.casefold() for s in _config(ctx).sources.tainted_repos}
    members, skipped = _skipped_lines(group, tainted)
    layer = _refuse_if_shadowed(ctx, config_only)
    note = _shadow_note(layer)

    if adding:
        before, changed = _apply(add=members)
    else:
        before, changed = _apply(remove=members)
    present = {s.casefold(): s for s in before}

    if adding:
        done = [s for s in members if s.casefold() not in present]
        already = [s for s in members if s.casefold() in present]
        for slug in done:
            click.echo(f"Added {slug}")
        for slug in already:
            click.echo(f"Already in config.toml: {slug}")
        if not done:
            click.echo("Nothing to add" + note)
    else:
        done = [present[s.casefold()] for s in members if s.casefold() in present]
        for slug in done:
            click.echo(f"Removed {slug}")
        if not done:
            click.echo("Nothing to remove" + note)
    for line in skipped:
        click.echo(line)
    if changed:
        click.echo(_REFRESH_REMINDER if adding else _REMOVE_REMINDER)
        if note:
            click.echo(f"Note{note}", err=True)


def _run_positional(
    ctx: click.Context, slug: str, *, unlisted: bool, config_only: bool, adding: bool
) -> None:
    target = _resolve_positional(ctx, slug, unlisted=unlisted)  # checks 1, 2
    layer = _refuse_if_shadowed(ctx, config_only)  # check 3
    note = _shadow_note(layer)
    if adding:  # checks 4, 5
        before, changed = _apply(add=[target])
        if not changed:
            click.echo(f"Already in config.toml: {target}" + note)
            return
        click.echo(f"Added {target}")
        click.echo(_REFRESH_REMINDER)
    else:
        before, changed = _apply(remove=[target])
        if not changed:
            click.echo(f"Not in config.toml: {target}" + note)
            return
        stored = next((s for s in before if s.casefold() == target.casefold()), target)
        click.echo(f"Removed {stored}")
        click.echo(_REMOVE_REMINDER)
    if note:
        click.echo(f"Note{note}", err=True)


_SLUG_ARG = click.argument("slug", required=False)
_GROUP_OPT = click.option(
    "--group",
    "group",
    metavar="NAME",
    help="Act on every parseable, non-tainted repo of this catalog group.",
)
_CONFIG_ONLY_OPT = click.option(
    "--config-only",
    is_flag=True,
    help="Edit config.toml even when an env var or cwd .env overrides PIPECAT_HUB_EXTRA_REPOS.",
)


@click.group("repos")
def repos_group() -> None:
    """List, add and remove extra repos (PIPECAT_HUB_EXTRA_REPOS in config.toml).

    \b
    `add` and `remove` edit ~/.config/pipecat-context-hub/config.toml only, offline;
    run `pipecat-context-hub refresh` afterwards to (re)index.
    """


@repos_group.command("add")
@_SLUG_ARG
@_GROUP_OPT
@click.option(
    "--unlisted",
    is_flag=True,
    help="Allow a well-formed owner/repo that is not in the repo catalog.",
)
@_CONFIG_ONLY_OPT
@click.pass_context
def add_command(
    ctx: click.Context, slug: str | None, group: str | None, unlisted: bool, config_only: bool
) -> None:
    """Add SLUG (a catalog repo, or any owner/repo with --unlisted) to config.toml."""
    _validate_selector(slug, group, unlisted)
    if group is not None:
        _run_group(ctx, group, config_only=config_only, adding=True)
    else:
        assert slug is not None  # nosec B101 - narrowed by _validate_selector
        _run_positional(ctx, slug, unlisted=unlisted, config_only=config_only, adding=True)


@repos_group.command("remove")
@_SLUG_ARG
@_GROUP_OPT
@click.option(
    "--unlisted",
    is_flag=True,
    help="Allow a well-formed owner/repo that is not in the repo catalog.",
)
@_CONFIG_ONLY_OPT
@click.pass_context
def remove_command(
    ctx: click.Context, slug: str | None, group: str | None, unlisted: bool, config_only: bool
) -> None:
    """Remove SLUG (or a whole --group) from config.toml."""
    _validate_selector(slug, group, unlisted)
    if group is not None:
        _run_group(ctx, group, config_only=config_only, adding=False)
    else:
        assert slug is not None  # nosec B101 - narrowed by _validate_selector
        _run_positional(ctx, slug, unlisted=unlisted, config_only=config_only, adding=False)


def _configured_in_toml() -> tuple[list[str], str | None]:
    """``(slugs in config.toml, problem)``; read-only, via ``parse_global_config`` only."""
    path = resolve_global_config_path()
    if path is None:
        return [], "no config.toml location could be resolved"
    try:
        data = parse_global_config(path)
    except ConfigNotTrusted as exc:
        return [], f"ignoring {redact_home(path)}: not trusted ({exc.reason})"
    except tomllib.TOMLDecodeError as exc:
        return [], f"ignoring {redact_home(path)}: not valid TOML ({redact_home_in_text(str(exc))})"
    value = data.get(_KEY)
    if value is None:
        return [], None
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()], None
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [v.strip() for v in value if v.strip()], None
    return [], f"ignoring {_KEY} in {redact_home(path)}: not an array of strings"


@repos_group.command("list")
@click.pass_context
def list_command(ctx: click.Context) -> None:
    """Show the effective repos, tagged default, catalog or unlisted."""
    config = _config(ctx)
    effective = config.sources.effective_repos
    defaults = {s.casefold() for s in config.sources.repos}
    catalog = load_catalog()
    listed = (
        {r.slug.casefold() for g in catalog.groups for r in g.repos}
        if catalog is not None
        else set()
    )

    def tag(slug: str) -> str:
        folded = slug.casefold()
        if folded in defaults:
            return "default"
        return "catalog" if folded in listed else "unlisted"

    click.echo(f"Consulted .env in cwd: {Path.cwd()}")
    click.echo(f"Effective repos ({len(effective)}):")
    for slug in effective:
        click.echo(f"  {slug}  [{tag(slug)}]")

    configured, problem = _configured_in_toml()
    if problem is not None:
        click.echo(f"config.toml: {problem}")
    layer = _shadowing_layer(ctx)
    if layer is not None:
        in_effect = {s.casefold() for s in effective}
        tainted = {s.casefold() for s in config.sources.tainted_repos}
        hidden = [s for s in configured if s.casefold() not in in_effect | tainted]
        if hidden:
            click.echo(f"In config.toml but hidden by {_layer_phrase(layer)}:")
            for slug in hidden:
                click.echo(f"  {slug}  [{tag(slug)}]")


def register_repos_commands(group: click.Group) -> None:
    """Attach the ``repos`` command group to the main CLI group."""
    group.add_command(repos_group)
