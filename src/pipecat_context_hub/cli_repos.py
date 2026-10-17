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
applies checks 3 to 5 to the rest; with no member left, nothing can be written,
so it exits 0 (``Nothing to add`` / ``Nothing to remove``) without check 3.

``shared/config_writer`` (hence ``tomlkit``) is imported inside the add/remove
bodies only, so ``import pipecat_context_hub.cli`` never loads it.

Error boundary: every command exits ``EXIT_INVALID_INPUT`` with a
home-redacted message on a writer refusal, an untrusted config or an
``OSError``; none ends in a traceback.
"""

from __future__ import annotations

import functools
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import click

from pipecat_context_hub.shared.config import HubConfig, SourceConfig
from pipecat_context_hub.shared.env_keys import EXTRA_REPOS_ENV
from pipecat_context_hub.shared.env_loading import (
    LAYER_ENV,
    SHADOWING_LAYERS,
    ConfigNotTrusted,
    config_list_value,
    parse_global_config,
    resolve_global_config_path,
)
from pipecat_context_hub.shared.exit_codes import EXIT_INVALID_INPUT
from pipecat_context_hub.shared.paths import redact_home, redact_home_in_text
from pipecat_context_hub.shared.repo_catalog import Catalog, CatalogGroup, load_catalog
from pipecat_context_hub.shared.repo_slug import is_valid_repo_slug

_RUNNING_SERVE = "A running `serve` reopens the rebuilt index itself."
_REFRESH_REMINDER = f"Run `pipecat-context-hub refresh` to index it. {_RUNNING_SERVE}"
_REMOVE_REMINDER = (
    f"Run `pipecat-context-hub refresh --prune` to drop its indexed data. {_RUNNING_SERVE}"
)

_F = TypeVar("_F", bound=Callable[..., Any])


def _fail(message: str) -> NoReturn:
    click.echo(f"Error: {message}", err=True)
    raise SystemExit(EXIT_INVALID_INPUT)


def _os_error_boundary(func: _F) -> _F:
    """Turn an ``OSError`` escaping a command into a clean exit 1 (redacted)."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except OSError as exc:
            _fail(redact_home_in_text(str(exc)))

    return wrapper  # type: ignore[return-value]


def _layer_phrase(layer: str) -> str:
    where = "the real environment" if layer == LAYER_ENV else "the `.env` in the current directory"
    return f"layer `{layer}` ({where})"


def _shadowing_layer(ctx: click.Context) -> str | None:
    """The higher layer holding ``PIPECAT_HUB_EXTRA_REPOS``, from the provenance map only.

    ``main()`` always sets ``ctx.obj["env_provenance"]``; a missing map is a
    wiring bug and fails loudly rather than reading as "not shadowed".
    """
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    if "env_provenance" not in obj:
        raise RuntimeError(
            "repos: ctx.obj['env_provenance'] is missing; the command must run under main()"
        )
    layer = obj["env_provenance"].get(EXTRA_REPOS_ENV)
    return layer if layer in SHADOWING_LAYERS else None


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


def _resolve_positional(ctx: click.Context, slug: str, *, unlisted: bool) -> str:
    """Checks 1 and 2 for a positional slug; returns the slug to act on."""
    config = _config(ctx)
    # 1. default or tainted repo
    if config.sources.is_default(slug):
        _fail(f"{slug} is a default repo; it is always indexed and cannot be added or removed.")
    if config.sources.is_tainted(slug):
        _fail(f"{slug} is tainted (PIPECAT_HUB_TAINTED_REPOS) and is never indexed.")
    # 2. syntax and catalog membership
    if not is_valid_repo_slug(slug):
        _fail(f"{slug!r} is not a valid repo slug; expected owner/repo.")
    catalog = _catalog_or_fail()
    entry = catalog.find_repo(slug)
    if entry is None:
        if not unlisted:
            _fail(
                f"{slug} is not in the repo catalog. Pass --unlisted to add a repo "
                "that is not listed there."
            )
        return slug
    if unlisted:
        _fail(
            f"{entry.slug} is in the repo catalog; drop --unlisted (the catalog spelling is used)."
        )
    return entry.slug


def _resolve_group(catalog: Catalog, name: str) -> CatalogGroup:
    group = catalog.find_group(name)
    if group is None:
        valid = ", ".join(g.name for g in catalog.groups)
        _fail(f"unknown group {name!r}. Valid groups: {valid} (see `repos list --groups`).")
    return group


def _apply(*, add: Sequence[str] = (), remove: Sequence[str] = ()) -> tuple[list[str], bool]:
    """Run the writer; returns ``(before, changed)``. Maps every writer failure to exit 1.

    The writer wraps invalid TOML into ``ConfigWriteRefused``, so its raise set
    is ``ConfigWriteRefused``, ``ConfigNotTrusted`` and ``OSError``.
    """
    from pipecat_context_hub.shared import config_writer

    try:
        result = config_writer.update_extra_repos(add=add, remove=remove)
    except config_writer.ConfigWriteRefused as exc:
        _fail(f"config.toml was not changed: {exc.reason}")
    except ConfigNotTrusted as exc:
        _fail(f"config.toml was not changed: it is not trusted ({exc.reason}).")
    except OSError as exc:
        _fail(f"config.toml was not changed: {redact_home_in_text(str(exc))}")
    return result.before, result.changed


def _shadow_note(layer: str | None) -> str:
    if layer is None:
        return ""
    return f" (not in effect: {_layer_phrase(layer)} overrides {EXTRA_REPOS_ENV}, so this only edits config.toml)"


def _after_write(layer: str | None, *, adding: bool) -> str:
    """The reminder after a write, accurate when a higher layer shadows config.toml."""
    if layer is None:
        return _REFRESH_REMINDER if adding else _REMOVE_REMINDER
    phrase = _layer_phrase(layer)
    if adding:
        return (
            f"Not in effect while {phrase} sets {EXTRA_REPOS_ENV}: a `refresh` run with that layer in "
            "force will not index it. Run `pipecat-context-hub refresh` where that layer "
            f"does not set the key. {_RUNNING_SERVE}"
        )
    return (
        f"Not in effect while {phrase} sets {EXTRA_REPOS_ENV}: a `refresh` run with that layer in "
        "force still indexes that layer's list. Do not run `refresh --prune` with that layer "
        "in force: it deletes the indexed data of every repo config.toml lists that the "
        f"layer hides. {_RUNNING_SERVE}"
    )


def _refuse_if_shadowed(ctx: click.Context, config_only: bool) -> str | None:
    """Check 3. Returns the shadowing layer when ``--config-only`` lets it through."""
    layer = _shadowing_layer(ctx)
    if layer is not None and not config_only:
        _fail(
            f"{EXTRA_REPOS_ENV} is set by {_layer_phrase(layer)}, which outranks config.toml, so this "
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


def _skipped_lines(group: CatalogGroup, sources: SourceConfig) -> tuple[list[str], list[str]]:
    """``(actionable, skipped_lines)`` for a group's members."""
    actionable: list[str] = []
    skipped: list[str] = []
    for repo in group.repos:
        if sources.is_tainted(repo.slug):
            skipped.append(f"Skipped (tainted): {repo.slug}")
        elif not repo.parseable:
            skipped.append(f"Skipped (parseable = false): {repo.slug}")
        else:
            actionable.append(repo.slug)
    return actionable, skipped


def _run_group(ctx: click.Context, name: str, *, config_only: bool, adding: bool) -> None:
    catalog = _catalog_or_fail()
    group = _resolve_group(catalog, name)
    members, skipped = _skipped_lines(group, _config(ctx).sources)
    before: list[str] = []
    changed = False
    if members:
        layer = _refuse_if_shadowed(ctx, config_only)  # check 3: only when a write could happen
        before, changed = _apply(add=members) if adding else _apply(remove=members)
    else:
        # Nothing actionable (every member tainted or `parseable = false`): no
        # write can happen, so neither the shadow refusal nor config.toml's
        # validation applies; exit 0 with the skipped list (parent plan).
        layer = _shadowing_layer(ctx)
    note = _shadow_note(layer)
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
        click.echo(_after_write(layer, adding=adding))


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
    else:
        before, changed = _apply(remove=[target])
        if not changed:
            click.echo(f"Not in config.toml: {target}" + note)
            return
        stored = next((s for s in before if s.casefold() == target.casefold()), target)
        click.echo(f"Removed {stored}")
    click.echo(_after_write(layer, adding=adding))


_SLUG_ARG = click.argument("slug", required=False)
_GROUP_OPT = click.option(
    "--group",
    "group",
    metavar="NAME",
    help=(
        "Act on every parseable, non-tainted repo of this catalog group "
        "(`repos list --groups` names them)."
    ),
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
@_os_error_boundary
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
@_os_error_boundary
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
    if EXTRA_REPOS_ENV not in data:
        return [], None
    configured = config_list_value(EXTRA_REPOS_ENV, data[EXTRA_REPOS_ENV])  # the loader's own rule
    if configured is None:
        return [], f"ignoring {EXTRA_REPOS_ENV} in {redact_home(path)}: a value the loader ignores"
    return configured, None


def _cwd_line() -> str:
    try:
        return f"Consulted .env in cwd: {Path.cwd()}"
    except (OSError, RuntimeError):
        # Same guard as `load_cwd_dotenv`: no cwd means no cwd `.env` was read.
        return "Consulted .env in cwd: none (the working directory is unavailable)"


def _print_groups(catalog: Catalog | None) -> None:
    if catalog is None:
        click.echo("Catalog groups: unavailable (the repo catalog failed to load).")
        return
    click.echo("Catalog groups (for `repos add --group NAME`):")
    for group in catalog.groups:
        click.echo(f"  {group.name}  [{group.kind}]  {group.description}")


@repos_group.command("list")
@click.option(
    "--groups", "show_groups", is_flag=True, help="Also list the catalog groups for --group."
)
@click.pass_context
@_os_error_boundary
def list_command(ctx: click.Context, show_groups: bool) -> None:
    """Show the effective repos, tagged default, catalog or unlisted."""
    config = _config(ctx)
    effective = config.sources.effective_repos
    catalog = load_catalog()
    listed = catalog.slugs() if catalog is not None else frozenset()

    def tag(slug: str) -> str:
        if config.sources.is_default(slug):
            return "default"
        return "catalog" if slug.casefold() in listed else "unlisted"

    click.echo(_cwd_line())
    click.echo(f"Effective repos ({len(effective)}):")
    for slug in effective:
        click.echo(f"  {slug}  [{tag(slug)}]")

    configured, problem = _configured_in_toml()
    if problem is not None:
        click.echo(f"config.toml: {problem}")
    layer = _shadowing_layer(ctx)
    if layer is not None:
        in_effect = {s.casefold() for s in effective}
        hidden = [
            s
            for s in configured
            if s.casefold() not in in_effect and not config.sources.is_tainted(s)
        ]
        if hidden:
            click.echo(f"In config.toml but hidden by {_layer_phrase(layer)}:")
            for slug in hidden:
                click.echo(f"  {slug}  [{tag(slug)}]")
    if show_groups:
        _print_groups(catalog)


def register_repos_commands(group: click.Group) -> None:
    """Attach the ``repos`` command group to the main CLI group."""
    group.add_command(repos_group)
