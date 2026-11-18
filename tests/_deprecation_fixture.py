"""Shared helpers for the vendored pipecat v1.16.0 deprecation snapshot.

The fixture lives in ``tests/fixtures/deprecations/v1.16.0``. These are plain
helpers (like ``tests/_ingest_helpers.py``), imported directly by the registry,
handler and CLI tests so they build the same map from one source of truth.
"""

from __future__ import annotations

import json
import re
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
SOURCE_PATH = FIXTURE_DIR / "SOURCE.md"


def build_fixture_map() -> DeprecationMap:
    """Build the map from the vendored fixture with the production builders."""
    dep_map = build_deprecation_map_from_registry(DEPRECATIONS_PATH)
    add_removals_from_registry(dep_map, REMOVALS_PATH)
    return dep_map


def raw_records() -> list[dict[str, object]]:
    """All raw registry records: deprecations.json then removals.json."""
    deps = json.loads(DEPRECATIONS_PATH.read_text(encoding="utf-8"))["deprecations"]
    rems = json.loads(REMOVALS_PATH.read_text(encoding="utf-8"))["removals"]
    return [*deps, *rems]


def _is_member(kind: object, subject: str) -> bool:
    return kind != "module" and "." in subject


def raw_member_subjects() -> list[str]:
    return [
        str(r["subject"]) for r in raw_records() if _is_member(r.get("kind"), str(r["subject"]))
    ]


def fixture_member_subjects(dep_map: DeprecationMap | None = None) -> set[str]:
    """Built-map member subjects: bare dotted non-module keys (not the qualified duplicates)."""
    dm = dep_map if dep_map is not None else build_fixture_map()
    return {k for k, e in dm.entries.items() if e.old_path == k and _is_member(e.kind, k)}


def homes(subjects: set[str]) -> dict[str, set[str]]:
    by_last: dict[str, set[str]] = defaultdict(set)
    for s in subjects:
        by_last[s.rsplit(".", 1)[1]].add(s)
    return by_last


def parse_counts(text: str) -> dict[str, int]:
    """Parse ``key: integer`` lines from SOURCE.md's ``## Counts`` section."""
    m = re.search(r"^## Counts\s*$(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, "SOURCE.md has no '## Counts' section"
    counts: dict[str, int] = {}
    for line in m.group(1).splitlines():
        lm = re.fullmatch(r"([a-z][a-z0-9_]*):\s*(\d+)", line.strip())
        if lm:
            assert lm.group(1) not in counts, f"duplicate count key {lm.group(1)}"
            counts[lm.group(1)] = int(lm.group(2))
    return counts
