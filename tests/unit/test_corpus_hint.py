"""Tests for ``shared/corpus_hint.py``: the one "not indexed" predicate.

``indexed_repos`` / ``groups_not_indexed`` / ``configured_no_records`` are shared by the
corpus hint and ``get_hub_status``. Fixture indexes here are fakes exposing only
``get_counts_by_repo`` (no ``IndexStore``), so this module stays Windows-eligible.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from pipecat_context_hub.shared.corpus_hint import (
    attach_corpus_hint,
    configured_no_records,
    corpus_hint,
    groups_not_indexed,
    indexed_repos,
    missing_repos,
)
from pipecat_context_hub.shared.interfaces import IndexUnavailableError
from pipecat_context_hub.shared.repo_catalog import Catalog, CatalogGroup, CatalogRepo
from pipecat_context_hub.shared.support_links import RETRIEVAL_QUALITY_ISSUE_URL


class _CountsIndex:
    """Fake serving index exposing only ``get_counts_by_repo``."""

    def __init__(self, counts: Any) -> None:
        self._counts = counts

    def get_counts_by_repo(self) -> Any:
        return self._counts


class _RaisingIndex:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_counts_by_repo(self) -> Any:
        raise self._exc


class _NoMethodIndex:
    """A fake lacking ``get_counts_by_repo`` entirely."""


def _group(name: str, *repos: CatalogRepo, aliases: tuple[str, ...] = ()) -> CatalogGroup:
    return CatalogGroup(
        name=name,
        kind="vendor",
        description=f"{name} group",
        aliases=aliases or (name,),
        repos=tuple(repos),
    )


def _repo(slug: str, *, parseable: bool = True) -> CatalogRepo:
    return CatalogRepo(slug=slug, parseable=parseable)


def _catalog() -> Catalog:
    return Catalog(
        verified=date(2026, 10, 17),
        groups=(
            _group("full", _repo("acme/full-one"), _repo("acme/full-two")),
            _group("partial", _repo("acme/part-have"), _repo("acme/part-missing")),
            _group(
                "unparseable",
                _repo("acme/unparse-one", parseable=False),
                _repo("acme/unparse-two", parseable=False),
            ),
        ),
    )


def _names(groups: Any) -> list[str]:
    return [g.name if hasattr(g, "name") else g for g in groups]


class TestIndexedRepos:
    def test_synthetic_docs_key_is_absent(self) -> None:
        idx = _CountsIndex({"acme/full-one": 5, "docs.pipecat.ai": 900})
        result = indexed_repos(idx)
        assert result is not None
        assert "docs.pipecat.ai" not in result
        assert "acme/full-one" in result

    def test_keys_are_casefolded(self) -> None:
        idx = _CountsIndex({"Owner/Repo": 3})
        result = indexed_repos(idx)
        assert result is not None
        assert "owner/repo" in result
        assert "Owner/Repo" not in result

    def test_zero_record_repo_is_not_indexed(self) -> None:
        idx = _CountsIndex({"acme/empty": 0, "acme/real": 2})
        result = indexed_repos(idx)
        assert result is not None
        assert "acme/empty" not in result
        assert "acme/real" in result

    def test_empty_but_readable_index_is_an_empty_set_not_none(self) -> None:
        result = indexed_repos(_CountsIndex({}))
        assert result is not None
        assert len(result) == 0

    def test_bare_magicmock_store_is_unreadable(self) -> None:
        assert indexed_repos(MagicMock()) is None

    def test_missing_method_is_unreadable(self) -> None:
        assert indexed_repos(cast("Any", _NoMethodIndex())) is None

    @pytest.mark.parametrize(
        "exc",
        [IndexUnavailableError("refreshing"), RuntimeError("boom"), OSError("disk")],
    )
    def test_raise_is_unreadable(self, exc: Exception) -> None:
        assert indexed_repos(_RaisingIndex(exc)) is None

    @pytest.mark.parametrize("value", [None, [("a/b", 1)], "a/b", 3])
    def test_non_mapping_result_is_unreadable(self, value: Any) -> None:
        assert indexed_repos(_CountsIndex(value)) is None

    def test_unreadable_count_logs_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert indexed_repos(_RaisingIndex(RuntimeError("boom"))) is None
        assert any(r.levelno >= logging.WARNING for r in caplog.records)


class TestGroupsNotIndexed:
    def test_fully_indexed_group_does_not_fire(self) -> None:
        indexed = {"acme/full-one", "acme/full-two"}
        assert "full" not in _names(groups_not_indexed(_catalog(), indexed))

    def test_partly_indexed_group_fires(self) -> None:
        indexed = {"acme/full-one", "acme/full-two", "acme/part-have"}
        assert "partial" in _names(groups_not_indexed(_catalog(), indexed))

    def test_unindexed_group_fires(self) -> None:
        assert "full" in _names(groups_not_indexed(_catalog(), set()))

    def test_parseable_false_only_group_never_fires(self) -> None:
        assert "unparseable" not in _names(groups_not_indexed(_catalog(), set()))

    def test_parseable_false_member_does_not_keep_a_group_firing(self) -> None:
        catalog = Catalog(
            verified=date(2026, 10, 17),
            groups=(_group("mixed", _repo("acme/a"), _repo("acme/b", parseable=False)),),
        )
        assert _names(groups_not_indexed(catalog, {"acme/a"})) == []

    def test_catalog_order_is_preserved(self) -> None:
        assert _names(groups_not_indexed(_catalog(), set())) == ["full", "partial"]

    def test_catalog_slug_matching_is_casefolded(self) -> None:
        catalog = Catalog(
            verified=date(2026, 10, 17),
            groups=(_group("mixed-case", _repo("Acme/Mixed-Case")),),
        )
        assert _names(groups_not_indexed(catalog, {"acme/mixed-case"})) == []

    def test_unreadable_count_returns_empty_list(self) -> None:
        # None must mean "no hint", never "everything is unindexed".
        assert groups_not_indexed(_catalog(), None) == []

    def test_unreadable_differs_from_empty_index(self) -> None:
        assert groups_not_indexed(_catalog(), set()) != []
        assert groups_not_indexed(_catalog(), None) == []


class TestConfiguredNoRecords:
    def test_configured_without_records_is_listed(self) -> None:
        result = configured_no_records(["acme/empty", "acme/real"], {"acme/real"})
        assert [s.casefold() for s in result] == ["acme/empty"]

    def test_case_variant_lands_in_indexed_not_no_records(self) -> None:
        # Configured "Owner/Repo", index key "owner/repo" (via indexed_repos casefolding).
        indexed = indexed_repos(_CountsIndex({"owner/repo": 4}))
        assert indexed is not None
        assert list(configured_no_records(["Owner/Repo"], indexed)) == []

    def test_case_variant_end_to_end_through_indexed_repos(self) -> None:
        indexed = indexed_repos(_CountsIndex({"Owner/Repo": 4}))
        assert indexed is not None
        assert list(configured_no_records(["owner/repo"], indexed)) == []
        assert list(configured_no_records(["Owner/Repo"], indexed)) == []

    def test_nothing_configured(self) -> None:
        assert list(configured_no_records([], {"acme/real"})) == []


class TestTaintedSkipInThePredicate:
    """``groups_not_indexed`` / ``missing_repos`` take a keyword-only casefolded ``tainted`` set,
    shared by the hint and ``get_hub_status`` (one predicate, not two)."""

    def test_tainted_defaults_to_empty(self) -> None:
        group = _group("g", _repo("acme/a"))
        assert [r for r in missing_repos(group, set())] == ["acme/a"]
        assert _names(groups_not_indexed(_catalog(), set())) == ["full", "partial"]

    def test_tainted_repo_is_not_missing(self) -> None:
        group = _group("g", _repo("acme/a"), _repo("acme/b"))
        assert missing_repos(group, set(), tainted=frozenset({"acme/a"})) == ["acme/b"]

    def test_group_whose_only_unindexed_repo_is_tainted_does_not_fire(self) -> None:
        catalog = Catalog(
            verified=date(2026, 10, 17),
            groups=(_group("g", _repo("acme/have"), _repo("acme/bad")),),
        )
        assert _names(groups_not_indexed(catalog, {"acme/have"})) == ["g"]
        assert (
            _names(groups_not_indexed(catalog, {"acme/have"}, tainted=frozenset({"acme/bad"})))
            == []
        )

    def test_tainted_match_is_casefolded_against_catalog_slugs(self) -> None:
        catalog = Catalog(
            verified=date(2026, 10, 17),
            groups=(_group("g", _repo("Acme/Bad")),),
        )
        assert _names(groups_not_indexed(catalog, set(), tainted=frozenset({"acme/bad"}))) == []

    def test_tainted_does_not_hide_other_unindexed_repos(self) -> None:
        catalog = Catalog(
            verified=date(2026, 10, 17),
            groups=(_group("g", _repo("acme/bad"), _repo("acme/other")),),
        )
        assert _names(groups_not_indexed(catalog, set(), tainted=frozenset({"acme/bad"}))) == ["g"]

    def test_unreadable_count_still_wins_over_tainted(self) -> None:
        assert groups_not_indexed(_catalog(), None, tainted=frozenset({"acme/x"})) == []


# --- corpus_hint ---------------------------------------------------------------------------

_HINT_CATALOG = """
verified = 2026-10-17

[groups.aws]
kind = "vendor"
description = "AWS"
aliases = ["aws", "nova sonic"]
repos = [{ slug = "aws/one" }, { slug = "aws/two" }, { slug = "aws/three" }]

[groups.nvidia]
kind = "vendor"
description = "NVIDIA"
aliases = ["nvidia"]
repos = [{ slug = "nv/one" }]

[groups.murf]
kind = "vendor"
description = "Murf"
aliases = ["murf"]
repos = [{ slug = "murf/one" }]

[groups.swift]
kind = "topic"
description = "Swift only"
aliases = ["swiftkit"]
repos = [{ slug = "sw/one", parseable = false }]

[groups.shady]
kind = "vendor"
description = "Shady"
aliases = ["shady"]
repos = [{ slug = "bad/one" }]
"""

_REPORT_KEYS = {
    "matched_groups",
    "repos_not_indexed",
    "repos_configured_no_records",
    "add_command",
    "refresh_command",
    "report_issue_url",
}


@pytest.fixture
def hint_catalog(monkeypatch: pytest.MonkeyPatch) -> Any:
    from pipecat_context_hub.shared import repo_catalog

    repo_catalog.load_catalog.cache_clear()
    monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: _HINT_CATALOG)
    yield
    repo_catalog.load_catalog.cache_clear()


def _hint(
    query: str,
    counts: Any = None,
    *,
    tool: str = "search_docs",
    args: dict[str, Any] | None = None,
    configured: frozenset[str] = frozenset(),
    tainted: frozenset[str] = frozenset(),
    index: Any = None,
) -> dict[str, Any] | None:
    store = index if index is not None else _CountsIndex({} if counts is None else counts)
    return corpus_hint(
        store,
        tool=tool,
        args={"query": query} if args is None else args,
        configured_repos=configured,
        tainted_repos=tainted,
    )


@pytest.mark.usefixtures("hint_catalog")
class TestCorpusHint:
    def test_unindexed_vendor_query_hints(self) -> None:
        hint = _hint("nvidia voice agent")
        assert hint is not None
        assert hint["matched_groups"] == ["nvidia"]
        assert hint["repos_not_indexed"] == ["nv/one"]
        assert hint["report_issue_url"] == RETRIEVAL_QUALITY_ISSUE_URL
        assert set(hint) <= _REPORT_KEYS

    def test_fully_indexed_group_gives_no_hint(self) -> None:
        assert _hint("nvidia voice agent", {"nv/one": 4}) is None

    def test_query_matching_no_alias_gives_no_hint(self) -> None:
        assert _hint("how do I add a custom frame processor") is None

    def test_alias_match_is_case_insensitive(self) -> None:
        hint = _hint("NVIDIA Riva setup")
        assert hint is not None
        assert hint["matched_groups"] == ["nvidia"]

    def test_multiword_alias_matches(self) -> None:
        hint = _hint("Nova Sonic voice agent")
        assert hint is not None
        assert hint["matched_groups"] == ["aws"]

    @pytest.mark.parametrize("query", ["laws of robotics", "flaws in awsome code", "claws"])
    def test_alias_needs_a_word_boundary(self, query: str) -> None:
        # "aws" inside "laws" must not match.
        assert _hint(query) is None

    @pytest.mark.parametrize("query", ["TTS + nvidia", "TTS & nvidia"])
    def test_concept_queries_match_every_concept(self, query: str) -> None:
        hint = _hint(query)
        assert hint is not None
        assert hint["matched_groups"] == ["nvidia"]

    def test_partial_group_lists_only_the_missing_repos(self) -> None:
        hint = _hint("aws bot", {"aws/one": 3})
        assert hint is not None
        assert hint["repos_not_indexed"] == ["aws/two", "aws/three"]

    def test_matched_groups_follow_catalog_order(self) -> None:
        # Query names nvidia first, aws second; the catalog lists aws first.
        hint = _hint("nvidia and aws together")
        assert hint is not None
        assert hint["matched_groups"] == ["aws", "nvidia"]

    def test_parseable_false_only_group_never_hints(self) -> None:
        assert _hint("swiftkit please") is None

    def test_tainted_only_group_never_hints(self) -> None:
        assert _hint("shady business", tainted=frozenset({"bad/one"})) is None

    def test_tainted_repo_is_in_neither_field(self) -> None:
        hint = _hint("aws bot", {"aws/one": 1}, tainted=frozenset({"aws/two"}))
        assert hint is not None
        assert "aws/two" not in hint["repos_not_indexed"]
        assert "aws/two" not in hint.get("repos_configured_no_records", [])
        assert "aws/two" not in json.dumps(hint)

    def test_tainted_match_is_casefolded(self) -> None:
        assert _hint("shady", tainted=frozenset({"BAD/One"})) is None

    def test_add_command_golden_single_group(self) -> None:
        hint = _hint("nvidia")
        assert hint is not None
        assert hint["add_command"] == "pipecat-context-hub repos add --config-only --group nvidia"

    def test_add_command_joins_groups_with_semicolon_in_catalog_order(self) -> None:
        hint = _hint("nvidia aws")
        assert hint is not None
        assert hint["add_command"] == (
            "pipecat-context-hub repos add --config-only --group aws; "
            "pipecat-context-hub repos add --config-only --group nvidia"
        )

    def test_add_command_never_uses_unlisted(self) -> None:
        hint = _hint("nvidia aws")
        assert hint is not None
        assert "--unlisted" not in hint["add_command"]

    def test_add_command_skips_group_whose_missing_repos_are_all_configured(self) -> None:
        hint = _hint("nvidia aws", configured=frozenset({"nv/one"}))
        assert hint is not None
        assert hint["add_command"] == "pipecat-context-hub repos add --config-only --group aws"

    def test_configured_without_records_goes_to_its_own_field_with_refresh(self) -> None:
        hint = _hint("aws bot", {"aws/one": 2}, configured=frozenset({"aws/two"}))
        assert hint is not None
        assert hint["repos_configured_no_records"] == ["aws/two"]
        assert hint["repos_not_indexed"] == ["aws/three"]
        assert hint["refresh_command"] == "pipecat-context-hub refresh"

    def test_only_configured_no_records_hints_without_add_command(self) -> None:
        hint = _hint("nvidia", configured=frozenset({"nv/one"}))
        assert hint is not None
        assert hint["repos_configured_no_records"] == ["nv/one"]
        assert hint.get("repos_not_indexed", []) == []
        assert "add_command" not in hint
        assert hint["refresh_command"] == "pipecat-context-hub refresh"

    def test_no_refresh_command_without_configured_no_records(self) -> None:
        hint = _hint("nvidia")
        assert hint is not None
        assert "refresh_command" not in hint
        assert not hint.get("repos_configured_no_records")

    def test_case_variant_configured_slug_is_not_configured_no_records(self) -> None:
        # Configured "Aws/One", index key "aws/one": indexed, so not "configured, no records".
        hint = _hint("aws bot", {"aws/one": 3}, configured=frozenset({"Aws/One"}))
        assert hint is not None
        assert not hint.get("repos_configured_no_records")
        assert "refresh_command" not in hint

    def test_case_variant_configured_slug_is_not_in_repos_not_indexed(self) -> None:
        hint = _hint("aws bot", {"aws/one": 1}, configured=frozenset({"AWS/Two"}))
        assert hint is not None
        assert "aws/two" not in [s.casefold() for s in hint["repos_not_indexed"]]
        assert [s.casefold() for s in hint["repos_configured_no_records"]] == ["aws/two"]

    def test_docs_pipecat_ai_key_does_not_count_as_indexed(self) -> None:
        assert _hint("nvidia", {"docs.pipecat.ai": 900}) is not None


@pytest.mark.usefixtures("hint_catalog")
class TestCorpusHintPerTool:
    @pytest.mark.parametrize("tool", ["search_docs", "search_examples", "search_api"])
    def test_search_tools_read_query(self, tool: str) -> None:
        assert _hint("nvidia", tool=tool) is not None
        # A symbol/intent on these tools is not the matched text.
        assert _hint("x", tool=tool, args={"query": "x", "symbol": "nvidia"}) is None

    def test_get_code_snippet_reads_symbol(self) -> None:
        assert _hint("", tool="get_code_snippet", args={"symbol": "NvidiaSTTService nvidia"})

    def test_get_code_snippet_reads_intent(self) -> None:
        assert _hint("", tool="get_code_snippet", args={"intent": "nvidia riva setup"})

    def test_get_code_snippet_path_alone_never_hints(self) -> None:
        args = {"path": "src/pipecat/services/nvidia/stt.py"}
        assert _hint("", tool="get_code_snippet", args=args) is None

    def test_get_code_snippet_ignores_query_key(self) -> None:
        assert _hint("", tool="get_code_snippet", args={"query": "nvidia"}) is None

    @pytest.mark.parametrize("tool", ["get_doc", "get_example", "check_deprecation"])
    def test_non_search_tools_never_hint(self, tool: str) -> None:
        args = {"query": "nvidia", "symbol": "nvidia", "intent": "nvidia", "path": "nvidia"}
        assert _hint("", tool=tool, args=args) is None

    def test_hinting_tool_set_is_exactly_the_four_search_tools(self) -> None:
        """Drift guard: the per-tool text table must cover exactly the four Requirement 2
        tools, and every one must be a registered tool. Pinned here, not derived from input
        model fields (``CheckDeprecationInput`` also has ``symbol``)."""
        from pipecat_context_hub.server.dispatch import iter_tool_definitions

        registered = {d.name for d in iter_tool_definitions(include_hub_status=True)}
        expected = {"search_docs", "search_examples", "search_api", "get_code_snippet"}
        assert expected <= registered
        args = {"query": "nvidia", "symbol": "nvidia", "intent": "nvidia"}
        hinting = {t for t in registered if _hint("", tool=t, args=args) is not None}
        assert hinting == expected

    def test_unknown_tool_never_hints(self) -> None:
        assert _hint("nvidia", tool="not_a_tool") is None

    def test_missing_args_never_raise(self) -> None:
        assert _hint("", tool="search_docs", args={}) is None
        assert _hint("", tool="get_code_snippet", args={}) is None


@pytest.mark.usefixtures("hint_catalog")
class TestCorpusHintUnreadableCounts:
    """An unreadable count is "no hint", never "everything is unindexed"."""

    def test_raising_store(self) -> None:
        assert _hint("nvidia", index=_RaisingIndex(RuntimeError("boom"))) is None

    def test_index_unavailable_store(self) -> None:
        assert _hint("nvidia", index=_RaisingIndex(IndexUnavailableError("refreshing"))) is None

    def test_store_lacking_the_method(self) -> None:
        assert _hint("nvidia", index=cast("Any", _NoMethodIndex())) is None

    def test_bare_magicmock_store(self) -> None:
        assert _hint("nvidia", index=MagicMock()) is None

    def test_readable_empty_index_does_hint(self) -> None:
        assert _hint("nvidia", {}) is not None


class TestCorpusHintCatalogLoadFailure:
    def test_broken_catalog_gives_no_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pipecat_context_hub.shared import repo_catalog

        repo_catalog.load_catalog.cache_clear()
        monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: "not [valid toml")
        try:
            assert _hint("nvidia aws murf") is None
        finally:
            repo_catalog.load_catalog.cache_clear()


class TestCorpusHintNeverTouchesNetworkOrEmbeddings:
    @pytest.mark.usefixtures("hint_catalog")
    def test_only_the_counts_method_is_called(self) -> None:
        store = MagicMock(spec=["get_counts_by_repo"])
        store.get_counts_by_repo.return_value = {}
        assert _hint("nvidia", index=store) is not None
        store.get_counts_by_repo.assert_called_once_with()


class TestAttachCorpusHint:
    def test_none_hint_returns_input_unchanged(self) -> None:
        text = '{"hits": [],  "x": 1}'
        assert attach_corpus_hint(text, None) == text

    def test_attaches_top_level_key(self) -> None:
        hint = {"matched_groups": ["g"], "report_issue_url": RETRIEVAL_QUALITY_ISSUE_URL}
        out = json.loads(attach_corpus_hint('{"hits": [1]}', hint))
        assert out["hits"] == [1]
        assert out["corpus_hint"] == hint

    def test_high_confidence_result_still_carries_the_hint(self) -> None:
        # Confidence is not an input: a 0.95 result keeps the hint.
        result = json.dumps({"hits": [{"id": "x"}], "evidence": {"confidence": 0.95}})
        out = json.loads(attach_corpus_hint(result, {"matched_groups": ["g"]}))
        assert out["evidence"]["confidence"] == 0.95
        assert out["corpus_hint"]["matched_groups"] == ["g"]

    def test_existing_staleness_footer_is_preserved(self) -> None:
        result = json.dumps({"hits": [], "index_staleness": {"hint": "refresh"}})
        out = json.loads(attach_corpus_hint(result, {"matched_groups": ["g"]}))
        assert out["index_staleness"] == {"hint": "refresh"}

    @pytest.mark.parametrize("text", ["not json", "[1, 2]", "3", ""])
    def test_non_object_result_is_returned_unchanged(self, text: str) -> None:
        assert attach_corpus_hint(text, {"matched_groups": ["g"]}) == text


# Frozen real queries (2026-10-17), copied verbatim. The verdicts below come from the shipped
# catalog and a fixture index, never from the live index.
_FROZEN_AWS = "AWS Bedrock Nova Sonic voice agent"
_FROZEN_TAVUS = "Tavus avatar"
_FROZEN_MURF = "Murf TTS"
_FROZEN_SUPERMEMORY = "Supermemory memory"

_AWS_INDEXED = {
    "aws-samples/build-intelligent-ai-voice-agents-with-pipecat-and-amazon-bedrock": 435,
    "pipecat-ai/aws-deepgram-workshop": 390,
    "pipecat-ai/aws-workshops": 33,
}


class TestFrozenRealQueries:
    @pytest.fixture(autouse=True)
    def _shipped_catalog(self) -> Any:
        from pipecat_context_hub.shared import repo_catalog

        repo_catalog.load_catalog.cache_clear()
        yield
        repo_catalog.load_catalog.cache_clear()

    @pytest.mark.parametrize("tool", ["search_docs", "search_examples"])
    def test_aws_partly_indexed_still_hints(self, tool: str) -> None:
        # The live 0.95 top hit (aaronsu11/Dum-E) is not an AWS repo: confidence must not
        # suppress the hint.
        hint = _hint(_FROZEN_AWS, {**_AWS_INDEXED, "aaronsu11/Dum-E": 50}, tool=tool)
        assert hint is not None
        assert hint["matched_groups"] == ["aws"]
        assert not set(_AWS_INDEXED) & {s for s in hint["repos_not_indexed"]}
        assert hint["repos_not_indexed"]

    def test_tavus_unindexed_hints(self) -> None:
        hint = _hint(_FROZEN_TAVUS, _AWS_INDEXED)
        assert hint is not None
        assert hint["matched_groups"] == ["tavus"]
        assert hint["repos_not_indexed"] == ["aws-samples/sample-voice-ai-tavus-avatar-demo"]

    def test_tavus_indexed_gives_no_hint(self) -> None:
        assert _hint(_FROZEN_TAVUS, {"aws-samples/sample-voice-ai-tavus-avatar-demo": 9}) is None

    def test_murf_indexed_gives_no_hint(self) -> None:
        assert _hint(_FROZEN_MURF, {"murf-ai/pipecat-murf-tts": 24}) is None

    def test_murf_unindexed_hints(self) -> None:
        hint = _hint(_FROZEN_MURF, {})
        assert hint is not None
        assert hint["matched_groups"] == ["murf"]

    def test_supermemory_indexed_gives_no_hint(self) -> None:
        assert _hint(_FROZEN_SUPERMEMORY, {"supermemoryai/pipecat-memory": 462}) is None

    def test_supermemory_unindexed_hints(self) -> None:
        hint = _hint(_FROZEN_SUPERMEMORY, {})
        assert hint is not None
        assert hint["matched_groups"] == ["supermemory"]
