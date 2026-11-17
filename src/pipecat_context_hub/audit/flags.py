"""Every heuristic flag the extractor can raise, declared once.

Each flag has one row: its outcome class and likely owner (``analysis.classify_call``), its severity
weight (a call row's ``severity``) and its glossary text (``SCHEMA.md``). The three views used to be
three tables with no parity check, and a flag missing from one was silently classed as clean;
now a flag that is not declared here raises ``UnknownFlagError`` wherever it is weighed or classed.

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


OUTCOME_ORDER = ("failed", "miss", "suspect", "bypassed", "inefficient")

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
        "non-Context-Hub tool result within 25 events. CORRELATION ONLY: needs judgment to say it "
        "came from what Context Hub returned.",
    ),
    # bypassed
    "agent_bypassed_after_result": FlagSpec(
        "bypassed", "needs-judgment", 2,
        "Within 8 events the agent read pipecat source in site-packages/.venv or fetched pipecat "
        "web docs.",
    ),
    # inefficient
    "reformulation_after_bad_result": FlagSpec(
        "inefficient", "hub-retrieval", 2,
        "Same search tool called again within 3 events after a zero-hit or low-confidence result.",
    ),
    "exact_repeat": FlagSpec(
        "inefficient", "agent-misuse", 1,
        "Same tool and identical input already called earlier in the session.",
    ),
    "multi_concept_without_delimiter": FlagSpec(
        "inefficient", "agent-misuse", 1,
        "Long query joined concepts with and/commas instead of ' + '.",
    ),
    "large_result": FlagSpec(
        "inefficient", "hub-tools", 1, "Result over 30 KB, even if the harness accepted it."
    ),
    "slow": FlagSpec(
        "inefficient", "hub-tools", 1,
        "Latency over 8 s (tool result timestamp minus call timestamp; includes harness overhead).",
    ),
}  # fmt: skip

# Derived views: never edit these, edit FLAGS.
FLAG_GLOSSARY = {name: s.glossary for name, s in FLAGS.items()}
SEVERITY = {name: s.weight for name, s in FLAGS.items()}


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
