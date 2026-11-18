"""Whole-registry tests against the vendored pipecat v1.16.0 deprecation snapshot.

The fixture is pinned (``tests/fixtures/deprecations/v1.16.0``). Every count
recorded in its ``SOURCE.md`` ``## Counts`` section must equal what the existing
builders recompute, so the snapshot and the tests cannot drift apart silently.

The helpers live in ``tests/_deprecation_fixture.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tests._deprecation_fixture import (
    DEPRECATIONS_PATH,
    FIXTURE_TAG,
    REMOVALS_PATH,
    SOURCE_PATH,
    build_fixture_map,
    fixture_member_subjects,
    homes,
    parse_counts,
    raw_member_subjects,
    raw_records,
)


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
        "raw_dotted_member_records": lambda: len(raw_member_subjects()),
        "raw_dotted_member_last_segments": lambda: len(homes(set(raw_member_subjects()))),
        "map_total_entries": lambda: len(build_fixture_map().entries),
        "map_member_subjects": lambda: len(map_subjects()),
        "map_member_last_segments": lambda: len(homes(map_subjects())),
        "map_one_home_last_segments": lambda: sum(
            1 for v in homes(map_subjects()).values() if len(v) == 1
        ),
        "map_multi_home_last_segments": lambda: sum(
            1 for v in homes(map_subjects()).values() if len(v) > 1
        ),
        "map_model_homes": lambda: len(homes(map_subjects()).get("model", set())),
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
