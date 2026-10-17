"""Comment-preserving, atomic edit of ``PIPECAT_HUB_EXTRA_REPOS`` in the global config.

The writer never reimplements a trust check: the existing file is read through
``env_loading.read_trusted_config``; a file that does not exist yet is vetted by
``env_loading.check_config_write_target`` (the reader's own path-level
predicates) before anything is created; and the result is re-parsed through
``env_loading.parse_global_config`` and the reader's list rule
(``env_loading.config_list_value``) before the rename. So it cannot create or
replace a file the reader would ignore, nor write a list the reader would read
differently. The module never exits; callers map the errors
(``ConfigWriteRefused``, ``env_loading.ConfigNotTrusted``, and ``OSError`` from
the write itself) to exit codes.

No lock: two simultaneous invocations can lose one edit (last write wins); the
atomic ``os.replace`` prevents a torn file.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from pipecat_context_hub.shared.config import EXTRA_REPOS_ENV
from pipecat_context_hub.shared.env_loading import (
    check_config_write_target,
    config_list_value,
    parse_global_config,
    read_trusted_config,
    resolve_global_config_path,
)
from pipecat_context_hub.shared.paths import redact_home_in_text
from pipecat_context_hub.shared.repo_catalog import is_valid_repo_slug

__all__ = [
    "EXTRA_REPOS_KEY",
    "ConfigWriteRefused",
    "ExtraReposUpdate",
    "update_extra_repos",
]

_logger = logging.getLogger(__name__)

EXTRA_REPOS_KEY = EXTRA_REPOS_ENV


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
    if EXTRA_REPOS_KEY not in data:
        return []
    value = data[EXTRA_REPOS_KEY]
    if not isinstance(value, (str, list)):
        raise ConfigWriteRefused(
            f"{EXTRA_REPOS_KEY} must be an array of strings or a CSV string, "
            f"not {type(value).__name__}"
        )
    current = config_list_value(EXTRA_REPOS_KEY, value)
    if current is None:
        raise ConfigWriteRefused(
            f"{EXTRA_REPOS_KEY} holds an array the loader ignores (mixed or nested "
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
    drop = {s.casefold() for s in remove}
    result = [s for s in current if s.casefold() not in drop]
    seen = {s.casefold() for s in result}
    for slug in add:
        if slug.casefold() not in seen:
            result.append(slug)
            seen.add(slug.casefold())
    return result


def _ensure_dir(directory: Path) -> None:
    """Create missing directories 0700 (existing ones are left alone)."""
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

    arr = tomlkit.array()
    arr.extend(after)
    doc[EXTRA_REPOS_KEY] = arr
    payload = tomlkit.dumps(doc).encode("utf-8")

    # A symlink keeps working: write beside, and replace, the resolved target.
    target = Path(os.path.realpath(path))
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
        expected_rest = {k: v for k, v in original.items() if k != EXTRA_REPOS_KEY}
        got_rest = {k: v for k, v in reparsed.items() if k != EXTRA_REPOS_KEY}
        # The list is checked as the reader will read it, not as raw TOML.
        got_list = config_list_value(EXTRA_REPOS_KEY, reparsed.get(EXTRA_REPOS_KEY, []))
        if got_list != after or not _same(got_rest, expected_rest):
            raise ConfigWriteRefused("re-parse of the edited config differs from the intended edit")

        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return ExtraReposUpdate(before, after, True, path)
