"""Docs, smoke-script classifier and changelog pins for the bare-member hint."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipecat_context_hub.shared.types import BARE_NAME_RULE
from tests.unit.test_config import _import_script

_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _ROOT / "docs"


def _flat(text: str) -> str:
    """Collapse whitespace so a soft-wrapped sentence still matches."""
    return " ".join(text.split())


@pytest.mark.parametrize("doc", ["tools.md", "cli.md", "index-contract.md"])
def test_docs_carry_bare_name_rule_verbatim(doc: str) -> None:
    assert _flat(BARE_NAME_RULE) in _flat((_DOCS / doc).read_text())


def _numbered_main_items() -> list[tuple[int, str]]:
    """Top-level numbered items of the first (live smoke) list, with bodies."""
    lines = (_DOCS / "smoke-test.md").read_text().splitlines()
    items: list[tuple[int, str]] = []
    started = False
    for line in lines:
        m = re.match(r"^(\d+)\. ", line)
        if m:
            n = int(m.group(1))
            if not started and n != 1:
                continue
            if started and n <= items[-1][0]:
                break  # the next list (e.g. the CLI-query list) restarts at 1
            started = True
            items.append((n, line))
        elif started and items and not line.startswith("#"):
            items[-1] = (items[-1][0], items[-1][1] + "\n" + line)
    return items


def test_smoke_test_numbering_contiguous_with_bare_member_items() -> None:
    items = _numbered_main_items()
    assert [n for n, _ in items] == list(range(1, len(items) + 1))
    bodies = {n: body for n, body in items}
    bare = [n for n, b in bodies.items() if "trigger_user_turn_stopped" in b]
    params = [n for n, b in bodies.items() if "check-deprecation InputParams" in b]
    assert bare, "no numbered item for trigger_user_turn_stopped"
    assert params, "no numbered item for InputParams"
    assert "report_user_turn_stopped" in bodies[bare[0]]
    assert "candidates" in bodies[params[0]]


@pytest.fixture(scope="module")
def smoke():
    return _import_script(_ROOT / "scripts" / "smoke_check_deprecation.py", "smoke_dep")


HINT_EXPECTED = ("A.trigger_user_turn_stopped", "report_user_turn_stopped")
HINT_RESPONSE = {
    "deprecated": False,
    "status": None,
    "candidates": [
        {"symbol": "A.trigger_user_turn_stopped", "replacement": "report_user_turn_stopped"}
    ],
    "candidates_total": 1,
}
CLEAN_RESPONSE = {"deprecated": False, "status": "current", "candidates": None}
DEPRECATED_RESPONSE = {"deprecated": True, "status": "deprecated", "candidates": None}


def test_hint_fails_not_deprecated_expectation(smoke) -> None:
    assert smoke.classify(HINT_RESPONSE, "current") is not None


def test_clean_current_passes_not_deprecated(smoke) -> None:
    assert smoke.classify(CLEAN_RESPONSE, "current") is None


def test_deprecated_passes_deprecated(smoke) -> None:
    assert smoke.classify(DEPRECATED_RESPONSE, "deprecated") is None
    assert smoke.classify(CLEAN_RESPONSE, "deprecated") is not None


def test_hint_group_requires_candidates(smoke) -> None:
    assert smoke.classify(HINT_RESPONSE, "hint", HINT_EXPECTED) is None
    assert smoke.classify({**HINT_RESPONSE, "candidates": []}, "hint", HINT_EXPECTED)
    assert smoke.classify({**HINT_RESPONSE, "candidates": None}, "hint", HINT_EXPECTED)


def test_hint_group_rejects_wrong_candidate(smoke) -> None:
    good = HINT_RESPONSE["candidates"][0]  # type: ignore[index]
    wrong_symbol = {**HINT_RESPONSE, "candidates": [{**good, "symbol": "Wrong.method"}]}
    wrong_repl = {**HINT_RESPONSE, "candidates": [{**good, "replacement": "other"}]}
    no_repl = {**HINT_RESPONSE, "candidates": [{"symbol": good["symbol"]}]}
    extra = {
        **HINT_RESPONSE,
        "candidates": [good, {**good, "symbol": "B.trigger_user_turn_stopped"}],
    }
    for bad in (wrong_symbol, wrong_repl, no_repl, extra):
        assert smoke.classify(bad, "hint", HINT_EXPECTED) is not None
    assert smoke.classify({**HINT_RESPONSE, "status": "current"}, "hint", HINT_EXPECTED)
    assert smoke.classify({**HINT_RESPONSE, "deprecated": True}, "hint", HINT_EXPECTED)


def test_hint_group_requires_present_keys_and_total(smoke) -> None:
    only_candidates = {"candidates": HINT_RESPONSE["candidates"]}
    no_total = {k: v for k, v in HINT_RESPONSE.items() if k != "candidates_total"}
    wrong_total = {**HINT_RESPONSE, "candidates_total": 2}
    null_total = {**HINT_RESPONSE, "candidates_total": None}
    null_deprecated = {**HINT_RESPONSE, "deprecated": None}
    bool_total = {**HINT_RESPONSE, "candidates_total": True}
    for bad in (
        only_candidates,
        no_total,
        wrong_total,
        null_total,
        null_deprecated,
        bool_total,
    ):
        assert smoke.classify(bad, "hint", HINT_EXPECTED) is not None
    assert smoke.classify(HINT_RESPONSE, "hint", HINT_EXPECTED) is None


def test_hint_group_needs_an_expected_candidate(smoke) -> None:
    with pytest.raises(ValueError):
        smoke.classify(HINT_RESPONSE, "hint")


def test_clean_current_is_not_a_hint_pass(smoke) -> None:
    assert smoke.classify(CLEAN_RESPONSE, "hint", HINT_EXPECTED) is not None
    assert smoke.classify(DEPRECATED_RESPONSE, "hint", HINT_EXPECTED) is not None


def test_smoke_script_declares_hint_group(smoke) -> None:
    assert "trigger_user_turn_stopped" in smoke.HINT
    assert set(smoke.HINT_EXPECTED) == set(smoke.HINT)


def test_changelog_mentions_hint() -> None:
    """Whole-file search, so it survives the release that moves [Unreleased] under a version."""
    text = (_ROOT / "CHANGELOG.md").read_text()
    assert "check_deprecation` no longer reads a bare deprecated member name" in text
