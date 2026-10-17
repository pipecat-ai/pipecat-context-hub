"""Comment-preserving, atomic edit of ``PIPECAT_HUB_EXTRA_REPOS`` in the global config.

The writer never reimplements a trust check: the existing file is read through
``env_loading.read_trusted_config`` and the result is re-parsed through
``env_loading.parse_global_config`` before the rename, so it cannot write a file
the reader would ignore. The module never exits; callers map the errors
(``ConfigWriteRefused``, ``ConfigNotTrusted``) to exit codes.

No lock: two simultaneous invocations can lose one edit (last write wins); the
atomic ``os.replace`` prevents a torn file.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from pipecat_context_hub.shared.env_loading import (
    ConfigNotTrusted,
    parse_global_config,
    read_trusted_config,
    resolve_global_config_path,
)
from pipecat_context_hub.shared.paths import redact_home_in_text

__all__ = [
    "EXTRA_REPOS_KEY",
    "ConfigNotTrusted",
    "ConfigWriteRefused",
    "ExtraReposUpdate",
    "update_extra_repos",
]

_logger = logging.getLogger(__name__)

EXTRA_REPOS_KEY = "PIPECAT_HUB_EXTRA_REPOS"


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
    value = data.get(EXTRA_REPOS_KEY)
    if value is None:
        return []
    if isinstance(value, str):
        # Same CSV rule as the reader (`shared/config.py::_split_csv_env`).
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise ConfigWriteRefused(f"{EXTRA_REPOS_KEY} must be an array of strings")
        return [v.strip() for v in value if v.strip()]
    raise ConfigWriteRefused(f"{EXTRA_REPOS_KEY} must be an array of strings or a CSV string")


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
    stays missing. Raises :class:`ConfigWriteRefused` (non-array/non-string
    value, invalid TOML, failed re-parse, no resolvable path) or
    :class:`ConfigNotTrusted` (propagated unchanged from the reader's seam).
    """
    path = resolve_global_config_path()
    if path is None:
        raise ConfigWriteRefused("no config file path could be resolved")
    add_list = list(add)
    remove_list = list(remove)

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
        if reparsed.get(EXTRA_REPOS_KEY) != after or got_rest != expected_rest:
            raise ConfigWriteRefused("re-parse of the edited config differs from the intended edit")

        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return ExtraReposUpdate(before, after, True, path)
