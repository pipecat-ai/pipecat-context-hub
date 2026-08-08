"""Shared env-var / config-file loading, used by every entry point.

Precedence (highest to lowest): real environment variables > cwd ``.env`` >
``~/.config/pipecat-context-hub/config.toml`` > :class:`HubConfig` field
defaults. Both loaders in this module follow the same first-writer-wins
pattern: a key already present in ``os.environ`` (from a real env var, or
from an earlier loader call) is never overwritten.

Every entry point that constructs ``StorageConfig``/``HubConfig`` — the CLI
group callback, the dashboard scripts, and ``scripts/smoke_check_removals.py``
— must call ``load_cwd_dotenv()`` then ``load_global_config()`` (in that
order) before constructing config, so all of them see the same layering.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

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
        "PIPECAT_HUB_DEBUG_PROBE",
        "PIPECAT_HUB_CONFIG_FILE",
        "PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK",
        "PIPECAT_HUB_STABILITY_OUTPUT",
    }
)

_ENV_PREFIX = "PIPECAT_HUB_"
_CONFIG_FILE_ENV_VAR = "PIPECAT_HUB_CONFIG_FILE"
_DATA_DIR_ENV_VAR = "PIPECAT_HUB_DATA_DIR"

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
        return Path(override)
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

    ``candidate_dir`` must already be an absolute, symlink-resolved path
    (both current call sites pass one) — this function does not normalize
    it, so an unresolved ``candidate_dir`` (e.g. one reached through a
    symlinked ancestor, as macOS's ``/var`` -> ``/private/var`` and
    ``tempfile``-based paths often are) can silently fail to match even a
    plain, non-symlinked collision.
    """
    config_path = resolve_global_config_path()
    if config_path is None:
        return None

    # Lexical: absolute + '..'/'.'-normalized, zero syscalls, zero symlink deref.
    lexical = Path(os.path.abspath(config_path))

    # Physical: directory chain dereferenced, final component left intact —
    # this is the path that would actually be unlinked by rmtree(candidate_dir)
    # even when that path is itself a symlink pointing elsewhere.
    try:
        physical = lexical.parent.resolve(strict=False) / lexical.name
    except (ValueError, OSError):
        physical = lexical

    # Target: full dereference (original behavior) — kept because it covers
    # the mirror-image hazard: config path lives OUTSIDE candidate_dir but is
    # a symlink whose real content lives INSIDE it (rmtree would destroy the
    # actual config content, not just a path entry).
    try:
        resolved = config_path.resolve(strict=False)
    except (ValueError, OSError):
        resolved = lexical

    if not lexical.is_file():
        return None
    if physical.is_relative_to(candidate_dir):
        return physical
    if resolved.is_relative_to(candidate_dir):
        return resolved
    return None


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
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return
    except (tomllib.TOMLDecodeError, UnicodeError, OSError) as exc:
        _module_logger.warning("Failed to read config.toml at %s: %s", config_path, exc)
        return

    loaded = 0
    for key, value in data.items():
        if not key.startswith(_ENV_PREFIX):
            _module_logger.warning("config.toml key %r is not a PIPECAT_HUB_* var; skipping", key)
            continue
        if key in _INVOCATION_SCOPED_KEYS:
            _module_logger.warning(
                "config.toml key %r is invocation-scoped, not machine config; skipping",
                key,
            )
            continue
        if key in os.environ:
            # A real env var or cwd .env already won this key — nothing this
            # config.toml value could do (including a DATA_DIR collision
            # warning below) is actually going to apply, so don't validate
            # or warn about a value that was never eligible to be used.
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
            _module_logger.warning(
                "config.toml key %r has an unsupported value type (table); skipping",
                key,
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
                    colliding_config_path,
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
        _module_logger.info("Loaded %d key(s) from %s", loaded, config_path)
