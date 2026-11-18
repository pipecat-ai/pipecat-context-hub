"""Tests for server.tools.check_deprecation: version resolution and the handler."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from pipecat_context_hub.server.tools.check_deprecation import (
    handle_check_deprecation,
    resolve_framework_version,
)
from pipecat_context_hub.services.ingest.deprecation_map import (
    DeprecationEntry,
    DeprecationMap,
    status_for,
)
from pipecat_context_hub.shared.types import (
    BARE_NAME_RULE,
    DEPRECATION_CANDIDATE_CAP,
    CheckDeprecationInput,
    CheckDeprecationOutput,
    DeprecationCandidate,
    DeprecationStatus,
)
from tests._deprecation_fixture import (
    expected_member_counts,
    fixture_member_subjects,
    homes,
    load_fixture_map,
)


def _index_store(metadata: dict[str, Any]) -> MagicMock:
    store = MagicMock()
    store.get_all_metadata.return_value = metadata
    return store


class TestResolveFrameworkVersion:
    def test_no_index_store_returns_none(self):
        assert resolve_framework_version(None) is None

    def test_indexed_version_preferred_even_when_pin_is_latest(self):
        """indexed_framework_version is the concrete resolved revision; it must
        win even though framework_version still holds the unresolved 'latest'
        sentinel.
        """
        store = _index_store(
            {
                "framework_version": "latest",
                "indexed_framework_version": "1.10.0",
                "indexed_framework_commits_ahead": "0",
            }
        )
        assert resolve_framework_version(store) == "1.10.0"

    def test_floor_version_with_commit_distance_is_not_exact(self):
        """A nearest tag on the default branch must not drive version-aware status."""
        store = _index_store(
            {
                "framework_version": "latest",
                "indexed_framework_version": "1.5.0",
                "indexed_framework_commits_ahead": "80",
            }
        )
        assert resolve_framework_version(store) is None

    def test_falls_back_to_framework_version_when_indexed_unset(self):
        store = _index_store({"framework_version": "v0.0.96"})
        assert resolve_framework_version(store) == "v0.0.96"

    def test_neither_set_returns_none(self):
        store = _index_store({})
        assert resolve_framework_version(store) is None

    @pytest.mark.parametrize("pin", ["latest", "  LATEST  ", "Latest"])
    def test_latest_pin_is_never_returned_as_a_version(self, pin: str):
        """Metadata contract v2 permits ``framework_version == "latest"``. The
        sentinel is a pin, not a version — returning it verbatim hands a
        version-shaped non-version to the deprecation handler.
        """
        store = _index_store({"framework_version": pin})
        assert resolve_framework_version(store) is None

    @pytest.mark.parametrize("pin", ["head", "  MAIN  ", "Head"])
    def test_head_pin_is_never_returned_as_a_version(self, pin: str):
        """``head`` is a pin like ``latest`` — it names no release to evaluate against."""
        store = _index_store({"framework_version": pin})
        assert resolve_framework_version(store) is None

    def test_map_provenance_mismatch_falls_back_to_none(self):
        """Round 10 Finding #1 regression: if the on-disk deprecation map's
        commit SHA doesn't match the metadata's `deprecation_map_commit_sha`
        stamp (a crash between the two writes left them describing different
        revisions), don't assert version-exactness against a map that may not
        match — fall back to None.
        """
        store = _index_store(
            {
                "indexed_framework_version": "1.2.0",
                "indexed_framework_commits_ahead": "0",
                "deprecation_map_commit_sha": "abc123",
            }
        )
        dep_map = MagicMock()
        dep_map.pipecat_commit_sha = "def456"
        assert resolve_framework_version(store, dep_map) is None

    def test_map_provenance_match_returns_version(self):
        store = _index_store(
            {
                "indexed_framework_version": "1.2.0",
                "indexed_framework_commits_ahead": "0",
                "deprecation_map_commit_sha": "abc123",
            }
        )
        dep_map = MagicMock()
        dep_map.pipecat_commit_sha = "abc123"
        assert resolve_framework_version(store, dep_map) == "1.2.0"

    def test_missing_map_commit_sha_stamp_preserves_old_behavior(self):
        """A pre-fix index has no `deprecation_map_commit_sha` key at all —
        the cross-check must skip (fail open), not force a None regression
        for existing indexes built before this fix shipped.
        """
        store = _index_store(
            {
                "indexed_framework_version": "1.2.0",
                "indexed_framework_commits_ahead": "0",
            }
        )
        dep_map = MagicMock()
        dep_map.pipecat_commit_sha = "def456"
        assert resolve_framework_version(store, dep_map) == "1.2.0"


class TestUnloadableMap:
    """An unloadable map answers "not deprecated" for everything; say so."""

    @pytest.mark.parametrize(
        ("content", "fragment"),
        [
            (None, "is missing"),
            ("{not json", "could not be read"),
            ('{"entries": []}', "could not be read"),
        ],
    )
    def test_warns_instead_of_asserting_current(
        self, tmp_path: Path, content: str | None, fragment: str
    ) -> None:
        path = tmp_path / "deprecation_map.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        out = json.loads(
            asyncio.run(
                handle_check_deprecation({"symbol": "PipelineTask"}, DeprecationMap.load(path))
            )
        )
        assert out["deprecated"] is False
        assert out["status"] is None
        assert fragment in out["note"]
        assert "refresh" in out["note"]

    def test_loaded_empty_map_still_reports_current(self, tmp_path: Path) -> None:
        path = tmp_path / "deprecation_map.json"
        DeprecationMap().save(path)
        out = json.loads(
            asyncio.run(
                handle_check_deprecation({"symbol": "PipelineTask"}, DeprecationMap.load(path))
            )
        )
        assert out["status"] == "current"
        assert out["note"] is None


# ---------------------------------------------------------------------------
# Bare member names: hint-only candidates (dev plan 20261117, Phase 3)
# ---------------------------------------------------------------------------

CAP = DEPRECATION_CANDIDATE_CAP


def _run(
    dep_map: DeprecationMap,
    symbol: str,
    version: str | None = None,
    fw: str | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"symbol": symbol}
    if version is not None:
        args["version"] = version
    return json.loads(asyncio.run(handle_check_deprecation(args, dep_map, fw)))


@pytest.fixture(scope="module")
def loaded_map(tmp_path_factory: pytest.TempPathFactory) -> DeprecationMap:
    """The vendored v1.16.0 registry, built, saved and loaded (as serve sees it)."""
    return load_fixture_map(tmp_path_factory.mktemp("depmap"))


def _entry(
    old_path: str,
    *,
    deprecated_in: str | None = "1.0.0",
    removed_in: str | None = "2.0.0",
    kind: str | None = "method",
    status: DeprecationStatus = "deprecated",
    new_path: str | None = None,
) -> DeprecationEntry:
    return DeprecationEntry(
        old_path=old_path,
        new_path=new_path,
        deprecated_in=deprecated_in,
        removed_in=removed_in,
        kind=kind,
        status=status,
    )


def _synthetic(entries: list[DeprecationEntry]) -> DeprecationMap:
    return DeprecationMap(entries={e.old_path: e for e in entries})


def _is_hint(out: dict[str, Any]) -> bool:
    return out["status"] is None and out.get("candidates") is not None


def _all_keys(out: dict[str, Any]) -> set[str]:
    return set(out)


_CAND_KEYS = {"symbol", "kind", "status", "deprecated_in", "removed_in", "replacement"}


class TestFrozenRealRecord:
    def test_trigger_user_turn_stopped_is_a_hint_not_current(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "trigger_user_turn_stopped")
        assert out["deprecated"] is False
        assert out["status"] is None
        assert out["candidates_total"] == 1
        (cand,) = out["candidates"]
        assert cand["symbol"] == "BaseUserTurnStopStrategy.trigger_user_turn_stopped"
        assert cand["replacement"] == "report_user_turn_stopped"
        assert cand["deprecated_in"] == "1.15.0"
        assert set(cand) == _CAND_KEYS
        assert out["note"]

    def test_candidate_symbol_resolves_to_a_verdict(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "trigger_user_turn_stopped")
        symbol = out["candidates"][0]["symbol"]
        verdict = _run(loaded_map, symbol)
        assert verdict["deprecated"] is True
        assert verdict["replacement"] == "report_user_turn_stopped"


class TestInvariantOverFixture:
    def test_bare_non_key_names(self, loaded_map: DeprecationMap):
        """Both clauses of the invariant for every bare name that is not an exact key."""
        # The expectation comes from the fixture's own member subjects, never from
        # ``members_named``, so a name that function drops cannot hide a regression.
        by_name = homes(fixture_member_subjects())
        expected = expected_member_counts()
        assert set(expected) == set(by_name)
        segments = {seg for k in loaded_map.entries for seg in k.split(".")}
        bare = {s for s in segments if s and s not in loaded_map.entries}
        assert set(by_name) - set(loaded_map.entries), "fixture has no bare member names"
        checked_hint = checked_current = 0
        for name in sorted(bare):
            out = _run(loaded_map, name)
            assert out["deprecated"] is False, name
            if name in by_name:
                assert out["status"] is None, name
                assert out["candidates"], name
                # Exact, from the raw records (a removal sharing an old_path with
                # an active entry is a second candidate), so over-counting fails.
                assert out["candidates_total"] == expected[name], name
                assert len(out["candidates"]) <= DEPRECATION_CANDIDATE_CAP, name
                checked_hint += 1
            else:
                assert out["status"] == "current", name
                assert out["candidates"] is None, name
                assert out["candidates_total"] is None, name
                checked_current += 1
        assert checked_hint > 50 and checked_current > 0

    def test_deprecated_true_only_for_registry_symbols(self, loaded_map: DeprecationMap):
        """No bare non-key name ever yields deprecated: true."""
        for name in ("model", "params", "voice_id", "task", "llm", "transport", "set_prompt"):
            if name in loaded_map.entries:
                continue
            assert _run(loaded_map, name)["deprecated"] is False, name


class TestModulePathsNeverHint:
    @pytest.mark.parametrize(
        "path",
        [
            "pipecat.services.openai.llm",
            "pipecat.services.anthropic.llm",
            "pipecat.transports.daily.transport",
        ],
    )
    def test_frozen_module_paths_are_current(self, loaded_map: DeprecationMap, path: str):
        assert path not in loaded_map.entries
        out = _run(loaded_map, path)
        assert out["deprecated"] is False
        assert out["status"] == "current"
        assert out["candidates"] is None
        assert out["candidates_total"] is None

    def test_no_module_shaped_query_hints(self, loaded_map: DeprecationMap):
        """Fixture-wide: a pipecat.-prefixed module path that is not an exact key or a
        descendant of one never returns a hint."""
        queries: set[str] = set()
        for e in loaded_map.entries.values():
            if e.location and e.location.endswith(".py"):
                parts = e.location[:-3].split("/")
                parts = parts[:-1] if parts[-1] == "__init__" else parts
                for i in range(2, len(parts) + 1):
                    queries.add(".".join(parts[:i]))
        for k, e in loaded_map.entries.items():
            segs = k.split(".")
            for i in range(2, len(segs) + 1):
                prefix = segs[:i]
                if all(s[:1].islower() for s in prefix):
                    queries.add(".".join(prefix))
        queries = {q for q in queries if q.startswith("pipecat.")}
        queries = {q for q in queries if loaded_map.check(q) is None}
        assert len(queries) > 50
        offenders = [q for q in sorted(queries) if _is_hint(_run(loaded_map, q))]
        assert offenders == []


class TestInputParams:
    def test_exact_verdict_unchanged_plus_candidates(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "InputParams")
        assert out["deprecated"] is True
        assert out["status"] == "deprecated"
        assert out["replacement"] == "GeminiLiveLLMService.Settings"
        homes = loaded_map.members_named("InputParams")
        exact = loaded_map.check("InputParams")
        others = [s for s, e in homes if e is not exact and e.old_path != "InputParams"]
        assert len(out["candidates"]) == CAP
        assert out["candidates_total"] == len(others) > CAP
        symbols = [c["symbol"] for c in out["candidates"]]
        assert "InputParams" not in symbols
        assert all(s.endswith(".InputParams") for s in symbols)

    def test_current_at_version_branch_also_attaches(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "InputParams", version="0.0.1")
        assert out["deprecated"] is False
        assert out["status"] == "current"
        assert out["candidates"]
        assert out["candidates_total"] == len(loaded_map.members_named("InputParams"))

    def test_removed_branch_also_attaches(self):
        dep = _synthetic(
            [
                _entry("Params", kind="class", status="removed", removed_in="2.0.0"),
                _entry("A.Params", kind="class"),
                _entry("B.Params", kind="class"),
            ]
        )
        out = _run(dep, "Params", version="2.0.0")
        assert out["status"] == "removed"
        assert out["deprecated"] is True
        assert [c["symbol"] for c in out["candidates"]] == ["A.Params", "B.Params"]
        assert out["candidates_total"] == 2

    def test_prefix_match_has_no_candidates(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "InputParams.voice_id")
        assert out["deprecated"] is True
        assert out["candidates"] is None
        assert out["candidates_total"] is None

    def test_dotted_exact_key_has_no_candidates(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "AWSBedrockLLMService.InputParams")
        assert out["deprecated"] is True
        assert out["candidates"] is None
        assert out["candidates_total"] is None

    def test_bare_exact_key_without_same_named_members_gets_null(self):
        dep = _synthetic([_entry("OldThing", kind="class")])
        out = _run(dep, "OldThing")
        assert out["deprecated"] is True
        assert out["candidates"] is None
        assert out["candidates_total"] is None

    def test_exact_record_excluded_from_candidates(self):
        dep = _synthetic(
            [
                _entry("Params", kind="class"),
                _entry("A.Params", kind="class"),
                _entry("B.Params", kind="class"),
            ]
        )
        out = _run(dep, "Params")
        assert out["deprecated"] is True
        assert [c["symbol"] for c in out["candidates"]] == ["A.Params", "B.Params"]
        assert out["candidates_total"] == 2


class TestCapAndOrder:
    def test_cap_contract_is_ten(self):
        """Pins the public contract literally: the other cap tests derive from the constant."""
        assert DEPRECATION_CANDIDATE_CAP == 10
        desc = CheckDeprecationOutput.model_json_schema()["properties"]["candidates"]["description"]
        assert "at most 10" in desc

    def test_model_cap_and_order(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "model")
        homes = loaded_map.members_named("model")
        assert len(out["candidates"]) == 10  # literal: the public contract, not the constant
        assert len(out["candidates"]) == CAP
        assert out["candidates_total"] == len(homes) > CAP
        assert [c["symbol"] for c in out["candidates"]] == sorted([s for s, _ in homes])[:CAP]
        assert out["note"]

    @pytest.mark.parametrize("version", ["0.0.104", "0.0.105", "1.16.0"])
    def test_model_mixed_statuses_sorted_current_last(
        self, loaded_map: DeprecationMap, version: str
    ):
        homes = loaded_map.members_named("model")
        expected = sorted(
            ((status_for(e, version) == "current", s) for s, e in homes),
        )[:CAP]
        out = _run(loaded_map, "model", version=version)
        got = [(c["status"] == "current", c["symbol"]) for c in out["candidates"]]
        assert got == expected
        assert out["candidates_total"] == len(homes)

    def test_version_with_some_current_puts_non_current_first(self):
        entries = [_entry(f"C{i:02d}.model", deprecated_in="2.0.0") for i in range(3)]
        entries += [_entry(f"Z{i}.model", deprecated_in="1.0.0") for i in range(3)]
        out = _run(_synthetic(entries), "model", version="1.5.0")
        statuses = [c["status"] for c in out["candidates"]]
        assert statuses == ["deprecated"] * 3 + ["current"] * 3
        symbols = [c["symbol"] for c in out["candidates"]]
        assert symbols == [
            "Z0.model",
            "Z1.model",
            "Z2.model",
            "C00.model",
            "C01.model",
            "C02.model",
        ]

    @pytest.mark.parametrize("n", [1, 9, 10, 11, 12])
    def test_boundaries(self, n: int):
        dep = _synthetic([_entry(f"C{i:02d}.foo") for i in range(n)])
        out = _run(dep, "foo")
        assert out["candidates_total"] == n
        assert len(out["candidates"]) == min(n, CAP)

    def test_cap_applies_after_sorting_by_status(self):
        # 11 current (sorted first by symbol) and 1 deprecated sorting last by symbol.
        entries = [_entry(f"A{i:02d}.foo", deprecated_in="3.0.0") for i in range(11)]
        entries.append(_entry("Zed.foo", deprecated_in="1.0.0"))
        out = _run(_synthetic(entries), "foo", version="2.0.0")
        assert out["candidates"][0]["symbol"] == "Zed.foo"
        assert out["candidates_total"] == 12
        assert len(out["candidates"]) == CAP


class TestDottedQueries:
    @pytest.mark.parametrize(
        "symbol",
        [
            "SomeSubclass.trigger_user_turn_stopped",
            "pipecat.x.y.Sub.trigger_user_turn_stopped",
        ],
    )
    def test_class_like_owner_with_one_home_hints(self, loaded_map: DeprecationMap, symbol: str):
        out = _run(loaded_map, symbol)
        assert out["deprecated"] is False
        assert out["status"] is None
        assert out["candidates_total"] == 1
        assert (
            out["candidates"][0]["symbol"] == "BaseUserTurnStopStrategy.trigger_user_turn_stopped"
        )

    def test_dotted_hint_note_names_the_other_owner(self, loaded_map: DeprecationMap):
        # A real, current method that shares a name with a deprecated member of another
        # class must not read as "possibly deprecated": the note says the registry holds
        # the name only under a different owner and the inheritance caveat.
        out = _run(loaded_map, "FrameProcessor.setup")
        assert out["deprecated"] is False
        assert out["status"] is None
        assert out["candidates"][0]["symbol"] == "TaskManager.setup"
        note = out["note"]
        assert "`FrameProcessor.setup` is not in the deprecation registry" in note
        assert "another owner, `TaskManager.setup`" in note
        assert "unless `FrameProcessor` inherits the member" in note
        assert "matched no symbol exactly" not in note

    def test_second_owner_adds_a_candidate_and_keeps_the_hint(self, loaded_map: DeprecationMap):
        # The invariant: registering another owner of the same member name must never
        # remove the hint (it used to flip FrameProcessor.setup to plain "current").
        before = _run(loaded_map, "FrameProcessor.setup")
        assert _is_hint(before) and before["candidates_total"] == 1
        entries = list(loaded_map.entries.values()) + [_entry("Sibling.setup")]
        after = _run(_synthetic(entries), "FrameProcessor.setup")
        assert _is_hint(after)
        assert after["candidates_total"] == 2
        assert {c["symbol"] for c in after["candidates"]} == {"TaskManager.setup", "Sibling.setup"}
        assert "under other owners" in after["note"]
        assert "`Sibling.setup`" in after["note"] and "`TaskManager.setup`" in after["note"]
        assert "a listed symbol" in after["note"]

    @pytest.mark.parametrize("owners", [1, 2, CAP - 1, CAP, CAP + 1, CAP + 2])
    def test_dotted_hint_iff_the_candidate_list_is_complete(self, owners: int):
        dep = _synthetic([_entry(f"Home{i:02d}.shared") for i in range(owners)])
        out = _run(dep, "Queried.shared")
        if owners <= CAP:
            assert _is_hint(out)
            # Never truncated: every owner is listed.
            assert len(out["candidates"]) == out["candidates_total"] == owners
        else:
            assert out["status"] == "current"
            assert out["candidates"] is None
            assert out["candidates_total"] is None

    def test_bare_hint_note_is_unchanged(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "trigger_user_turn_stopped")
        assert "matched no symbol exactly" in out["note"]
        assert "another owner" not in out["note"]

    def test_many_homes_stays_current(self, loaded_map: DeprecationMap):
        assert "OpenAILLMServiceX.model" not in loaded_map.entries
        out = _run(loaded_map, "OpenAILLMServiceX.model")
        assert out["status"] == "current"
        assert out["candidates"] is None

    def test_dotted_exact_key_keeps_verdict_without_candidates(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "OpenAILLMService.model")
        assert out["deprecated"] is True
        assert out["candidates"] is None
        assert out["candidates_total"] is None

    def test_dotted_miss_ending_in_input_params_stays_current(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "SomeService.InputParams")
        assert out["status"] == "current"
        assert out["candidates"] is None

    def test_module_qualified_miss_stays_current(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "pipecat.not.a.module.trigger_user_turn_stopped")
        assert out["status"] == "current"
        assert out["candidates"] is None

    def test_lower_case_owner_stays_current(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "someclass.trigger_user_turn_stopped")
        assert out["status"] == "current"
        assert out["candidates"] is None

    def test_sole_current_candidate_still_hints(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "Sub.trigger_user_turn_stopped", version="1.14.0")
        assert out["status"] is None
        assert out["deprecated"] is False
        assert out["candidates_total"] == 1
        assert out["candidates"][0]["status"] == "current"

    def test_sole_removed_candidate_hints(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "SomeSTT.set_prompt")
        assert out["status"] is None
        assert out["candidates"][0]["symbol"] == "SarvamSTTService.set_prompt"
        assert out["candidates"][0]["status"] == "removed"

    def test_two_homes_one_current_still_hints(self):
        dep = _synthetic(
            [_entry("A.foo", deprecated_in="1.0.0"), _entry("B.foo", deprecated_in="3.0.0")]
        )
        out = _run(dep, "Sub.foo", version="2.0.0")
        assert out["status"] is None
        assert out["candidates_total"] == 2
        assert [c["symbol"] for c in out["candidates"]] == ["A.foo", "B.foo"]
        assert [c["status"] for c in out["candidates"]] == ["deprecated", "current"]


class TestRemovals:
    def test_removal_bare_name_is_removed_candidate(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "set_prompt")
        assert out["deprecated"] is False
        assert out["status"] is None
        assert out["candidates_total"] == 1
        (cand,) = out["candidates"]
        assert cand["symbol"] == "SarvamSTTService.set_prompt"
        assert cand["status"] == "removed"
        assert cand["removed_in"]


class TestVersionAware:
    def test_status_by_version(self, loaded_map: DeprecationMap):
        sym = "trigger_user_turn_stopped"
        entry = loaded_map.members_named(sym)[0][1]
        cases = {
            None: entry.status,
            "1.14.0": "current",
            "1.15.0": "deprecated",
            "1.16.0": "deprecated",
            "2.0.0": status_for(entry, "2.0.0"),
        }
        for version, expected in cases.items():
            out = _run(loaded_map, sym, version=version)
            assert out["candidates"][0]["status"] == expected, version
        # An active deprecation is never reported removed; a recorded removal is.
        removal = _run(loaded_map, "set_prompt", version="2.0.0")
        assert removal["candidates"][0]["status"] == "removed"

    def test_indexed_version_default(self, loaded_map: DeprecationMap):
        out = _run(loaded_map, "trigger_user_turn_stopped", fw="1.14.0")
        assert out["candidates"][0]["status"] == "current"
        # An explicit version wins over the indexed default.
        out = _run(loaded_map, "trigger_user_turn_stopped", version="1.16.0", fw="1.14.0")
        assert out["candidates"][0]["status"] == "deprecated"

    def test_all_current_note_says_none_deprecated_and_stays_a_hint(
        self, loaded_map: DeprecationMap
    ):
        out = _run(loaded_map, "trigger_user_turn_stopped", version="1.14.0")
        assert out["deprecated"] is False
        assert out["status"] is None
        assert out["candidates"]
        assert "none" in str(out["note"]).lower()
        # The note names the version it evaluated, explicit or the indexed default.
        assert "None is deprecated at 1.14.0." in str(out["note"])
        out = _run(loaded_map, "trigger_user_turn_stopped", fw="1.14.0")
        assert "None is deprecated at 1.14.0." in str(out["note"])


class TestOutputShape:
    _BASE = {
        "deprecated",
        "status",
        "replacement",
        "deprecated_in",
        "removed_in",
        "announced_removed_in",
        "note",
        "kind",
        "relation",
        "location",
        "candidates",
        "candidates_total",
    }

    def test_key_set_identical_across_shapes(self, loaded_map: DeprecationMap):
        exact = _run(loaded_map, "BaseUserTurnStopStrategy.trigger_user_turn_stopped")
        current = _run(loaded_map, "NotDeprecatedThing")
        hint = _run(loaded_map, "trigger_user_turn_stopped")
        for out in (exact, current, hint):
            assert set(out) == self._BASE
        assert exact["candidates"] is None and exact["candidates_total"] is None
        assert current["candidates"] is None and current["candidates_total"] is None
        assert hint["candidates"] and hint["candidates_total"] == 1

    def test_defaults_are_none(self):
        out = CheckDeprecationOutput(deprecated=False)
        assert out.candidates is None and out.candidates_total is None

    def test_round_trip_with_candidates(self):
        out = CheckDeprecationOutput(
            deprecated=False,
            candidates=[
                DeprecationCandidate(symbol="A.foo", kind="method", status="current"),
                DeprecationCandidate(
                    symbol="B.foo",
                    status="removed",
                    removed_in="1.8.0",
                    deprecated_in="1.0.0",
                    replacement="bar",
                ),
            ],
            candidates_total=2,
        )
        again = CheckDeprecationOutput.model_validate_json(out.model_dump_json())
        assert again == out


class TestUnloadableMapPrecedence:
    def test_note_wins_and_no_candidates(self):
        dep = DeprecationMap(
            entries={"A.foo": _entry("A.foo")},
            load_error="unreadable",
        )
        assert dep.members_named("foo")  # members present: only the load_error can win
        out = _run(dep, "foo")
        assert out["deprecated"] is False
        assert out["status"] is None
        assert "could not be read" in str(out["note"])
        assert out["candidates"] is None
        assert out["candidates_total"] is None


class TestInputEdgeCases:
    @pytest.mark.parametrize(
        "symbol",
        [
            "",
            " ",
            "  trigger_user_turn_stopped  ",
            " trigger_user_turn_stopped",
            "trigger_user_turn_stopped ",
            "trigger_user_turn_stopped\n",
            "\ttrigger_user_turn_stopped",
            ".trigger_user_turn_stopped",
            "trigger_user_turn_stopped.",
            "trigger_user_turn_stopped()",
            "Model",
            "MODEL",
        ],
    )
    def test_match_nothing(self, loaded_map: DeprecationMap, symbol: str):
        out = _run(loaded_map, symbol)
        assert out["deprecated"] is False
        assert out["status"] == "current"
        assert out["candidates"] is None
        assert out["candidates_total"] is None


class TestEmptyNameMatchesNothing:
    """A persisted map may hold a method subject ``Owner.`` (empty last segment).

    ``CheckDeprecationInput`` accepts ``symbol=""``, so an empty bare query must
    not list that subject as a candidate: empty input matches nothing.
    """

    @pytest.mark.parametrize("symbol", ["", " ", ".", "Owner. "])
    def test_empty_member_subject_is_never_listed(self, symbol: str):
        dep = _synthetic([_entry("Owner."), _entry("Owner.real_name")])
        out = _run(dep, symbol)
        assert out["deprecated"] is False
        assert out["status"] == "current"
        assert out["candidates"] is None
        assert out["candidates_total"] is None

    def test_neighbouring_member_still_hints(self):
        dep = _synthetic([_entry("Owner."), _entry("Owner.real_name")])
        out = _run(dep, "real_name")
        assert _is_hint(out)
        assert [c["symbol"] for c in out["candidates"]] == ["Owner.real_name"]


class TestDottedSymbolSegments:
    """A dotted symbol is valid only when EVERY segment is a well-formed identifier.

    State space: empty segments (leading dot / trailing dot / interior) x
    whitespace (leading on the first segment, around an earlier segment, on the
    owner, on the last segment) x non-identifier text (spaces inside, ``()``,
    hyphen, leading digit) x lowercase or uppercase owner x owner count within or over the candidate cap x
    ASCII or unicode identifier. Only the well-formed rows whose owners fit in the cap hint.
    """

    @pytest.mark.parametrize(
        ("symbol", "hints"),
        [
            ("Sub.trigger_user_turn_stopped", True),
            ("pipecat.x.Sub.trigger_user_turn_stopped", True),
            (".Sub.trigger_user_turn_stopped", False),
            ("..Sub.trigger_user_turn_stopped", False),
            ("Sub..trigger_user_turn_stopped", False),
            ("pipecat..Sub.trigger_user_turn_stopped", False),
            ("Sub.trigger_user_turn_stopped.", False),
            (".pipecat.x.Sub.trigger_user_turn_stopped", False),
            ("  pipecat.x.Sub.trigger_user_turn_stopped", False),
            ("Sub .trigger_user_turn_stopped", False),
            (" Sub.trigger_user_turn_stopped", False),
            ("Sub.trigger_user_turn_stopped ", False),
            ("a b.Sub.trigger_user_turn_stopped", False),
            ("x().Sub.trigger_user_turn_stopped", False),
            ("pipecat.x-y.Sub.trigger_user_turn_stopped", False),
            ("1pipecat.x.Sub.trigger_user_turn_stopped", False),
            ("Sub.trigger_user_turn_stopped()", False),
            ("pipecat.x.sub.trigger_user_turn_stopped", False),
            # Underscore-led (private) class owners are class-like; a lone or
            # lower-case underscore-led owner is not.
            ("_Priv.trigger_user_turn_stopped", True),
            ("pipecat.x._Priv.trigger_user_turn_stopped", True),
            ("__Priv.trigger_user_turn_stopped", True),
            ("_priv.trigger_user_turn_stopped", False),
            ("_.trigger_user_turn_stopped", False),
            ("__.trigger_user_turn_stopped", False),
            # Python keywords are identifiers to str.isidentifier but never a
            # valid dotted reference segment (owner or package).
            ("class.Sub.trigger_user_turn_stopped", False),
            ("pipecat.import.Sub.trigger_user_turn_stopped", False),
            ("None.trigger_user_turn_stopped", False),
            ("True.trigger_user_turn_stopped", False),
            ("pipecat.None.Sub.trigger_user_turn_stopped", False),
            # Documented: an uppercase-initial owner with one same-named member is read
            # as a member reference even inside a module-shaped path.
            ("pipecat.services.OpenAI.llm", True),
            ("Sub.model", False),
            ("p\u00e4ckage.Sub.trigger_user_turn_stopped", True),
            ("\u00dcnder.trigger_user_turn_stopped", True),
        ],
    )
    def test_only_wellformed_dotted_symbol_hints(
        self, loaded_map: DeprecationMap, symbol: str, hints: bool
    ):
        out = _run(loaded_map, symbol)
        assert out["deprecated"] is False
        if hints:
            assert _is_hint(out)
        else:
            assert out["status"] == "current"
            assert out["candidates"] is None
            assert out["candidates_total"] is None


class TestBareNameRuleSurfaces:
    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    def test_rule_is_one_sentence_about_listed_symbols(self):
        assert "query one of the listed symbols" in BARE_NAME_RULE

    def test_dispatch_description(self):
        from pipecat_context_hub.server.dispatch import iter_tool_definitions

        (tool,) = [t for t in iter_tool_definitions() if t.name == "check_deprecation"]
        assert self._norm(BARE_NAME_RULE) in self._norm(tool.description)

    def test_symbol_schema_description(self):
        desc = CheckDeprecationInput.model_json_schema()["properties"]["symbol"]["description"]
        assert self._norm(BARE_NAME_RULE) in self._norm(desc)

    def test_server_instructions(self):
        from pipecat_context_hub.server.main import _SERVER_INSTRUCTIONS

        assert self._norm(BARE_NAME_RULE) in self._norm(_SERVER_INSTRUCTIONS)
        assert "{BARE_NAME_RULE}" not in _SERVER_INSTRUCTIONS
