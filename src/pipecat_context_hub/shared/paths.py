"""Path helpers: home redaction for logs, plus filesystem-identity predicates.

Startup telemetry and one-shot CLI error messages are included in the "share
this with maintainers" guidance, so stripping usernames out of absolute paths
keeps routine bug reports from leaking local filesystem layout.

Two helpers, because the leak has two shapes:

- :func:`redact_home` — the *whole argument* is a path (banner / cache-dir
  sites). A prefix match suffices.
- :func:`redact_home_in_text` — the path is embedded *inside* an arbitrary
  message string (e.g. a ``{exc}`` rendering whose ``__str__`` carries an
  absolute path). A prefix-only helper cannot redact this, so the home dir is
  matched and replaced wherever it appears in the text.

Lives in ``shared/`` (not ``cli.py``) because ``cli.py`` imports ``cli_query``;
importing the helper back from ``cli.py`` would be a cycle. ``shared/`` peers
import neither CLI module, so the layer stays leaf-level.

Terminal safety (escaping non-printable text) is not a path concern; it
lives in ``shared/terminal.py``.

:func:`same_dir`, :func:`is_inside`, :func:`resolution_chain` and
:func:`lookup_target` are the general filesystem-identity primitives shared by
every deletion, trust and write guard in this project (``env_loading``'s
``config_collides_with_dir()`` / ``_config_parent_refusal()``, ``cli.py``'s
``_refuse_unsafe_data_dir()``, and ``config_writer``'s write target).
They live here rather than in ``env_loading`` because they are path predicates,
not env-var/config-file loading.
"""

from __future__ import annotations

import os
import stat as stat_module
from collections import deque
from pathlib import Path
from typing import Final, NamedTuple

from pipecat_context_hub.shared.terminal import shown

# Upper bound on symlink hops followed by `_walk`, the one walk behind
# :func:`resolution_chain` and :func:`lookup_target`. Chosen to
# sit above any legitimate layout (the kernel's own ELOOP limit is typically
# 40 on Linux, 32 on macOS) while still terminating on a cycle.
_MAX_SYMLINK_HOPS: Final = 64

# Queued by `_walk` after a symlink target that ends in a separator (`name/`,
# `name/.`): the kernel then requires what precedes it to be a directory.
# NUL can never be a path component, so it cannot collide with a real name.
_MUST_BE_DIR: Final = "\0"


def _ends_in_separator(raw: str) -> bool:
    """True when the raw link text ends in a separator (or `/.`), which ``Path`` drops."""
    seps = [os.sep] + ([os.altsep] if os.altsep else [])
    return any(raw.endswith(sep) or raw.endswith(sep + os.curdir) for sep in seps)


def is_reparse_link(path: Path) -> bool:
    """True when ``path`` is a symbolic link *or* a Windows directory junction.

    ``Path.is_symlink()`` / ``os.path.islink()`` are not sufficient on Windows:
    a directory junction (``mklink /J``) is a reparse point the kernel follows
    exactly like a symlink, but whose reparse tag is
    ``IO_REPARSE_TAG_MOUNT_POINT``, not ``…_SYMLINK`` — so both link predicates
    report ``False`` for it. Two guards in this project depend on the
    distinction:

    * :func:`resolution_chain` — a junctioned component that is not expanded is
      walked as an ordinary directory, so the location it actually resolves to
      never enters the chain. A config path can then enter the data directory
      through one junction and leave through another with both walked endpoints
      outside it, and ``refresh --reset-index`` deletes or severs the live
      config path anyway.
    * ``cli.py``'s ``_delete_local_index_storage`` — ``rmtree`` destroys a
      junctioned ``PIPECAT_HUB_DATA_DIR``'s *target* through the junction; if
      the junction is not recognised as a link the target is never recreated,
      leaving it dangling so ``IndexStore``'s follow-up
      ``mkdir(parents=True, exist_ok=True)`` raises ``FileExistsError``.

    ``os.path.isjunction`` exists from Python 3.12; on 3.11 (this project's
    floor) the same test is done directly against the ``lstat`` reparse tag,
    which is what CPython's own implementation does. Both are absent on POSIX,
    where the answer is always ``False``.

    Never raises: an unstattable path is simply "not a link", matching the
    crash-safety contract of every caller.
    """
    try:
        if path.is_symlink():
            return True
    except (ValueError, OSError):
        return False

    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        try:
            return bool(isjunction(path))
        except (ValueError, OSError):
            return False

    # Python 3.11 fallback (Windows only — st_reparse_tag does not exist
    # elsewhere, so this degrades to False on POSIX).
    mount_point_tag = getattr(stat_module, "IO_REPARSE_TAG_MOUNT_POINT", None)
    if mount_point_tag is None:
        return False
    try:
        reparse_tag = getattr(os.lstat(path), "st_reparse_tag", None)
    except (ValueError, OSError):
        return False
    return bool(reparse_tag == mount_point_tag)


def resolution_chain(path: Path, *, max_hops: int = _MAX_SYMLINK_HOPS) -> list[Path]:
    """Every filesystem location visited while resolving ``path``, root-first.

    Neither of the two obvious spellings exposes this. ``os.path.abspath``
    reports only the *lexical* path; ``os.path.realpath`` reports only the
    *final* target. A path that reaches its file through a chain of directory
    symlinks passes through intermediate locations that are neither — and each
    of those is a real, deletable, replaceable directory entry:

    * a hop deleted by ``rmtree`` makes the path stop resolving, even though
      both endpoints live outside the deleted tree
      (``config_collides_with_dir``'s concern);
    * a hop whose *holding directory* another local account can write is
      enough to redirect the whole lookup, even though both endpoints sit in
      well-permissioned directories (``_config_parent_refusal``'s concern).

    So this walks ``path`` one component at a time, expanding each symlink by a
    single ``readlink`` (never ``realpath``, which would skip exactly the
    intermediates being looked for) and recording every location it stands on.
    Duplicate locations are recorded once, in first-visit order. "Symlink" here
    means :func:`is_reparse_link`, so a Windows directory junction — which the
    kernel follows identically but no link predicate reports — is expanded too;
    ``os.readlink`` reads a junction's target on Windows just as it does a
    symlink's.

    Best-effort by contract: the walk is bounded by ``max_hops`` and swallows
    per-component ``OSError``, so a cycle or an unreadable component truncates
    the list rather than raising. Callers must therefore treat a location's
    *presence* as evidence and its *absence* as unproven — every consumer here
    keeps an independent endpoint check as its floor.

    The seed is absolutised but deliberately **not** normalized. Anything that
    collapses ``..`` up front (``os.path.abspath``, ``Path.resolve``) does it
    *lexically*, before a single symlink is expanded — the opposite of what the
    kernel does. A ``..`` that follows a directory symlink pops out of the
    link's **target**, not out of the link's own parent, so a pre-normalized
    seed yields a chain of locations the lookup never visits (and, worse, omits
    the symlinks it does traverse). The loop below handles ``os.pardir`` itself,
    at the right point in the walk, which is what makes the result agree with
    ``os.path.realpath`` on the endpoint.
    """
    return _walk(path, max_hops).visited


class _Walk(NamedTuple):
    visited: list[Path]
    endpoint: Path | None
    problem: str | None


def _walk(path: Path, max_hops: int) -> _Walk:
    """The one hop-by-hop walk behind :func:`resolution_chain` and :func:`lookup_target`.

    ``visited`` is every location stood on (root-first, deduplicated);
    ``endpoint`` is where the walk ends (``None`` when it stopped on the hop
    bound); ``problem`` names the first step at which the kernel's lookup of
    the same path would fail where this walk carries on (a ``..`` after a
    component that is not an existing directory, a symlink target ending in a
    separator whose endpoint is not an existing directory, the hop bound, an
    unreadable link). :func:`resolution_chain` ignores ``problem`` (best-effort by
    contract); :func:`lookup_target` refuses on it.
    """
    try:
        raw = Path(path)
        absolute = raw if raw.is_absolute() else Path(os.getcwd()) / raw
    except (OSError, ValueError):
        # `os.getcwd()` raises when the working directory has been unlinked.
        return _Walk([], None, "the working directory cannot be resolved")
    parts = absolute.parts
    if not parts:
        return _Walk([], None, "the path is empty")

    resolved = Path(parts[0])
    pending = deque(parts[1:])
    visited: list[Path] = []
    seen: set[str] = set()
    hops = 0
    problem: str | None = None

    while pending:
        name = pending.popleft()
        if name == _MUST_BE_DIR:
            # A link target `name/` makes the kernel demand a directory there.
            # Only the endpoint matters: an intermediate component must be a
            # directory anyway (a creator makes it one), and a `..` after it is
            # checked below. `Path(...).parts` drops the separator, so without
            # this a dangling `link -> "cfg.toml/"` walked to a creatable file
            # the kernel's open(link) then fails with ENOTDIR.
            final = all(n in ("", os.curdir, _MUST_BE_DIR) for n in pending)
            if problem is None and final and not resolved.is_dir():
                problem = (
                    f"a symlink target ends in a separator, so the lookup needs {resolved} "
                    "to be an existing directory"
                )
            continue
        if not name or name == os.curdir:
            continue
        if name == os.pardir:
            # The kernel looks `..` up *inside* `resolved`, so the lookup fails
            # (ENOENT / ENOTDIR) unless `resolved` is an existing directory;
            # popping lexically here is what `os.path.realpath` does instead.
            if problem is None and not resolved.is_dir():
                problem = f"a `..` follows {resolved}, which is not an existing directory"
            resolved = resolved.parent
            continue

        current = resolved / name
        marker = str(current)
        if marker not in seen:
            seen.add(marker)
            visited.append(current)

        # `is_reparse_link`, not `os.path.islink`: on Windows a directory
        # junction is followed by the kernel exactly like a symlink but is not
        # reported by either link predicate, so gating expansion on `islink`
        # walked the junction as an ordinary directory and never visited what
        # it resolves to — silently voiding both guards built on this chain.
        if not is_reparse_link(current):
            resolved = current
            continue

        hops += 1
        if hops > max_hops:
            return _Walk(visited, None, problem or f"more than {max_hops} symlink hops")
        try:
            raw_target = os.readlink(current)
            target = Path(raw_target)
        except (OSError, ValueError, NotImplementedError):
            problem = problem or f"the symlink {current} cannot be read"
            resolved = current
            continue
        if _ends_in_separator(raw_target):
            pending.appendleft(_MUST_BE_DIR)  # checked once the target's own parts are walked
        if target.is_absolute():
            # Fully-qualified target: restart from *its* anchor, which on
            # Windows may be a different drive than the one the walk started on.
            resolved = Path(target.anchor)
            pending.extendleft(reversed(target.parts[1:]))
        elif target.anchor:
            # Windows-only third case: an anchor that is not a full absolute
            # path — root-relative (`\foo`, anchor `\`, no drive) or
            # drive-relative (`C:rel`, anchor `C:`, no root). Testing
            # `target.anchor` alone treated both as absolute and dropped the
            # originating drive. Joining against `resolved` is what supplies
            # the missing half: pathlib takes the drive from the left operand
            # for a root-relative target, and treats a same-drive
            # drive-relative target as relative to it.
            combined = resolved / target
            resolved = Path(combined.anchor)
            pending.extendleft(reversed(combined.parts[1:]))
        else:
            # Relative target: resolves against the link's own directory, which
            # is `resolved` — deliberately left untouched.
            pending.extendleft(reversed(target.parts))

    return _Walk(visited, resolved, problem)


class LookupMismatch(ValueError):
    """:func:`lookup_target` cannot prove where the kernel's lookup of a path lands."""


def lookup_target(path: Path, *, max_hops: int = _MAX_SYMLINK_HOPS) -> Path:
    """The location ``open(path)`` resolves to, derived hop by hop as the kernel walks it.

    For a writer that must create or replace exactly the file a later
    ``open(path)`` reads. ``os.path.realpath`` is not that: it pops a ``..``
    lexically when the component before it is missing or not a directory
    (``nope/..``), at any hop, including inside a symlink's target, while the
    kernel fails the same lookup with ENOENT / ENOTDIR. This walks the path
    exactly as :func:`resolution_chain` does and raises :class:`LookupMismatch`
    at any such ``..`` (and on the hop bound or an unreadable link).
    Components that do not exist yet are fine, so long as no ``..`` follows
    one: a creator makes them, and the kernel then walks the same names.

    POSIX semantics. Windows normalizes ``..`` lexically before the lookup,
    exactly as ``realpath`` does, so there it returns ``realpath(path)``.
    """
    if os.name != "posix":
        return Path(os.path.realpath(path))
    walk = _walk(path, max_hops)
    if walk.problem is not None or walk.endpoint is None:
        raise LookupMismatch(walk.problem or "the path cannot be walked")
    return walk.endpoint


def same_dir(path: Path, other_stat: os.stat_result) -> bool:
    """True when ``path`` is the same on-disk directory as ``other_stat``.

    The second parameter is named ``other_stat``, not ``dir_stat``: the
    relation is symmetric (``samestat`` compares ``(st_dev, st_ino)`` both
    ways), and callers legitimately pass either side's stat, so a
    directionality-implying name misdescribes the contract.

    Public because it is the single filesystem-identity primitive shared by
    every deletion guard in this project: ``env_loading``'s
    ``config_collides_with_dir()`` and ``cli.py``'s
    ``_refuse_unsafe_data_dir()``. Both must compare ``(st_dev, st_ino)``
    rather than path strings, because ``Path.resolve()`` preserves the
    caller's casing on case-insensitive volumes (macOS APFS, Windows NTFS) —
    so ``/Users/Varun`` and ``/Users/varun`` compare unequal while naming one
    directory that ``shutil.rmtree`` would happily delete.
    """
    try:
        return os.path.samestat(path.stat(), other_stat)
    except (OSError, ValueError):
        return False


def is_inside(path: Path, directory: Path) -> bool:
    """True when ``path`` lives under ``directory``.

    Two checks, because a path-string comparison alone is not sound:

    1. Lexical containment (``is_relative_to``) — works for paths that don't
       exist yet, and is the common case.
    2. Filesystem identity — compare ``(st_dev, st_ino)`` of ``path`` *and*
       each of its ancestors against ``directory``'s. This is what catches a
       spelling of ``directory`` that names the *same* directory without
       matching character-for-character: a differently-cased spelling on a
       case-insensitive volume (macOS APFS, Windows NTFS), where
       ``Path.resolve()`` preserves the caller's casing so
       ``~/.CONFIG/...`` and ``~/.config/...`` compare unequal while being one
       directory on disk; or a path reached through a symlinked ancestor.
       Skipped when either side doesn't exist (nothing to stat) — which is
       safe, because a directory that doesn't exist cannot be ``rmtree``'d.

    ``path`` itself is tested before its ancestors because ``path.parents``
    never yields ``path``, while check 1's ``is_relative_to`` *is* reflexive —
    so without it the two checks disagreed about the ``path == directory``
    case, and only for differently-spelled equality. A ``resolution_chain``
    location that *is* the candidate directory under a case-insensitive-volume
    alias (or any other same-inode spelling) matched neither check, so
    ``config_collides_with_dir()`` reported no collision for a location
    ``rmtree(candidate_dir)`` removes outright. "Inside" here means "destroyed
    by deleting ``directory``", and the directory itself qualifies.

    ``directory`` is stat'd here rather than accepted pre-stat'd from the
    caller. An earlier revision took a ``dir_stat`` cache for callers testing
    several paths against one directory; it saved a handful of syscalls on a
    once-per-invocation path and, in exchange, gave this deletion guard a
    silent fail-open mode whenever a caller's cached stat drifted from the
    ``directory`` argument beside it. Not a trade worth keeping.
    """
    if path.is_relative_to(directory):
        return True
    try:
        dir_stat = directory.stat()
    except (OSError, ValueError):
        return False
    return any(same_dir(location, dir_stat) for location in (path, *path.parents))


def redact_home(path: Path | str) -> str:
    """Replace the user's home-directory prefix with ``~`` for logs.

    Startup telemetry is included in the "share this with maintainers"
    guidance, so stripping usernames out of absolute paths keeps routine
    bug reports from leaking local filesystem layout.
    """
    try:
        s = str(path)
        home = str(Path.home())
        if home and (s == home or s.startswith(home + os.sep)):
            return "~" + s[len(home) :]
        return s
    except Exception:
        return str(path)


def shown_path(path: Path | str) -> str:
    """A configured path, whole, for a message: home-redacted, flagged when not printable.

    The one presentation rule for naming a configured path (``config.toml``, a
    cwd ``.env``, the data dir...): :func:`redact_home` (prefix match, since the
    argument is the whole path) then :func:`terminal.shown`. Terminal safety is
    not this helper's job: the sinks escape every message (``shared/terminal``).
    A path embedded in other text (an ``OSError`` message) goes through
    :func:`redact_home_in_text` instead.
    """
    return shown(redact_home(path))


def redact_home_in_text(text: str) -> str:
    """Replace home-directory occurrences embedded anywhere in ``text`` with ``~``.

    Unlike :func:`redact_home` (whole-arg-is-a-path, prefix match), this handles
    a path embedded mid-message — e.g. an absolute path inside a ``{exc}``
    rendering such as ``"Error: ... '/Users/x/.../chroma.sqlite3'"``.

    Guards against partial-username over-match: a naive ``text.replace(home,
    "~")`` would corrupt ``/Users/xavier`` when home is ``/Users/x``. Only the
    exact ``home`` token and the ``home + os.sep`` prefix-within-a-path form are
    replaced. Returns the original text on any failure, mirroring
    :func:`redact_home`'s defensive style.
    """
    try:
        home = str(Path.home())
        # A degenerate home of exactly the separator (e.g. `HOME=/`, seen in
        # some minimal/root containers) makes `home + os.sep` a bare `"//"`,
        # which occurs *inside* every `https://` URL — including the
        # report-hint URLs this function's own callers append. Redacting it
        # would silently mangle those URLs (`https://` -> `https:~/`) instead
        # of a real path, so treat it as nothing-to-redact rather than
        # matching `os.sep` doubled.
        if not home or home == os.sep:
            return text
        # Replace `home + sep` (a path continuing past home — the common case)
        # with `~/`. Anchoring on the trailing separator avoids over-matching a
        # longer sibling like `/Users/xavier` when home is `/Users/x`: that
        # path has no separator right after `home`, so it is left intact.
        result = text.replace(home + os.sep, "~" + os.sep)
        # Handle a bare `home` occurrence (no trailing separator) only when it
        # is the exact whole message — the `== home` case. A raw substring
        # replace here would reintroduce the partial-username over-match, so it
        # is deliberately scoped to equality.
        if result == home:
            result = "~"
        return result
    except Exception:
        return text
