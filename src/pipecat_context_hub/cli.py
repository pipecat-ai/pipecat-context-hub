"""CLI entry point for the Pipecat Context Hub.

Provides ``serve`` (default) and ``refresh`` commands, plus the one-shot query
subcommands registered from :mod:`pipecat_context_hub.cli_query`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click

from pipecat_context_hub.cli_install import register_install_command
from pipecat_context_hub.cli_query import register_query_commands
from pipecat_context_hub.shared import env_loading
from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.paths import (
    is_reparse_link,
    redact_home,
    redact_home_in_text,
    same_dir,
)
from pipecat_context_hub.shared.support_links import bug_report_hint
from pipecat_context_hub.shared.versioning import (
    canonicalize_framework_pin,
    is_latest_sentinel,
    strip_v_prefix,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pipecat_context_hub.services.index.store import IndexStore

# Back-compat alias: tests/unit/test_cli.py imports the underscored name and
# the banner call sites below reference it. The redaction helper itself now
# lives in shared/paths.py (shared with cli_query, avoiding a cli<->cli_query
# import cycle); this re-export keeps that suite green and the call sites stable.
_redact_home = redact_home

# Shared sentinel used by refresh bookkeeping for missing/unknown cells
# (SHA, existing count, updated count). Centralised so the summary
# renderer and the producers cannot drift.
_MISSING_SENTINEL = "\u2014"

# Exit code for `serve` when the index cannot be used (unopenable or empty).
# Documented in CHANGELOG 0.0.17 "Changed" section.
_EXIT_INDEX_UNREADY = 2

_module_logger = logging.getLogger(__name__)


# Values of PIPECAT_HUB_WARMUP that disable pre-warm. Anything else
# (including unset, "1", "true", or garbage like "yes") enables it — the
# default is "warm unless explicitly told not to".
_WARMUP_DISABLED_VALUES = frozenset({"0", "false", "False", "FALSE", "no", "No", "NO"})


def _warmup_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True when model pre-warm should run.

    Reads ``PIPECAT_HUB_WARMUP`` from ``env`` (defaults to ``os.environ``).
    Disabled on ``0`` / ``false`` / ``no`` (any common casing). Any other
    value — including unset — enables pre-warm.
    """
    source = env if env is not None else os.environ
    return source.get("PIPECAT_HUB_WARMUP", "1").strip() not in _WARMUP_DISABLED_VALUES


# Env-var values that turn on a *default-off* opt-in — shared by
# PIPECAT_HUB_PRUNE (refresh's repo-deletion cleanup) and
# PIPECAT_HUB_DEBUG_PROBE (serve's probe file). Named for the shape rather
# than for the first consumer, since it now has two.
#
# Unlike _WARMUP_DISABLED_VALUES above, membership *enables*: pruning deletes
# previously-indexed data, so an unrecognized value (typo, unexpected casing)
# must resolve to False, not True.
_OPT_IN_ENABLED_VALUES = frozenset({"1", "true", "True", "TRUE", "yes", "Yes", "YES"})


def _prune_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True when refresh should delete unconfigured-this-run repo data.

    Reads ``env_loading.PRUNE_ENV_VAR`` (``PIPECAT_HUB_PRUNE``) from ``env``
    (defaults to ``os.environ``). Deliberately not wired through Click's
    ``envvar=`` for the ``--prune`` option: Click's own boolean parser would
    reject a garbage value with a UsageError before this function's
    safe-default handling could apply. Enabled only on an exact match
    (case-exact, after stripping whitespace) against
    ``_OPT_IN_ENABLED_VALUES``; any other value — including unset, "0", or a
    typo like "tRuE" — resolves to False (no deletion).
    """
    source = env if env is not None else os.environ
    return source.get(env_loading.PRUNE_ENV_VAR, "").strip() in _OPT_IN_ENABLED_VALUES


def _debug_probe_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True when `serve` should write the opt-in debug probe file.

    Shares ``_OPT_IN_ENABLED_VALUES`` with :func:`_prune_enabled` rather than
    testing ``== "1"`` inline: both are default-off opt-ins, and there is no
    reason for ``PIPECAT_HUB_DEBUG_PROBE=true`` to silently do nothing while
    ``PIPECAT_HUB_PRUNE=true`` works. Same safe default — an unrecognized
    value resolves to False.
    """
    source = env if env is not None else os.environ
    return source.get(env_loading.DEBUG_PROBE_ENV_VAR, "").strip() in _OPT_IN_ENABLED_VALUES


def _prewarm_models(embedding_svc: object, cross_encoder: object | None) -> None:
    """Eagerly load retrieval models so the first MCP query is warm.

    Far cheaper than it once was: loading through ``sentence_transformers``
    dragged in ``torch`` and could take 30-130s on Windows CPU, whereas the
    ONNX backend imports and loads in well under a second. Pre-warming is kept
    because it still moves that cost off the first query, and because
    ``PIPECAT_HUB_WARMUP=0`` is a documented escape hatch. Failures are logged
    and swallowed — the lazy-load paths still handle first-query loading.
    """
    if not _warmup_enabled():
        _module_logger.info("Model pre-warm skipped: PIPECAT_HUB_WARMUP=0")
        return
    warmup_start = time.monotonic()
    try:
        embedding_svc.embed_query("warmup")  # type: ignore[attr-defined]
        _module_logger.info("Embedding model pre-warmed in %.1fs", time.monotonic() - warmup_start)
    except Exception:
        _module_logger.exception("Embedding model pre-warm failed; falling back to lazy load")
    if cross_encoder is not None:
        ce_start = time.monotonic()
        try:
            cross_encoder.ensure_model()  # type: ignore[attr-defined]
            _module_logger.info("Cross-encoder pre-warmed in %.1fs", time.monotonic() - ce_start)
        except Exception:
            _module_logger.exception("Cross-encoder pre-warm failed; falling back to lazy load")


def _configure_logging(level: str) -> None:
    """Set up basic logging to stderr (stdout is used by MCP stdio transport)."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _refuse_unsafe_data_dir(data_dir: Path, resolved_data_dir: Path) -> None:
    """Raise unless ``resolved_data_dir`` is a plausible index directory.

    Deliberately a *floor*, not a whitelist: it refuses the three targets
    whose deletion is never a legitimate ``--reset-index`` — a filesystem
    root, the home directory itself, and any ancestor of home (``/Users``,
    ``/home``). A depth threshold (``len(parts) < 3``) was considered and
    rejected: it would refuse a perfectly reasonable containerised
    ``PIPECAT_HUB_DATA_DIR=/data``.

    Comparison is by *filesystem identity* (``shared.paths.same_dir``, i.e.
    ``(st_dev, st_ino)``), not ``==``, for the same reason
    ``config_collides_with_dir()`` uses it: ``Path.resolve()`` preserves the
    caller's casing on case-insensitive volumes, so
    ``PIPECAT_HUB_DATA_DIR=/Users/Varun`` would pass a string comparison
    against a real home of ``/Users/varun`` and reach ``shutil.rmtree``. The
    string comparison is kept as well, since it still catches a
    not-yet-existing path that cannot be stat'd.
    """
    unsafe: list[Path] = [Path(resolved_data_dir.anchor or resolved_data_dir.root)]
    try:
        home = Path.home().resolve(strict=False)
    except (RuntimeError, OSError):
        home = None
    if home is not None:
        unsafe.append(home)
        unsafe.extend(home.parents)

    try:
        target_stat: os.stat_result | None = resolved_data_dir.stat()
    except (OSError, ValueError):
        target_stat = None

    def _is_unsafe(candidate: Path) -> bool:
        if resolved_data_dir == candidate:
            return True
        if target_stat is None:
            return False
        return same_dir(candidate, target_stat)

    if any(_is_unsafe(candidate) for candidate in unsafe):
        raise click.ClickException(
            f"Refusing to delete {redact_home(data_dir)}: it is a filesystem root or "
            "home directory, not an index directory. Point PIPECAT_HUB_DATA_DIR at a "
            "dedicated directory before retrying --reset-index."
        )


def _delete_local_index_storage(data_dir: Path) -> None:
    """Delete the persisted local index directory for a clean rebuild.

    Defense in depth against a colliding ``PIPECAT_HUB_DATA_DIR`` supplied by
    a real environment variable or cwd ``.env`` (the narrower case of a
    colliding value originating in ``config.toml`` itself is handled by
    ``load_global_config()``'s own skip-and-warn above): if the active
    config file (per ``resolve_global_config_path()``) lives or resolves
    inside ``data_dir`` and actually exists, abort before ``shutil.rmtree``
    rather than delete the operator's machine-global config out from under
    them. A stale/deleted or never-created config path never blocks a real
    ``--reset-index`` — only an existing file in effect is worth protecting.

    The same protection covers the *project-local* config source, the cwd
    ``.env`` (``env_loading.dotenv_collides_with_dir``, sharing one predicate
    with the ``config.toml`` check above). Protecting only ``config.toml`` left
    the more natural spelling of the same hazard wide open: a project ``.env``
    saying ``PIPECAT_HUB_DATA_DIR=.`` makes the data dir the working tree that
    holds that very ``.env``, and the ``config.toml`` check cannot fire because
    the machine-global file lives elsewhere.

    A second, unconditional floor guards the case the config-collision check
    structurally cannot: an operator who has never created a ``config.toml``
    (i.e. every user before this branch) gets no collision hit at all, so a
    ``PIPECAT_HUB_DATA_DIR`` of ``/`` or ``$HOME`` would otherwise reach
    ``shutil.rmtree`` unguarded. Refuse a filesystem root, the home directory
    itself, and any ancestor of home — see ``_refuse_unsafe_data_dir()`` for
    the exact contract (notably: no depth threshold, which was considered and
    rejected).

    Both guards, and the deletion itself, operate on the *normalized* path, so
    what was validated is exactly what is removed.

    One post-deletion repair: when ``data_dir`` is itself a symlink to a
    directory — or, on Windows, a directory junction, which ``resolve()``
    follows identically but ``is_symlink()`` does not report (see
    :func:`~pipecat_context_hub.shared.paths.is_reparse_link`) —
    ``rmtree(resolved_data_dir)`` deletes the link's *target* through the link,
    leaving the link dangling. ``IndexStore``'s subsequent
    ``data_dir.mkdir(parents=True, exist_ok=True)`` then raises
    ``FileExistsError`` — ``exist_ok`` only suppresses when the existing path
    ``is_dir()``, which a dangling symlink is not — so the reset would abort
    the very rebuild it exists to enable. Recreating the resolved directory
    restores the link. Deliberately scoped to the symlink case: for an
    ordinary directory the contract is "gone after reset", which
    ``TestDataDirSafetyFloor`` pins.
    """
    try:
        expanded_data_dir = data_dir.expanduser()
    except (ValueError, OSError, RuntimeError):
        expanded_data_dir = data_dir
    try:
        resolved_data_dir = expanded_data_dir.resolve(strict=False)
    except (ValueError, OSError, RuntimeError):
        # Same crash-safety contract as env_loading.config_collides_with_dir:
        # `resolve()` raises RuntimeError for a symlink loop on Python <=3.12.
        resolved_data_dir = Path(os.path.abspath(data_dir))
    data_dir_was_symlink = is_reparse_link(expanded_data_dir)
    _refuse_unsafe_data_dir(data_dir, resolved_data_dir)
    colliding_config_path = env_loading.config_collides_with_dir(resolved_data_dir)
    if colliding_config_path is not None:
        raise click.ClickException(
            f"Refusing to delete {redact_home(data_dir)}: it contains the active "
            f"config.toml ({redact_home(colliding_config_path)}). Move PIPECAT_HUB_DATA_DIR "
            "or the config file so they don't collide, then retry --reset-index."
        )
    colliding_dotenv_path = env_loading.dotenv_collides_with_dir(resolved_data_dir)
    if colliding_dotenv_path is not None:
        raise click.ClickException(
            f"Refusing to delete {redact_home(data_dir)}: it contains the active "
            f"project .env ({redact_home(colliding_dotenv_path)}). Point PIPECAT_HUB_DATA_DIR "
            "at a directory outside your project (or run from elsewhere), then retry "
            "--reset-index."
        )
    shutil.rmtree(resolved_data_dir, ignore_errors=True)
    if data_dir_was_symlink:
        # See the docstring: the symlink survives, its target does not. Put
        # the target back so `PIPECAT_HUB_DATA_DIR=<symlink>` still resolves
        # to a directory for the rebuild that immediately follows.
        try:
            resolved_data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Never fatal: the rebuild's own mkdir gets the next attempt, and
            # a reset that deleted successfully has already done its job.
            _module_logger.warning(
                "Could not recreate %s after reset (%s); the symlinked data dir may "
                "need to be recreated manually",
                redact_home(resolved_data_dir),
                exc,
            )


async def _delete_repo_index_data(index_store: IndexStore, slug: str, meta_key: str) -> None:
    """Remove every trace of ``slug`` from the index: records + bookkeeping.

    Shared by both deletion branches of ``refresh``'s cleanup pass (tainted
    repo, and ``--prune``-authorized removal of an unconfigured one) so the
    sequence — including the framework-repo special case, whose version
    metadata describes records that no longer exist once the repo is gone —
    cannot drift between them.
    """
    from pipecat_context_hub.services.ingest.github_ingest import _FRAMEWORK_REPO

    await index_store.delete_by_repo(slug)
    index_store.delete_metadata(meta_key)
    if slug == _FRAMEWORK_REPO:
        # No framework records left to describe.
        index_store.delete_metadata("indexed_framework_version")
        index_store.delete_metadata("indexed_framework_commits_ahead")


def _log_serve_cwd() -> None:
    """Log the cwd and PIPECAT_HUB_* key names this invocation actually saw.

    Permanent diagnostic (not removed after investigation): confirms, at a
    glance, what config provenance a globally-installed MCP server had, whose
    cwd/env may differ from an operator's interactive shell. Key *names* only,
    never values (``PIPECAT_HUB_EXTRA_REPOS`` can be ~70 repos), and the cwd is
    home-redacted through the same helper as the startup banner — these stderr
    lines are exactly what operators paste into the bug-report flow
    ``shared/support_links.py`` promotes, and an absolute cwd carries the OS
    username and often a client or project name.
    """
    # `Path.cwd()` is not infallible: it raises FileNotFoundError when the
    # process's working directory has been unlinked (a real case for a
    # long-lived MCP server whose launching project directory is deleted or
    # moved), and OSError/RuntimeError on other getcwd failures. This
    # diagnostic is *mandatory* on every serve boot, so it must be at least as
    # crash-safe as its opt-in sibling `_write_serve_debug_probe` — which
    # already wraps the identical call precisely because a diagnostic must
    # never take down `serve`.
    try:
        cwd = redact_home(Path.cwd())
    except Exception:
        cwd = "<unavailable>"
    _module_logger.info(
        "serve cwd=%s env_keys=%s",
        cwd,
        sorted(k for k in os.environ if k.startswith("PIPECAT_HUB_")),
    )


def _write_serve_debug_probe() -> None:
    """Write cwd/env-key evidence to a temp file, opt-in via PIPECAT_HUB_DEBUG_PROBE=1.

    Secondary observable for Phase 0's MCP-cwd verification, for use only if
    the MCP client's stderr/log surface can't be located. Never fires unless
    explicitly requested — it is not a substitute for the permanent
    ``logger.info`` line above, just a fallback in case that line's output
    isn't reachable. Failures are logged and swallowed; a debug probe must
    never crash `serve`.
    """
    try:
        home = Path.home()
        probe_dir = home / ".cache" / "pipecat-context-hub"
        probe_path = probe_dir / "serve-debug.log"
        # O_NOFOLLOW below protects the *leaf* only. `mkdir(parents=True)`
        # follows a symlinked (or, on Windows, junctioned) intermediate
        # component without complaint, so a `~/.cache` — or
        # `~/.cache/pipecat-context-hub` — planted as a link by another local
        # account would have this probe create its directory and append its
        # diagnostics inside an attacker-chosen tree, with the leaf-level
        # O_NOFOLLOW reporting success. Refuse any linked component *under*
        # home instead: home itself is legitimately a link on plenty of setups
        # (`/home/u` -> `/mnt/…`), and it is the account's own directory, so it
        # is the trust root here rather than something to check.
        #
        # This is a pre-flight check, not an atomic one — a link planted
        # between the check and the mkdir still wins. That race needs write
        # access to a directory under the server user's own home, and this is
        # an opt-in diagnostic with a permanent `logger.info` equivalent, so
        # closing the common pre-planted case is the proportionate fix.
        for ancestor in (probe_dir.parent, probe_dir):
            if is_reparse_link(ancestor):
                _module_logger.warning(
                    "PIPECAT_HUB_DEBUG_PROBE=1: skipping debug probe — %s is a symlink or "
                    "junction, so writing through it could land the probe in a directory "
                    "chosen by another account. Use the `serve cwd=… env_keys=…` log line "
                    "instead.",
                    redact_home(ancestor),
                )
                return
        # 0o700, not mkdir's default 0o777&~umask: this directory is created
        # by a *server* process and holds a log only its owner should read or
        # replace. exist_ok=True means an already-present directory keeps its
        # mode, which is fine — the point is not to create a world-traversable
        # one here.
        probe_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # O_NOFOLLOW + O_CREAT is what makes the append safe: `open("a")`
        # follows symlinks, so anyone able to write in this directory could
        # pre-create the log as a symlink and have `serve` append to an
        # arbitrary file the server user can write. With O_NOFOLLOW the open
        # fails (ELOOP) instead, and the failure is logged and swallowed like
        # every other probe failure. 0o600 keeps the created file private.
        #
        # Where the flag does not exist (notably Windows), the previous
        # `getattr(os, "O_NOFOLLOW", 0)` spelling silently OR'd in a no-op and
        # opened the path anyway — i.e. the one platform without the
        # protection was the one that skipped it. Refuse instead: this is an
        # optional diagnostic with a permanent `logger.info` equivalent, so
        # not writing it costs nothing next to appending into an
        # attacker-chosen file.
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            _module_logger.warning(
                "PIPECAT_HUB_DEBUG_PROBE=1: skipping debug probe — this platform has no "
                "O_NOFOLLOW, so the append at %s could follow a symlink planted by "
                "another account. Use the `serve cwd=… env_keys=…` log line instead.",
                redact_home(probe_path),
            )
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | nofollow
        fd = os.open(probe_path, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now(UTC).isoformat()} serve cwd={redact_home(Path.cwd())} "
                f"env_keys={sorted(k for k in os.environ if k.startswith('PIPECAT_HUB_'))}\n"
            )
        _module_logger.info(
            "PIPECAT_HUB_DEBUG_PROBE=1: wrote serve debug probe to %s",
            redact_home(probe_path),
        )
    except Exception as exc:
        # Path.home() raises RuntimeError (not OSError) when the home
        # directory can't be determined — broadened from except OSError to
        # actually cover that case, matching shared/paths.py's redact_home,
        # which wraps the same call for the same reason. A debug probe must
        # never crash serve, regardless of which exception it hits.
        #
        # A redacted single-line warning, not `logger.exception`: the traceback
        # renders source lines and the exception's own `__str__`, and OSError's
        # `__str__` appends the absolute filename it failed on — putting the
        # operator's home directory, and with it the OS username, into exactly
        # the stderr lines `shared/support_links.py`'s bug-report flow asks
        # them to paste. The success path above and `_log_serve_cwd` both
        # already redact; this path was the one hole. The exception type is
        # named explicitly since the traceback no longer supplies it, and the
        # message goes through `redact_home_in_text` (not `redact_home`) because
        # the path is embedded mid-string rather than being the whole argument.
        _module_logger.warning(
            "PIPECAT_HUB_DEBUG_PROBE=1: failed to write serve debug probe (%s: %s)",
            type(exc).__name__,
            redact_home_in_text(str(exc)),
        )


@click.group(invoke_without_command=True)
@click.version_option(package_name="pipecat-ai-context-hub", prog_name="pipecat-context-hub")
@click.option("--log-level", default="INFO", help="Logging level.")
@click.pass_context
def main(ctx: click.Context, log_level: str) -> None:
    """Pipecat Context Hub — local-first MCP server + one-shot query CLI.

    \b
    Query commands (search-*, get-*, check-deprecation, status) print the
    tool's JSON to stdout; logs and errors go to stderr. Exit codes:
    0 = success, 1 = invalid input, 2 = index missing or empty — build it
    once with `pipecat-context-hub refresh` (first run takes minutes).
    """
    # Loader calls run *after* _configure_logging (deliberately inverting the
    # prior order): no .env/config.toml-settable key feeds logging setup
    # (it reads only the --log-level CLI option), so nothing is lost by
    # moving both later, and malformed-config warnings are emitted through
    # configured logging instead of Python's last-resort handler.
    _configure_logging(log_level)
    env_loading.load_env_layers()
    ctx.ensure_object(dict)
    config = HubConfig()
    ctx.obj["config"] = config.model_copy(
        update={"server": config.server.model_copy(update={"log_level": log_level})}
    )
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


def _resolve_watch_and_idle_plan(
    config: HubConfig, original_ppid: int, logger: logging.Logger
) -> tuple[int | None, float]:
    """Resolve the client-death watch plan and the gated idle timeout.

    Centralizes the policy that sits between the config layer (operator
    intent) and the transport layer (process topology): when a reliable
    client-death watchdog is active, the idle timeout is redundant and
    would only reap a warm hub during a quiet stretch of an active
    session, so it is disabled — unless the operator set it explicitly.
    Returns ``(client_watch_pid, idle_timeout_secs)`` for ``serve_stdio``.

    Call this at process entry, *before* the slow index/model startup.
    ``resolve_watch_plan`` probes the parent with ``ps``; a client that
    dies during the cold-start window would, by the time startup
    finishes, have left ``uv`` reparented to PID 1 — unresolvable — so
    the grandparent watch would never be installed. Resolving early
    captures the grandparent while its PID is still live.
    """
    from pipecat_context_hub.server.transport import resolve_watch_plan

    idle_timeout_secs = config.server.effective_idle_timeout_secs
    parent_watch_secs = config.server.effective_parent_watch_interval_secs
    # Reliable client-death detection is only available off-Windows with
    # the parent-watch enabled; otherwise the idle timeout stays as the
    # sole fallback and is never auto-disabled.
    if sys.platform == "win32" or parent_watch_secs <= 0:
        return (None, idle_timeout_secs)

    plan = resolve_watch_plan(original_ppid)
    if (
        plan.detection_reliable
        and idle_timeout_secs > 0
        and not config.server.idle_timeout_explicitly_set
    ):
        target = (
            "direct-parent death"
            if plan.client_watch_pid is None
            else f"client pid {plan.client_watch_pid} (intermediate launcher)"
        )
        logger.info(
            "Idle watchdog disabled: watching %s for client exit. "
            "Set PIPECAT_HUB_IDLE_TIMEOUT_SECS to re-enable an idle backstop.",
            target,
        )
        idle_timeout_secs = 0.0
    return (plan.client_watch_pid, idle_timeout_secs)


@main.command()
@click.pass_context
def serve(ctx: click.Context) -> None:
    """Start the MCP server (stdio transport)."""
    # Capture PPID at the very top — before IndexStore/embedding/reranker
    # construction, which can take several seconds. If the client dies
    # during that startup window, os.getppid() has already flipped to 1
    # by the time run_stdio() snapshots it, and the watchdog would lock
    # in the already-reparented PID and never fire.
    _original_ppid = os.getppid()

    # Quiet, offline-first model loading (must precede the heavy imports):
    # skips huggingface_hub's network revalidation of already-cached models
    # (~20 HEAD requests, seconds off every boot) and keeps progress bars and
    # the transformers load report out of the MCP logs. setdefault semantics —
    # an explicit env (e.g. HF_HUB_OFFLINE=0) always wins. `refresh` must NOT
    # do this: it is the code path that downloads models.
    from pipecat_context_hub.shared.model_loading import quiet_model_loading

    quiet_model_loading()

    from pipecat_context_hub.server.main import create_server
    from pipecat_context_hub.server.transport import serve_stdio
    from pipecat_context_hub.services.embedding import EmbeddingService
    from pipecat_context_hub.services.index import IncompatibleIndexFormatError
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker
    from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever
    from pipecat_context_hub.shared.tracking import IdleTracker

    config: HubConfig = ctx.obj["config"]
    logger = logging.getLogger(__name__)
    logger.info("Starting server with transport=%s", config.server.transport)
    _log_serve_cwd()
    if _debug_probe_enabled():
        _write_serve_debug_probe()

    # Resolve the client-death watch plan now, before the slow index +
    # model startup below. A client that dies during cold-start would
    # leave `uv` reparented to PID 1 (unresolvable) by the time startup
    # finishes, so the grandparent watch would never be installed. See
    # _resolve_watch_and_idle_plan.
    client_watch_pid, idle_timeout_secs = _resolve_watch_and_idle_plan(
        config, _original_ppid, logger
    )

    index_store: IndexStore | None = None
    try:
        index_store = IndexStore(config.storage)
        stats = index_store.get_index_stats()
    except IncompatibleIndexFormatError as exc:
        # Specific, non-mutating signal: the on-disk Chroma format predates 1.x.
        # The probe ran before PersistentClient, so nothing was written.
        if index_store is not None:
            try:
                index_store.close()
            except Exception:
                logger.exception("Failed to close partially-opened index store")
        # exc.__str__ embeds the absolute chroma_path; redact it for front-door
        # parity with the cli_query one-shot error sites.
        logger.error("%s %s", redact_home_in_text(str(exc)), bug_report_hint())
        raise SystemExit(_EXIT_INDEX_UNREADY) from exc
    except Exception as exc:
        # IndexStore.__init__ opens two backends (Chroma + SQLite) without
        # rolling back on partial failure; close() whatever did come up.
        if index_store is not None:
            try:
                index_store.close()
            except Exception:
                logger.exception("Failed to close partially-opened index store")
        logger.error(
            "Failed to open index at %s: %s. "
            "Run 'uv run pipecat-context-hub refresh --force --reset-index' to rebuild. %s",
            _redact_home(config.storage.data_dir),
            redact_home_in_text(str(exc)),
            bug_report_hint(),
        )
        raise SystemExit(_EXIT_INDEX_UNREADY) from exc

    if stats.get("total", 0) == 0:
        logger.error(
            "Index at %s is empty (0 records). "
            "MCP clients would hang waiting for results. "
            "Run 'uv run pipecat-context-hub refresh' before 'serve'. %s",
            _redact_home(config.storage.data_dir),
            bug_report_hint(),
        )
        index_store.close()
        raise SystemExit(_EXIT_INDEX_UNREADY)

    # Startup banner: one line so operators can confirm version, data dir,
    # and content-type shape from an MCP JSONL trace. Helps diagnose
    # "upgraded but still running the old exe" and "docs indexed but code
    # didn't" without extra tooling.
    #
    # Logs the raw counts_by_type mapping (whose keys — doc, code, source —
    # come straight from FTS) so a future content_type rename or new type
    # cannot silently zero out this signal. `data_dir` is redacted to ~/…
    # because server instructions now encourage clients to share startup
    # log lines in bug reports.
    from pipecat_context_hub.server.main import _SERVER_VERSION

    counts_by_type = stats.get("counts_by_type", {}) or {}
    counts_repr = ",".join(f"{k}={v}" for k, v in sorted(counts_by_type.items()))
    logger.info(
        "pipecat-context-hub v%s starting: data_dir=%s total=%d counts_by_type={%s}",
        _SERVER_VERSION,
        _redact_home(config.storage.data_dir),
        stats.get("total", 0),
        counts_repr or "(empty)",
    )

    # From here on, any failure must close the index_store — otherwise a
    # crash between open and serve_stdio() leaks Chroma/SQLite handles and
    # hinders the `refresh --reset-index` recovery path.
    try:
        embedding_svc = EmbeddingService(config.embedding)

        # Optional cross-encoder reranker (env var or config). The config/cache
        # decision (and the operator's raw requested_model, surfaced by
        # get_hub_status even on a typo'd env var) is shared with the one-shot
        # CLI via probe_reranker so the two front doors cannot drift on which
        # reasons disable the reranker.
        from pipecat_context_hub.shared.reranker import probe_reranker, runtime_reranker_reason
        from pipecat_context_hub.shared.types import RerankerStatus

        cross_encoder: CrossEncoderReranker | None = None
        active_model, requested_model, startup_disabled_reason = probe_reranker(config)
        if startup_disabled_reason is None:
            cross_encoder = CrossEncoderReranker(
                model_name=active_model,
                top_n=config.reranker.top_n,
                enabled=True,
            )
            logger.info("Cross-encoder reranker enabled: %s", active_model)

        # Single telemetry line when the reranker is off at boot. Operators
        # grep this from MCP traces to diagnose degraded startups without
        # calling get_hub_status.
        if startup_disabled_reason is not None:
            hint = ""
            if startup_disabled_reason == "config_disabled":
                hint = (
                    "PIPECAT_HUB_RERANKER_ENABLED=0 (or config). "
                    "Unset the env var (or set it to 1) to re-enable."
                )
            elif startup_disabled_reason == "not_cached":
                probed = _redact_home(CrossEncoderReranker.resolve_hf_cache_dir())
                hint = (
                    f"model not downloaded (checked HF cache: {probed}). "
                    "Run 'pipecat-context-hub refresh' to pre-download, or set "
                    "PIPECAT_HUB_RERANKER_MODEL to a smaller cached model "
                    "(e.g. cross-encoder/ms-marco-TinyBERT-L-2-v2). "
                    "If this path is unexpected, check HF_HOME / HUGGINGFACE_HUB_CACHE. "
                    f"{bug_report_hint()}"
                )
            logger.warning(
                "Reranker disabled at startup: reason=%s configured_model=%s — %s",
                startup_disabled_reason,
                requested_model or "(default)",
                hint,
            )

        _prewarm_models(embedding_svc, cross_encoder)

        def _reranker_status() -> RerankerStatus:
            """Compute live reranker status at get_hub_status query time."""
            reason = runtime_reranker_reason(cross_encoder, startup_disabled_reason)
            if reason is None:
                return RerankerStatus(
                    enabled=True,
                    model=active_model,
                    configured_model=requested_model,
                )
            return RerankerStatus(
                enabled=False,
                configured_model=requested_model,
                disabled_reason=reason,
            )

        retriever = HybridRetriever(index_store, embedding_svc, cross_encoder=cross_encoder)

        # Load deprecation map from disk if available
        from pipecat_context_hub.services.ingest.deprecation_map import DeprecationMap

        dep_map_path = config.storage.data_dir / "deprecation_map.json"
        retriever.deprecation_map = DeprecationMap.load(dep_map_path)
        if retriever.deprecation_map.entries:
            logger.info(
                "Loaded deprecation map: %d entries", len(retriever.deprecation_map.entries)
            )

        idle_tracker = IdleTracker()
        server = create_server(
            retriever,
            index_store,
            reranker_status_provider=_reranker_status,
            idle_tracker=idle_tracker,
        )

        def _close_index_store_on_watchdog_shutdown() -> None:
            """Release index handles on any watchdog-triggered shutdown.

            `run_stdio` calls this on both the graceful watchdog path
            (inline, while the hard-exit timer is armed) and the
            hard-exit path (timer thread, if graceful unwind hangs).
            A single-shot guard in `run_stdio` ensures at most one
            invocation. Since `os._exit(0)` then skips the outer
            `finally`, closing the store here keeps Chroma's SQLite
            WAL / FTS handles from leaking on abrupt exit.
            """
            try:
                index_store.close()
            except Exception:
                logger.exception("Failed to close index store on watchdog shutdown")

        # `client_watch_pid` and the gated `idle_timeout_secs` were
        # resolved at process entry (see _resolve_watch_and_idle_plan),
        # before the slow startup above, so a client that died during
        # cold-start was still captured while its PID was live.
        serve_stdio(
            server,
            original_ppid=_original_ppid,
            idle_tracker=idle_tracker,
            parent_watch_interval_secs=config.server.effective_parent_watch_interval_secs,
            idle_timeout_secs=idle_timeout_secs,
            client_watch_pid=client_watch_pid,
            on_watchdog_shutdown=_close_index_store_on_watchdog_shutdown,
            exit_on_watchdog_shutdown=True,
        )
    finally:
        index_store.close()


@main.command()
@click.pass_context
def start(ctx: click.Context) -> None:
    """Start the MCP server (alias for `serve`)."""
    ctx.invoke(serve)


@main.command()
@click.option("--force", is_flag=True, help="Force full refresh, ignoring cached state.")
@click.option(
    "--reset-index",
    is_flag=True,
    help="Delete local index state before rebuilding. Use this when the persisted Chroma index is unhealthy.",
)
@click.option(
    "--framework-version",
    default=None,
    help="Pin the framework repo (pipecat-ai/pipecat) to a specific git tag "
    "(e.g. 'v0.0.96'), or 'latest' for its newest release tag. Source chunks "
    "will come from that version instead of HEAD. Can also be set via "
    "PIPECAT_HUB_FRAMEWORK_VERSION env var.",
)
@click.option(
    "--prune",
    is_flag=True,
    help="Delete previously-indexed data for repos not configured in this run. "
    "Without this flag, refresh only warns and leaves those records in place. "
    "Can also be enabled via PIPECAT_HUB_PRUNE=1.",
)
@click.pass_context
def refresh(
    ctx: click.Context,
    force: bool,
    reset_index: bool,
    framework_version: str | None,
    prune: bool,
) -> None:
    """Rebuild the index, skipping unchanged sources when possible.

    The first run downloads local embedding models and indexes all sources —
    allow several minutes. With an authenticated `gh` CLI, GitHub release
    notes are also ingested for deprecation data; without it,
    check-deprecation coverage is limited.
    """
    from pipecat_context_hub.services.embedding import (
        EmbeddingIndexWriter,
        EmbeddingService,
    )
    from pipecat_context_hub.services.index import IncompatibleIndexFormatError
    from pipecat_context_hub.services.index.fts import METADATA_CONTRACT_VERSION
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.services.ingest.docs_crawler import DocsCrawler
    from pipecat_context_hub.services.ingest.github_ingest import (
        _FRAMEWORK_REPO,
        GitHubRepoIngester,
        describe_framework_checkout,
        repo_ref_is_tainted,
    )
    from pipecat_context_hub.services.ingest.source_ingest import SourceIngester

    logger = logging.getLogger(__name__)
    config: HubConfig = ctx.obj["config"]

    # Propagate --framework-version CLI flag into config (CLI > env var).
    if framework_version is not None:
        config = config.model_copy(update={"framework_version": framework_version})

    fw_version = config.effective_framework_version
    prune_enabled = prune or _prune_enabled()
    logger.info(
        "Starting index refresh (force=%s reset_index=%s framework_version=%s prune=%s)",
        force,
        reset_index,
        fw_version,
        prune_enabled,
    )
    start = time.monotonic()

    if reset_index:
        logger.warning("Deleting local index storage before refresh")
        _delete_local_index_storage(config.storage.data_dir)
        force = True

    # Build the ingestion pipeline
    try:
        index_store = IndexStore(config.storage)
    except IncompatibleIndexFormatError as exc:
        # A pre-1.0 Chroma dir cannot be opened by 1.x. --reset-index deletes
        # storage above before this point, so this only fires on a plain
        # refresh against a stale 0.6 index — point the user at the rebuild.
        # exc.__str__ embeds the absolute chroma_path; redact for front-door
        # parity with the serve / cli_query error sites.
        logger.error("%s %s", redact_home_in_text(str(exc)), bug_report_hint())
        raise SystemExit(_EXIT_INDEX_UNREADY) from exc
    embedding_svc = EmbeddingService(config.embedding)
    writer = EmbeddingIndexWriter(index_store, embedding_svc)

    # Pre-download the embedding model. refresh is the only code path allowed
    # to reach the network (serve and the one-shot CLI both set
    # HF_HUB_OFFLINE=1), so fetching here turns a missing or newly-required
    # model file into a clear failure now rather than an opaque one on the
    # first query. Cheap when already cached.
    embedding_svc.ensure_model()

    # Pre-download cross-encoder model if enabled (env var or config)
    if config.reranker.effective_enabled:
        from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker

        ce = CrossEncoderReranker(
            model_name=config.reranker.effective_model,
            enabled=True,
        )
        ce.ensure_model()

    total_upserted = 0
    all_errors: list[str] = []

    # Per-source tracking for the summary table.
    # Each entry: {status, sha, existing, updated}
    source_status: dict[str, dict[str, str | int]] = {}

    # Built inside _run_refresh; read later by the summary pass. Created
    # once per refresh invocation, so no cross-run state leakage.
    github = GitHubRepoIngester(config, writer)

    # Framework checkout used this run, captured by _run_refresh so the metadata
    # pass below can record which pipecat revision the index reflects. None when
    # the framework repo was not cloned (unconfigured, tainted, or clone failed).
    framework_checkout: Path | None = None

    # The concrete tag/version selected for a pinned framework checkout,
    # captured during the clone loop so both chunk metadata and the metadata
    # pass below use the same release identity instead of re-deriving one via
    # `git describe` (which can disagree when multiple tags point at the same
    # commit). None for an unpinned default-branch refresh or a failed clone.
    resolved_framework_tag: str | None = None

    # Count of repos left un-pruned this run (not configured here, but not
    # deleted because --prune/PIPECAT_HUB_PRUNE wasn't set). Read later by
    # the summary pass.
    unpruned_repo_count = 0

    async def _run_refresh() -> None:
        nonlocal total_upserted, all_errors, framework_checkout
        nonlocal unpruned_repo_count, resolved_framework_tag

        # Snapshot per-repo chunk counts before any changes.
        pre_counts = index_store.get_counts_by_repo()

        # ----- 1. Docs -----
        crawler = DocsCrawler(writer, config.sources, config.chunking)
        docs_key = "docs.pipecat.ai"
        try:
            raw_text = await crawler.fetch_llms_txt()
        except Exception as exc:
            all_errors.append(f"Failed to fetch llms-full.txt: {exc}")
            raw_text = None
            source_status[docs_key] = {
                "status": "error",
                "sha": _MISSING_SENTINEL,
                "existing": pre_counts.get(docs_key, 0),
                "updated": _MISSING_SENTINEL,
            }

        if raw_text is not None:
            content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
            stored_hash = index_store.get_metadata("docs:content_hash")
            if not force and stored_hash == content_hash:
                logger.info("Docs unchanged (hash=%s…), skipping", content_hash[:8])
                source_status[docs_key] = {
                    "status": "skipped",
                    "sha": _MISSING_SENTINEL,
                    "existing": pre_counts.get(docs_key, 0),
                    "updated": _MISSING_SENTINEL,
                }
            else:
                await index_store.delete_by_content_type("doc")
                docs_result = await crawler.ingest(prefetched_text=raw_text)
                total_upserted += docs_result.records_upserted
                all_errors.extend(docs_result.errors)
                logger.info(
                    "Docs crawl: upserted=%d errors=%d",
                    docs_result.records_upserted,
                    len(docs_result.errors),
                )
                if not docs_result.errors:
                    index_store.set_metadata("docs:content_hash", content_hash)
                source_status[docs_key] = {
                    "status": "error" if docs_result.errors else "updated",
                    "sha": _MISSING_SENTINEL,
                    "existing": pre_counts.get(docs_key, 0),
                    "updated": docs_result.records_upserted,
                }
        await crawler.close()

        # ----- 2. Repos (code + source) -----
        changed_repos: list[str] = []
        repo_shas: dict[str, str] = {}
        prefetched: dict[str, tuple[Path, str]] = {}
        frozen_sha_repos: set[str] = set()

        # Clean up repos removed from configuration (P2: stale data from
        # repos no longer in effective_repos would persist indefinitely).
        configured = set(config.sources.effective_repos)
        tainted_repos = set(config.sources.tainted_repos)
        all_meta = index_store.get_all_metadata()
        for meta_key in all_meta:
            if meta_key.startswith("repo:") and meta_key.endswith(":commit_sha"):
                slug = meta_key[len("repo:") : -len(":commit_sha")]
                if slug not in configured:
                    if slug in tainted_repos:
                        # Explicit exclusion — always cleaned up, regardless
                        # of --prune.
                        logger.warning("Repo %s is tainted by local policy, cleaning up", slug)
                        await _delete_repo_index_data(index_store, slug, meta_key)
                    elif prune_enabled:
                        logger.info("Repo %s no longer configured, cleaning up", slug)
                        await _delete_repo_index_data(index_store, slug, meta_key)
                    elif pre_counts.get(slug, 0) > 0:
                        # Implicit absence — not seen from this invocation's
                        # env layering, but not necessarily unconfigured
                        # elsewhere. Leave both records and metadata in place
                        # (deleting metadata here would orphan this repo from
                        # future cleanup passes, since the loop keys off
                        # all_meta) so a later --prune can still find and
                        # remove them.
                        #
                        # Gated on there actually being records: a repo whose
                        # only remnant is the `repo:*:commit_sha` metadata key
                        # has no indexed data to protect, so warning "leaving 0
                        # indexed record(s) in place — pass --prune to remove"
                        # advertises a destructive flag whose sole effect would
                        # be dropping a stale key. Nothing is at risk, so say
                        # nothing.
                        unpruned_repo_count += 1
                        logger.warning(
                            "Repo %s not configured in this run; leaving %d indexed "
                            "record(s) in place — pass --prune to remove",
                            slug,
                            pre_counts.get(slug, 0),
                        )
                    else:
                        # Zero *FTS* records — which is not the same as zero
                        # records. `get_counts_by_repo()` reads SQLite only, so
                        # a divergent index (an interrupted delete leaving
                        # vector-only rows behind) reports 0 here while those
                        # rows still surface through the hybrid retriever.
                        # Staying silent entirely left that unexplainable, so
                        # the trace is kept — at INFO, and without the counter
                        # or the "Skipped pruning" summary line, because
                        # nothing is *provably* at risk and the warning above
                        # would be advertising a destructive flag on a guess.
                        logger.info(
                            "Repo %s not configured in this run; no indexed records found "
                            "(stale bookkeeping only — pass --prune to drop it)",
                            slug,
                        )

        framework_slug = _FRAMEWORK_REPO
        for repo_slug in config.sources.effective_repos:
            stored_sha_key = f"repo:{repo_slug}:commit_sha"
            # Pin the framework repo to a specific tag when configured.
            repo_tag = fw_version if repo_slug == framework_slug and fw_version else None
            try:
                repo_path, commit_sha = await asyncio.to_thread(
                    github.clone_or_fetch, repo_slug, False, tag=repo_tag
                )
                if repo_slug == framework_slug and repo_tag and is_latest_sentinel(repo_tag):
                    resolved_framework_tag = await asyncio.to_thread(
                        github.resolve_tag_name, repo_path, repo_tag
                    )
                elif repo_slug == framework_slug:
                    resolved_framework_tag = repo_tag
                repo_shas[repo_slug] = commit_sha
                prefetched[repo_slug] = (repo_path, commit_sha)
            except Exception as exc:
                all_errors.append(f"Failed to clone/fetch {repo_slug}: {exc}")
                source_status[repo_slug] = {
                    "status": "error",
                    "sha": _MISSING_SENTINEL,
                    "existing": pre_counts.get(repo_slug, 0),
                    "updated": _MISSING_SENTINEL,
                }
                continue

            stored_sha = index_store.get_metadata(stored_sha_key)
            tainted_refs = set(config.sources.tainted_refs_by_repo.get(repo_slug, []))
            if tainted_refs and repo_ref_is_tainted(repo_path, commit_sha, tainted_refs):
                logger.warning(
                    "Repo %s resolved to tainted ref (sha=%s), skipping refresh",
                    repo_slug,
                    commit_sha[:8],
                )
                if stored_sha and repo_ref_is_tainted(repo_path, stored_sha, tainted_refs):
                    logger.warning(
                        "Indexed ref for %s is also tainted; removing local records",
                        repo_slug,
                    )
                    await index_store.delete_by_repo(repo_slug)
                    index_store.delete_metadata(stored_sha_key)
                    if repo_slug == framework_slug:
                        # Framework records are gone, so any recorded provenance
                        # would describe content the index no longer holds.
                        index_store.delete_metadata("indexed_framework_version")
                        index_store.delete_metadata("indexed_framework_commits_ahead")
                    source_status[repo_slug] = {
                        "status": "tainted",
                        "sha": commit_sha[:8],
                        "existing": pre_counts.get(repo_slug, 0),
                        "updated": 0,
                    }
                else:
                    source_status[repo_slug] = {
                        "status": "tainted",
                        "sha": commit_sha[:8],
                        "existing": pre_counts.get(repo_slug, 0),
                        "updated": _MISSING_SENTINEL,
                    }
                # Preserve the last known-good SHA (or lack of one) until this
                # repo is ingested successfully at a non-tainted ref.
                frozen_sha_repos.add(repo_slug)
                continue

            # The stored SHA is bookkeeping, not proof that the records it
            # describes are still in the index — the two diverge whenever a
            # repo's records are removed without its metadata key going with
            # them. A repo left unconfigured for one invocation keeps its key
            # (the no-prune default deliberately preserves it so a later
            # `--prune` can still find the repo), and an interrupted delete can
            # strip records the same way. Skipping on the SHA alone then left
            # the repo silently absent from the index — indefinitely, since
            # every subsequent run took the same shortcut — until someone
            # thought to run `--force`. So the shortcut requires both halves:
            # the SHA is unchanged *and* records for it actually exist.
            indexed_records = pre_counts.get(repo_slug, 0)
            if (
                not force
                and stored_sha == commit_sha
                and indexed_records > 0
                and repo_slug not in github.recovered_repos
            ):
                logger.info(
                    "Repo %s unchanged (sha=%s…), skipping",
                    repo_slug,
                    commit_sha[:8],
                )
                source_status[repo_slug] = {
                    "status": "skipped",
                    "sha": commit_sha[:8],
                    "existing": pre_counts.get(repo_slug, 0),
                    "updated": _MISSING_SENTINEL,
                }
            else:
                if repo_slug in github.recovered_repos and stored_sha == commit_sha:
                    logger.warning(
                        "Repo %s SHA unchanged but local clone was recovered "
                        "from corrupt state — forcing re-ingest",
                        repo_slug,
                    )
                elif not force and stored_sha == commit_sha and indexed_records == 0:
                    logger.warning(
                        "Repo %s SHA unchanged (sha=%s…) but no indexed records "
                        "found — re-ingesting",
                        repo_slug,
                        commit_sha[:8],
                    )
                changed_repos.append(repo_slug)

        # Delete and re-ingest each changed repo atomically to minimise
        # the window where a repo's index is empty (crash-safety).
        ingested_repos: set[str] = set()
        # Repos whose indexed records were *replaced* this run — populated at the
        # delete, not at the end of a clean ingest. Distinct from
        # `ingested_repos` (error-free ingest) because the two diverge exactly in
        # the partial-failure case, and it is record replacement, not ingest
        # cleanliness, that decides which revision the index now describes.
        replaced_repos: set[str] = set()
        for repo_slug in changed_repos:
            repo_path, commit_sha = prefetched[repo_slug]
            try:
                await asyncio.to_thread(github.checkout_commit, repo_path, commit_sha)
            except Exception as exc:
                msg = f"Failed to checkout fetched ref for {repo_slug}: {exc}"
                all_errors.append(msg)
                logger.error(msg)
                source_status[repo_slug] = {
                    "status": "error",
                    "sha": commit_sha[:8],
                    "existing": pre_counts.get(repo_slug, 0),
                    "updated": _MISSING_SENTINEL,
                }
                continue

            await index_store.delete_by_repo(repo_slug)
            replaced_repos.add(repo_slug)
            logger.info("Deleted stale records for %s", repo_slug)

            repo_has_errors = False
            repo_upserted = 0

            # Code ingest (per-repo for error tracking)
            code_result = await github.ingest(
                repos=[repo_slug],
                prefetched=prefetched,
                framework_version=resolved_framework_tag if repo_slug == framework_slug else None,
            )
            total_upserted += code_result.records_upserted
            repo_upserted += code_result.records_upserted
            all_errors.extend(code_result.errors)
            if code_result.errors:
                repo_has_errors = True
            logger.info(
                "GitHub ingest (%s): upserted=%d errors=%d",
                repo_slug,
                code_result.records_upserted,
                len(code_result.errors),
            )

            # Source ingest
            source_ingester = SourceIngester(config, writer, repo_slug)
            source_result = await source_ingester.ingest()
            total_upserted += source_result.records_upserted
            repo_upserted += source_result.records_upserted
            all_errors.extend(source_result.errors)
            if source_result.errors:
                repo_has_errors = True
            if source_result.records_upserted > 0:
                logger.info(
                    "Source ingest (%s): upserted=%d errors=%d",
                    repo_slug,
                    source_result.records_upserted,
                    len(source_result.errors),
                )

            source_status[repo_slug] = {
                "status": "error" if repo_has_errors else "updated",
                "sha": repo_shas.get(repo_slug, _MISSING_SENTINEL)[:8],
                "existing": pre_counts.get(repo_slug, 0),
                "updated": repo_upserted,
            }

            if not repo_has_errors:
                ingested_repos.add(repo_slug)

        # Gated on record *replacement*, not on error-free ingest. Once
        # `delete_by_repo` has run, the index holds the new checkout's records —
        # whole or partial — so both the deprecation map and the provenance
        # stamp must follow it. A framework repo whose `checkout_commit` failed
        # never reached the delete, so its records still describe the old
        # revision and are left alone.
        #
        # Also excludes a tainted framework repo: `prefetched` is populated as
        # soon as the clone succeeds, before the taint check, but a tainted ref
        # is never ingested — stamping its version would describe content the
        # index does not hold.
        if (
            framework_slug in prefetched
            and framework_slug not in frozen_sha_repos
            and (framework_slug not in changed_repos or framework_slug in replaced_repos)
        ):
            framework_checkout = prefetched[framework_slug][0]

        # Store SHAs: unchanged repos (handles first-run) + successfully ingested repos.
        # For failed repos: delete the cached SHA so the next non-force refresh
        # retries them (P1: --force deletes records before ingest, so a failure
        # leaves the repo empty; keeping the old SHA would skip it next time).
        for repo_slug, sha in repo_shas.items():
            if repo_slug in frozen_sha_repos:
                continue
            if repo_slug not in changed_repos or repo_slug in ingested_repos:
                index_store.set_metadata(f"repo:{repo_slug}:commit_sha", sha)
            else:
                index_store.delete_metadata(f"repo:{repo_slug}:commit_sha")

        # ----- 3. Deprecation map -----
        # Built solely from pipecat's machine-readable registry
        # (scripts/deprecations/deprecations.json) — the single source of truth.
        # It reflects the current checkout, so --framework-version pins it to the
        # registry as it stood at that tag.
        from pipecat_context_hub.services.ingest.deprecation_map import (
            REGISTRY_RELATIVE_PATH,
            REMOVALS_RELATIVE_PATH,
            add_removals_from_registry,
            build_deprecation_map_from_registry,
        )

        dep_map_path = config.storage.data_dir / "deprecation_map.json"

        # Reuse the same record-replacement gate as framework provenance below,
        # so the map and the stamp can never describe different revisions.
        # `prefetched` is populated immediately after clone/fetch, so it can
        # contain a new checkout whose records were never swapped in (tainted
        # ref, or a failed checkout); publishing its registry would then pair a
        # new deprecation map with old indexed framework records.
        if framework_checkout is not None:
            fw_path, fw_sha = prefetched[framework_slug]
            registry_path = fw_path / REGISTRY_RELATIVE_PATH
            dep_map = build_deprecation_map_from_registry(registry_path, commit_sha=fw_sha)
            # Merge removed symbols (no-op until pipecat ships removals.json).
            add_removals_from_registry(dep_map, fw_path / REMOVALS_RELATIVE_PATH)
            dep_map.save(dep_map_path)
        else:
            logger.debug(
                "Framework repo %s not cloned or tainted — preserving existing deprecation map",
                framework_slug,
            )

    try:
        asyncio.run(_run_refresh())

        duration = round(time.monotonic() - start, 1)
        logger.info(
            "Refresh complete: upserted=%d errors=%d duration=%.1fs",
            total_upserted,
            len(all_errors),
            duration,
        )
        if all_errors:
            for err in all_errors:
                logger.warning("  %s", err)

        # Persist refresh metadata for get_hub_status tool. Collected into a
        # single dict (plus a delete-keys list) and written with one batched
        # commit so a reader never observes a partially-updated related-key
        # set (e.g. a new indexed_framework_version paired with a stale
        # indexed_framework_commits_ahead).
        now = datetime.now(UTC).isoformat()
        metadata_to_set: dict[str, str] = {
            "metadata_contract_version": str(METADATA_CONTRACT_VERSION),
            "last_refresh_duration_seconds": str(duration),
            "last_refresh_records_upserted": str(total_upserted),
            "last_refresh_error_count": str(len(all_errors)),
        }
        metadata_to_delete: list[str] = []

        stats = index_store.get_index_stats()
        metadata_to_set["content_type_counts"] = json.dumps(stats["counts_by_type"])

        # Persist pinned framework version (or clear it) for get_hub_status.
        # Normalize the `latest` sentinel's case/whitespace so a pin like
        # " Latest " is recorded as the canonical "latest", matching what
        # `is_latest_sentinel` accepts everywhere else.
        if fw_version:
            metadata_to_set["framework_version"] = canonicalize_framework_pin(fw_version)
        else:
            metadata_to_delete.append("framework_version")

        # Record the pipecat revision the index was actually built from.
        # `framework_version` above is the operator's pin — frequently unset;
        # this is observed from the checkout, so consumers can tell which
        # release the index reflects and whether it matches the version a
        # project builds against. Left untouched when the framework repo was
        # not cloned this run, so a transient clone failure keeps the last
        # known-good stamp rather than erasing it.
        if framework_checkout is not None:
            if resolved_framework_tag is not None:
                # A `latest`-pinned refresh already knows the exact tag it
                # resolved to (`resolve_tag_name`, reusing `_latest_version_tag`'s
                # algorithm) — trust that over `git describe`, which can pick a
                # different tag when multiple point at the same commit.
                metadata_to_set["indexed_framework_version"] = strip_v_prefix(
                    resolved_framework_tag
                )
                metadata_to_set["indexed_framework_commits_ahead"] = "0"
            else:
                indexed_version, commits_ahead = describe_framework_checkout(framework_checkout)
                if indexed_version is not None and commits_ahead is not None:
                    metadata_to_set["indexed_framework_version"] = indexed_version
                    metadata_to_set["indexed_framework_commits_ahead"] = str(commits_ahead)

        metadata_to_set["last_refresh_at"] = now
        if all_errors:
            metadata_to_set["last_refresh_errored_at"] = now

        index_store.set_metadata_batch(metadata_to_set, delete_keys=metadata_to_delete)

        # ----- Summary table -----
        _print_refresh_summary(
            source_status,
            total_upserted,
            len(all_errors),
            duration,
            recovered_repos=sorted(github.recovered_repos),
            unpruned_repo_count=unpruned_repo_count,
        )
    finally:
        index_store.close()


def _stdout_can_encode(text: str) -> bool:
    """Return True if ``sys.stdout`` can encode ``text`` without errors.

    Re-reads ``sys.stdout`` on every call so tests (and callers) can swap
    the stream. A missing ``encoding`` attribute is treated as ``ascii``.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _encode_safe(value: str, fallback: str) -> str:
    """Return ``value`` if stdout can encode every character, else ``fallback``."""
    return value if _stdout_can_encode(value) else fallback


def _safe_hr(width: int) -> str:
    """Return a horizontal-rule string of ``width`` characters.

    Uses U+2500 (box-drawing light horizontal) when ``sys.stdout`` can encode
    it; falls back to ASCII ``-`` on non-UTF-8 consoles (cp1252, cp1254,
    cp437, etc.) so the refresh summary never crashes after a successful
    index.
    """
    if not _stdout_can_encode("\u2500"):
        return "-" * width
    return "\u2500" * width


def _safe_placeholder() -> str:
    """Return a missing-value placeholder that the current stdout can encode.

    U+2014 em dash on UTF-8 terminals; ASCII ``-`` when stdout cannot encode
    it (cp437 notably rejects U+2014). Used for empty SHA / count cells in
    the refresh summary.
    """
    if _stdout_can_encode("\u2014"):
        return "\u2014"
    return "-"


def _echo_prune_skip_notice(unpruned_repo_count: int) -> None:
    """Print the 'Skipped pruning' line, shared by both summary branches below."""
    if unpruned_repo_count:
        click.echo(
            f"Skipped pruning: {unpruned_repo_count} repo(s) not in this run's config "
            "(use --prune to remove)"
        )


def _print_refresh_summary(
    source_status: dict[str, dict[str, str | int]],
    total_upserted: int,
    error_count: int,
    duration: float,
    *,
    recovered_repos: list[str] | None = None,
    unpruned_repo_count: int = 0,
) -> None:
    """Print a summary table after refresh."""
    if not source_status:
        # Notice before the completion line, matching the table branch below —
        # a warning whose whole point is to be noticed shouldn't sit after the
        # line that reads as "done".
        _echo_prune_skip_notice(unpruned_repo_count)
        click.echo(f"Refresh complete: {total_upserted} records upserted in {duration}s.")
        return

    # Compute column widths
    name_width = max(len(name) for name in source_status)
    name_width = max(name_width, len("Repository"))

    hr = f"{_safe_hr(name_width)}  {_safe_hr(8)}  {_safe_hr(10)}  {_safe_hr(8)}  {_safe_hr(8)}"
    placeholder = _safe_placeholder()

    # Header
    click.echo()
    click.echo(
        f"{'Repository':<{name_width}}  {'Status':<8}  {'SHA':<10}  {'Existing':>8}  {'Updated':>8}"
    )
    click.echo(hr)

    # Rows — updated/error first, then skipped
    total_existing = 0
    total_updated = 0
    for name in sorted(source_status, key=lambda n: (source_status[n]["status"] == "skipped", n)):
        entry = source_status[name]
        status = str(entry["status"])
        # Normalise any non-encodable cell (typically the missing-value
        # sentinel) to the placeholder so the summary never crashes on
        # terminals that cannot encode it.
        sha = _encode_safe(str(entry["sha"]), placeholder)
        existing = entry["existing"]
        updated = entry["updated"]

        existing_int = int(existing) if isinstance(existing, int) else 0
        total_existing += existing_int

        if isinstance(updated, int):
            total_updated += updated
            updated_str = f"{updated:,}"
        elif status == "skipped":
            # Skipped repos carry forward their existing count —
            # their chunks are still in the index unchanged.
            total_updated += existing_int
            updated_str = placeholder
        else:
            # Error repos: don't carry forward (chunks may have been deleted).
            updated_str = placeholder

        existing_str = f"{existing_int:,}" if existing_int else placeholder

        click.echo(
            f"{name:<{name_width}}  {status:<8}  {sha:<10}  {existing_str:>8}  {updated_str:>8}"
        )

    # Footer
    click.echo(hr)
    click.echo(
        f"{'Total':<{name_width}}  {'':<8}  {'':<10}  {total_existing:>8,}  {total_updated:>8,}"
    )
    click.echo()
    if recovered_repos:
        click.echo(
            f"Recovered {len(recovered_repos)} corrupt clone(s): {', '.join(recovered_repos)}"
        )
    _echo_prune_skip_notice(unpruned_repo_count)
    click.echo(f"Refresh complete: {total_upserted:,} upserted, {error_count} errors, {duration}s.")


# One-shot query subcommands (search-docs, check-deprecation, status, ...) —
# the same tool handlers the MCP server dispatches, exposed for shell callers.
register_query_commands(main)
register_install_command(main)


if __name__ == "__main__":
    main()
