"""Whole-registry tests against the vendored pipecat v1.16.0 deprecation snapshot.

The fixture is pinned (``tests/fixtures/deprecations/v1.16.0``). Every count
recorded in its ``SOURCE.md`` ``## Counts`` section must equal what the existing
builders recompute, so the snapshot and the tests cannot drift apart silently.

Later phases import :func:`build_fixture_map` / :func:`fixture_member_subjects`.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path


from pipecat_context_hub.services.ingest.deprecation_map import (
    DeprecationMap,
    add_removals_from_registry,
    build_deprecation_map_from_registry,
)

FIXTURE_TAG = "v1.16.0"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "deprecations" / FIXTURE_TAG
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


def _raw_member_subjects() -> list[str]:
    return [
        str(r["subject"]) for r in raw_records() if _is_member(r.get("kind"), str(r["subject"]))
    ]


def fixture_member_subjects(dep_map: DeprecationMap | None = None) -> set[str]:
    """Built-map member subjects: bare dotted non-module keys (not the qualified duplicates)."""
    dm = dep_map if dep_map is not None else build_fixture_map()
    return {k for k, e in dm.entries.items() if e.old_path == k and _is_member(e.kind, k)}


def _homes(subjects: set[str]) -> dict[str, set[str]]:
    homes: dict[str, set[str]] = defaultdict(set)
    for s in subjects:
        homes[s.rsplit(".", 1)[1]].add(s)
    return homes


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


def _compute() -> dict[str, Callable[[], int]]:
    def map_subjects() -> set[str]:
        return fixture_member_subjects()

    def raw_deps() -> int:
        return len(json.loads(DEPRECATIONS_PATH.read_text(encoding="utf-8"))["deprecations"])

    def raw_rems() -> int:
        return len(json.loads(REMOVALS_PATH.read_text(encoding="utf-8"))["removals"])

    return {
        "raw_records_total": lambda: len(raw_records()),
        "raw_deprecations_records": raw_deps,
        "raw_removals_records": raw_rems,
        "raw_dotted_member_records": lambda: len(_raw_member_subjects()),
        "raw_dotted_member_last_segments": lambda: len(_homes(set(_raw_member_subjects()))),
        "map_total_entries": lambda: len(build_fixture_map().entries),
        "map_member_subjects": lambda: len(map_subjects()),
        "map_member_last_segments": lambda: len(_homes(map_subjects())),
        "map_one_home_last_segments": lambda: sum(
            1 for v in _homes(map_subjects()).values() if len(v) == 1
        ),
        "map_multi_home_last_segments": lambda: sum(
            1 for v in _homes(map_subjects()).values() if len(v) > 1
        ),
        "map_model_homes": lambda: len(_homes(map_subjects()).get("model", set())),
    }


class TestFixtureFiles:
    def test_files_exist(self) -> None:
        for p in (DEPRECATIONS_PATH, REMOVALS_PATH, SOURCE_PATH):
            assert p.is_file(), f"missing fixture file {p}"

    def test_json_loads(self) -> None:
        deps = json.loads(DEPRECATIONS_PATH.read_text(encoding="utf-8"))
        rems = json.loads(REMOVALS_PATH.read_text(encoding="utf-8"))
        assert isinstance(deps["deprecations"], list) and deps["deprecations"]
        assert isinstance(rems["removals"], list) and rems["removals"]

    def test_source_names_tag(self) -> None:
        assert FIXTURE_TAG in SOURCE_PATH.read_text(encoding="utf-8")

    def test_builders_produce_entries(self) -> None:
        dm = build_fixture_map()
        assert dm.entries
        assert {e.status for e in dm.entries.values()} == {"deprecated", "removed"}


class TestSourceCounts:
    def test_counts_section_non_empty(self) -> None:
        assert parse_counts(SOURCE_PATH.read_text(encoding="utf-8"))

    def test_every_count_matches_recomputed(self) -> None:
        recorded = parse_counts(SOURCE_PATH.read_text(encoding="utf-8"))
        compute = _compute()
        unknown = sorted(set(recorded) - set(compute))
        assert not unknown, (
            f"SOURCE.md records count keys with no computation in this test: {unknown}; "
            "add them to _compute() so the two stay in sync"
        )
        mismatches = {k: (v, compute[k]()) for k, v in recorded.items() if compute[k]() != v}
        assert not mismatches, f"SOURCE.md (recorded, recomputed) mismatch: {mismatches}"

    def test_every_computation_is_recorded(self) -> None:
        recorded = parse_counts(SOURCE_PATH.read_text(encoding="utf-8"))
        missing = sorted(set(_compute()) - set(recorded))
        assert not missing, f"SOURCE.md lacks counts for: {missing}"
