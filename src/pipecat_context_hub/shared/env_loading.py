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

from pipecat_context_hub.shared.paths import is_inside, redact_home

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
        "PIPECAT_HUB_CONFIG_FILE",
        "PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK",
        "PIPECAT_HUB_STABILITY_OUTPUT",
    }
)

_ENV_PREFIX = "PIPECAT_HUB_"
_CONFIG_FILE_ENV_VAR = "PIPECAT_HUB_CONFIG_FILE"
_DATA_DIR_ENV_VAR = "PIPECAT_HUB_DATA_DIR"

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
        "PIPECAT_HUB_DATA_DIR",
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
    """
    env_path = Path.cwd() / ".env"
    if not env_path.is_file():
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
            os.environ[key] = value


def resolve_global_config_path() -> Path | None:
    """Resolve the machine-global config file path.

    Honors ``PIPECAT_HUB_CONFIG_FILE`` if set (a test-hermeticity seam only —
    not a documented operator feature), else falls back to
    ``DEFAULT_CONFIG_PATH``. Reads ``DEFAULT_CONFIG_PATH`` via module-attribute
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
    """Return the active config path if it or its resolved target lives inside ``candidate_dir``.

    Checks both the lexical/physical location of the config path itself (so a
    ``candidate_dir`` that would delete a symlink hop pointing *at* the config
    is caught even though that symlink's own target lives elsewhere) and the
    config path's fully-resolved target (so a config path living outside
    ``candidate_dir`` but symlinked to real content *inside* it is also
    caught). Returns ``None`` if there's no collision on either check, the
    active config path can't be resolved (no home directory), or the config
    file doesn't exist — a nonexistent or unresolvable config can't be
    protected from, matching the loader's own missing-file-silent contract.
    Shared by this module's own ``PIPECAT_HUB_DATA_DIR`` skip below and
    ``cli.py``'s ``_delete_local_index_storage()`` reset-path guard, so both
    stay in lockstep on the same predicate.

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

    # Lexical: absolute + '..'/'.'-normalized, zero syscalls, zero symlink deref.
    lexical = Path(os.path.abspath(config_path))

    # Physical: directory chain dereferenced, final component left intact —
    # this is the path that would actually be unlinked by rmtree(candidate_dir)
    # even when that path is itself a symlink pointing elsewhere.
    #
    # RuntimeError is caught alongside ValueError/OSError here (and below):
    # on Python <=3.12 `Path.resolve(strict=False)` raises RuntimeError — not
    # an OSError subclass — for a symlink loop. A `PIPECAT_HUB_CONFIG_FILE`
    # pointing into a loop would otherwise abort `refresh --reset-index` with
    # an uncaught traceback instead of degrading to "no collision detected",
    # breaking this project's "entry points never crash on a bad config" rule.
    try:
        physical = lexical.parent.resolve(strict=False) / lexical.name
    except (ValueError, OSError, RuntimeError):
        physical = lexical

    # Target: full dereference (original behavior) — kept because it covers
    # the mirror-image hazard: config path lives OUTSIDE candidate_dir but is
    # a symlink whose real content lives INSIDE it (rmtree would destroy the
    # actual config content, not just a path entry).
    try:
        resolved = config_path.resolve(strict=False)
    except (ValueError, OSError, RuntimeError):
        resolved = lexical

    try:
        if not lexical.is_file():
            return None
    except (OSError, ValueError):
        # `is_file()` swallows OSError for most failures, but not every one on
        # every platform (e.g. ELOOP / ENAMETOOLONG on some libc versions).
        # An unstattable config path can't be protected; treat as absent.
        return None

    try:
        dir_stat: os.stat_result | None = candidate_dir.stat()
    except (OSError, ValueError):
        dir_stat = None

    if is_inside(physical, candidate_dir, dir_stat):
        return physical
    if is_inside(resolved, candidate_dir, dir_stat):
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
    """False when the config path is a symlink planted by another local user.

    ``fstat`` on the opened file describes the *target*, never the link, so a
    foreign-owned symlink pointing at a victim-owned file would otherwise pass
    every check. Symlink mode bits are meaningless (0777 on Linux/macOS), so
    only ownership is tested — and symlinked config files are otherwise
    allowed, since pointing the config at a dotfiles checkout is a legitimate
    setup.
    """
    try:
        lst = os.lstat(config_path)
    except (OSError, ValueError):
        return True
    if not stat.S_ISLNK(lst.st_mode):
        return True
    if lst.st_uid in (uid, 0):
        return True
    _module_logger.warning(
        "Ignoring config.toml at %s: it is a symlink owned by uid %d, not the current "
        "user (uid %d). Another local account could redirect this hub's settings.",
        redact_home(config_path),
        lst.st_uid,
        uid,
    )
    return False


def _config_parent_is_trusted(config_path: Path, uid: int) -> bool:
    """False when the directory holding config.toml is another user's to rewrite.

    A per-file mode check cannot see this: write permission on the *directory*
    is what authorizes unlink-and-replace, so a ``0600`` file in a
    world-writable directory is still fully controllable by any local
    principal. World-writable **sticky** directories (``/tmp``) are exempt —
    the sticky bit is precisely what withdraws that unlink permission.
    """
    parent = config_path.parent
    try:
        pst = os.stat(parent)
    except (OSError, ValueError):
        # Unstattable parent: the open() already succeeded, so don't invent a
        # refusal from a check we couldn't perform.
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
    if pst.st_mode & stat.S_IWGRP and not pst.st_mode & stat.S_ISVTX:
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
        _module_logger.warning(
            "Failed to read config.toml at %s: %s", redact_home(config_path), exc
        )
        return

    # `fd` ownership transfers to the file object the moment `os.fdopen`
    # returns; until then this function must close it on every exit path.
    fd_owned = True
    try:
        if not _config_fd_is_trusted(fd, config_path):
            return
        with os.fdopen(fd, "rb") as f:
            fd_owned = False
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, UnicodeError, OSError, ValueError) as exc:
        _module_logger.warning(
            "Failed to read config.toml at %s: %s", redact_home(config_path), exc
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
        if key not in _KNOWN_KEYS:
            # Warn, don't skip: an unrecognized key is far more often a typo
            # (`PIPECAT_HUB_DATADIR`) than a genuinely newer setting, but
            # skipping would break a config.toml written for a newer hub.
            _module_logger.warning(
                "config.toml key %r is not a recognized PIPECAT_HUB_* setting "
                "(typo?); setting it anyway",
                key,
            )
        if key in os.environ:
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
                _module_logger.warning(
                    "config.toml key %r has a value that is not a usable path (%s); skipping",
                    key,
                    exc,
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
