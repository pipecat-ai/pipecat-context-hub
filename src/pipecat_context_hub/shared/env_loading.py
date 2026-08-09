"""Shared env-var / config-file loading, used by every entry point.

Precedence (highest to lowest): real environment variables > cwd ``.env`` >
``~/.config/pipecat-context-hub/config.toml`` > :class:`HubConfig` field
defaults. Both loaders in this module follow the same first-writer-wins
pattern: a key already present in ``os.environ`` (from a real env var, or
from an earlier loader call) is never overwritten.

Every entry point that constructs ``StorageConfig``/``HubConfig`` — the CLI
group callback, the dashboard scripts, and ``scripts/smoke_check_removals.py``
— must call :func:`load_env_layers`, which performs both loader calls in the
required order, so all of them see the same layering. The individual loaders
stay public for tests, but no entry point should hand-replicate the ordering.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import tomllib
from pathlib import Path

from pipecat_context_hub.shared.paths import (
    is_inside,
    redact_home,
    redact_home_in_text,
    resolution_chain,
)

_module_logger = logging.getLogger(__name__)

# Default lookup path for the machine-global config file. Read via
# `resolve_global_config_path()` (module-attribute lookup at call time, not a
# captured default or an aliased import) so `monkeypatch.setattr(env_loading,
# "DEFAULT_CONFIG_PATH", ...)` is observed by every consumer of that helper.
#
# Computed at import time, but guarded: `Path.home()` raises `RuntimeError`
# when the home directory can't be determined (e.g. a hardened container
# running as an arbitrary UID with no matching /etc/passwd entry). Every
# other `Path.home()` call site in this codebase is lazy (inside a function
# body); this one can't be, because tests need to monkeypatch it as a plain
# attribute. A None fallback here means "no default config.toml location
# available" rather than crashing every entry point that imports this module
# (cli.py's console-script entry point among them) before argv is even
# parsed.
try:
    DEFAULT_CONFIG_PATH: Path | None = (
        Path.home() / ".config" / "pipecat-context-hub" / "config.toml"
    )
except RuntimeError as _home_exc:
    _module_logger.warning(
        "Could not determine home directory (%s); config.toml auto-discovery is "
        "disabled unless PIPECAT_HUB_CONFIG_FILE is set",
        _home_exc,
    )
    DEFAULT_CONFIG_PATH = None

# Single source of truth for the PIPECAT_HUB_PRUNE literal, referenced (not
# re-hardcoded) by _INVOCATION_SCOPED_KEYS below and by Phase 4's
# --prune/_prune_enabled() wiring in cli.py.
PRUNE_ENV_VAR = "PIPECAT_HUB_PRUNE"

# Single source of truth for the PIPECAT_HUB_DEBUG_PROBE literal, referenced
# (not re-hardcoded) by _INVOCATION_SCOPED_KEYS below and by cli.py's
# serve-time debug-probe gate — the structural sibling of PRUNE_ENV_VAR.
DEBUG_PROBE_ENV_VAR = "PIPECAT_HUB_DEBUG_PROBE"

_ENV_PREFIX = "PIPECAT_HUB_"

# Declared above the frozensets below (rather than after them) so those sets
# reference these constants instead of re-hardcoding the same literals — the
# same single-source-of-truth rule PRUNE_ENV_VAR/DEBUG_PROBE_ENV_VAR follow.
_CONFIG_FILE_ENV_VAR = "PIPECAT_HUB_CONFIG_FILE"
_DATA_DIR_ENV_VAR = "PIPECAT_HUB_DATA_DIR"

# Keys that look like PIPECAT_HUB_* but must never be set from config.toml,
# each for its own one-clause reason (see dev plan Phase 1): PRUNE keeps
# refresh's deletion behavior an explicit per-run choice; DEBUG_PROBE must
# never be persistently enabled by a machine-global file; CONFIG_FILE is the
# loader's own lookup-path seam (honoring it from inside the file it locates
# would be circular); the two STABILITY_* vars are pytest-invocation-only dev
# controls for the opt-in stability benchmark suite.
_INVOCATION_SCOPED_KEYS = frozenset(
    {
        PRUNE_ENV_VAR,
        DEBUG_PROBE_ENV_VAR,
        _CONFIG_FILE_ENV_VAR,
        "PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK",
        "PIPECAT_HUB_STABILITY_OUTPUT",
    }
)

# The documented settings registry — every key in `config.toml.example` and
# docs/README.md's Environment Variables table. Pinned against both by
# `tests/unit/test_config.py::TestConfigTomlExampleParity`, so this set cannot
# silently drift from the documented one.
#
# Used only to *warn* about an unrecognized key (e.g. a typo'd
# `PIPECAT_HUB_DATADIR`), never to skip it: warn-and-set keeps a config.toml
# written for a newer hub version forward-compatible with an older binary.
_KNOWN_KEYS = frozenset(
    {
        _DATA_DIR_ENV_VAR,
        "PIPECAT_HUB_EXTRA_REPOS",
        "PIPECAT_HUB_FRAMEWORK_VERSION",
        "PIPECAT_HUB_IDLE_TIMEOUT_SECS",
        "PIPECAT_HUB_PARENT_WATCH_INTERVAL",
        "PIPECAT_HUB_RERANKER_ENABLED",
        "PIPECAT_HUB_RERANKER_MODEL",
        "PIPECAT_HUB_STALE_AFTER_DAYS",
        "PIPECAT_HUB_TAINTED_REFS",
        "PIPECAT_HUB_TAINTED_REPOS",
        "PIPECAT_HUB_WARMUP",
    }
)

# Exclusion controls: a higher-precedence layer *replaces* these rather than
# adding to them (first-writer-wins applies uniformly), so a config.toml entry
# that is shadowed by a real env var or cwd `.env` is worth saying out loud —
# an operator who deliberately excluded a repo machine-globally should learn
# that this invocation is not honouring that list.
_EXCLUSION_KEYS = frozenset({"PIPECAT_HUB_TAINTED_REPOS", "PIPECAT_HUB_TAINTED_REFS"})

# Scalar TOML value types accepted as-is (coerced via str()) or as elements
# of a homogeneous array.
_SCALAR_TYPES = (str, int, float, bool)


def load_cwd_dotenv() -> None:
    """Load ``.env`` file from the current directory if it exists.

    Only sets variables that are not already in the environment so that
    explicit env vars always take precedence.  Supports quoted values
    and inline comments::

        KEY="value"          # ok
        KEY='value'          # ok
        KEY=value            # ok
        KEY="value" # note   # inline comment stripped
        KEY=value # note     # inline comment stripped

    Never raises. ``Path.cwd()`` is not infallible — it raises
    ``FileNotFoundError`` when the process's working directory has been
    unlinked (a real case for a long-lived MCP server whose launching project
    directory is deleted or moved), and ``OSError``/``RuntimeError`` on other
    ``getcwd`` failures. Since this runs from ``load_env_layers()`` before argv
    is dispatched, in every entry point (CLI, dashboard scripts,
    ``smoke_check_removals.py``), an uncaught raise here would take down the
    whole process before it could report anything useful — violating this
    module's "entry points never crash on a bad config" contract. No cwd means
    no cwd ``.env``, which is exactly the "file absent" case handled below.
    The same call is guarded for the same reason in ``cli.py``'s
    ``_log_serve_cwd`` / ``_write_serve_debug_probe`` and in
    ``shared/paths.py``'s ``resolution_chain``.
    """
    try:
        env_path = Path.cwd() / ".env"
        if not env_path.is_file():
            return
    except (OSError, ValueError, RuntimeError):
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Quoted value: extract content between matching quotes.
        if value and value[0] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            # Unquoted: strip inline comments (# preceded by whitespace).
            idx = value.find(" #")
            if idx != -1:
                value = value[:idx].rstrip()
        if key not in os.environ:
            try:
                os.environ[key] = value
            except ValueError as exc:
                # Same guard as `load_global_config()`'s assignment: putenv
                # rejects an embedded NUL or `=` in the key/value with
                # ValueError. A hand-edited `.env` carrying one must skip that
                # line, not abort the whole loader (and with it every entry
                # point) before argv is dispatched.
                _module_logger.warning(
                    ".env key %r could not be set as an environment variable (%s); skipping",
                    key,
                    exc,
                )


def resolve_global_config_path() -> Path | None:
    """Resolve the machine-global config file path.

    Honors ``PIPECAT_HUB_CONFIG_FILE`` if set, else falls back to
    ``DEFAULT_CONFIG_PATH``. That override is invocation-scoped (hence its
    place in ``_INVOCATION_SCOPED_KEYS``, and its deliberate absence from
    ``_KNOWN_KEYS`` / ``config.toml.example`` / the README's Environment
    Variables table), but it is a real operator-visible capability, not merely
    a test seam — it is documented under docs/README.md's Troubleshooting
    section alongside ``PIPECAT_HUB_DEBUG_PROBE``. Reads ``DEFAULT_CONFIG_PATH`` via module-attribute
    lookup so a test-time ``monkeypatch.setattr(env_loading,
    "DEFAULT_CONFIG_PATH", ...)`` is observed. Returns ``None`` only when no
    override is set and ``DEFAULT_CONFIG_PATH`` itself is ``None`` (home
    directory could not be determined at import time) — callers must treat
    that the same as "no config file available", not an error.
    """
    override = os.environ.get(_CONFIG_FILE_ENV_VAR)
    if override:
        # expanduser so a `~`-rooted override resolves to a real path instead
        # of a literal `~` directory that silently takes the missing-file
        # branch (no warning, no config).
        #
        # Guarded for the same reason DEFAULT_CONFIG_PATH's `Path.home()` is:
        # `expanduser()` raises RuntimeError when the home directory of the
        # named user can't be determined — `~nosuchuser/config.toml`, or a
        # bare `~` in a container with no passwd entry. An uncaught raise here
        # would crash *every* CLI command before argv is even dispatched,
        # violating this module's "entry points never crash on a bad config"
        # contract. Degrade to "no config file available" instead of silently
        # falling back to DEFAULT_CONFIG_PATH: the operator asked for a
        # specific file, and quietly reading a different one would be worse
        # than reading none.
        try:
            return Path(override).expanduser()
        except (RuntimeError, ValueError, OSError) as exc:
            _module_logger.warning(
                "Ignoring %s=%r: not a usable path (%s); no config.toml will be loaded",
                _CONFIG_FILE_ENV_VAR,
                override,
                exc,
            )
            return None
    return DEFAULT_CONFIG_PATH


def config_collides_with_dir(candidate_dir: Path) -> Path | None:
    """Return a config-path location inside ``candidate_dir``, or ``None``.

    Checks *every* filesystem location the active config path stands on while
    it resolves — the path as given plus each intermediate symlink hop, via
    :func:`shared.paths.resolution_chain` — because each of those is a
    directory entry ``rmtree(candidate_dir)`` would delete, and deleting any
    one of them severs the operator's path to their config. Checking only the
    two endpoints (as this function once did) misses the middle: a chain that
    enters ``candidate_dir`` and leaves again has both endpoints outside it
    while still being destroyed by the delete.

    The fully-resolved target is re-checked explicitly afterwards as a floor,
    since ``resolution_chain`` is bounded and truncates on a cycle.

    The path's *lexical* form (``abspath``: absolute and ``..``-collapsed, with
    no symlink dereference) is deliberately **not** among the checked
    locations. It is computed only as the fallback spelling for ``resolved``
    when full resolution fails. Collapsing ``..`` lexically names a location
    the kernel never visits — for ``<dirlink>/../config.toml`` the lexical form
    is a sibling of the *link*, not of its target — so testing it against
    ``candidate_dir`` would both miss real collisions and invent phantom ones.

    Returns ``None`` if nothing collides, the active config path can't be
    resolved (no home directory), or the config file doesn't exist — a
    nonexistent or unresolvable config can't be protected from, matching the
    loader's own missing-file-silent contract. Shared by this module's own
    ``PIPECAT_HUB_DATA_DIR`` skip below and ``cli.py``'s
    ``_delete_local_index_storage()`` reset-path guard, so both stay in
    lockstep on the same predicate.

    ``candidate_dir`` is normalized here (``expanduser`` +
    ``resolve(strict=False)``) rather than at the call sites: this is a
    safety-critical predicate that fails *open* (silent non-detection) when
    handed an unnormalized path, so it must not depend on every caller —
    present and future — getting that right. Normalization is idempotent, so
    callers that already normalize are unaffected.
    """
    config_path = resolve_global_config_path()
    if config_path is None:
        return None

    try:
        candidate_dir = Path(candidate_dir).expanduser().resolve(strict=False)
    except (ValueError, OSError, RuntimeError):
        candidate_dir = Path(os.path.abspath(candidate_dir))

    # Lexical: absolute + '..'/'.'-normalized, zero syscalls, zero symlink
    # deref. Kept only as the fallback spelling when full resolution fails —
    # never as an existence test (see the `is_file()` gate below).
    lexical = Path(os.path.abspath(config_path))

    # Target: full dereference — the floor beneath the chain walk, and the
    # mirror-image hazard in its own right (config path lives OUTSIDE
    # candidate_dir but is a symlink whose real content lives INSIDE it, so
    # rmtree destroys the actual config content, not just a path entry).
    #
    # RuntimeError is caught alongside ValueError/OSError: on Python <=3.12
    # `Path.resolve(strict=False)` raises RuntimeError — not an OSError
    # subclass — for a symlink loop. A `PIPECAT_HUB_CONFIG_FILE` pointing into
    # a loop would otherwise abort `refresh --reset-index` with an uncaught
    # traceback instead of degrading to "no collision detected", breaking this
    # project's "entry points never crash on a bad config" rule.
    try:
        resolved = config_path.resolve(strict=False)
    except (ValueError, OSError, RuntimeError):
        resolved = lexical

    # Existence is tested on the path *as the kernel resolves it*, not on its
    # lexically-collapsed spelling. `os.path.abspath` folds `..` away before
    # any symlink is expanded, so for a path like `<dirlink>/../config.toml`
    # the lexical form names a location that need not exist at all — and this
    # gate would short-circuit the whole deletion guard to "no collision" for a
    # config file that is genuinely inside `candidate_dir`.
    try:
        if not config_path.is_file():
            return None
    except (OSError, ValueError):
        # `is_file()` swallows OSError for most failures, but not every one on
        # every platform (e.g. ELOOP / ENAMETOOLONG on some libc versions).
        # An unstattable config path can't be protected; treat as absent.
        return None

    # Root-first, so the reported collision is the outermost path entry the
    # delete would take out — the most actionable one to name in the warning.
    for location in resolution_chain(config_path):
        if is_inside(location, candidate_dir):
            return location
    if is_inside(resolved, candidate_dir):
        return resolved
    return None


def load_env_layers() -> None:
    """Load every config layer below real env vars, in precedence order.

    The single bootstrap entry point: ``load_cwd_dotenv()`` (cwd ``.env``)
    then ``load_global_config()`` (machine-global ``config.toml``). Every
    entry point that constructs ``StorageConfig``/``HubConfig`` outside
    ``cli.py:main()`` — the dashboard scripts, ``scripts/smoke_check_removals.py``
    — calls this rather than hand-replicating the two-call ordering, so the
    contract is enforced by the code path instead of by convention.

    The individual loaders remain public for tests that exercise one layer.
    """
    load_cwd_dotenv()
    load_global_config()


def _stringify_scalar_array(value: list[object], key: str) -> str | None:
    """Coerce a homogeneous scalar array to a CSV string, or None to skip.

    Losslessly equivalent to the existing CSV parser (`shared/config.py`)
    only when no stringified element contains a comma — that parser has no
    escaping/quoting syntax, so a comma-bearing element would otherwise split
    into multiple values on read-back.
    """
    stringified = [str(v) for v in value]
    if any("," in s for s in stringified):
        _module_logger.warning(
            "config.toml key %r: array element contains a comma, which the CSV "
            "parser cannot escape; skipping",
            key,
        )
        return None
    return ",".join(stringified)


def _file_kind(mode: int) -> str:
    """Human-readable name for a non-regular file's type, for warnings."""
    for predicate, name in (
        (stat.S_ISDIR, "directory"),
        (stat.S_ISFIFO, "named pipe (FIFO)"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISCHR, "character device"),
        (stat.S_ISBLK, "block device"),
        (stat.S_ISLNK, "symbolic link"),
    ):
        if predicate(mode):
            return name
    return "not a regular file"


def _link_owner_is_trusted(config_path: Path, uid: int) -> bool:
    """False when any symlink on the config path was planted by another user.

    ``fstat`` on the opened file describes the *target*, never the link, so a
    foreign-owned symlink pointing at a victim-owned file would otherwise pass
    every check. Symlink mode bits are meaningless (0777 on Linux/macOS), so
    only ownership is tested — and symlinked config paths are otherwise
    allowed, since pointing the config at a dotfiles checkout is a legitimate
    setup.

    Every location on the resolution chain is ``lstat``'d, not just the final
    component: a foreign-owned *directory* symlink partway up the path (say
    ``~/.config`` itself) redirects the lookup exactly as effectively as a
    foreign-owned leaf, and every stat after that hop just follows it to a
    target tree that looks perfectly fine.

    ``config_path`` itself is appended as an independent floor rather than
    relied on being the chain's last element. The chain is best-effort by
    contract — bounded, and truncating on any per-component ``OSError`` — so a
    truncated or (as round 6 found) mis-seeded walk could otherwise silently
    drop the single most attackable component, the leaf the loader actually
    opened, and this guard would report "trusted" having examined none of it.
    Re-``lstat``ing one path is not worth making that failure mode possible.
    """
    chain = resolution_chain(config_path)
    seen: set[str] = set()
    locations: list[Path] = []
    for candidate in [*chain, config_path]:
        marker = str(candidate)
        if marker not in seen:
            seen.add(marker)
            locations.append(candidate)

    for location in locations:
        try:
            lst = os.lstat(location)
        except (OSError, ValueError):
            # Unstattable component: the open() already succeeded, so don't
            # invent a refusal from a check that couldn't be performed.
            continue
        if not stat.S_ISLNK(lst.st_mode):
            continue
        if lst.st_uid in (uid, 0):
            continue
        _module_logger.warning(
            "Ignoring config.toml at %s: path component %s is a symlink owned by uid %d, "
            "not the current user (uid %d). Another local account could redirect this "
            "hub's settings.",
            redact_home(config_path),
            redact_home(location),
            lst.st_uid,
            uid,
        )
        return False
    return True


def _config_parent_is_trusted(config_path: Path, uid: int) -> bool:
    """False when any directory on config.toml's path is another user's to rewrite.

    A per-file mode check cannot see this: write permission on a *directory*
    is what authorizes unlink-and-replace, so a ``0600`` file in a
    world-writable directory is still fully controllable by any local
    principal. World-writable **sticky** directories (``/tmp``) are exempt —
    the sticky bit is precisely what withdraws that unlink permission.

    The set of directories that must be trusted is "every directory that holds
    a path entry the lookup depends on". That is not the same as the ancestors
    of either endpoint, because a symlink hop moves the walk into a different
    tree: the ancestors of the *lexical* path stop being examined the moment
    a directory symlink is followed (``stat`` sees through it to a target that
    may be impeccably permissioned), and the ancestors of the *resolved*
    target never included the tree the walk came from. Write access on any
    directory in either tree is enough to substitute a hop and redirect the
    whole lookup.

    So the check is driven by :func:`shared.paths.resolution_chain`: every
    location the path stands on while resolving, and every ancestor of each,
    up to the filesystem root. The resolved parent is appended as a floor,
    since the chain walk is bounded and truncates on a cycle.

    The group-writable *warning* (allow, don't refuse) is emitted only for the
    two directories that directly hold the file — the lexical parent and the
    resolved one. A per-ancestor version would be noise on any machine with a
    group-writable dir anywhere in ``/``.
    """
    checked: set[str] = set()
    directories: list[tuple[Path, bool]] = []

    lexical_parent = Path(os.path.abspath(config_path)).parent
    real_parent: Path | None
    try:
        real_parent = Path(os.path.realpath(config_path)).parent
    except (OSError, ValueError):
        real_parent = None

    # Warn-worthy holders first, so the dedup set records them with
    # `warn_group=True` before the ancestor sweep can claim them with False.
    directories.append((lexical_parent, True))
    if real_parent is not None:
        directories.append((real_parent, True))

    for location in resolution_chain(config_path):
        directories.extend((ancestor, False) for ancestor in location.parents)
    if real_parent is not None:
        directories.append((real_parent, False))
        directories.extend((ancestor, False) for ancestor in real_parent.parents)

    for directory, warn_group in directories:
        marker = str(directory)
        if marker in checked:
            continue
        checked.add(marker)
        if not _config_dir_is_trusted(config_path, directory, uid, warn_group=warn_group):
            return False
    return True


def _config_dir_is_trusted(config_path: Path, parent: Path, uid: int, *, warn_group: bool) -> bool:
    """One directory's leg of :func:`_config_parent_is_trusted`."""
    try:
        pst = os.stat(parent)
    except (OSError, ValueError):
        # Unstattable directory: the open() already succeeded, so don't invent
        # a refusal from a check we couldn't perform.
        return True

    if pst.st_uid not in (uid, 0):
        _module_logger.warning(
            "Ignoring config.toml at %s: its directory is owned by uid %d, not the "
            "current user (uid %d). Another local account could replace the file.",
            redact_home(config_path),
            pst.st_uid,
            uid,
        )
        return False
    if pst.st_mode & stat.S_IWOTH and not pst.st_mode & stat.S_ISVTX:
        _module_logger.warning(
            "Ignoring config.toml at %s: its directory %s is world-writable without the "
            "sticky bit (mode %o), so any local account could replace the file. "
            "Run `chmod 755 %s` and retry.",
            redact_home(config_path),
            redact_home(parent),
            stat.S_IMODE(pst.st_mode),
            redact_home(parent),
        )
        return False
    if warn_group and pst.st_mode & stat.S_IWGRP and not pst.st_mode & stat.S_ISVTX:
        _module_logger.warning(
            "config.toml's directory %s is group-writable (mode %o); anyone in group %d "
            "can replace this hub's settings file. Consider `chmod 755 %s`.",
            redact_home(parent),
            stat.S_IMODE(pst.st_mode),
            pst.st_gid,
            redact_home(parent),
        )
    return True


def _open_config_fd(config_path: Path) -> int:
    """Open ``config_path`` read-only, without blocking on a special file.

    ``O_NONBLOCK`` is the load-bearing flag: a plain ``open()`` on a FIFO
    blocks in the kernel until a writer appears, so a ``config.toml`` that is
    (accidentally or maliciously) a named pipe would hang *every* invocation
    of every entry point before argv is dispatched. With ``O_NONBLOCK`` the
    open returns immediately and :func:`_config_fd_is_trusted` rejects the
    non-regular file. On a regular file the flag is a no-op.

    Raises ``OSError`` (``FileNotFoundError`` included) for the caller to
    classify — mirroring the exception surface the previous ``open("rb")``
    call had.
    """
    flags = os.O_RDONLY
    for flag_name in ("O_NONBLOCK", "O_CLOEXEC", "O_NOCTTY", "O_BINARY"):
        flags |= getattr(os, flag_name, 0)
    return os.open(config_path, flags)


def _config_fd_is_trusted(fd: int, config_path: Path) -> bool:
    """False when the opened config file can be controlled by another principal.

    ``config.toml``'s contents are promoted straight into ``os.environ``
    (``PIPECAT_HUB_DATA_DIR``, ``PIPECAT_HUB_EXTRA_REPOS``, the taint lists),
    so a file another user can write is a persistent, machine-wide injection
    point into every invocation.

    Checks, in order:

    * **Refuse** anything that is not a regular file (FIFO, socket, device,
      directory). Every platform: a config file is a plain file by contract,
      and the FIFO case would otherwise hang or mis-parse.
    * **Refuse** a file that is world-writable, or not owned by the current
      user (nor by root). Both are unambiguously wrong for a per-user config.
    * **Refuse** a *containing directory* that is world-writable without the
      sticky bit, or owned by a foreign non-root user: either lets another
      local principal unlink-and-replace the config file wholesale, which the
      per-file mode check alone cannot see. ``/tmp``-style world-writable
      **sticky** directories are exempt — the sticky bit is exactly what stops
      that unlink.
    * **Refuse** a symlinked config path whose *link* is owned by a foreign
      non-root user (checked with ``lstat``, which the file ``fstat`` cannot
      see through). The symlink itself is honoured otherwise: pointing
      ``~/.config/pipecat-context-hub/config.toml`` at a dotfiles checkout
      (GNU stow et al.) is a legitimate, common setup.
    * **Allow with a warning** for a group-writable file or directory.
      Distributions using user-private groups (RHEL/Fedora's ``umask 002``)
      create ``0664`` files whose group contains only the owner — refusing
      those would break a correctly-configured machine, so the operator is
      told rather than overruled.

    The ownership/mode checks are POSIX-only (Windows' ACL model is not
    described by these mode bits); the regular-file check is not.

    Takes the *file descriptor*, not the path, so the bytes later handed to
    ``tomllib`` are the bytes that were validated — closing the TOCTOU window
    a ``stat(path)``-then-``open(path)`` sequence leaves open.
    """
    try:
        st = os.fstat(fd)
    except OSError:
        # Should not happen on a live fd; the caller's read reports it.
        return True

    if not stat.S_ISREG(st.st_mode):
        _module_logger.warning(
            "Ignoring config.toml at %s: not a regular file (%s); config.toml must be a plain file",
            redact_home(config_path),
            _file_kind(st.st_mode),
        )
        return False

    if os.name != "posix":
        return True

    uid = os.getuid()
    if not _link_owner_is_trusted(config_path, uid):
        return False
    if not _config_parent_is_trusted(config_path, uid):
        return False

    if st.st_uid not in (uid, 0):
        _module_logger.warning(
            "Ignoring config.toml at %s: owned by uid %d, not the current user (uid %d). "
            "Another local account could control this hub's settings.",
            redact_home(config_path),
            st.st_uid,
            uid,
        )
        return False
    if st.st_mode & stat.S_IWOTH:
        _module_logger.warning(
            "Ignoring config.toml at %s: it is world-writable (mode %o). "
            "Run `chmod 600 %s` and retry.",
            redact_home(config_path),
            stat.S_IMODE(st.st_mode),
            redact_home(config_path),
        )
        return False
    if st.st_mode & stat.S_IWGRP:
        _module_logger.warning(
            "config.toml at %s is group-writable (mode %o); anyone in group %d can change "
            "this hub's settings. Consider `chmod 600 %s`.",
            redact_home(config_path),
            stat.S_IMODE(st.st_mode),
            st.st_gid,
            redact_home(config_path),
        )
    return True


def _shadowed_exclusion_entries(config_value: object, active_value: str) -> list[str]:
    """Entries in a config.toml exclusion list that the winning layer drops.

    Best-effort and lenient: this runs on a value that was never coerced (it
    lost precedence before validation), so a shape the loader wouldn't accept
    simply yields no named entries rather than an error.
    """
    if isinstance(config_value, str):
        configured = [part.strip() for part in config_value.split(",")]
    elif isinstance(config_value, list) and all(isinstance(v, str) for v in config_value):
        configured = [str(v).strip() for v in config_value]
    else:
        return []
    active = {part.strip() for part in active_value.split(",")}
    return sorted({entry for entry in configured if entry and entry not in active})


def _is_homogeneous_scalar_array(value: list[object]) -> bool:
    if not value:
        return True
    first_type = type(value[0])
    if first_type not in _SCALAR_TYPES:
        return False
    return all(type(v) is first_type for v in value)


def load_global_config() -> None:
    """Load ``PIPECAT_HUB_*`` keys from the machine-global ``config.toml``.

    Only sets variables not already in ``os.environ`` (first-writer-wins,
    same as `load_cwd_dotenv`). A missing file returns silently. Malformed or
    unreadable files log a warning and never raise.
    """
    config_path = resolve_global_config_path()
    if config_path is None:
        # Home directory couldn't be determined at import time (see
        # DEFAULT_CONFIG_PATH above) and no PIPECAT_HUB_CONFIG_FILE override
        # is set. Nothing to load; the warning was already logged once, at
        # import time.
        return
    try:
        fd = _open_config_fd(config_path)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        # `exc` is redacted, not just `config_path`: OSError's own __str__
        # appends the absolute filename ("[Errno 13] Permission denied:
        # '/Users/<name>/...'"), which would leak the home directory back into
        # a log line whose path argument was carefully redacted.
        _module_logger.warning(
            "Failed to read config.toml at %s: %s",
            redact_home(config_path),
            redact_home_in_text(str(exc)),
        )
        return

    # `fd` ownership transfers to `os.fdopen` on *entry*, not on success:
    # `io.open` closes the descriptor itself when construction fails. Clearing
    # the flag beforehand is therefore the correct handoff point — clearing it
    # after the call returns would leave the `finally` below closing an fd the
    # interpreter had already closed, which after fd-number reuse closes some
    # unrelated file. Until that call, this function closes it on every path.
    fd_owned = True
    try:
        if not _config_fd_is_trusted(fd, config_path):
            return
        fd_owned = False
        with os.fdopen(fd, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, UnicodeError, OSError, ValueError) as exc:
        # Same redaction rationale as the open() site above: an OSError raised
        # mid-read still carries the absolute filename in its message.
        _module_logger.warning(
            "Failed to read config.toml at %s: %s",
            redact_home(config_path),
            redact_home_in_text(str(exc)),
        )
        return
    finally:
        if fd_owned:
            with contextlib.suppress(OSError):
                os.close(fd)

    loaded = 0
    for key, value in data.items():
        if not key.startswith(_ENV_PREFIX):
            if isinstance(value, dict):
                # A TOML *table* header, not a stray scalar. Settings written
                # under `[some_table]` are dropped wholesale, and naming only
                # the table would leave the operator with no idea which of
                # their settings went missing — so name the nested
                # PIPECAT_HUB_* keys explicitly. This is a likely first-attempt
                # TOML mistake (section headers are the format's idiom).
                nested = sorted(k for k in value if k.startswith(_ENV_PREFIX))
                if nested:
                    _module_logger.warning(
                        "config.toml table %r is not read; %s must be top-level "
                        "key(s), not nested under a section header — skipping",
                        key,
                        ", ".join(repr(k) for k in nested),
                    )
                    continue
            _module_logger.warning("config.toml key %r is not a PIPECAT_HUB_* var; skipping", key)
            continue
        # Invocation-scoped first: these keys are deliberately absent from
        # `_KNOWN_KEYS`, so checking typos first would emit two contradictory
        # warnings in sequence ("typo?; setting it anyway" immediately
        # followed by "invocation-scoped; skipping") for the same key.
        if key in _INVOCATION_SCOPED_KEYS:
            _module_logger.warning(
                "config.toml key %r is invocation-scoped, not machine config; skipping",
                key,
            )
            continue
        if os.environ.get(key, "").strip():
            # Deliberately *not* `key in os.environ`: a blank/whitespace-only
            # higher-layer value is inert, because every consumer in
            # `shared/config.py` reads its var as
            # `os.environ.get(NAME, "").strip()` and falls back to the field
            # default when that is empty. Treating `PIPECAT_HUB_DATA_DIR=`
            # as "present" would skip config.toml's value as shadowed while
            # the shadowing value itself does nothing — so neither layer
            # applies. Blank therefore means absent here, matching the
            # consumers.
            #
            # This applies uniformly, list-valued keys included
            # (`PIPECAT_HUB_EXTRA_REPOS`, `PIPECAT_HUB_TAINTED_REPOS`/`_REFS`),
            # and that is deliberate rather than an oversight for that key
            # class: those consumers read
            # `_split_csv_env(os.environ.get(NAME, ""))`, so a blank yields the
            # empty list — bit-for-bit the same result as the key being unset.
            # A blank list key is therefore inert in exactly the same sense as
            # a blank scalar, and the "neither layer applies" outcome above is
            # the same one. The cost is real but is the lesser one: an operator
            # writing `PIPECAT_HUB_EXTRA_REPOS=` in a project `.env` to narrow
            # an inherited machine-global list to empty does not get that (the
            # `config.toml` list still wins); the remedy is to omit the repo
            # list rather than blank it, or to taint the unwanted entries.
            # Making blank shadow *only* for list keys was considered and
            # rejected: it splits one precedence rule into two by key class,
            # and the intent argument ("an explicit blank is a deliberate
            # override") is exactly as true for `PIPECAT_HUB_STALE_AFTER_DAYS=`,
            # where the uniform rule was already settled the other way and is
            # pinned by `TestBlankHigherLayerValueDoesNotShadow`.
            #
            # A real env var or cwd .env already won this key — nothing this
            # config.toml value could do (including a DATA_DIR collision
            # warning below) is actually going to apply, so don't validate
            # or warn about a value that was never eligible to be used.
            #
            # Exception: exclusion controls. Precedence is uniformly
            # first-writer-wins (a higher layer replaces, it does not union),
            # so a shadowed taint list means a deliberate machine-global
            # exclusion is silently not in effect this run. Say so.
            if key in _EXCLUSION_KEYS:
                lost = _shadowed_exclusion_entries(value, os.environ[key])
                if lost:
                    # Name the entries, not just the key: "your taint list is
                    # shadowed" is not actionable, "these repos are no longer
                    # excluded this run" is. This is the mitigation for the
                    # `.env`-clears-the-taint-list hazard that is compatible
                    # with the plan's uniform first-writer-wins precedence
                    # (a union would not be — see dev plan Objective:
                    # "whole-string override, not a list merge").
                    _module_logger.warning(
                        "config.toml key %r is shadowed by a higher-precedence layer "
                        "(real env var or cwd .env); these machine-global exclusions are "
                        "NOT in effect for this run: %s",
                        key,
                        ", ".join(lost),
                    )
                else:
                    _module_logger.warning(
                        "config.toml key %r is shadowed by a higher-precedence layer "
                        "(real env var or cwd .env); the machine-global exclusion list "
                        "is NOT in effect for this run",
                        key,
                    )
            continue

        if isinstance(value, list):
            if not _is_homogeneous_scalar_array(value):
                _module_logger.warning(
                    "config.toml key %r has a non-homogeneous or non-scalar array value; skipping",
                    key,
                )
                continue
            coerced = _stringify_scalar_array(value, key)
            if coerced is None:
                continue
        elif isinstance(value, _SCALAR_TYPES):
            coerced = str(value)
        else:
            # Name the actual type: this branch is reached by every non-scalar,
            # non-list TOML type, including the native date/time/datetime ones —
            # reporting "table" for a bare date sends the operator hunting for
            # a syntax problem they don't have.
            _module_logger.warning(
                "config.toml key %r has an unsupported value type (%s); skipping",
                key,
                type(value).__name__,
            )
            continue

        if key == _DATA_DIR_ENV_VAR:
            try:
                candidate_dir = Path(coerced).expanduser().resolve(strict=False)
            except (ValueError, OSError, RuntimeError) as exc:
                # `exc` is redacted for the same reason the read-failure
                # warnings above are: `Path.resolve()`'s OSError/RuntimeError
                # text embeds the absolute path it was resolving (symlink loop,
                # ELOOP, ENAMETOOLONG), which puts the operator's home
                # directory — and OS username — straight into a startup log
                # line operators are asked to paste into bug reports.
                _module_logger.warning(
                    "config.toml key %r has a value that is not a usable path (%s); skipping",
                    key,
                    redact_home_in_text(str(exc)),
                )
                continue
            colliding_config_path = config_collides_with_dir(candidate_dir)
            if colliding_config_path is not None:
                _module_logger.warning(
                    "config.toml key %r would relocate the data dir to contain the "
                    "active config file (%s); skipping",
                    key,
                    redact_home(colliding_config_path),
                )
                continue

        if key not in _KNOWN_KEYS:
            # Warn, don't skip: an unrecognized key is far more often a typo
            # (`PIPECAT_HUB_DATADIR`) than a genuinely newer setting, but
            # skipping would break a config.toml written for a newer hub.
            #
            # Deliberately the *last* gate before the assignment, for the same
            # reason `_INVOCATION_SCOPED_KEYS` is checked before `_KNOWN_KEYS`:
            # "setting it anyway" must not be printed above a shadow / type /
            # collision warning that then skips the key, which would make the
            # pair contradict each other.
            _module_logger.warning(
                "config.toml key %r is not a recognized PIPECAT_HUB_* setting "
                "(typo?); setting it anyway",
                key,
            )

        try:
            os.environ[key] = coerced
        except ValueError as exc:
            _module_logger.warning(
                "config.toml key %r could not be set as an environment variable (%s); skipping",
                key,
                exc,
            )
            continue
        loaded += 1

    if loaded:
        _module_logger.info("Loaded %d key(s) from %s", loaded, redact_home(config_path))
