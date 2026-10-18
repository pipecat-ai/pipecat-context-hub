"""Comment-preserving, atomic edit of ``PIPECAT_HUB_EXTRA_REPOS`` in the global config.

The writer never reimplements a trust check: the existing file is read through
``env_loading.read_trusted_config``; a file that does not exist yet is vetted by
``env_loading.check_config_write_target`` (the reader's own path-level
predicates) before anything is created; the file written is the one the
reader's ``open(path)`` opens, because its location comes from
``paths.lookup_target`` (the kernel's hop-by-hop walk, not ``realpath``); and
the result is re-parsed through
``env_loading.parse_global_config`` and the reader's list rule
(``env_loading.config_list_value``) before the rename. So it cannot create or
replace a file the reader would ignore, nor write a list the reader would read
differently. Comments and layout survive an edit: an existing array of strings
is edited in place (an untouched element keeps its trailing comment, own-line
comments inside the array stay, a removed element's trailing comment goes with
it), and a file with CRLF line endings stays CRLF. A CSV string, a non-string
array or an absent key becomes a fresh array. The module never exits; callers map the errors
(``ConfigWriteRefused``, ``env_loading.ConfigNotTrusted``, and ``OSError`` from
the write itself) to exit codes.

No lock: two simultaneous invocations can lose one edit (last write wins); the
atomic ``os.replace`` prevents a torn file. The rename target is re-derived
from ``path`` immediately before ``os.replace`` and the edit refused if it
moved (a symlink retargeted while the file was checked), which narrows but
cannot close the window: without a lock or an fd-relative rename, a retarget
in the instant between that re-check and the rename still lands the file at
the old target. That residual race is an accepted limit.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Array
from tomlkit.toml_document import TOMLDocument

from pipecat_context_hub.shared.env_keys import EXTRA_REPOS_ENV
from pipecat_context_hub.shared.env_loading import (
    check_config_write_target,
    config_list_value,
    parse_global_config,
    read_trusted_config,
    resolve_global_config_path,
)
from pipecat_context_hub.shared.paths import (
    LookupMismatch,
    lookup_target,
    redact_home_in_text,
    shown_path,
)
from pipecat_context_hub.shared.repo_slug import is_valid_repo_slug, slug_key

__all__ = [
    "ConfigWriteRefused",
    "ExtraReposUpdate",
    "update_extra_repos",
]

_logger = logging.getLogger(__name__)

_BARE_LF_RE = re.compile(r"(?<!\r)\n")


class ConfigWriteRefused(Exception):
    """The writer declined to edit the config; the file is untouched."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ExtraReposUpdate:
    """Outcome of :func:`update_extra_repos`.

    ``before`` / ``after`` are the ``PIPECAT_HUB_EXTRA_REPOS`` lists in the
    config file; ``changed`` is False when nothing was written.
    """

    before: list[str]
    after: list[str]
    changed: bool
    path: Path


def _current_list(data: dict[str, object]) -> list[str]:
    """The configured list, read by the reader's own rule (``config_list_value``).

    Editable shapes are a CSV string and an array the loader reads. A
    non-string scalar is refused (parent plan Decision 8) even though the
    loader stringifies it, and so is any value the loader skips (a mixed or
    nested array, an element with a comma, a table, a date): the writer cannot
    know what such a value is meant to hold.
    """
    if EXTRA_REPOS_ENV not in data:
        return []
    value = data[EXTRA_REPOS_ENV]
    if not isinstance(value, (str, list)):
        raise ConfigWriteRefused(
            f"{EXTRA_REPOS_ENV} must be an array of strings or a CSV string, "
            f"not {type(value).__name__}"
        )
    current = config_list_value(EXTRA_REPOS_ENV, value)
    if current is None:
        raise ConfigWriteRefused(
            f"{EXTRA_REPOS_ENV} holds an array the loader ignores (mixed or nested "
            "elements, or an element containing a comma); fix it by hand"
        )
    return current


def _same(a: object, b: object) -> bool:
    """Structural equality of parsed TOML values in which NaN equals NaN.

    Plain ``==`` is wrong for the re-parse check: ``float('nan') != float('nan')``,
    so any other key holding ``nan`` would make every edit look corrupted.
    """
    if isinstance(a, float) and isinstance(b, float):
        return a == b or (math.isnan(a) and math.isnan(b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def _compute(current: list[str], add: Iterable[str], remove: Iterable[str]) -> list[str]:
    drop = {slug_key(s) for s in remove}
    result = [s for s in current if slug_key(s) not in drop]
    seen = {slug_key(s) for s in result}
    for slug in add:
        if slug_key(slug) not in seen:
            result.append(slug)
            seen.add(slug_key(slug))
    return result


def _edit_in_place(doc: TOMLDocument, after: list[str], remove: Iterable[str]) -> None:
    """Set ``PIPECAT_HUB_EXTRA_REPOS`` to ``after``, editing an existing array in place.

    ``after`` is ``_compute``'s result: the current list minus ``remove``, then
    the added slugs. An existing array of strings is edited in place (so its
    comments and layout survive): elements matching ``remove`` (by the
    reader's rule: stripped, then :func:`slug_key`) are deleted, the new slugs appended.
    Anything else (absent key, CSV string, a non-string array the loader
    stringifies) is replaced by a fresh array. The caller's re-parse check is
    what proves the result reads back as ``after``.
    """
    existing = doc.get(EXTRA_REPOS_ENV)
    if isinstance(existing, Array) and all(isinstance(v, str) for v in existing):
        drop = {slug_key(s) for s in remove}
        for index in reversed(range(len(existing))):
            if slug_key(str(existing[index]).strip()) in drop:
                del existing[index]
        kept = {slug_key(str(v).strip()) for v in existing}
        for slug in after:
            if slug_key(slug) not in kept:
                existing.append(slug)
                kept.add(slug_key(slug))
        return
    arr = tomlkit.array()
    arr.extend(after)
    doc[EXTRA_REPOS_ENV] = arr


def _same_newlines(original: str | None, rendered: str) -> str:
    """Keep a CRLF file CRLF: tomlkit writes a newly added line with a bare LF."""
    if original and "\r\n" in original and original.count("\n") == original.count("\r\n"):
        return _BARE_LF_RE.sub("\r\n", rendered)
    return rendered


def _require_same_target(path: Path, target: Path) -> None:
    """Raise :class:`ConfigWriteRefused` unless ``path`` still resolves to ``target``."""
    try:
        now = lookup_target(path)
    except LookupMismatch as exc:
        raise ConfigWriteRefused(
            f"{shown_path(path)} changed while it was being edited: {redact_home_in_text(str(exc))}"
        ) from exc
    if now != target:
        raise ConfigWriteRefused(
            f"{shown_path(path)} changed while it was being edited (it now "
            f"leads to {shown_path(now)}, not {shown_path(target)})"
        )


def _ensure_dir(directory: Path) -> None:
    """Create missing directories 0700 (existing ones are left alone).

    A directory that appears concurrently (``FileExistsError``) is not trusted
    here: the caller re-runs the reader's path predicates
    (``check_config_write_target``) once every directory exists, before any
    file is created inside them.
    """
    missing: list[Path] = []
    cur = directory
    while not cur.exists():
        missing.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        with contextlib.suppress(OSError):
            d.chmod(0o700)  # umask may have narrowed mkdir's mode; make it exact


def update_extra_repos(*, add: Iterable[str] = (), remove: Iterable[str] = ()) -> ExtraReposUpdate:
    """Add and/or remove slugs in ``PIPECAT_HUB_EXTRA_REPOS`` of the global config.

    Slugs compare case-insensitively. Nothing is written when the list would
    not change (idempotent add, remove of an absent slug), so a missing file
    stays missing. Raises :class:`ConfigWriteRefused` (a value it cannot edit,
    invalid TOML, a malformed slug to add, failed re-parse, no resolvable
    path), ``env_loading.ConfigNotTrusted`` (propagated unchanged from the
    reader's seam, or from ``check_config_write_target`` for a file that does
    not exist yet), or ``OSError`` from creating the directory or writing and
    renaming the file (the original is untouched and the temp file removed).
    """
    path = resolve_global_config_path()
    if path is None:
        raise ConfigWriteRefused("no config file path could be resolved")
    add_list = list(add)
    remove_list = list(remove)
    for slug in add_list:
        # The reader splits on commas and strips, so a slug outside the grammar
        # could not round-trip; the CLI validates first, this is the floor.
        if not is_valid_repo_slug(slug):
            raise ConfigWriteRefused(f"{slug!r} is not a valid owner/repo slug")

    # The file written is the file the reader's open(path) opens: the target
    # comes from the kernel's hop-by-hop walk, never from `realpath`, which
    # collapses `nope/..` lexically (also inside a symlink's target) where the
    # kernel's lookup fails. A symlink keeps working: the target is its end.
    try:
        target = lookup_target(path)
    except LookupMismatch as exc:
        raise ConfigWriteRefused(
            f"{shown_path(path)}: {redact_home_in_text(str(exc))}, so the "
            "file written would not be the one the loader reads"
        ) from exc

    text = read_trusted_config(path)  # ConfigNotTrusted propagates
    if text is None:
        original: dict[str, object] = {}
        doc = tomlkit.document()
    else:
        try:
            original = tomllib.loads(text)
            doc = tomlkit.parse(text)
        except (tomllib.TOMLDecodeError, TOMLKitError) as exc:
            raise ConfigWriteRefused(
                f"existing config is not valid TOML: {redact_home_in_text(str(exc))}"
            ) from exc

    before = _current_list(original)
    after = _compute(before, add_list, remove_list)
    if after == before:
        return ExtraReposUpdate(before, after, False, path)

    _edit_in_place(doc, after, remove_list)
    payload = _same_newlines(text, tomlkit.dumps(doc)).encode("utf-8")

    existing_mode: int | None = None
    if text is not None:
        with contextlib.suppress(OSError):
            existing_mode = os.stat(target).st_mode & 0o7777
    if text is None:
        # No file yet, so the reader ran no path-level check: vet the lookup
        # before creating anything (a dangling or foreign-owned symlink, an
        # untrusted directory on the chain). ConfigNotTrusted propagates.
        check_config_write_target(path)
    _ensure_dir(target.parent)
    if text is None:
        # Again, now that every directory exists: one that appeared between the
        # check above and mkdir (FileExistsError) may be another account's.
        # Refuse before creating even the temp file inside it.
        check_config_write_target(path)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, existing_mode if existing_mode is not None else 0o600)

        # Verify through the reader's own seam before the rename.
        try:
            reparsed = parse_global_config(tmp)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigWriteRefused(
                f"re-parse of the edited config failed: {redact_home_in_text(str(exc))}"
            ) from exc
        expected_rest = {k: v for k, v in original.items() if k != EXTRA_REPOS_ENV}
        got_rest = {k: v for k, v in reparsed.items() if k != EXTRA_REPOS_ENV}
        # The list is checked as the reader will read it, not as raw TOML.
        got_list = config_list_value(EXTRA_REPOS_ENV, reparsed.get(EXTRA_REPOS_ENV, []))
        if got_list != after or not _same(got_rest, expected_rest):
            raise ConfigWriteRefused("re-parse of the edited config differs from the intended edit")

        # The file replaced must be the one open(path) reaches at the rename,
        # not the one it reached before the trust checks: re-walk now and
        # refuse if a hop was retargeted in between (residual race: module
        # docstring).
        _require_same_target(path, target)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return ExtraReposUpdate(before, after, True, path)
