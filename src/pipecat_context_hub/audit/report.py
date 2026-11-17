"""``report.md``: one renderer, used by ``extract`` and ``repack``, for every section (the
computed answers from ``manifest.json``'s ``analysis`` included).

The report is rebuilt from the bundle's own files (``manifest.json`` and ``calls.jsonl``), never
edited in place. It is a function of rows the tripwire already accepted: every value is rendered
through the egress's canonical text form (``value_text``, never ``str()``/``repr``), inline with
:func:`_line` and in a table cell with :func:`_cell`. In running text a value is preceded by
``label: `` and followed only by the item separator `` · `` or the end of the line, so no rule can
match across a value and the text around it, and a report rendered from clean values is clean.

A participant who deletes a call from ``calls.jsonl`` and runs ``repack`` therefore loses
everything derived from it, by construction: there is no second copy of a query to find, and no
line-oriented parser whose idea of a row can differ from the builder's.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import Any

from pipecat_context_hub.audit.analysis import latency_stats, triage_order
from pipecat_context_hub.audit.egress import md_cell, one_line, value_text

LOOK_FIRST_LIMIT = 60
QUERY_CELL_CHARS = 80
_LAST_SPACE_RE = re.compile(r"\s(?!.*\s)", re.DOTALL)


def tool_rows(calls: list[dict[str, Any]]) -> list[str]:
    rows = []
    for tool in sorted({c["tool"] for c in calls}):
        cs = [c for c in calls if c["tool"] == tool]
        median, p90 = latency_stats(c["latency_s"] for c in cs if c.get("latency_s") is not None)
        conf = [
            c["result"]["confidence"]
            for c in cs
            if isinstance(c["result"].get("confidence"), (int, float))
        ]
        rows.append(
            f"| {_cell(tool)} | {len(cs)} | {sum(bool(c['flags']) for c in cs)} "
            f"| {'' if median is None else median} | {'' if p90 is None else p90} "
            f"| {sum('zero_hits' in c['flags'] for c in cs)} "
            f"| {round(statistics.mean(conf), 2) if conf else ''} |"
        )
    return rows


def _cell(v: Any) -> str:
    """A table cell for a row value: its canonical text, one line, ``|`` escaped. No redaction.

    Every value rendered here already came through the egress (``extract``) or is checked in its own
    file (``repack``), and ``verify_files`` scans ``report.md`` with the ``|`` escape undone, so the
    escape can never make a match the value did not have. Substituting a character (``|`` to ``/``)
    could fabricate a path the row never held.
    """
    return md_cell(value_text(v))


def _line(v: Any) -> str:
    """A value inline in running text: its canonical text on one line."""
    return one_line(value_text(v))


def _prefix(text: str, n: int) -> str:
    """At most ``n`` characters of ``text``, cut only at whitespace.

    A cut inside a token could create a match the full text does not have (a hex run that was
    followed by a letter now ends the string); a cut at whitespace leaves every token whole, so the
    prefix trips no rule the text did not.
    """
    if len(text) <= n:
        return text
    m = _LAST_SPACE_RE.search(text[: n + 1])
    return (text[: m.start()] if m else "") + " …"


def _query(c: dict[str, Any]) -> str:
    raw = c.get("input")
    inp: dict[str, Any] = raw if isinstance(raw, dict) else {}
    q = inp.get("query") or inp.get("path") or inp.get("doc_id") or c.get("hub_invocation") or ""
    return _cell(_prefix(value_text(q), QUERY_CELL_CHARS))


def _answers(a: dict[str, Any]) -> list[str]:
    """The answers to the four questions, from a manifest ``analysis`` block. Reads only the keys
    ``bundle.ANALYSIS_SHAPE`` names (``load_files`` / ``decode_manifest`` check them first)."""
    u, e, c = a["usage"], a["efficiency"], a["correctness"]
    md = [
        "## Answers to the four questions (computed, heuristic)",
        "",
        "### 1. Did they use Context Hub?",
        "",
        f"- Sessions that called it: {_line(u['used'])} · sessions: {_line(u['sessions'])} · "
        f"session trees: {_line(u['session_trees'])} · subagent sessions: "
        f"{_line(u['subagent_sessions'])}",
        f"- Sessions with Pipecat work that called it: {_line(u['pipecat_work_and_used'])} · "
        f"with Pipecat work: {_line(u['with_pipecat_work'])} · adoption percent: "
        f"{_line(u['adoption_pct_of_pipecat_work'])}",
        f"- Sessions listing its tools as deferred: {_line(u['hub_tools_listed_deferred'])} · "
        f"sessions using `ToolSearch` (which loads them): {_line(u['tool_search_ever_used'])}",
    ]
    if u["missed_opportunity"]:
        md.append(
            f"- Sessions with Pipecat work without it: {_line(u['missed_opportunity'])} · "
            f"signals: {_line(u['why_not_used'])}"
        )
    md += ["", "### 2. Was it inefficient?", ""]
    if e["calls"]:
        v = e["versioned_search_calls_without_pipecat_version"]
        md += [
            f"- Calls: {_line(e['calls'])} · percent with no flag: {_line(e['clean_pct'])} · "
            f"median latency in seconds: {_line(e['median_latency_s'])}",
            "- Sessions that searched 3+ times and never fetched a doc or example: "
            f"{_line(e['sessions_searching_3plus_times_never_fetching'])} · sessions with 3+ "
            f"`get_hub_status` calls: {_line(e['sessions_with_3plus_status_calls'])}",
            f"- Versioned searches without `pipecat_version`: {_line(v['without'])} · "
            f"versioned searches: {_line(v['of'])}",
        ]
    else:
        md.append("- No calls, so nothing to measure.")
    md += ["", "### 3. Correct or incorrect?", "", f"- {_line(c['note'])}"]
    if c["outcomes"]:
        md.append(f"- Outcomes: {_line(c['outcomes'])}")
    if c["read_these_first"]:
        md.append(f"- Read first: {_line(c['read_these_first'])}")
    md += ["", "### 4. What to improve (by suspected owner)", ""]
    if a["improvement_backlog"]:
        md += [
            "| target | calls | sessions | participants | top signals | examples |",
            "|---|---|---|---|---|---|",
        ]
        for b in a["improvement_backlog"]:
            md.append(
                f"| {_cell(b['target'])} | {_cell(b['calls'])} | {_cell(b['sessions'])} "
                f"| {_cell(b['participants'])} | {_cell(b['top_signals'])} "
                f"| {_cell(b['example_refs'][:3])} |"
            )
    else:
        md.append("- Nothing to improve was signalled.")
    md.append("")
    return md


def render_report(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """The report for ``rows`` (share-form call rows) under ``manifest``'s counts and analysis."""
    counts = manifest.get("counts") or {}
    analysis = manifest.get("analysis") or {}
    missed = counts.get("missed_opportunity", 0)  # a row count, re-counted by repack
    flagged = triage_order(c for c in rows if c["flags"])
    look = []
    for c in flagged[:LOOK_FIRST_LIMIT]:
        r = c["result"]
        conf = r.get("confidence")
        look.append(
            f"| {_cell(c['ref'])} | {_cell(c['tool'])} | {_query(c)} "
            f"| {_cell(', '.join(c['flags']))} "
            f"| {_cell(r.get('n_hits'))} | {_cell(round(conf, 2) if isinstance(conf, float) else None)} |"
        )
    flag_totals = Counter(f for c in rows for f in c["flags"])
    md = [
        "# Context Hub session report (auto-generated, unjudged)",
        "",
        f"Sessions: {counts.get('sessions', 0)} · used Context Hub: {counts.get('hub_used', 0)} · "
        f"Pipecat work without it: {missed} · calls: {len(rows)}",
        f"Harnesses: {_line(manifest.get('harnesses', {}))}",
        f"Calls by tool: {_line(dict(Counter(c['tool'] for c in rows)))}",
        f"Flags: {_line(dict(flag_totals))}",
        "",
        *(_answers(analysis) if analysis else []),
        "## Look first (flagged calls, highest severity first)"
        + (
            f" — first {LOOK_FIRST_LIMIT} of {len(flagged)}"
            if len(flagged) > LOOK_FIRST_LIMIT
            else ""
        ),
        "",
        "| ref | tool | query | flags | hits | confidence |",
        "|---|---|---|---|---|---|",
        *look,
        "",
        "## Per-tool stats",
        "",
        "| tool | calls | flagged | median s | p90 s | zero hits | mean confidence |",
        "|---|---|---|---|---|---|---|",
        *tool_rows(rows),
        "",
        "## What is in this bundle",
        "",
        "- `calls.jsonl`: one row per Context Hub call. Contains your search queries and result shape "
        "(hit counts, confidence, top hit ids), flags, latency.",
        "- `sessions.jsonl`: per-session counters. Session and participant ids are hashed.",
        "- **Not included:** transcript text, shell commands, file paths, other tools' output "
        "(only an error class name such as `ImportError`).",
        "- `report.md` is rebuilt from `calls.jsonl` and `manifest.json` by `audit repack`: delete a "
        "row in `calls.jsonl`, repack, and it is gone from here too. The answers section counts the "
        "original extraction.",
        "- Redaction is best-effort. Queries are your own words: read them before sharing.",
    ]
    return "\n".join(md) + "\n"
