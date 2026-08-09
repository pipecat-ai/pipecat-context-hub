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

:func:`same_dir`, :func:`is_inside` and :func:`resolution_chain` are the
general filesystem-identity primitives shared by every deletion and trust guard
in this project (``env_loading``'s ``config_collides_with_dir()`` /
``_config_parent_is_trusted()`` and ``cli.py``'s ``_refuse_unsafe_data_dir()``).
They live here rather than in ``env_loading`` because they are path predicates,
not env-var/config-file loading.
"""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path
from typing import Final

# Upper bound on symlink hops followed by :func:`resolution_chain`. Chosen to
# sit above any legitimate layout (the kernel's own ELOOP limit is typically
# 40 on Linux, 32 on macOS) while still terminating on a cycle.
_MAX_SYMLINK_HOPS: Final = 64


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
      well-permissioned directories (``_config_parent_is_trusted``'s concern).

    So this walks ``path`` one component at a time, expanding each symlink by a
    single ``readlink`` (never ``realpath``, which would skip exactly the
    intermediates being looked for) and recording every location it stands on.
    Duplicate locations are recorded once, in first-visit order.

    Best-effort by contract: the walk is bounded by ``max_hops`` and swallows
    per-component ``OSError``, so a cycle or an unreadable component truncates
    the list rather than raising. Callers must therefore treat a location's
    *presence* as evidence and its *absence* as unproven — every consumer here
    keeps an independent endpoint check as its floor.
    """
    try:
        absolute = Path(os.path.abspath(path))
    except (OSError, ValueError):
        return []
    parts = absolute.parts
    if not parts:
        return []

    resolved = Path(parts[0])
    pending = deque(parts[1:])
    visited: list[Path] = []
    seen: set[str] = set()
    hops = 0

    while pending:
        name = pending.popleft()
        if not name or name == os.curdir:
            continue
        if name == os.pardir:
            resolved = resolved.parent
            continue

        current = resolved / name
        marker = str(current)
        if marker not in seen:
            seen.add(marker)
            visited.append(current)

        try:
            is_link = os.path.islink(current)
        except (OSError, ValueError):
            is_link = False
        if not is_link:
            resolved = current
            continue

        hops += 1
        if hops > max_hops:
            break
        try:
            target = Path(os.readlink(current))
        except (OSError, ValueError, NotImplementedError):
            resolved = current
            continue
        if target.anchor:
            # Absolute target: restart from *its* anchor, which on Windows may
            # be a different drive than the one the walk started on.
            resolved = Path(target.anchor)
            pending.extendleft(reversed(target.parts[1:]))
        else:
            # Relative target: resolves against the link's own directory, which
            # is `resolved` — deliberately left untouched.
            pending.extendleft(reversed(target.parts))

    return visited


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
    2. Filesystem identity — walk ``path``'s ancestors and compare
       ``(st_dev, st_ino)`` against ``directory``'s. This is what catches a
       spelling of ``directory`` that names the *same* directory without
       matching character-for-character: a differently-cased spelling on a
       case-insensitive volume (macOS APFS, Windows NTFS), where
       ``Path.resolve()`` preserves the caller's casing so
       ``~/.CONFIG/...`` and ``~/.config/...`` compare unequal while being one
       directory on disk; or a path reached through a symlinked ancestor.
       Skipped when either side doesn't exist (nothing to stat) — which is
       safe, because a directory that doesn't exist cannot be ``rmtree``'d.

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
    return any(same_dir(ancestor, dir_stat) for ancestor in path.parents)


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
