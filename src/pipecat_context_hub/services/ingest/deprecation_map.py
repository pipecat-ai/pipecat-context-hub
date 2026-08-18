"""Build and persist a deprecation map from the pipecat deprecation registry.

The pipecat framework ships a machine-readable registry at
``scripts/deprecations/deprecations.json``, generated from the ``..
deprecated::`` directives and PEP 702 ``@deprecated`` decorators in its source
(see pipecat's ``scripts/deprecations/``). It is the single source of truth for
what is deprecated, since when, until when, the replacement, and how the two
relate. Each record carries ``subject``, ``module``, ``kind``, ``deprecated_in``,
``removed_in``, ``relation``, ``replacement``, and a canonical ``message``.

This module loads that registry into a :class:`DeprecationMap` for fast,
exact-ish symbol lookup by the ``check_deprecation`` tool. Earlier versions
parsed deprecation prose out of GitHub release notes and CHANGELOG headings
heuristically; that approach produced false positives (current APIs reported as
deprecated) and has been retired in favor of the registry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from packaging.version import InvalidVersion, Version

from pipecat_context_hub.shared.types import DeprecationStatus
from pipecat_context_hub.shared.versioning import parse_release_version

logger = logging.getLogger(__name__)

# Locations within a pipecat checkout.
REGISTRY_RELATIVE_PATH = Path("scripts") / "deprecations" / "deprecations.json"
REMOVALS_RELATIVE_PATH = Path("scripts") / "deprecations" / "removals.json"


@dataclass
class DeprecationEntry:
    """A single deprecated symbol and its replacement.

    Mirrors one record from pipecat's ``deprecations.json`` registry. ``old_path``
    is the deprecated symbol (registry ``subject``); ``new_path`` is its
    replacement, or ``None`` when the deprecation has no replacement.
    """

    old_path: str
    new_path: str | None = None
    deprecated_in: str | None = None
    removed_in: str | None = None
    note: str = ""
    kind: str | None = None
    relation: str | None = None
    location: str | None = None
    """Source file of the deprecation marker, relative to the pipecat repo root (registry ``location``)."""
    status: DeprecationStatus = "deprecated"
    """Lifecycle: ``"deprecated"`` (still present) or ``"removed"`` (from removals.json)."""
    announced_removed_in: str | None = None
    """For removed symbols: the version the directive originally promised removal in
    (may differ from ``removed_in`` if the removal slipped). ``None`` for active
    deprecations, where ``removed_in`` is itself the announced/planned version."""


@dataclass
class DeprecationMap:
    """Map of deprecated symbols with fuzzy lookup.

    Entries are keyed by the registry ``subject`` (e.g. ``ResampyResampler``,
    ``pipecat.services.grok.llm``, or ``AnthropicLLMService.InputParams``).
    Non-module symbols are additionally keyed by their fully-qualified path
    (``<module>.<subject>``) so both bare and qualified queries resolve.
    """

    entries: dict[str, DeprecationEntry] = field(default_factory=dict)
    pipecat_commit_sha: str = ""

    def check(self, symbol: str) -> DeprecationEntry | None:
        """Look up whether a symbol is deprecated.

        Matches:
        - Exact: the queried symbol is itself a deprecated key — a bare
          ``ResampyResampler``, a fully-qualified
          ``pipecat.audio.resamplers.resampy_resampler.ResampyResampler``, or a
          deprecated module path ``pipecat.services.grok.llm``.
        - Prefix: the queried symbol is *nested under* a deprecated key — e.g.
          ``pipecat.services.grok.llm.GrokLLMService`` resolves to the deprecated
          ``pipecat.services.grok.llm`` module, and a method on a deprecated class
          resolves to that class.

        It deliberately does NOT match *ancestors*. Querying ``pipecat.services``
        or ``pipecat.services.openai.llm`` must report not-deprecated even though a
        submodule moved or a member within it is deprecated — reporting a current
        package/module as deprecated (with some descendant's replacement) is the
        worst failure mode for this tool.
        """
        if symbol in self.entries:
            return self.entries[symbol]
        for key, entry in self.entries.items():
            if symbol.startswith(key + "."):
                return entry
        return None

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict."""
        return {
            "pipecat_commit_sha": self.pipecat_commit_sha,
            "entries": {
                k: {
                    "old_path": e.old_path,
                    "new_path": e.new_path,
                    "deprecated_in": e.deprecated_in,
                    "removed_in": e.removed_in,
                    "note": e.note,
                    "kind": e.kind,
                    "relation": e.relation,
                    "location": e.location,
                    "status": e.status,
                    "announced_removed_in": e.announced_removed_in,
                }
                for k, e in self.entries.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DeprecationMap:
        """Deserialize from a JSON-compatible dict."""
        entries: dict[str, DeprecationEntry] = {}
        raw_entries = data.get("entries", {})
        if isinstance(raw_entries, dict):
            for key, val in raw_entries.items():
                if isinstance(val, dict):
                    entries[key] = DeprecationEntry(
                        old_path=str(val.get("old_path", key)),
                        new_path=val.get("new_path"),
                        deprecated_in=val.get("deprecated_in"),
                        removed_in=val.get("removed_in"),
                        note=val.get("note", ""),
                        kind=val.get("kind"),
                        relation=val.get("relation"),
                        location=val.get("location"),
                        status=cast(DeprecationStatus, val.get("status", "deprecated")),
                        announced_removed_in=val.get("announced_removed_in"),
                    )
        commit_sha = data.get("pipecat_commit_sha", "")
        return cls(
            entries=entries,
            pipecat_commit_sha=str(commit_sha) if commit_sha else "",
        )

    def save(self, path: Path) -> None:
        """Persist the deprecation map to a JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        logger.info("Deprecation map saved to %s (%d entries)", path, len(self.entries))

    @classmethod
    def load(cls, path: Path) -> DeprecationMap:
        """Load a deprecation map from a JSON file. Returns empty map on failure."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except Exception:
            logger.debug("Could not load deprecation map from %s", path)
            return cls()


class DeprecationRegistryError(Exception):
    """Raised when the deprecation registry is present but unreadable/corrupt.

    Distinguishes this case from a *legitimate* missing registry (older
    pipecat versions predate it, ``FileNotFoundError``), which returns an
    empty :class:`DeprecationMap` instead of raising. Callers must not treat
    an empty map returned in response to this exception as authoritative —
    the previously published map should be preserved.
    """


def build_deprecation_map_from_registry(
    registry_path: Path,
    commit_sha: str = "",
) -> DeprecationMap:
    """Build a deprecation map from pipecat's ``deprecations.json`` registry.

    Reads the generated registry — the single source of truth — and maps each
    record into a :class:`DeprecationEntry`. Non-module symbols are keyed by both
    their bare subject and their fully-qualified ``<module>.<subject>`` path so a
    query for either form resolves; the bare subject always wins a key collision.

    Args:
        registry_path: Path to ``scripts/deprecations/deprecations.json`` inside
            a pipecat checkout.
        commit_sha: Current pipecat commit SHA, for staleness detection.

    Returns:
        A :class:`DeprecationMap`. Empty if the registry is legitimately
        missing (older pipecat versions predate the registry).

    Raises:
        DeprecationRegistryError: The registry file exists but could not be
            read or parsed (corrupt JSON, I/O error, etc.). Callers must not
            treat this the same as a legitimate empty map — the caller should
            preserve whatever deprecation map was previously published rather
            than overwrite it with an empty one.
    """
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info(
            "No deprecation registry at %s — this pipecat version predates it; "
            "deprecation map will be empty",
            registry_path,
        )
        return DeprecationMap(pipecat_commit_sha=commit_sha)
    except Exception as exc:
        logger.warning("Could not read deprecation registry at %s", registry_path, exc_info=True)
        raise DeprecationRegistryError(
            f"Could not read deprecation registry at {registry_path}"
        ) from exc

    records = data.get("deprecations", []) if isinstance(data, dict) else []
    entries: dict[str, DeprecationEntry] = {}
    aliases: dict[str, DeprecationEntry] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        subject = rec.get("subject")
        if not subject:
            continue
        entry = DeprecationEntry(
            old_path=subject,
            new_path=rec.get("replacement") or None,
            deprecated_in=rec.get("deprecated_in"),
            removed_in=rec.get("removed_in"),
            note=rec.get("message", ""),
            kind=rec.get("kind"),
            relation=rec.get("relation"),
            location=rec.get("location"),
        )
        # Bare subjects are assumed globally unique in the registry. If two
        # records collide on the same bare key (e.g. same-named classes in
        # different modules), the last one silently wins the bare lookup — the
        # fully-qualified aliases below keep both resolvable. Log so the shadowing
        # is observable instead of silent.
        if subject in entries and entries[subject].location != entry.location:
            logger.warning(
                "Deprecation registry: duplicate bare subject %r (%s shadows %s); "
                "bare lookup resolves to the latter, both remain resolvable by full path",
                subject,
                entries[subject].location,
                entry.location,
            )
        entries[subject] = entry
        # Register a fully-qualified alias so "pipecat.x.y.ClassName" resolves the
        # same as the bare "ClassName". Module records are already fully qualified.
        module = rec.get("module")
        if module and rec.get("kind") != "module" and not subject.startswith(module + "."):
            aliases[f"{module}.{subject}"] = entry

    for key, entry in aliases.items():
        entries.setdefault(key, entry)

    dep_map = DeprecationMap(entries=entries, pipecat_commit_sha=commit_sha)
    logger.info("Built deprecation map from registry: %d entries", len(entries))
    return dep_map


def add_removals_from_registry(dep_map: DeprecationMap, removals_path: Path) -> None:
    """Merge pipecat's ``removals.json`` into an existing map (``status="removed"``).

    Symbols that were deprecated and have since been removed live in ``removals.json``
    (a sibling of ``deprecations.json``), not in the active registry. Each is added
    with ``status="removed"``, the *actual* ``removed_in``, and ``announced_removed_in``
    — keyed by bare subject and fully-qualified path, like active deprecations. No-op
    if the file is absent (older pipecat predates it). Mutates ``dep_map`` in place.
    """
    try:
        data = json.loads(removals_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("No removals registry at %s — no removed symbols merged.", removals_path)
        return
    except Exception:
        logger.warning("Could not read removals registry at %s", removals_path, exc_info=True)
        return

    records = data.get("removals", []) if isinstance(data, dict) else []
    aliases: dict[str, DeprecationEntry] = {}
    added = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        subject = rec.get("subject")
        if not subject:
            continue
        entry = DeprecationEntry(
            old_path=subject,
            new_path=rec.get("replacement") or None,
            deprecated_in=rec.get("deprecated_in"),
            removed_in=rec.get("removed_in"),
            note=rec.get("message", ""),
            kind=rec.get("kind"),
            relation=rec.get("relation"),
            status="removed",
            announced_removed_in=rec.get("announced_removed_in"),
        )
        existing = dep_map.entries.get(subject)
        if existing is not None and existing.status != "removed":
            # Never let a removal silently shadow an *active* deprecation on the
            # bare key. The producer only emits removals for subjects no longer in
            # deprecations.json, so a bare-key collision here means a *different*
            # symbol shares this name. Reporting "removed" for a still-deprecated
            # symbol would break the safety invariant `status_for` upholds, so keep
            # the active entry on the bare key; the removal stays resolvable via its
            # fully-qualified alias registered below.
            logger.warning(
                "Removals registry: bare subject %r already maps to an active "
                "deprecation; keeping the active entry on the bare key — the "
                "removal resolves by full path only",
                subject,
            )
        else:
            dep_map.entries[subject] = entry
        added += 1
        module = rec.get("module")
        if module and rec.get("kind") != "module" and not subject.startswith(module + "."):
            aliases[f"{module}.{subject}"] = entry

    for key, entry in aliases.items():
        dep_map.entries.setdefault(key, entry)
    logger.info("Merged %d removal record(s) into the deprecation map", added)


def _as_version(value: str | None) -> Version | None:
    """Parse a ``X.Y.Z`` (optionally ``v``-prefixed) version, or ``None``.

    Routed through the shared :func:`parse_release_version` so a doubled prefix
    (``"vv1.0.0"``) is *not* silently coerced to ``1.0.0`` by ``Version()``'s
    own normalisation. Registry values (``deprecated_in`` / ``removed_in``) are
    plain ``X.Y.Z`` and unaffected; a caller who types a malformed version now
    falls back to the entry's intrinsic status rather than getting an answer for
    a version they did not ask about.
    """
    if not value:
        return None
    try:
        return parse_release_version(str(value))
    except InvalidVersion:
        return None


def status_for(entry: DeprecationEntry, version: str | None) -> DeprecationStatus:
    """Lifecycle status of ``entry`` relative to ``version``.

    Returns ``"current"`` (not yet deprecated at that version), ``"deprecated"``, or
    ``"removed"``. With no version — or an unparseable one — falls back to the entry's
    intrinsic status, i.e. its state as of the indexed framework version.

    Never reports ``"removed"`` for an active deprecation: a removal must be recorded
    in ``removals.json`` (``entry.status == "removed"``), since an active entry's
    ``removed_in`` is only an announced/planned version, not evidence it happened.
    """
    requested = _as_version(version)
    if requested is None:
        return entry.status

    deprecated = _as_version(entry.deprecated_in)
    if entry.status == "removed":
        removed = _as_version(entry.removed_in)
        if removed is not None and requested >= removed:
            return "removed"
        if deprecated is not None and requested < deprecated:
            return "current"
        return "deprecated"

    # Active deprecation — the most we can assert is "deprecated".
    if deprecated is not None and requested < deprecated:
        return "current"
    return "deprecated"
