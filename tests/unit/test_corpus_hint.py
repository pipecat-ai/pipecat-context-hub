"""Tests for ``shared/corpus_hint.py``: the one "not indexed" predicate.

``indexed_repos`` / ``groups_not_indexed`` / ``configured_no_records`` are shared by the
corpus hint and ``get_hub_status``. Fixture indexes here are fakes exposing only
``get_counts_by_repo`` (no ``IndexStore``), so this module stays Windows-eligible.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from pipecat_context_hub.shared.corpus_hint import (
    configured_no_records,
    groups_not_indexed,
    indexed_repos,
)
from pipecat_context_hub.shared.interfaces import IndexUnavailableError
from pipecat_context_hub.shared.repo_catalog import Catalog, CatalogGroup, CatalogRepo


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
