"""Tests for ``shared/corpus_hint.py``: the one "not indexed" predicate.

``indexed_repos`` / ``groups_not_indexed`` / ``configured_no_records`` are shared by the
corpus hint and ``get_hub_status``. Fixture indexes here are fakes exposing only
``get_counts_by_repo`` (no ``IndexStore``), so this module stays Windows-eligible.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.shared.corpus_hint import (
    attach_corpus_hint,
    configured_no_records,
    corpus_hint,
    groups_not_indexed,
    indexed_repos,
    missing_repos,
)
from pipecat_context_hub.shared.interfaces import DOCS_COUNT_KEY, IndexUnavailableError
from pipecat_context_hub.shared.repo_catalog import Catalog, CatalogGroup, CatalogRepo
from pipecat_context_hub.shared.response_finish import finish_response
from pipecat_context_hub.shared.support_links import RETRIEVAL_QUALITY_ISSUE_URL
from pipecat_context_hub.shared.types import ChunkedRecord


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
    def test_the_real_docs_key_is_absent(self, tmp_path: Path) -> None:
        # Producer and consumer of the synthetic docs key agree: a real FTS index
        # with a doc chunk reports DOCS_COUNT_KEY, and indexed_repos skips it.
        fts = FTSIndex(tmp_path / "metadata.db", create=True)
        try:
            common: dict[str, Any] = {
                "content": "pipecat",
                "source_url": "https://example.invalid/x",
                "path": "/x.md",
                "indexed_at": datetime.now(tz=UTC),
                "embedding": [0.0],
            }
            fts.upsert(
                [
                    ChunkedRecord(chunk_id="c-doc", content_type="doc", repo=None, **common),
                    ChunkedRecord(
                        chunk_id="c-code", content_type="code", repo="acme/full-one", **common
                    ),
                ]
            )
            assert DOCS_COUNT_KEY in fts.get_counts_by_repo()
            assert indexed_repos(fts) == frozenset({"acme/full-one"})
        finally:
            fts.close()

    def test_every_chunk_is_counted_exactly_once(self, tmp_path: Path) -> None:
        # Crawled doc chunks (no repo) count under DOCS_COUNT_KEY only; a repo's
        # README fallback is content_type "doc" too but carries its repo, so it
        # counts under that repo and not again under DOCS_COUNT_KEY.
        fts = FTSIndex(tmp_path / "metadata.db", create=True)
        try:
            common: dict[str, Any] = {
                "content": "pipecat",
                "source_url": "https://example.invalid/x",
                "path": "/x.md",
                "indexed_at": datetime.now(tz=UTC),
                "embedding": [0.0],
            }
            fts.upsert(
                [
                    ChunkedRecord(chunk_id="c-doc", content_type="doc", repo=None, **common),
                    ChunkedRecord(
                        chunk_id="c-code", content_type="code", repo="acme/full-one", **common
                    ),
                    ChunkedRecord(
                        chunk_id="c-readme",
                        content_type="doc",
                        repo="acme/full-two",
                        metadata={"is_readme_fallback": True},
                        **common,
                    ),
                ]
            )
            counts = fts.get_counts_by_repo()
            assert counts == {DOCS_COUNT_KEY: 1, "acme/full-one": 1, "acme/full-two": 1}
            assert sum(counts.values()) == fts.get_index_stats()["total"]
        finally:
            fts.close()

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

    def test_tainted_configured_repo_is_never_listed(self) -> None:
        # Same keyword-only casefolded ``tainted`` as ``missing_repos`` /
        # ``groups_not_indexed``: no caller filters tainted slugs by hand.
        result = configured_no_records(
            ["acme/empty", "Acme/Bad"], set(), tainted=frozenset({"acme/BAD"})
        )
        assert result == ["acme/empty"]

    def test_tainted_is_ignored_when_counts_are_unreadable(self) -> None:
        assert configured_no_records(["acme/x"], None, tainted=frozenset({"acme/x"})) == []


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

    def test_taint_rule_is_the_exclusion_fold_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The hint and status fold tainted slugs through the same registry entry as
        # ``SourceConfig.is_tainted``, so a change to that fold reaches all of them.
        from types import MappingProxyType

        from pipecat_context_hub.shared import exclusion_folds
        from pipecat_context_hub.shared.config import SourceConfig
        from pipecat_context_hub.shared.env_keys import TAINTED_REPOS_ENV
        from pipecat_context_hub.shared.repo_slug import slug_key

        def fold(slug: str) -> str:
            return slug_key(slug).removesuffix("-mirror")

        monkeypatch.setattr(
            exclusion_folds,
            "EXCLUSION_FOLDS",
            MappingProxyType({**exclusion_folds.EXCLUSION_FOLDS, TAINTED_REPOS_ENV: fold}),
        )
        monkeypatch.setenv(TAINTED_REPOS_ENV, "acme/bad-mirror")
        assert SourceConfig().is_tainted("acme/bad")

        tainted = frozenset({"acme/bad-mirror"})
        group = _group("g", _repo("acme/bad"), _repo("acme/other"))
        assert missing_repos(group, set(), tainted=tainted) == ["acme/other"]
        assert configured_no_records(["acme/bad", "acme/x"], set(), tainted=tainted) == ["acme/x"]
        catalog = Catalog(verified=date(2026, 10, 17), groups=(_group("g", _repo("acme/bad")),))
        assert groups_not_indexed(catalog, set(), tainted=tainted) == []

    def test_one_taint_matcher_for_config_and_hint(self) -> None:
        # The comparison itself lives once, in exclusion_folds.taint_matcher;
        # neither SourceConfig nor corpus_hint re-implements it.
        import inspect

        from pipecat_context_hub.shared import config, corpus_hint

        assert "taint_matcher(" in inspect.getsource(config.SourceConfig.is_tainted)
        hint_src = inspect.getsource(corpus_hint)
        assert "taint_matcher(" in hint_src
        assert "exclusion_fold(" not in hint_src

    def test_hint_and_repos_add_group_pick_the_same_members(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``add_command`` advertises ``repos add --group G``: with nothing indexed,
        # the hint's missing set must be exactly the set that command adds.
        from pipecat_context_hub.cli_repos import _skipped_lines
        from pipecat_context_hub.shared.config import SourceConfig
        from pipecat_context_hub.shared.env_keys import TAINTED_REPOS_ENV

        monkeypatch.setenv(TAINTED_REPOS_ENV, "ACME/Bad")
        sources = SourceConfig()
        group = _group(
            "g",
            _repo("acme/bad"),
            _repo("acme/raw", parseable=False),
            _repo("acme/ok"),
            _repo("Acme/Also"),
        )
        actionable, skipped = _skipped_lines(group, sources)
        hint_missing = missing_repos(group, set(), tainted=frozenset(sources.tainted_repos))
        assert hint_missing == actionable == ["acme/ok", "Acme/Also"]
        assert skipped == [
            "Skipped (tainted): acme/bad",
            "Skipped (parseable = false): acme/raw",
        ]


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

[groups.mobile]
kind = "topic"
description = "Mobile"
aliases = ["ios"]
repos = [{ slug = "mo/one" }]

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

    @pytest.mark.parametrize(
        ("query", "group"),
        [
            ("AWSBedrockLLMService", "aws"),
            ("aws_bedrock", "aws"),
            ("NovaSonicLLMService", "aws"),
            ("NvidiaSTTService", "nvidia"),
            ("nvidia_stt_service", "nvidia"),
            ("MurfTTSService", "murf"),
            # A digit before a capital is a word break too ("Llama3 Nvidia Service").
            ("Llama3NvidiaService", "nvidia"),
            # A run of underscores is one separator, so a multi-word alias still reads.
            ("nova__sonic", "aws"),
            ("Nova__Sonic", "aws"),
        ],
    )
    def test_identifier_shaped_query_hints(self, query: str, group: str) -> None:
        # CamelCase runs and underscores join \w, so the plain word boundary missed these.
        hint = _hint(query)
        assert hint is not None
        assert hint["matched_groups"] == [group]

    @pytest.mark.parametrize(
        "query",
        [
            "LawsOfRobotics",
            "FlawsInCode",
            "ClawsClient",
            "claws_of_x",
            "InnovaSonicBoom",
            "SmurfTTSService",
            "DrawsClient",
            "iosx_core",
            "aws2bedrock",
        ],
    )
    def test_identifier_split_needs_a_whole_token(self, query: str) -> None:
        # An alias must equal a whole camel/snake token (run), never a substring of one.
        assert _hint(query) is None

    def test_ios_alias_still_matches_the_original_text(self) -> None:
        # "iOS app" splits to "i OS app", which has no "ios" word: only the original-text
        # arm keeps this hinting.
        hint = _hint("iOS app")
        assert hint is not None
        assert hint["matched_groups"] == ["mobile"]

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
        # The ``class_name`` / ``module`` filters are topic text only where the tool has them.
        for key in ("class_name", "module"):
            hinted = _hint("x", tool=tool, args={"query": "x", key: "nvidia"}) is not None
            assert hinted == (tool == "search_api"), (tool, key)

    @pytest.mark.parametrize(
        "args",
        [
            {"query": "__init__", "class_name": "NvidiaSTTService"},
            {"query": "connect", "module": "pipecat.services.nvidia.stt"},
        ],
    )
    def test_search_api_reads_its_class_and_module_filters(self, args: dict[str, str]) -> None:
        assert _hint("", tool="search_api", args=args)

    def test_get_code_snippet_reads_symbol(self) -> None:
        assert _hint("", tool="get_code_snippet", args={"symbol": "NvidiaSTTService nvidia"})

    def test_get_code_snippet_reads_intent(self) -> None:
        assert _hint("", tool="get_code_snippet", args={"intent": "nvidia riva setup"})

    def test_get_code_snippet_reads_class_name(self) -> None:
        args = {"symbol": "__init__", "class_name": "NvidiaSTTService"}
        assert _hint("", tool="get_code_snippet", args=args)

    def test_get_code_snippet_reads_module(self) -> None:
        args = {"symbol": "__init__", "module": "pipecat.services.nvidia.stt"}
        assert _hint("", tool="get_code_snippet", args=args)

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("search_api", {"query": "nova", "class_name": "SonicThing"}),
            ("get_code_snippet", {"symbol": "nova", "intent": "sonic"}),
            ("get_code_snippet", {"symbol": "nova", "module": "sonic.thing"}),
        ],
    )
    def test_a_multi_word_alias_never_spans_two_arguments(
        self, tool: str, args: dict[str, str]
    ) -> None:
        # "nova sonic" is one alias; "nova" in one argument and "sonic" in the next is not it.
        assert _hint("", tool=tool, args=args) is None

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

    def test_scanned_keys_are_fields_of_the_tools_input_model(self) -> None:
        """A renamed input field would otherwise leave the table scanning a key no call carries."""
        from pipecat_context_hub.shared import corpus_hint as module
        from pipecat_context_hub.shared.types import (
            GetCodeSnippetInput,
            SearchApiInput,
            SearchDocsInput,
            SearchExamplesInput,
        )

        models: dict[str, type[BaseModel]] = {
            "search_docs": SearchDocsInput,
            "search_examples": SearchExamplesInput,
            "search_api": SearchApiInput,
            "get_code_snippet": GetCodeSnippetInput,
        }
        assert set(models) == set(module._HINT_TEXT_ARGS)
        for tool, model in models.items():
            assert set(module._HINT_TEXT_ARGS[tool]) <= set(model.model_fields), tool

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
    """Deliberately pins the shipped catalog: a catalog edit that moves these hints must update them."""

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

    @pytest.mark.parametrize(
        ("query", "group"),
        [
            ("TavusVideoService", "tavus"),
            ("AWSBedrockLLMService", "aws"),
            ("GeminiLiveLLMService", "google"),
            ("aws_bedrock", "aws"),
        ],
    )
    def test_issue_148_identifier_queries_hint(self, query: str, group: str) -> None:
        # The four queries from issue #148, verbatim, against the shipped catalog.
        for tool in ("search_api", "search_docs", "search_examples"):
            hint = _hint(query, {}, tool=tool)
            assert hint is not None, (tool, query)
            assert group in hint["matched_groups"]

    def test_issue_148_get_code_snippet_class_name_hints(self) -> None:
        args = {"symbol": "__init__", "class_name": "TavusVideoService"}
        hint = _hint("", {}, tool="get_code_snippet", args=args)
        assert hint is not None
        assert hint["matched_groups"] == ["tavus"]

    @pytest.mark.parametrize(
        ("query", "group"),
        [
            ("TurnDetection", "audio-and-benchmarks"),
            ("NovaSonicService", "aws"),
            ("LipSyncBuffer", "lip-sync-characters"),
            ("AwsClient", "aws"),
        ],
    )
    def test_shipped_aliases_hint_from_a_whole_identifier_token(
        self, query: str, group: str
    ) -> None:
        # Positive controls for the substring negatives below: the same aliases do hint.
        hint = _hint(query, {})
        assert hint is not None
        assert group in hint["matched_groups"]

    @pytest.mark.parametrize(
        "query",
        ["ReturnDetection", "InnovaSonicBoom", "ClipSyncer", "FlawsInCode"],
    )
    def test_shipped_aliases_need_a_whole_identifier_token(self, query: str) -> None:
        # turn detection / nova sonic / lip sync / aws as substrings of one token.
        assert _hint(query, {}) is None

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


class _FinishIndex(_CountsIndex):
    """Counts plus the metadata ``annotate_response`` reads (a 30-day-old refresh)."""

    def get_all_metadata(self) -> dict[str, str]:
        return {"last_refresh_at": "2000-01-01T00:00:00+00:00"}

    def get_metadata(self, key: str) -> str | None:
        return self.get_all_metadata().get(key)


def _finish(tool: str, result_json: str, counts: Any = None) -> tuple[str, Any]:
    return finish_response(
        cast(Any, _FinishIndex({} if counts is None else counts)),
        tool=tool,
        args={"query": "nvidia voice agent"},
        result_json=result_json,
        configured_repos=frozenset(),
        tainted_repos=frozenset(),
    )


@pytest.mark.usefixtures("hint_catalog")
class TestFinishResponse:
    """The one post-handler step both front doors call (hint, footer, then attach)."""

    def test_search_tool_gets_footer_then_hint(self) -> None:
        text, hint = _finish("search_docs", '{"hits": []}')
        assert hint is not None and hint["matched_groups"] == ["nvidia"]
        out = json.loads(text)
        assert out["corpus_hint"] == hint
        assert "index_staleness" in out
        # The hint is attached after the footer.
        assert list(out) == ["hits", "index_staleness", "corpus_hint"]

    def test_no_hint_still_gets_the_footer(self) -> None:
        text, hint = _finish("search_docs", '{"hits": []}', {"nv/one": 3})
        assert hint is None
        out = json.loads(text)
        assert "corpus_hint" not in out and "index_staleness" in out

    def test_hub_status_is_returned_untouched(self) -> None:
        raw = '{"total_records": 1}'
        text, hint = _finish("get_hub_status", raw)
        assert text is raw and hint is None

    def test_both_front_doors_call_the_shared_step(self) -> None:
        # Parity by structure: neither door composes the hint and footer by hand.
        import inspect

        from pipecat_context_hub import cli_query
        from pipecat_context_hub.server import main

        for module in (main, cli_query):
            src = inspect.getsource(module)
            assert "finish_response(" in src, module.__name__
            for private_step in ("corpus_hint(", "attach_corpus_hint(", "annotate_response("):
                assert private_step not in src, (module.__name__, private_step)

    def test_both_front_doors_capture_repo_sets_through_one_accessor(self) -> None:
        # Two-door parity on the hint/status inputs rests on one expression, not two copies.
        import inspect

        from pipecat_context_hub import cli, cli_query

        for module in (cli, cli_query):
            src = inspect.getsource(module)
            assert "config.sources.repo_sets()" in src, module.__name__
            assert "frozenset(config.sources.effective_repos)" not in src, module.__name__
            assert "frozenset(config.sources.tainted_repos)" not in src, module.__name__

    def test_repo_sets_is_effective_and_tainted_as_frozensets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pipecat_context_hub.shared.config import SourceConfig

        monkeypatch.setenv("PIPECAT_HUB_EXTRA_REPOS", "acme/one,taint/me")
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REPOS", "taint/me")
        sources = SourceConfig()
        configured, tainted = sources.repo_sets()
        assert configured == frozenset(sources.effective_repos)
        assert "acme/one" in configured and "taint/me" not in configured
        assert tainted == frozenset({"taint/me"})

    def test_cli_runtime_has_no_empty_repo_set_defaults(self) -> None:
        import dataclasses

        from pipecat_context_hub.cli_query import _QueryRuntime

        fields = {f.name: f for f in dataclasses.fields(_QueryRuntime)}
        for name in ("configured_repos", "tainted_repos"):
            assert fields[name].default is dataclasses.MISSING, name
