"""Every heuristic flag the extractor can raise, declared once.

Each flag has one row: its outcome class and likely owner (``analysis.classify_call``), its severity
weight (a call row's ``severity``) and its glossary text (``SCHEMA.md``). The three views used to be
three tables with no parity check, and a flag missing from one was silently classed as clean;
now a flag that is not declared here raises ``UnknownFlagError`` wherever it is weighed or classed.

The outcome classes are declared here too (:data:`OUTCOMES`, with their descriptions), and so are
the thresholds the extractor's heuristics use: every glossary that names a threshold is built from
its constant, so text and behaviour cannot drift.

Order matters: outcomes are tried in ``OUTCOME_ORDER`` and, within an outcome, flags in the order
below; the first match names the outcome and the owner.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable


@dataclasses.dataclass(frozen=True)
class FlagSpec:
    outcome: str
    target: str
    weight: int
    glossary: str


# Heuristic thresholds (extract.process_session reads these; the glossaries below quote them).
LARGE_RESULT_BYTES = 30_000
SLOW_SECONDS = 8.0
FOLLOWUP_EVENTS = 8  # a call's follow-up window (bypass detection, the row's `followups`)
ERROR_WINDOW_EVENTS = 25  # how far after a call a Python error counts as downstream
REFORMULATION_EVENTS = 3  # same search tool again within this many events after a bad result
MULTI_CONCEPT_WORDS = 6  # a query longer than this that joins concepts is "multi-concept"
MULTI_CONCEPT_DELIMITERS = (" + ", " & ")  # the hub's multi-concept query syntax
# Session-level efficiency signals (analysis.analyze; the report's answers quote them).
SEARCH_ONLY_SEARCHES = 3  # a session that searched this often and never fetched a doc or example
STATUS_REPEAT_CALLS = 3  # a session that called get_hub_status this often

CLEAN_OUTCOME = "no_negative_signal"
# Every outcome class, in priority order (the first matching class wins), with what it means.
OUTCOMES: dict[str, str] = {
    "failed": "the call errored, was schema-invalid, or the result was too big or not JSON",
    "miss": "no hits, low confidence, or the topic's repos are not indexed",
    "suspect": (
        f"a Python error followed within {ERROR_WINDOW_EVENTS} events (correlation only; read "
        "the call to judge)"
    ),
    "bypassed": (
        f"the agent read pipecat source or web docs within {FOLLOWUP_EVENTS} events of the result"
    ),
    "inefficient": "the call worked but wasted effort (repeat, retry, slow, oversized)",
    CLEAN_OUTCOME: "no flag fired; NOT proof the answer was right",
}
OUTCOME_ORDER = tuple(o for o in OUTCOMES if o != CLEAN_OUTCOME)
# The outcomes the answers' "Read first" list draws from: a call that failed, preceded a Python
# error or was bypassed (a miss or an inefficiency is visible from its own row).
READ_FIRST_OUTCOMES = ("failed", "suspect", "bypassed")

FLAGS: dict[str, FlagSpec] = {
    # failed
    "error": FlagSpec("failed", "hub-tools", 3, "Context Hub returned a tool error."),
    "result_exceeds_harness_limit": FlagSpec(
        "failed", "hub-tools", 3,
        "Result too large for the harness to show the agent (it was truncated or saved to a file).",
    ),
    "input_validation_error": FlagSpec(
        "failed", "hub-tools", 3,
        "The harness rejected the call as schema-invalid: the agent misread the tool schema.",
    ),
    "non_json_result": FlagSpec(
        "failed", "hub-tools", 1,
        "A direct MCP result was not JSON (unexpected for Context Hub).",
    ),
    # miss
    "corpus_hint": FlagSpec(
        "miss", "hub-corpus", 1,
        "Response said the query names a vendor/topic whose repos are not indexed.",
    ),
    "zero_hits": FlagSpec("miss", "hub-retrieval", 2, "A search returned no hits."),
    "low_confidence": FlagSpec(
        "miss", "hub-retrieval", 2, "evidence.low_confidence was true (confidence < 0.3)."
    ),
    # suspect
    "downstream_error": FlagSpec(
        "suspect", "needs-judgment", 3,
        "A Python error (ImportError, AttributeError, unexpected keyword...) appeared in a "
        f"non-Context-Hub tool result within {ERROR_WINDOW_EVENTS} events. CORRELATION ONLY: "
        "needs judgment to say it came from what Context Hub returned.",
    ),
    # bypassed
    "agent_bypassed_after_result": FlagSpec(
        "bypassed", "needs-judgment", 2,
        f"Within {FOLLOWUP_EVENTS} events the agent read pipecat source in site-packages/.venv or "
        "fetched pipecat web docs.",
    ),
    # inefficient
    "reformulation_after_bad_result": FlagSpec(
        "inefficient", "hub-retrieval", 2,
        f"Same search tool called again within {REFORMULATION_EVENTS} events after a zero-hit or "
        "low-confidence result.",
    ),
    "exact_repeat": FlagSpec(
        "inefficient", "agent-misuse", 1,
        "Same tool and identical input already called earlier in the session.",
    ),
    "multi_concept_without_delimiter": FlagSpec(
        "inefficient", "agent-misuse", 1,
        f"Query of more than {MULTI_CONCEPT_WORDS} words joined concepts with 'and', commas or a "
        f"bare '&' instead of the multi-concept delimiter "
        f"{' or '.join(repr(d) for d in MULTI_CONCEPT_DELIMITERS)}.",
    ),
    "large_result": FlagSpec(
        "inefficient", "hub-tools", 1,
        f"Result over {LARGE_RESULT_BYTES // 1000} KB, even if the harness accepted it.",
    ),
    "slow": FlagSpec(
        "inefficient", "hub-tools", 1,
        f"Latency over {SLOW_SECONDS:g} s (tool result timestamp minus call timestamp; includes "
        "harness overhead).",
    ),
}  # fmt: skip


class UnknownFlagError(ValueError):
    """A flag that is not declared in ``FLAGS``: a new heuristic without its row."""


def spec(flag: str) -> FlagSpec:
    try:
        return FLAGS[flag]
    except KeyError:
        raise UnknownFlagError(f"flag {flag!r} is not declared in audit/flags.py") from None


def severity(flags: Iterable[str]) -> int:
    """A call's severity: the sum of its flags' weights. An undeclared flag raises."""
    return sum(spec(f).weight for f in flags)
