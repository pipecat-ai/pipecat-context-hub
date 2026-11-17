"""Answer the four audit questions from extracted sessions and calls, with coded evidence only.

The extractor sees the full local transcript; the share bundle must not. This module turns what only
the extractor can see (tool lists, ToolSearch use, prompt mentions, subagent trees, follow-up
events) into short coded answers, so a bundle is analysable without raw transcripts.

Everything here is a heuristic over structure. It can say *where* Context Hub looked unhelpful and
*why it was not used*; it cannot read whether a result's content was right. ``outcome`` is
therefore a signal class, never a verdict, and the report says so.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from pipecat_context_hub.audit.flags import CLEAN_OUTCOME, FLAGS, OUTCOME_ORDER, OUTCOMES, severity
from pipecat_context_hub.audit.flags import spec

SEARCH_TOOLS = {"search_docs", "search_examples", "search_api", "get_code_snippet"}
VERSIONED_TOOLS = {"search_examples", "search_api", "get_code_snippet"}

# Priority order: the first matching class wins. Each maps a flag to a likely owner. Derived from
# the one flag table (audit/flags.py), never written out here.
_CLASSES: list[tuple[str, dict[str, str]]] = [
    (outcome, {name: s.target for name, s in FLAGS.items() if s.outcome == outcome})
    for outcome in OUTCOME_ORDER
]


def classify_call(call: dict[str, Any]) -> tuple[str, str | None]:
    """(outcome, suspected target) for one call; first matching class wins.

    A flag not declared in ``audit/flags.py`` raises ``UnknownFlagError``: classing it as
    ``no_negative_signal`` ("no flag fired") would be false.
    """
    flags = set(call.get("flags") or [])
    for f in flags:
        spec(f)
    for outcome, mapping in _CLASSES:
        for flag, target in mapping.items():
            if flag in flags:
                return outcome, target
    return CLEAN_OUTCOME, None


def annotate_calls(calls: list[dict[str, Any]]) -> None:
    for c in calls:
        c["outcome"], c["suspect_target"] = classify_call(c)
        c["severity"] = severity(c["flags"])


def triage_order(calls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calls most worth reading first: highest ``severity`` first, ties in their original order.

    The one ordering for the report's "Look first" table, the answers' "Read first" list and the
    organizer's merged ``calls.jsonl``.
    """
    return sorted(calls, key=lambda c: -(c.get("severity") or 0))


def latency_stats(latencies: Iterable[float]) -> tuple[float | None, float | None]:
    """(median rounded to 0.1 s, p90) of ``latencies``; (None, None) when there are none.

    p90 is the nearest-rank 90th percentile: the smallest value at least 90% of the values are at
    or below, ``sorted[ceil(0.9 * n) - 1]`` (computed in integers, so no float rounding moves it).
    The one formula for the answers' overall and per-tool efficiency and the report's per-tool
    table.
    """
    lat = sorted(latencies)
    if not lat:
        return None, None
    rank = -(-9 * len(lat) // 10)  # ceil(0.9 * n)
    return round(statistics.median(lat), 1), lat[rank - 1]


def no_use_signals(session: dict[str, Any]) -> list[str]:
    """Coded reasons a session with Pipecat work never called Context Hub. Empty if it did or had none."""
    if session.get("hub_used") or not session.get("pipecat_work"):
        return []
    signals: list[str] = []
    if not session.get("hub_available"):
        signals.append("hub_not_in_transcript")
    if session.get("hub_tools_deferred") and not session.get("hub_tool_search_calls"):
        signals.append("hub_tools_deferred_never_loaded")
    if session.get("prompt_mentions_hub") is False:
        signals.append("prompt_silent_about_hub")
    if session.get("is_subagent") and session.get("parent_used_hub") is False:
        signals.append("parent_also_unused")
    if session.get("bypass_events_total"):
        signals.append("read_source_or_web_instead")
    return signals or ["unexplained"]


def link_parents(sessions: list[dict[str, Any]]) -> None:
    """Fill ``parent_used_hub`` / ``no_use_signals`` once every session is known."""
    by_id = {s["session_id"]: s for s in sessions}
    for s in sessions:
        parent = by_id.get(s.get("parent_session_id") or "")
        s["parent_used_hub"] = bool(parent["hub_used"]) if parent else None
    for s in sessions:
        s["no_use_signals"] = no_use_signals(s)


def _pct(n: int, d: int) -> float:
    return round(100 * n / d, 1) if d else 0.0


def analyze(
    sessions: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    ref: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    """The four answers. ``ref`` names a call the way the share bundle does (hashed)."""
    trees = {s.get("parent_session_id") or s["session_id"] for s in sessions}
    work = [s for s in sessions if s["pipecat_work"]]
    used = [s for s in sessions if s["hub_used"]]
    missed = [s for s in sessions if s["missed_opportunity"]]
    reasons: Counter[str] = Counter()
    for s in missed:
        reasons.update(s.get("no_use_signals") or [])
    usage = {
        "sessions": len(sessions),
        "session_trees": len(trees),
        "subagent_sessions": sum(bool(s.get("is_subagent")) for s in sessions),
        "used": len(used),
        "with_pipecat_work": len(work),
        "pipecat_work_and_used": sum(bool(s["hub_used"]) for s in work),
        "adoption_pct_of_pipecat_work": _pct(sum(bool(s["hub_used"]) for s in work), len(work)),
        "hub_tools_listed_deferred": sum(bool(s.get("hub_tools_deferred")) for s in sessions),
        "tool_search_ever_used": sum(bool(s.get("tool_search_calls")) for s in sessions),
        "missed_opportunity": len(missed),
        "why_not_used": dict(reasons.most_common()),
    }

    outcomes = Counter(c["outcome"] for c in calls)
    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        by_tool[c["tool"]].append(c)
    tool_stats = {}
    for tool, cs in sorted(by_tool.items()):
        median, p90 = latency_stats(c["latency_s"] for c in cs if c.get("latency_s") is not None)
        tool_stats[tool] = {
            "calls": len(cs),
            "not_clean": sum(c["outcome"] != CLEAN_OUTCOME for c in cs),
            "median_s": median,
            "p90_s": p90,
        }
    per_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        per_session[c["session_id"]].append(c)
    search_only = sum(
        1
        for cs in per_session.values()
        if sum(c["tool"] in SEARCH_TOOLS for c in cs) >= 3
        and not any(c["tool"] in {"get_doc", "get_example"} for c in cs)
    )
    status_spam = sum(
        1 for cs in per_session.values() if sum(c["tool"] == "get_hub_status" for c in cs) >= 3
    )
    versioned = [c for c in calls if c["tool"] in VERSIONED_TOOLS and c["via"] == "mcp"]
    no_version = sum(
        not (isinstance(c["input"], dict) and c["input"].get("pipecat_version")) for c in versioned
    )
    median_all, _ = latency_stats(c["latency_s"] for c in calls if c.get("latency_s") is not None)
    efficiency = {
        "calls": len(calls),
        "clean_pct": _pct(outcomes[CLEAN_OUTCOME], len(calls)),
        "median_latency_s": median_all,
        "per_tool": tool_stats,
        "sessions_searching_3plus_times_never_fetching": search_only,
        "sessions_with_3plus_status_calls": status_spam,
        "versioned_search_calls_without_pipecat_version": {
            "of": len(versioned),
            "without": no_version,
        },
    }

    correctness = {
        "note": "outcome is a signal class from structure, not a verdict on content; no_negative_signal is not proof of a right answer",
        "outcomes": dict(outcomes.most_common()),
        "glossary": dict(OUTCOMES),
        "read_these_first": [
            ref(c)
            for c in triage_order(
                c for c in calls if c["outcome"] in {"failed", "suspect", "bypassed"}
            )[:10]
        ],
    }

    groups: dict[str, dict[str, Any]] = {}
    for c in calls:
        target = c.get("suspect_target")
        if not target:
            continue
        g = groups.setdefault(
            target,
            {
                "calls": 0,
                "sessions": set(),
                "participants": set(),
                "signals": Counter(),
                "refs": [],
            },
        )
        g["calls"] += 1
        g["sessions"].add(c["session_id"])
        g["participants"].add(c["participant"])
        g["signals"].update(c["flags"])
        if len(g["refs"]) < 5:
            g["refs"].append(ref(c))
    backlog = [
        {
            "target": t,
            "calls": g["calls"],
            "sessions": len(g["sessions"]),
            "participants": len(g["participants"]),
            "top_signals": dict(g["signals"].most_common(4)),
            "example_refs": g["refs"],
        }
        for t, g in sorted(
            groups.items(), key=lambda kv: (-len(kv[1]["participants"]), -kv[1]["calls"])
        )
    ]
    if reasons:
        backlog.append(
            {
                "target": "adoption (agent setup / docs)",
                "calls": 0,
                "sessions": len(missed),
                "participants": len({s["participant"] for s in missed}),
                "top_signals": dict(reasons.most_common(4)),
                "example_refs": [],
            }
        )
    return {
        "heuristic": True,
        "usage": usage,
        "efficiency": efficiency,
        "correctness": correctness,
        "improvement_backlog": backlog,
    }


def prune_refs(a: dict[str, Any], keep: set[str]) -> None:
    """Drop example refs that point at calls no longer in the bundle (in place)."""
    for b in a.get("improvement_backlog") or []:
        b["example_refs"] = [r for r in b.get("example_refs", []) if r in keep]
    c = a.get("correctness") or {}
    c["read_these_first"] = [r for r in c.get("read_these_first", []) if r in keep]
