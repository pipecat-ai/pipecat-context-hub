"""Shared helpers for the vendored pipecat v1.16.0 deprecation snapshot.

The fixture lives in ``tests/fixtures/deprecations/v1.16.0``. Shared here, because
more than one test module needs it: ``build_fixture_map`` and ``load_fixture_map``
(the production builders over the snapshot, then save and load as serve sees it),
the fixture paths they read, and ``is_member`` / ``fixture_member_subjects`` / ``homes``,
the independent oracle (not calling ``members_named``) for which subjects are members and how they group by name.
The count-recompute helpers (raw records, ``SOURCE.md`` parsing) are private to
``tests/unit/test_deprecation_fixture.py``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from pipecat_context_hub.services.ingest.deprecation_map import (
    DeprecationMap,
    add_removals_from_registry,
    build_deprecation_map_from_registry,
)

FIXTURE_TAG = "v1.16.0"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "deprecations" / FIXTURE_TAG
DEPRECATIONS_PATH = FIXTURE_DIR / "deprecations.json"
REMOVALS_PATH = FIXTURE_DIR / "removals.json"


def build_fixture_map() -> DeprecationMap:
    """Build the map from the vendored fixture with the production builders."""
    dep_map = build_deprecation_map_from_registry(DEPRECATIONS_PATH)
    add_removals_from_registry(dep_map, REMOVALS_PATH)
    return dep_map


def load_fixture_map(directory: Path) -> DeprecationMap:
    """Build the fixture map, save it under ``directory`` and load it back, as serve does."""
    path = directory / "deprecation_map.json"
    build_fixture_map().save(path)
    loaded = DeprecationMap.load(path)
    assert loaded.load_error is None
    return loaded


def is_member(kind: object, subject: str) -> bool:
    """Whether a registry subject is a dotted member: the predicate ``members_named`` applies.

    Written out here rather than imported, so the invariant tests derive their
    expectations without calling the code under test: a dotted subject whose
    ``kind`` is neither ``"module"`` nor ``None``.
    """
    return kind not in (None, "module") and "." in subject


def fixture_member_subjects() -> set[str]:
    """Built-map member subjects: bare dotted non-module keys (not the qualified duplicates)."""
    dm = build_fixture_map()
    return {k for k, e in dm.entries.items() if e.old_path == k and is_member(e.kind, k)}


def homes(subjects: set[str]) -> dict[str, set[str]]:
    """Group member subjects by their last segment."""
    by_last: dict[str, set[str]] = defaultdict(set)
    for s in subjects:
        by_last[s.rsplit(".", 1)[1]].add(s)
    return by_last


def expected_member_counts() -> dict[str, int]:
    """Independent count of distinct member records per last-segment name.

    Computed from the raw registry files, never through ``members_named`` or the
    map builders. A record is one registry record; the last record wins per
    (source file, subject, module), as the builders do when one repeats; two
    records that agree on every field the map stores are one candidate (the
    accepted collapse of identical records from different modules). An active
    record and a removal sharing a subject differ in status, so both count.
    """
    last: dict[tuple[str, object, object], tuple[dict[str, object], str]] = {}
    for status, path, field in (
        ("deprecated", DEPRECATIONS_PATH, "deprecations"),
        ("removed", REMOVALS_PATH, "removals"),
    ):
        for rec in json.loads(path.read_text(encoding="utf-8"))[field]:
            last[(status, rec["subject"], rec.get("module"))] = (rec, status)
    distinct: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    for rec, status in last.values():
        subject = str(rec["subject"])
        if not is_member(rec.get("kind"), subject):
            continue
        distinct[subject.rsplit(".", 1)[1]].add(
            (
                subject,
                rec.get("replacement") or None,
                rec.get("deprecated_in"),
                rec.get("removed_in"),
                rec.get("message") or "",
                rec.get("kind"),
                rec.get("relation"),
                rec.get("location") if status == "deprecated" else None,
                status,
                rec.get("announced_removed_in") if status == "removed" else None,
            )
        )
    return {name: len(records) for name, records in distinct.items()}
