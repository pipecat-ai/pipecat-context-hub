#!/usr/bin/env python3
"""Extract Pipecat Context Hub usage facts from coding-agent transcripts.

Deterministic, offline, stdlib only. Judgment (was a result right?) is left to
the skill; this script only collects evidence and cheap heuristic flags.

Usage:
    extract.py --input DIR [--input DIR ...] --out OUT_DIR [--anonymize] [--since YYYY-MM-DD]
    extract.py --repack OUT_DIR      # after editing OUT_DIR/share/ by hand

Input layout: any directory tree of transcripts, e.g. one sub-directory per
participant. Adapters: Claude Code, Codex, pi (auto-detected per file). Anything
else (Cursor exports, chat dumps) falls back to a text scan: Context Hub use is detected
but not analysed, and the session is flagged needs_manual_read. The first path
component under --input is the participant label.

Output (OUT_DIR):
    sessions.jsonl  one line per session: usage flags + counters + flagged issues
    calls.jsonl     one line per Context Hub call: input, parsed result, follow-up window
    summary.json    aggregate counts across all sessions
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

SEARCH_TOOLS = {"search_docs", "search_examples", "search_api", "get_code_snippet"}
LARGE_RESULT_BYTES = 30_000
SLOW_SECONDS = 8.0
FOLLOWUP_EVENTS = 8
ERROR_WINDOW_EVENTS = 25

ERROR_RE = re.compile(
    r"Traceback \(most recent call last\)|ImportError|ModuleNotFoundError|AttributeError|"
    r"unexpected keyword argument|TypeError:|ValidationError|NameError",
)
# Secrets must never reach the output files. Over-redact rather than under-redact.
SECRET_RES = [
    re.compile(r"\b(sk|pk|rk|dg|gsk|xox[abprs]|ghp|gho|github_pat)[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\b([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*[=:]\s*\S{8,}"),
    re.compile(r"\b[0-9a-f]{40}\b(?<!\b[0-9a-f]{40}\b(?=.*commit))"),  # 40-hex tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
HOME_RE = re.compile(r"/(Users|home)/[^/\s\"']+")
HIT_ID_KEYS = ("doc_id", "example_id", "path", "file_path", "title", "class_name", "name", "id")


def redact(text: str) -> str:
    text = HOME_RE.sub(r"/\1/<user>", text)
    for rx in SECRET_RES:
        text = rx.sub("<redacted>", text)
    return text


def clip(text: str, n: int) -> str:
    text = redact(text)
    return text if len(text) <= n else text[:n] + f"…[+{len(text) - n}]"


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def summarize_hub_result(text: str, extras: list[str], via: str | None = None) -> dict:
    out: dict = {"bytes": len(text.encode()) + sum(len(e.encode()) for e in extras)}
    data = None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        if via == "cli":
            # Shell output is wrapped (pipes, `head -c`, echoes): take the first decodable JSON object.
            start = text.find("{")
            if start >= 0:
                try:
                    data, _ = json.JSONDecoder().raw_decode(text[start:])
                    out["parse"] = "embedded"
                except ValueError:
                    pass
        if data is None:
            # CLI text that is not JSON is the harness's shell, not a Context Hub fault: not flagged.
            out["parse"] = "cli_unparsed" if via == "cli" else "non_json"
            out["text_head"] = clip(text, 300)
            return out
    if not isinstance(data, dict):
        out["parse"] = "non_object"
        return out
    out.setdefault("parse", "ok")
    hits = data.get("hits")
    if isinstance(hits, list):
        out["n_hits"] = len(hits)
        top = []
        for h in hits[:3]:
            if isinstance(h, dict):
                key = next((k for k in HIT_ID_KEYS if h.get(k)), None)
                top.append(clip(str(h[key]), 120) if key else "?")
        out["top_hits"] = top
    ev = data.get("evidence")
    if isinstance(ev, dict):
        out["confidence"] = ev.get("confidence")
        out["low_confidence"] = bool(ev.get("low_confidence"))
        out["unknown"] = [clip(json.dumps(u), 160) for u in (ev.get("unknown") or [])[:3]]
        out["next_queries"] = [
            clip(str(q), 100) for q in (ev.get("next_retrieval_queries") or [])[:3]
        ]
    if data.get("corpus_hint"):
        out["corpus_hint"] = clip(json.dumps(data["corpus_hint"]), 300)
    if any("stale" in e.lower() or "index" in e.lower() for e in extras):
        out["staleness_footer"] = clip(" ".join(extras), 200)
    for k in ("error", "message"):
        if isinstance(data.get(k), str) and not hits:
            out["error_text"] = clip(data[k], 200)
    return out


HUB_TOOLS = (
    "search_docs",
    "search_examples",
    "search_api",
    "get_doc",
    "get_example",
    "get_code_snippet",
    "check_deprecation",
    "get_hub_status",
)
# MCP surface: any harness prefix, `-` or `_` spelling (Claude Code, Codex and others differ).
MCP_RE = re.compile(r"pipecat[-_]context[-_]hub__(" + "|".join(HUB_TOOLS) + r")\b")
# CLI surface: `pipecat-context-hub search-docs ...` / `pipecat context-hub ...` (uvx/uv run prefixes fine).
CLI_RE = re.compile(
    r"(?:pipecat[-_]context[-_]hub|pipecat\s+context-hub)\s+"
    r"(search-api|search-docs|search-examples|get-doc|get-example|get-code-snippet|check-deprecation|status)\b"
)
MCP_JS_CALL_RE = re.compile(
    r"tools\.[\w$]*pipecat[-_]context[-_]hub__(" + "|".join(HUB_TOOLS) + r")\s*\("
)
CLI_TO_TOOL = {"status": "get_hub_status"}
SHELLISH = {"bash", "shell", "exec", "exec_command", "run_shell_command", "local_shell", "terminal"}
READONLY = {"read", "grep", "glob", "ls", "find"}
# Real Pipecat development, not merely a path like /pipecat-ai/... in the cwd.
PIPECAT_WORK_RE = re.compile(
    r"(from|import)\s+pipecat\b|pipecat-ai\s*(\[|[<>=~])|uv add[^\"]*pipecat|"
    r"pipecat\.(pipeline|services|transports|frames|processors|runner|audio)|\bPipelineTask\b"
)
TEXT_SUFFIXES = {".jsonl", ".json", ".md", ".txt"}


def categorize(name: str, inp) -> str | None:
    """Tag events where the agent went around Context Hub (reading source / web docs). Heuristic."""
    blob = json.dumps(inp) if not isinstance(inp, str) else inp
    n = name.lower()
    if (n in READONLY or n in SHELLISH) and (
        "site-packages/pipecat" in blob or re.search(r"\.venv[^\"]*pipecat", blob)
    ):
        return "read_pipecat_source"
    if re.search(r"docs\.pipecat\.ai|github\.com/pipecat-ai|pipecat\.ai/", blob) and not (
        MCP_RE.search(blob) or CLI_RE.search(blob)
    ):
        return "web_pipecat"
    return None


def detect_hub(name: str, inp) -> tuple[str | None, str | None, int]:
    """Return (hub_tool, via, n_calls_in_event); via is 'mcp' or 'cli'."""
    m = MCP_RE.search(name)
    if m:
        return m.group(1), "mcp", 1
    n = name.lower()
    blob = inp if isinstance(inp, str) else json.dumps(inp)
    mm = (
        MCP_JS_CALL_RE.findall(blob) if n in SHELLISH else []
    )  # codex wraps MCP calls in a JS `exec`
    if mm:
        return mm[0], "mcp", len(mm)
    if n in SHELLISH:
        cm = CLI_RE.findall(blob)
        if cm:
            return CLI_TO_TOOL.get(cm[0], cm[0].replace("-", "_")), "cli", len(cm)
    return None, None, 0


def hub_invocation(inp) -> str:
    """Just the Context Hub part of a call: the CLI segment or the MCP call, never the whole shell line."""
    if isinstance(inp, dict):
        text = inp.get("command") or inp.get("cmd") or json.dumps(inp)
    else:
        text = inp or ""
    m = CLI_RE.search(text)
    if m:
        seg = re.split(r"\s*(?:\|\||&&|[|;&>\n])", text[m.start() :], maxsplit=1)[0]
        return clip(seg, 300)
    m = MCP_RE.search(text)
    return clip(text[m.start() : m.start() + 300], 300) if m else ""


def _text_blocks(c) -> tuple[str, list[str]]:
    if isinstance(c, str):
        return c, []
    if isinstance(c, list):
        t = [b.get("text", "") for b in c if isinstance(b, dict) and isinstance(b.get("text"), str)]
        if t:
            return t[0], t[1:]
    return "", []


# ---- harness adapters: yield ("meta", dict) | ("prompt",) | ("call", id, ts, name, input)
# ---- | ("result", id, ts, text, extras, is_error)


def detect_harness(lines: list[str]) -> str:
    for line in lines[:40]:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = o.get("type")
        if t in ("session_meta", "response_item", "turn_context", "event_msg"):
            return "codex"
        if "sessionId" in o and t in ("assistant", "user", "system", "attachment"):
            return "claude"
        if t in ("session", "message", "model_change") and "parentId" in o or t == "session":
            return "pi"
    return "unknown"


def adapt_claude(lines):
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        # `api-request-blob` lines duplicate real messages; counting them double-counts calls.
        if o.get("type") not in ("assistant", "user"):
            continue
        yield (
            "meta",
            {
                "id": o.get("sessionId"),
                "version": o.get("version"),
                "cwd": o.get("cwd"),
                "ts": o.get("timestamp"),
            },
        )
        content = (o.get("message") or {}).get("content")
        if o["type"] == "user" and isinstance(content, str):
            yield ("prompt",)
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                yield (
                    "call",
                    b.get("id", ""),
                    o.get("timestamp"),
                    b.get("name", ""),
                    b.get("input") or {},
                )
            elif b.get("type") == "tool_result":
                text, extras = _text_blocks(b.get("content"))
                yield (
                    "result",
                    b.get("tool_use_id", ""),
                    o.get("timestamp"),
                    text,
                    extras,
                    bool(b.get("is_error")),
                )


def adapt_pi(lines):
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "session":
            yield "meta", {"id": o.get("id"), "cwd": o.get("cwd"), "ts": o.get("timestamp")}
            continue
        m = o.get("message")
        if o.get("type") != "message" or not isinstance(m, dict):
            continue
        yield "meta", {"ts": o.get("timestamp")}
        role, content = m.get("role"), m.get("content")
        if role == "user":
            yield ("prompt",)
        elif role == "assistant" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "toolCall":
                    yield (
                        "call",
                        b.get("id", ""),
                        o.get("timestamp"),
                        b.get("name", ""),
                        b.get("arguments") or {},
                    )
        elif role == "toolResult":
            text, extras = _text_blocks(content)
            yield (
                "result",
                m.get("toolCallId", ""),
                o.get("timestamp"),
                text,
                extras,
                bool(m.get("isError")),
            )


def adapt_codex(lines):
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        p = o.get("payload")
        if not isinstance(p, dict):
            continue
        ts, pt = o.get("timestamp"), p.get("type")
        if o.get("type") == "session_meta":
            yield (
                "meta",
                {"id": p.get("id"), "version": p.get("cli_version"), "cwd": p.get("cwd"), "ts": ts},
            )
        if o.get("type") != "response_item":
            continue
        yield "meta", {"ts": ts}
        if pt == "message" and p.get("role") == "user":
            yield ("prompt",)
        elif pt == "function_call":
            try:
                args = json.loads(p.get("arguments") or "{}")
            except ValueError:
                args = {"raw": p.get("arguments")}
            yield "call", p.get("call_id", ""), ts, p.get("name", ""), args
        elif pt == "custom_tool_call":
            yield "call", p.get("call_id", ""), ts, p.get("name", ""), p.get("input") or ""
        elif pt in ("function_call_output", "custom_tool_call_output"):
            text, extras = _text_blocks(p.get("output"))
            yield "result", p.get("call_id", ""), ts, text, extras, False


ADAPTERS = {"claude": adapt_claude, "pi": adapt_pi, "codex": adapt_codex}


def event_target(name: str, inp) -> str:
    if isinstance(inp, str):
        return clip(inp, 120)
    for k in ("file_path", "path", "pattern", "command", "cmd", "url", "query", "description"):
        if isinstance(inp.get(k), str):
            return clip(inp[k], 120)
    return ""


def process_text_scan(path: Path, label: str, anonymize: bool, raw: str) -> tuple[dict, list[dict]]:
    """Fallback for harnesses without an adapter (Cursor exports, chat dumps, ...): detect only."""
    tools = Counter(m for m in MCP_RE.findall(raw))
    tools.update(CLI_TO_TOOL.get(c, c.replace("-", "_")) for c in CLI_RE.findall(raw))
    participant = hashlib.sha256(label.encode()).hexdigest()[:10] if anonymize else label
    return {
        "participant": participant,
        "session_id": path.stem,
        "file": redact(path.name),
        "harness": "unknown",
        "fidelity": "text_scan",
        "hub_available": bool(MCP_RE.search(raw) or CLI_RE.search(raw)),
        "hub_used": bool(tools),
        "pipecat_work": bool(PIPECAT_WORK_RE.search(raw)),
        "missed_opportunity": bool(PIPECAT_WORK_RE.search(raw)) and not tools,
        "hub_calls": sum(tools.values()),
        "hub_calls_by_tool": dict(tools),
        "flag_counts": {},
        "issues": [],
        "tool_calls_total": 0,
        "needs_manual_read": True,
    }, []


def process_session(path: Path, label: str, anonymize: bool) -> tuple[dict, list[dict]]:
    raw = path.read_text(errors="replace")
    lines = raw.splitlines()
    harness = detect_harness(lines) if path.suffix == ".jsonl" else "unknown"
    if harness == "unknown":
        if "pipecat" not in raw.lower():
            return {"tool_calls_total": 0}, []
        return process_text_scan(path, label, anonymize, raw)

    events: list[dict] = []  # every tool call, in order
    by_id: dict[str, dict] = {}
    meta: dict = {}
    pipecat_work = False
    user_prompts = 0

    for rec in ADAPTERS[harness](lines):
        kind = rec[0]
        if kind == "meta":
            for k, v in rec[1].items():
                if k == "ts":
                    meta.setdefault("start", v)
                    meta["end"] = v or meta.get("end")
                elif v:
                    meta.setdefault(k, v)
        elif kind == "prompt":
            user_prompts += 1
        elif kind == "call":
            _, cid, ts, name, inp = rec
            tool, via, n_in_event = detect_hub(name, inp)
            ev = {
                "idx": len(events),
                "ts": ts,
                "name": name,
                "input": inp,
                "target": event_target(name, inp),
                "category": categorize(name, inp),
                "is_hub": tool is not None,
                "hub_tool": tool,
                "via": via,
                "batched": n_in_event,
            }
            if (
                tool is None
                and name.lower() not in READONLY
                and PIPECAT_WORK_RE.search(json.dumps(inp))
            ):
                pipecat_work = True
            events.append(ev)
            by_id[cid] = ev
        elif kind == "result":
            _, cid, ts, text, extras, is_err = rec
            ev = by_id.get(cid)
            if not ev:
                continue
            ev["is_error"] = is_err
            t0, t1 = parse_ts(ev["ts"]), parse_ts(ts)
            ev["latency_s"] = round(t1 - t0, 2) if t0 and t1 else None
            if ev["is_hub"]:
                ev["result"] = summarize_hub_result(text, extras, ev["via"])
            ev["has_py_error"] = bool(ERROR_RE.search(text[:20_000]))
            ev["err_head"] = clip(text, 200) if ev["has_py_error"] else ""
    meta["sessionId"] = meta.get("id")
    meta["version"] = meta.get("version")

    hub_events = [e for e in events if e["is_hub"]]
    first_hub = hub_events[0]["idx"] if hub_events else None
    session_id = meta.get("sessionId") or path.stem
    if (
        path.stem != session_id
    ):  # subagent files share the parent's id: keep rows/refs unique per file
        session_id = f"{session_id}/{path.stem}"
    participant = hashlib.sha256(label.encode()).hexdigest()[:10] if anonymize else label

    calls: list[dict] = []
    seen: Counter = Counter()
    prev_search: dict | None = None
    flags: Counter = Counter()
    issues: list[dict] = []

    for e in hub_events:
        tool = e["hub_tool"]
        res = e.get("result", {})
        key = (tool, json.dumps(e["input"], sort_keys=True))
        seen[key] += 1
        after = events[e["idx"] + 1 : e["idx"] + 1 + FOLLOWUP_EVENTS]
        window = events[e["idx"] + 1 : e["idx"] + 1 + ERROR_WINDOW_EVENTS]
        call_flags = []
        if e.get("is_error") or res.get("error_text"):
            call_flags.append("error")
        head = res.get("text_head", "")
        if "InputValidationError" in head or "input that could not" in head:
            call_flags.append("input_validation_error")  # agent misread the tool schema
        elif re.search(r"exceeds maximum allowed tokens|Output too large|persisted-output", head):
            call_flags.append(
                "result_exceeds_harness_limit"
            )  # Context Hub response too big to read
        elif res.get("parse") == "non_json":
            call_flags.append("non_json_result")
        if tool in SEARCH_TOOLS and res.get("n_hits") == 0:
            call_flags.append("zero_hits")
        if res.get("low_confidence"):
            call_flags.append("low_confidence")
        if res.get("corpus_hint"):
            call_flags.append("corpus_hint")
        if res.get("bytes", 0) > LARGE_RESULT_BYTES:
            call_flags.append("large_result")
        if (e.get("latency_s") or 0) > SLOW_SECONDS:
            call_flags.append("slow")
        if seen[key] > 1:
            call_flags.append("exact_repeat")
        if (
            prev_search
            and tool in SEARCH_TOOLS
            and prev_search["tool"] == tool
            and prev_search["bad"]
            and e["idx"] - prev_search["idx"] <= 3
        ):
            call_flags.append("reformulation_after_bad_result")
        q = e["input"].get("query", "") if isinstance(e["input"], dict) else ""
        if (
            tool in SEARCH_TOOLS
            and isinstance(q, str)
            and re.search(r"\b(and|&|,)\b", q)
            and " + " not in q
            and " & " not in q
            and len(q.split()) > 6
        ):
            call_flags.append("multi_concept_without_delimiter")
        if any(w.get("category") for w in after):
            call_flags.append("agent_bypassed_after_result")
        downstream = [
            {"tool": w["name"], "target": w["target"], "err": w["err_head"]}
            for w in window
            if w.get("has_py_error") and not w["is_hub"]
        ]
        if downstream:
            call_flags.append("downstream_error")
        prev_search = {
            "tool": tool,
            "idx": e["idx"],
            "bad": bool({"zero_hits", "low_confidence"} & set(call_flags)),
        }
        for f in call_flags:
            flags[f] += 1
        calls.append(
            {
                "participant": participant,
                "session_id": session_id,
                "call_idx": e["idx"],
                "ts": e["ts"],
                "tool": tool,
                "via": e["via"],
                "hub_invocation": hub_invocation(e["input"]),
                "input": json.loads(redact(json.dumps(e["input"]))),
                "latency_s": e.get("latency_s"),
                "result": res,
                "flags": call_flags,
                "followups": [
                    {
                        "tool": w["name"],
                        "target": w["target"],
                        "category": w["category"],
                        "hub": w["is_hub"],
                    }
                    for w in after
                ],
                "downstream_errors": downstream[:3],
            }
        )
        if call_flags:
            issues.append({"call_idx": e["idx"], "tool": tool, "flags": call_flags})

    bypass_before = [
        e for e in events if e["category"] and (first_hub is None or e["idx"] < first_hub)
    ]
    session = {
        "participant": participant,
        "session_id": session_id,
        "file": redact(str(path.name)),
        "is_subagent": "subagents" in path.parts,
        "harness": harness,
        "fidelity": "events" if harness != "codex" else "events_approx_batched",
        "harness_version": meta.get("version"),
        "start": meta.get("start"),
        "end": meta.get("end"),
        "user_prompts": user_prompts,
        "tool_calls_total": len(events),
        "hub_available": bool(
            MCP_RE.search(raw) or CLI_RE.search(raw) or "mcp__pipecat-context-hub" in raw
        ),
        "hub_via": dict(Counter(e["via"] for e in hub_events)),
        "hub_used": bool(hub_events),
        "pipecat_work": pipecat_work,
        "missed_opportunity": pipecat_work and not hub_events,
        "hub_calls": len(hub_events),
        "hub_calls_by_tool": dict(Counter(e["hub_tool"] for e in hub_events)),
        "hub_share_of_calls": round(len(hub_events) / len(events), 3) if events else 0,
        "bypass_events_before_first_hub": len(bypass_before),
        "bypass_events_total": sum(1 for e in events if e["category"]),
        "flag_counts": dict(flags),
        "issues": issues,
    }
    return session, calls


SHARE_PATH_RE = re.compile(
    r"(?:/(?:Users|home|private|tmp|var|mnt|opt|root|Volumes|etc)/[^\s\"'`;|)\]]+|[A-Za-z]:\\[^\s\"'`;|)\]]+)"
)


def scrub_paths(v):
    """Replace local absolute paths in every string of a share row (doc paths like /guides/x stay)."""
    if isinstance(v, str):
        return SHARE_PATH_RE.sub("<path>", v)
    if isinstance(v, list):
        return [scrub_paths(x) for x in v]
    if isinstance(v, dict):
        return {k: scrub_paths(x) for k, x in v.items()}
    return v


SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "0.1.0"

FLAG_GLOSSARY = {
    "error": "Context Hub returned a tool error.",
    "input_validation_error": "The harness rejected the call as schema-invalid: the agent misread the tool schema.",
    "result_exceeds_harness_limit": "Result too large for the harness to show the agent (it was truncated or saved to a file).",
    "large_result": "Result over 30 KB, even if the harness accepted it.",
    "non_json_result": "MCP result was not JSON (unexpected for Context Hub).",
    "zero_hits": "A search returned no hits.",
    "low_confidence": "evidence.low_confidence was true (confidence < 0.3).",
    "corpus_hint": "Response said the query names a vendor/topic whose repos are not indexed.",
    "slow": "Latency over 8 s (tool result timestamp minus call timestamp; includes harness overhead).",
    "exact_repeat": "Same tool and identical input already called earlier in the session.",
    "reformulation_after_bad_result": "Same search tool called again within 3 events after a zero-hit or low-confidence result.",
    "multi_concept_without_delimiter": "Long query joined concepts with and/commas instead of ' + '.",
    "agent_bypassed_after_result": "Within 8 events the agent read pipecat source in site-packages/.venv or fetched pipecat web docs.",
    "downstream_error": "A Python error (ImportError, AttributeError, unexpected keyword...) appeared in a non-Context-Hub tool result within 25 events. CORRELATION ONLY: needs judgment to say it came from what Context Hub returned.",
}
SEVERITY = {
    "downstream_error": 3, "input_validation_error": 3, "result_exceeds_harness_limit": 3, "error": 3,
    "zero_hits": 2, "low_confidence": 2, "reformulation_after_bad_result": 2, "agent_bypassed_after_result": 2,
    "corpus_hint": 1, "slow": 1, "exact_repeat": 1, "large_result": 1, "multi_concept_without_delimiter": 1,
    "non_json_result": 1,
}  # fmt: skip

FIELDS_MD = """\
## Files

- `manifest.json`: schema_version, extractor_version, counts, scope, what was removed. Read first.
- `report.md`: auto-generated summary and "look first" list. **Unjudged**: flags are heuristics.
- `calls.jsonl`: one row per Context Hub call (the raw dataset).
- `sessions.jsonl`: one row per session.

## calls.jsonl row

| field | meaning |
|---|---|
| `ref` | stable id `participant/session#call_idx`; cite it in findings |
| `participant`, `session` | hashed ids |
| `tool` | `search_docs`, `search_api`, `search_examples`, `get_doc`, `get_example`, `get_code_snippet`, `check_deprecation`, `get_hub_status` |
| `via` | `mcp` (tool call) or `cli` (shell: `pipecat-context-hub <cmd>`) |
| `hub_invocation` | the Context Hub part of the call only (CLI segment / MCP call text) |
| `input` | MCP tool arguments; `{}` for CLI/batched calls (use `hub_invocation`) |
| `ts`, `latency_s` | start time; seconds to result (includes harness overhead) |
| `result` | shape only: `n_hits`, `top_hits` (ids), `confidence`, `low_confidence`, `unknown`, `next_queries`, `corpus_hint`, `bytes`, `parse` (`ok`/`embedded` = JSON found; `cli_unparsed` = shell text, not a defect) |
| `flags` | heuristic flags, glossary below |
| `severity` | sum of flag weights; sort by it to triage |
| `followups` | next 8 tool calls: `tool`, `category` (`read_pipecat_source`/`web_pipecat`/null), `hub` (another Context Hub call) |
| `downstream_errors` | Python error class names seen in the next 25 non-hub results (text removed) |

## sessions.jsonl row

`harness` (`claude`/`codex`/`pi`/`unknown`), `fidelity` (`events`; `events_approx_batched` = Codex JS batches count as one event; `text_scan` = detection only, no calls), `hub_available` (name seen in transcript, not proof the server connected), `hub_used`, `pipecat_work` (real Pipecat code/commands seen), `missed_opportunity` (pipecat_work and not hub_used), `hub_calls`, `hub_calls_by_tool`, `hub_via`, `bypass_events_before_first_hub`, `bypass_events_total`, `hub_share_of_calls`, `hub_via`, `harness_version`, `start`/`end`, `flag_counts`, `user_prompts`, `tool_calls_total`.

## How to analyse

1. Check `manifest.json` schema_version is 1; if not, stop and look for the matching skill version.
2. Start from the "Look first" list in `report.md`; read each row's `followups` and `downstream_errors`.
3. A flag is a place to look, never a verdict. Judge each as correct / partial / incorrect / unused / unverifiable, citing `ref` and the follow-up evidence. If the evidence is not in the row, say unverifiable.
4. To confirm a retrieval defect, re-run `hub_invocation` against a current index and say which index version.
5. Group non-correct findings by target: hub-retrieval, hub-corpus, hub-tools, pipecat-docs, pipecat-lib, agent-misuse.
6. Absence of data is not absence of a problem: `text_scan` sessions and un-parsed harnesses are blind spots.
"""

ERR_NAME_RE = re.compile(r"[A-Za-z]*(?:Error|Exception)\b|Traceback")


BUNDLE_FILES = ("manifest.json", "SCHEMA.md", "report.md", "calls.jsonl", "sessions.jsonl")


def pack_share(share: Path) -> tuple[Path, str]:
    """Deterministic share.tar.gz next to share/ (flat names, no owners, fixed mtime) + its sha256."""
    import gzip
    import tarfile

    tar_path = share.parent / "share.tar.gz"
    with tar_path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tf:
            for name in BUNDLE_FILES:
                info = tf.gettarinfo(str(share / name), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o644
                with (share / name).open("rb") as fh:
                    tf.addfile(info, fh)
    digest = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    (share.parent / "share.tar.gz.sha256").write_text(f"{digest}  share.tar.gz\n")
    return tar_path, digest


def tool_rows(calls: list[dict]) -> list[str]:
    import statistics

    rows = []
    for tool in sorted({c["tool"] for c in calls}):
        cs = [c for c in calls if c["tool"] == tool]
        lat = sorted(c["latency_s"] for c in cs if c.get("latency_s") is not None)
        conf = [
            c["result"]["confidence"]
            for c in cs
            if isinstance(c["result"].get("confidence"), (int, float))
        ]
        p90 = lat[min(len(lat) - 1, int(0.9 * len(lat)))] if lat else ""
        rows.append(
            f"| {tool} | {len(cs)} | {sum(bool(c['flags']) for c in cs)} "
            f"| {round(statistics.median(lat), 1) if lat else ''} | {p90} "
            f"| {sum('zero_hits' in c['flags'] for c in cs)} "
            f"| {round(statistics.mean(conf), 2) if conf else ''} |"
        )
    return rows


def build_share_bundle(
    out: Path, summary: dict, sessions: list[dict], calls: list[dict], scope: dict
) -> Path:
    """Write out/share/: a stricter, human-reviewable subset that is safe to send on.

    Kept: Context Hub tool, query/inputs, result shape (hit count, confidence, top hit ids), flags,
    latency. Dropped: session ids/file names (hashed), shell commands, file paths, and error text of
    the agent's other tool calls (only the error class name survives). Nothing is sent from here.
    """
    share = out / "share"
    share.mkdir(exist_ok=True)

    def sid(x: str) -> str:
        return hashlib.sha256(x.encode()).hexdigest()[:10]

    with (share / "sessions.jsonl").open("w") as fh:
        for r in sessions:
            keep = {
                k: v
                for k, v in r.items()
                if k not in {"file", "session_id", "participant", "issues"}
            }
            keep["session"] = sid(r["session_id"])
            keep["participant"] = sid(r["participant"])
            fh.write(json.dumps(scrub_paths(keep)) + "\n")
    with (share / "calls.jsonl").open("w") as fh:
        for c in calls:
            keep = dict(c)
            if c["via"] == "cli" or not isinstance(c["input"], dict):
                keep["input"] = {}  # whole shell line / JS batch: only hub_invocation is shared
            keep["session"] = sid(keep.pop("session_id"))
            keep["participant"] = sid(keep["participant"])
            keep["ref"] = f"{keep['participant']}/{keep['session']}#{keep['call_idx']}"
            keep["severity"] = sum(SEVERITY.get(f, 1) for f in c["flags"])
            keep["followups"] = [
                {k: w[k] for k in ("tool", "category", "hub")} for w in c["followups"]
            ]
            keep["downstream_errors"] = [
                {"tool": d["tool"], "error": (ERR_NAME_RE.search(d["err"]) or [""])[0]}
                for d in c["downstream_errors"]
            ]
            keep["result"] = {k: v for k, v in c["result"].items() if k != "text_head"}
            fh.write(json.dumps(scrub_paths(keep)) + "\n")

    flagged = sorted(
        (c for c in calls if c["flags"]),
        key=lambda c: -sum(SEVERITY.get(f, 1) for f in c["flags"]),
    )
    rows = []
    for c in flagged[:60]:
        r = c["result"]
        inp = c["input"] if isinstance(c["input"], dict) else {}
        q = inp.get("query") or inp.get("path") or inp.get("doc_id") or c["hub_invocation"]
        rows.append(
            f"| {sid(c['participant'])}/{sid(c['session_id'])}#{c['call_idx']} | {c['tool']} | {clip(str(q), 80).replace('|', '/')} | {', '.join(c['flags'])} "
            f"| {r.get('n_hits', '')} | {round(r['confidence'], 2) if isinstance(r.get('confidence'), float) else ''} |"
        )
    md = [
        "# Context Hub session report (auto-generated, unjudged)",
        "",
        f"Sessions: {summary['sessions']} · used Context Hub: {summary['hub_used']} · "
        f"Pipecat work without it: {summary['missed_opportunity']} · calls: {summary['hub_calls']}",
        f"Harnesses: {summary['by_harness']}",
        f"Calls by tool: {summary['calls_by_tool']}",
        f"Flags: {summary['flag_totals']}",
        "",
        "## Look first (flagged calls, highest severity first)"
        + (f" — first 60 of {len(flagged)}" if len(flagged) > 60 else ""),
        "",
        "| ref | tool | query | flags | hits | confidence |",
        "|---|---|---|---|---|---|",
        *rows,
        "",
        "## Per-tool stats",
        "",
        "| tool | calls | flagged | median s | p90 s | zero hits | mean confidence |",
        "|---|---|---|---|---|---|---|",
        *tool_rows(calls),
        "",
        "## What is in this bundle",
        "",
        "- `calls.jsonl`: one row per Context Hub call. Contains your search queries and result shape "
        "(hit counts, confidence, top hit ids), flags, latency.",
        "- `sessions.jsonl`: per-session counters. Session and participant ids are hashed.",
        "- **Not included:** transcript text, shell commands, file paths, other tools' output "
        "(only an error class name such as `ImportError`).",
        "- Redaction is best-effort. Queries are your own words: read them before sharing.",
    ]
    (share / "report.md").write_text("\n".join(md) + "\n")
    (share / "SCHEMA.md").write_text(
        "# Context Hub session bundle: schema v" + str(SCHEMA_VERSION) + "\n\n" + FIELDS_MD
        + "\n## Flag glossary (severity weight)\n\n"
        + "\n".join(f"- `{k}` ({SEVERITY.get(k, 1)}): {v}" for k, v in FLAG_GLOSSARY.items())
        + "\n"
    )  # fmt: skip
    from datetime import datetime, timezone

    (share / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "extractor_version": EXTRACTOR_VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "scope": scope,
                "counts": {k: summary[k] for k in ("sessions", "participants", "hub_used", "hub_calls")},
                "harnesses": summary["by_harness"],
                "needs_manual_read": summary["needs_manual_read"],
                "files": ["manifest.json", "SCHEMA.md", "report.md", "calls.jsonl", "sessions.jsonl"],
                "removed": [
                    "transcript text", "shell commands", "file paths (replaced with <path>)",
                    "other tools' output (error class names kept)", "session/participant ids (hashed)",
                    "raw non-JSON result text",
                ],
                "redaction": "best-effort regexes for keys/tokens/home paths; queries are verbatim",
                "caveats": [
                    "flags are heuristics, not verdicts",
                    "downstream_error is correlation only",
                    "latency includes harness overhead",
                ],
            },
            indent=2,
        )
    )  # fmt: skip
    return share


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--input", action="append", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument(
        "--repack",
        type=Path,
        metavar="OUT_DIR",
        help="rebuild share.tar.gz from an edited OUT_DIR/share/",
    )
    ap.add_argument("--anonymize", action="store_true", help="hash participant labels")
    ap.add_argument(
        "--since", help="YYYY-MM-DD: skip sessions that started earlier (limit to the event)"
    )
    args = ap.parse_args()
    if args.repack:
        share = args.repack / "share"
        missing = [n for n in BUNDLE_FILES if not (share / n).is_file()]
        if missing:
            print(f"cannot repack: {share} is missing {missing}", file=sys.stderr)
            return 1
        tar_path, digest = pack_share(share)
        print(f"{tar_path} sha256 {digest}")
        return 0
    if not args.input or not args.out:
        ap.error("--input and --out are required (unless --repack)")

    args.out.mkdir(parents=True, exist_ok=True)
    sessions: list[dict] = []
    all_calls: list[dict] = []
    for root in args.input:
        if not root.is_dir():
            print(f"not a directory: {root}", file=sys.stderr)
            return 1
        for f in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES):
            rel = f.relative_to(root)
            label = rel.parts[0] if len(rel.parts) > 1 else root.name
            try:
                s, c = process_session(f, label, args.anonymize)
            except Exception as exc:  # one bad file must not sink the batch
                print(f"skip {f.name}: {exc}", file=sys.stderr)
                continue
            if s["tool_calls_total"] == 0 and s.get("fidelity") != "text_scan":
                continue
            if args.since:
                from datetime import datetime, timezone

                started = (s.get("start") or "")[:10] or datetime.fromtimestamp(
                    f.stat().st_mtime, timezone.utc
                ).strftime("%Y-%m-%d")
                if started < args.since:
                    continue
            sessions.append(s)
            all_calls.extend(c)

    with (args.out / "sessions.jsonl").open("w") as fh:
        for s in sessions:
            fh.write(json.dumps(s) + "\n")
    with (args.out / "calls.jsonl").open("w") as fh:
        for c in all_calls:
            fh.write(json.dumps(c) + "\n")

    flag_totals: Counter = Counter()
    tool_totals: Counter = Counter()
    for s in sessions:
        flag_totals.update(s["flag_counts"])
        tool_totals.update(s["hub_calls_by_tool"])
    summary = {
        "sessions": len(sessions),
        "participants": len({s["participant"] for s in sessions}),
        "hub_available": sum(s["hub_available"] for s in sessions),
        "hub_used": sum(s["hub_used"] for s in sessions),
        "pipecat_work": sum(s["pipecat_work"] for s in sessions),
        "missed_opportunity": sum(s["missed_opportunity"] for s in sessions),
        "hub_calls": sum(s["hub_calls"] for s in sessions),
        "calls_by_tool": dict(tool_totals),
        "flag_totals": dict(flag_totals),
        "by_harness": dict(Counter(s["harness"] for s in sessions)),
        "needs_manual_read": sum(bool(s.get("needs_manual_read")) for s in sessions),
        "sessions_with_issues": sum(bool(s["issues"]) for s in sessions),
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    share = build_share_bundle(
        args.out, summary, sessions, all_calls, {"since": args.since, "anonymized": args.anonymize}
    )
    tar_path, digest = pack_share(share)
    print(
        f"share bundle (review before sending): {share}\n"
        f"tarball: {tar_path} ({tar_path.stat().st_size} bytes, sha256 {digest[:16]}...)",
        file=sys.stderr,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
