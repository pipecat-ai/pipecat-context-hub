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

logger = logging.getLogger(__name__)

# Location of the registry within a pipecat checkout.
REGISTRY_RELATIVE_PATH = Path("scripts") / "deprecations" / "deprecations.json"


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
        A :class:`DeprecationMap`. Empty if the registry is missing or unreadable
        (older pipecat versions predate the registry).
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
    except Exception:
        logger.warning("Could not read deprecation registry at %s", registry_path, exc_info=True)
        return DeprecationMap(pipecat_commit_sha=commit_sha)

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
