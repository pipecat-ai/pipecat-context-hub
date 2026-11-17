"""Extract Context Hub usage facts from coding-agent transcripts (``pipecat-context-hub audit``).

Deterministic, offline, stdlib only. Judgment (was a result right?) is left to the caller (the
``context-hub-session-audit`` skill); this module only collects evidence and cheap heuristic flags,
and builds the scrubbed ``share/`` bundle plus its tarball.

Adapters: Claude Code, Codex, pi (auto-detected per file). Anything else (Cursor exports, chat
dumps) falls back to a text scan: Context Hub use is detected but not analysed, and the session
is flagged ``needs_manual_read``. The first path component under an input root is the participant
label.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import re
import secrets
import shlex
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipecat_context_hub.audit.analysis import SEARCH_TOOLS, analyze, annotate_calls, link_parents
from pipecat_context_hub.audit.analysis import prune_refs
from pipecat_context_hub.audit.bundle import (
    BUNDLE_FILES,
    SCHEMA_VERSION,
    ROW_COUNT_KEYS,
    BundleError,
    decode_json,
    load_files,
    manifest_error,
    parse_rows,
    row_counts,
)
from pipecat_context_hub.audit.egress import (
    ShareLeakError,
    clip,
    map_strings,
    public_tool_name,
    redact,
    sanitize,
    verify_share,
)
from pipecat_context_hub.audit.flags import FLAGS
from pipecat_context_hub.audit.report import render_report
from pipecat_context_hub.audit.shell import ShellSyntaxError, shell_hub_calls, shell_source

HUB_MENTION_RE = re.compile(r"context[- ]hub|pipecat[-_]context[-_]hub|\bPCH\b", re.IGNORECASE)
HUB_TOOLSEARCH_RE = re.compile(
    r"pipecat|context[-_ ]hub|search_docs|search_api|search_examples", re.IGNORECASE
)
LARGE_RESULT_BYTES = 30_000
SLOW_SECONDS = 8.0
FOLLOWUP_EVENTS = 8
ERROR_WINDOW_EVENTS = 25

ERROR_RE = re.compile(
    r"Traceback \(most recent call last\)|ImportError|ModuleNotFoundError|AttributeError|"
    r"unexpected keyword argument|TypeError:|ValidationError|NameError",
)
_TRACEBACK_HEAD = "Traceback (most recent call last)"
_CHAIN_MARKERS = ("During handling of the above exception", "The above exception was the direct")
# The line a Python traceback ends with: `ImportError: msg`, `pkg.mod.Error: msg`, `KeyboardInterrupt`.
_EXC_LINE_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?::|[ \t]*(?:\r|\n|$))"
)
HIT_ID_KEYS = ("doc_id", "example_id", "path", "file_path", "title", "class_name", "name", "id")


def error_start(text: str, m: re.Match[str]) -> int:
    """Where the error that ``m`` (an ``ERROR_RE`` match) found is named in ``text``.

    For a traceback that is its final exception line, which follows the frames and can sit far past
    any fixed head; for a chained traceback, the last one's. Otherwise the match itself.
    """
    if not m.group(0).startswith("Traceback"):
        return m.start()
    found, in_traceback = -1, True
    pos = text.find("\n", m.start())
    while 0 <= pos < len(text):
        start = pos + 1
        pos = text.find("\n", start)
        line = text[start : len(text) if pos < 0 else pos]
        if not line.strip() or line[0] in " \t":
            continue  # frames and code lines are indented
        if line.startswith(_TRACEBACK_HEAD):
            in_traceback = True
        elif line.startswith(_CHAIN_MARKERS):
            continue
        elif in_traceback and _EXC_LINE_RE.match(line):
            found, in_traceback = start, False
        else:
            break  # the traceback is over: later output is not its exception
        if pos < 0:
            break
    return found if found >= 0 else m.start()


def error_class(head: str) -> str:
    """The error class an ``err_head`` names: an identifier only, never message text."""
    m = _EXC_LINE_RE.match(head)
    if m:
        name = m.group(1).rsplit(".", 1)[-1]
        if ERR_NAME_RE.fullmatch(name) or name.endswith(("Interrupt", "Exit", "Warning")):
            return name
    found = ERR_NAME_RE.search(head)
    return found.group(0) if found else ""


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# The top-level key every serialised response of each hub tool carries (the tool output models in
# shared/types.py; a test pins this table to them). A search's key holds its result list.
HUB_RESULT_KEY = {
    "search_docs": "hits",
    "search_examples": "hits",
    "search_api": "hits",
    "get_code_snippet": "snippets",
    "get_doc": "doc_id",
    "get_example": "example_id",
    "check_deprecation": "deprecated",
    "get_hub_status": "server_version",
}
RESULT_LIST_KEYS = frozenset({"hits", "snippets"})
_EMBEDDED_ATTEMPTS = 100
# Neutral parse classes: the text came from the agent's own program (a shell line, a JS batch), not
# from Context Hub, so not finding a hub response in it is not a Context Hub fault.
_UNPARSED = {"cli": "cli_unparsed", "mcp_js": "js_unparsed"}


def _is_hub_response(data: Any, tool: str | None) -> bool:
    key = HUB_RESULT_KEY.get(tool or "")
    return isinstance(data, dict) and key is not None and key in data


def _embedded_hub_response(text: str, tool: str | None) -> dict[str, Any] | None:
    """The first JSON object in ``text`` that has ``tool``'s response shape, else None.

    Another command's JSON (a log line with a ``message``) is skipped: only the hub's own response
    shape may become a result, so nothing else that command printed reaches the bundle.
    """
    decoder = json.JSONDecoder()
    start = text.find("{")
    for _ in range(_EMBEDDED_ATTEMPTS):
        if start < 0:
            return None
        try:
            data, end = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if _is_hub_response(data, tool):
            return data  # type: ignore[no-any-return]
        start = text.find("{", end)
    return None


def summarize_hub_result(
    text: str, extras: list[str], via: str | None = None, tool: str | None = None
) -> dict[str, Any]:
    """The shape of one hub call's result.

    A direct ``mcp`` result is the hub's own response. A ``cli`` or ``mcp_js`` result is the
    output of the agent's own program, which may print anything: there a JSON object counts only
    when it has ``tool``'s response shape (``HUB_RESULT_KEY``), bare or embedded, and otherwise the
    result is unparsed (``cli_unparsed`` / ``js_unparsed``, not flagged) and nothing is derived
    from the text.
    """
    out: dict[str, Any] = {"bytes": len(text.encode()) + sum(len(e.encode()) for e in extras)}
    data: Any = None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if via in _UNPARSED:
        if not _is_hub_response(data, tool):
            data = _embedded_hub_response(text, tool)
            if data is not None:
                out["parse"] = "embedded"
        if data is None:
            out["parse"] = _UNPARSED[via]
            out["text_head"] = clip(text, 300)
            return out
    elif data is None:
        out["parse"] = "non_json"
        out["text_head"] = clip(text, 300)
        return out
    if not isinstance(data, dict):
        out["parse"] = "non_object"
        return out
    out.setdefault("parse", "ok")
    list_key = HUB_RESULT_KEY.get(tool or "")
    if list_key not in RESULT_LIST_KEYS:
        list_key = "hits"  # an unknown tool: read the common search shape
    hits = data.get(list_key)
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
    if via == "mcp":
        # Only a direct call's extra blocks are the hub's (or its harness's); a shell's or a JS
        # batch's are the agent program's output.
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
# Sibling files of a Claude Code project's transcripts that are not sessions: auto-memory notes,
# persisted oversized tool results and subagent metadata. Scanned as text they would become
# phantom participants and phantom "Pipecat work without Context Hub".
NOT_SESSION_DIRS = {"memory", "tool-results", ".git", "node_modules"}
NOT_SESSION_SUFFIXES = (".meta.json",)


def categorize(name: str, inp: Any) -> str | None:
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


def _cli_calls(inp: Any) -> list[list[str]] | None:
    """The hub CLI calls a shell input runs; None when it has no script or cannot be tokenised."""
    src = shell_source(inp)
    if src is None:
        return None
    try:
        return shell_hub_calls(src)
    except ShellSyntaxError:
        return None


def _cli_tool(subcommand: str) -> str:
    return CLI_TO_TOOL.get(subcommand, subcommand.replace("-", "_"))


def detect_hub(name: str, inp: Any) -> tuple[str | None, str | None, int]:
    """Return (hub_tool, via, n_calls_in_event); via is 'mcp' (a direct tool call), 'mcp_js' (inside a
    JS ``exec`` batch) or 'cli' (shell)."""
    m = MCP_RE.search(name)
    if m:
        return m.group(1), "mcp", 1
    n = name.lower()
    blob = inp if isinstance(inp, str) else json.dumps(inp)
    mm = (
        MCP_JS_CALL_RE.findall(blob) if n in SHELLISH else []
    )  # codex wraps MCP calls in a JS `exec`
    if mm:
        return mm[0], "mcp_js", len(mm)
    if n in SHELLISH:
        calls = _cli_calls(inp)
        if calls:
            sub = calls[0][2] if calls[0][0] == "pipecat" else calls[0][1]
            return _cli_tool(sub), "cli", len(calls)
        if calls is None:  # no script, or not tokenisable: count what the regex sees (fail closed
            cm = CLI_RE.findall(blob)  # on text: hub_invocation shares nothing for these)
            if cm:
                return _cli_tool(cm[0]), "cli", len(cm)
    return None, None, 0


def _call_expression(text: str, start: int, limit: int = 2000) -> str:
    """``text`` from ``start`` to the paren closing the first call (quote-aware).

    Fails closed: when no closing paren is found within ``limit`` (unterminated string, template
    literal, hostile batch) only the call prefix up to its opening paren is returned, never the
    unrelated code that follows.
    """
    depth, quote, i = 0, "", start
    end = min(len(text), start + limit)
    while i < end:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    opening = text.find("(", start)
    return text[start : opening + 1] + "...)" if opening >= 0 else ""


def command_text(inp: Any) -> str:
    """The shell line or JS batch of a call as one string (Codex's ``shell`` passes an argv list)."""
    if isinstance(inp, dict):
        cmd = inp.get("command") or inp.get("cmd")
        if isinstance(cmd, list):
            return " ".join(str(part) for part in cmd)
        return cmd if isinstance(cmd, str) else json.dumps(inp)
    return inp if isinstance(inp, str) else ""


def hub_invocation(inp: Any, via: str | None = None) -> str:
    """Just the Context Hub part of a call, never the whole shell line or batch.

    ``cli``: the hub command rebuilt from its parsed argv (re-quoted, launcher and path dropped), or
    ``""`` when the line cannot be tokenised. ``mcp_js``: the one call expression. ``mcp``: ``""``
    (a direct call's arguments live in ``input``). ``via=None`` tries each in turn.
    """
    if via in ("cli", None):
        calls = _cli_calls(inp)
        if calls:
            return clip(shlex.join(calls[0]), 300)
        if via == "cli":
            return ""
    if via in ("mcp_js", None):
        text = command_text(inp)
        m = MCP_JS_CALL_RE.search(text)
        if m:
            return clip(_call_expression(text, m.start()), 300)
    return ""


def _text_blocks(c: Any) -> tuple[str, list[str]]:
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


def adapt_claude(lines: list[str]) -> Iterator[tuple[Any, ...]]:
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        att = o.get("attachment")
        if o.get("type") == "attachment" and isinstance(att, dict):
            if att.get("type") == "deferred_tools_delta":
                names = att.get("addedNames") or []
                listed = [n for n in names if isinstance(n, str) and MCP_RE.search(n)]
                yield ("tools", {"hub_deferred": len(listed)})
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
        if o["type"] == "user" and not o.get("isMeta"):  # isMeta: harness-injected, not typed
            if isinstance(content, str):
                yield ("prompt", content)
            elif isinstance(content, list) and not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                # Prompts with images or IDE context arrive as text blocks, not a bare string.
                texts = [
                    b["text"]
                    for b in content
                    if isinstance(b, dict)
                    and b.get("type") == "text"
                    and isinstance(b.get("text"), str)
                ]
                if texts:
                    yield ("prompt", "\n".join(texts))
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


def adapt_pi(lines: list[str]) -> Iterator[tuple[Any, ...]]:
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


def adapt_codex(lines: list[str]) -> Iterator[tuple[Any, ...]]:
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


def event_target(name: str, inp: Any) -> str:
    if isinstance(inp, str):
        return clip(inp, 120)
    if not isinstance(inp, dict):
        return ""
    for k in ("file_path", "path", "pattern", "command", "cmd", "url", "query", "description"):
        if isinstance(inp.get(k), str):
            return clip(inp[k], 120)
        if k in ("command", "cmd") and isinstance(inp.get(k), list):
            return clip(command_text(inp), 120)
    return ""


def process_text_scan(
    path: Path, label: str, raw: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fallback for harnesses without an adapter (Cursor exports, chat dumps, ...): detect only."""
    tools = Counter(m for m in MCP_RE.findall(raw))
    tools.update(CLI_TO_TOOL.get(c, c.replace("-", "_")) for c in CLI_RE.findall(raw))
    participant = label
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


def process_session(path: Path, label: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = path.read_text(errors="replace")
    lines = raw.splitlines()
    harness = detect_harness(lines) if path.suffix == ".jsonl" else "unknown"
    if harness == "unknown":
        if "pipecat" not in raw.lower():
            return {"tool_calls_total": 0}, []
        return process_text_scan(path, label, raw)

    events: list[dict[str, Any]] = []  # every tool call, in order
    by_id: dict[str, dict[str, Any]] = {}
    ev: dict[str, Any] | None
    meta: dict[str, Any] = {}
    pipecat_work = False
    user_prompts = 0
    hub_deferred = 0
    prompt_texts = 0
    prompt_mentions = False
    tool_search_calls = 0
    hub_tool_search_calls = 0

    for rec in ADAPTERS[harness](lines):
        kind = rec[0]
        if kind == "tools":
            hub_deferred += rec[1]["hub_deferred"]
        elif kind == "meta":
            for k, v in rec[1].items():
                if k == "ts":
                    if v:  # a record without a timestamp must not pin `start` to None
                        meta.setdefault("start", v)
                    meta["end"] = v or meta.get("end")
                elif v:
                    meta.setdefault(k, v)
        elif kind == "prompt":
            user_prompts += 1
            if len(rec) > 1 and isinstance(rec[1], str):
                prompt_texts += 1
                prompt_mentions = prompt_mentions or bool(HUB_MENTION_RE.search(rec[1]))
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
            if name == "ToolSearch":
                tool_search_calls += 1
                hub_tool_search_calls += bool(HUB_TOOLSEARCH_RE.search(json.dumps(inp)))
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
                ev["result"] = summarize_hub_result(text, extras, ev["via"], ev["hub_tool"])
            err = ERROR_RE.search(text[:20_000])
            ev["has_py_error"] = bool(err)
            # Start where the error is named: an error after 200 characters of output, or at the end
            # of a long traceback, must keep its class name.
            ev["err_head"] = clip(text[error_start(text, err) :], 200) if err else ""
    meta["sessionId"] = meta.get("id")
    meta["version"] = meta.get("version")

    hub_events = [e for e in events if e["is_hub"]]
    first_hub = hub_events[0]["idx"] if hub_events else None
    session_id = meta.get("sessionId") or path.stem
    if (
        path.stem != session_id
    ):  # subagent files share the parent's id: keep rows/refs unique per file
        session_id = f"{session_id}/{path.stem}"
    participant = label

    calls: list[dict[str, Any]] = []
    seen: Counter[tuple[str, str]] = Counter()
    prev_search: dict[str, Any] | None = None
    flags: Counter[str] = Counter()
    issues: list[dict[str, Any]] = []

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
            and re.search(r"\band\b|[,&]", q)
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
                "hub_invocation": hub_invocation(e["input"], e["via"]),
                "input": map_strings(e["input"], redact),
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
    # Claude Code keeps a subagent's transcript in `<session>/subagents/`; only that directory
    # counts, so a user's own folder named `subagents` does not turn every session into one.
    is_subagent = path.parent.name == "subagents"
    session = {
        "participant": participant,
        "session_id": session_id,
        "file": redact(str(path.name)),
        "is_subagent": is_subagent,
        "parent_session_id": meta.get("sessionId") if is_subagent else None,
        "hub_tools_deferred": hub_deferred if harness == "claude" else None,
        "tool_search_calls": tool_search_calls,
        "hub_tool_search_calls": hub_tool_search_calls,
        "prompt_mentions_hub": prompt_mentions if prompt_texts else None,
        "harness": harness,
        "fidelity": "events" if harness != "codex" else "events_approx_batched",
        "harness_version": meta.get("version"),
        "start": meta.get("start"),
        "end": meta.get("end"),
        "user_prompts": user_prompts,
        "tool_calls_total": len(events),
        "hub_available": bool(
            "mcp__pipecat-context-hub" in raw or MCP_RE.search(raw) or CLI_RE.search(raw)
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


EXTRACTOR_VERSION = "0.1.0"

FIELDS_MD = """\
## Files

- `manifest.json`: schema_version, extractor_version, counts, scope, what was removed. Read first. `counts.call_rows`, `sessions`, `participants`, `hub_used` and `missed_opportunity` are counted from the rows in this bundle (`audit repack` re-counts them after an edit); `counts.hub_calls` is the extraction-time total and also counts calls seen only by a text scan (no rows), so it can exceed `call_rows`.
- `report.md`: starts with computed answers to four questions (used it? inefficient? correct? what to improve), then a "look first" list. **Unjudged**: all of it is heuristic. The same answers are in `manifest.json` under `analysis`.
- `calls.jsonl`: one row per Context Hub call (the raw dataset).
- `sessions.jsonl`: one row per session.

## calls.jsonl row

| field | meaning |
|---|---|
| `ref` | stable id `participant/session#call_idx`; cite it in findings |
| `participant`, `session` | hashed ids |
| `tool` | `search_docs`, `search_api`, `search_examples`, `get_doc`, `get_example`, `get_code_snippet`, `check_deprecation`, `get_hub_status` |
| `via` | `mcp` (direct tool call), `mcp_js` (inside a Codex-style JS batch) or `cli` (shell: `pipecat-context-hub <cmd>`) |
| `hub_invocation` | `cli`: the executed hub command rebuilt from its parsed argv, re-quoted, launcher (`uv run`, `uvx`, `env`...) and path dropped; empty when the shell line cannot be tokenised. `mcp_js`: the one call expression. Empty for direct `mcp` calls |
| `input` | MCP tool arguments for direct `mcp` calls only; `{}` otherwise (use `hub_invocation`). A field whose name says it holds a credential (`api_key`, `token`, `password`...) appears as `<redacted>` |
| `ts`, `latency_s` | start time; seconds to result (includes harness overhead) |
| `result` | shape only: `n_hits`, `top_hits` (ids), `confidence`, `low_confidence`, `unknown`, `next_queries`, `corpus_hint`, `bytes`, `parse` (`ok`/`embedded` = a hub-shaped JSON response found; `cli_unparsed` = shell output and `js_unparsed` = JS batch output with no hub-shaped JSON, including JSON without the tool's response shape: the agent's program printed it, not a defect; `non_json` = a direct `mcp` result that was not JSON, the only parse value that raises a flag) |
| `flags` | heuristic flags, glossary below |
| `severity` | sum of flag weights; sort by it to triage |
| `followups` | next 8 tool calls: `tool` (a Context Hub tool, a standard harness tool such as `Bash`/`Read`, or `other`: names of the participant's other tools are not shared), `category` (`read_pipecat_source`/`web_pipecat`/null), `hub` (another Context Hub call) |
| `downstream_errors` | Python error class names seen in the next 25 non-hub results (text removed) |
| `outcome` | signal class, NOT a verdict: `failed` / `miss` / `suspect` / `bypassed` / `inefficient` / `no_negative_signal` (first match wins; see `manifest.json` analysis.correctness.glossary) |
| `suspect_target` | likely owner of the signal: `hub-tools` / `hub-retrieval` / `hub-corpus` / `agent-misuse` / `needs-judgment`; null when `no_negative_signal` |

## sessions.jsonl row

`parent_session` (a subagent's parent `session` hash), `parent_used_hub`, `hub_tools_deferred` (how many Context Hub tools the harness listed as deferred, Claude Code only; deferred tools cannot be called until `ToolSearch` loads them), `tool_search_calls`, `hub_tool_search_calls`, `prompt_mentions_hub` (a user prompt, for a subagent its delegation prompt, names Context Hub; it may only cite it as a source of a fact, not tell the agent to use it; null when prompt text is not available), `no_use_signals` (coded reasons for a `missed_opportunity`: `hub_not_in_transcript`, `hub_tools_deferred_never_loaded`, `prompt_silent_about_hub`, `parent_also_unused`, `read_source_or_web_instead`, `unexplained`), `harness` (`claude`/`codex`/`pi`/`unknown`), `fidelity` (`events`; `events_approx_batched` = Codex JS batches count as one event; `text_scan` = detection only, no calls), `hub_available` (name seen in transcript, not proof the server connected), `hub_used`, `pipecat_work` (real Pipecat code/commands seen), `missed_opportunity` (pipecat_work and not hub_used), `hub_calls`, `hub_calls_by_tool`, `hub_via`, `bypass_events_before_first_hub`, `bypass_events_total`, `hub_share_of_calls`, `hub_via`, `harness_version`, `start`/`end`, `flag_counts`, `user_prompts`, `tool_calls_total`.

## How to analyse

1. Check `manifest.json` schema_version is 1; if not, stop and look for the matching skill version.
2. Read `manifest.json` `analysis` (or the top of `report.md`): usage, efficiency, outcome counts and the improvement backlog are already computed. Judge only what it flags.
2b. Start from the "Look first" list in `report.md`; read each row's `followups` and `downstream_errors`.
3. A flag is a place to look, never a verdict. Judge each as correct / partial / incorrect / unused / unverifiable, citing `ref` and the follow-up evidence. If the evidence is not in the row, say unverifiable.
4. To confirm a retrieval defect, re-run `hub_invocation` against a current index and say which index version.
5. Group non-correct findings by target: hub-retrieval, hub-corpus, hub-tools, pipecat-docs, pipecat-lib, agent-misuse.
6. Absence of data is not absence of a problem: `text_scan` sessions and un-parsed harnesses are blind spots.
"""

ERR_NAME_RE = re.compile(r"[A-Za-z]*(?:Error|Exception)\b|Traceback")


def refuse_unsafe_outputs(out: Path) -> None:
    """Refuse an output directory whose share/ or any file this tool writes is not a plain one.

    A pre-planted symlink (``share/SCHEMA.md -> ~/.zshrc``, ``--out`` under a shared ``/tmp``) would
    be written through, and packed as a link whose target path no check ever sees. Checked before
    anything is written, by ``extract`` and (through ``pack_share``) ``repack``.
    """
    share = out / "share"
    if share.is_symlink() or (share.exists() and not share.is_dir()):
        raise ValueError(f"refusing to write: {share.name}/ is a symlink or not a directory")
    targets = [out / n for n in _LOCAL_OUTPUTS] + [share / n for n in BUNDLE_FILES]
    bad = [p.name for p in targets if p.is_symlink() or (p.exists() and not p.is_file())]
    if bad:
        raise ValueError(f"refusing to write: {bad} already exist as a symlink or non-file")


def pack_share(share: Path) -> tuple[Path, str]:
    """Deterministic share.tar.gz next to share/ (flat names, no owners, fixed mtime) + its sha256.

    Packs regular files only: anything else in the bundle refuses the pack.
    """
    import gzip
    import tarfile

    refuse_unsafe_outputs(share.parent)
    tar_path = share.parent / "share.tar.gz"
    with tar_path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tf:
            for name in BUNDLE_FILES:
                info = tf.gettarinfo(str(share / name), arcname=name)
                if not info.isreg():
                    raise ValueError(f"refusing to pack: {name} is not a regular file")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o644
                with (share / name).open("rb") as fh:
                    tf.addfile(info, fh)
    digest = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    (share.parent / "share.tar.gz.sha256").write_text(f"{digest}  share.tar.gz\n")
    return tar_path, digest


def _hub_name(name: str, is_hub: bool) -> str | None:
    """A hub follow-up's own tool name (``search_docs``), ``hub`` if it cannot be read, else None."""
    m = MCP_RE.search(name)
    return m.group(1) if m else ("hub" if is_hub else None)


def build_share_bundle(
    out: Path,
    summary: dict[str, Any],
    sessions: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    scope: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Write out/share/: a stricter, human-reviewable subset that is safe to send on.

    Kept: Context Hub tool, query/inputs, result shape (hit count, confidence, top hit ids), flags,
    latency. Dropped: session ids/file names (hashed), shell commands, file paths, and error text of
    the agent's other tool calls (only the error class name survives). Nothing is sent from here.

    Every row and the manifest go through ``sanitize`` as they are written, ``report.md`` is
    rendered from the sanitised rows, and the finished files must pass ``verify_share`` or none of
    them is kept: a sink that bypassed the egress fails the build instead of shipping.
    """
    share = out / "share"
    share.mkdir(exist_ok=True)

    key = secrets.token_bytes(16)  # per run, never written: ids cannot be dictionary-reversed

    def sid(x: str) -> str:
        return hmac.new(key, x.encode(), hashlib.sha256).hexdigest()[:12]

    share_sessions: list[dict[str, Any]] = []
    with (share / "sessions.jsonl").open("w") as fh:
        for r in sessions:
            keep = {
                k: v
                for k, v in r.items()
                if k not in {"file", "session_id", "participant", "issues", "parent_session_id"}
            }
            if r.get("parent_session_id"):
                keep["parent_session"] = sid(
                    r["parent_session_id"]
                )  # links to the parent's `session`
            keep["session"] = sid(r["session_id"])
            keep["participant"] = sid(r["participant"])
            srow = sanitize(keep)
            share_sessions.append(srow)
            fh.write(json.dumps(srow) + "\n")
    share_calls: list[dict[str, Any]] = []
    with (share / "calls.jsonl").open("w") as fh:
        for c in calls:
            keep = dict(c)
            if c["via"] != "mcp" or not isinstance(c["input"], dict):
                keep["input"] = {}  # shell line / JS batch: only hub_invocation is shared
            keep["session"] = sid(keep.pop("session_id"))
            keep["participant"] = sid(keep["participant"])
            keep["ref"] = f"{keep['participant']}/{keep['session']}#{keep['call_idx']}"
            keep["followups"] = [
                {
                    "tool": public_tool_name(w["tool"], _hub_name(w["tool"], w["hub"])),
                    "category": w["category"],
                    "hub": w["hub"],
                }
                for w in c["followups"]
            ]
            keep["downstream_errors"] = [
                {
                    "tool": public_tool_name(d["tool"], None),
                    "error": error_class(d["err"]),
                }
                for d in c["downstream_errors"]
            ]
            keep["result"] = {k: v for k, v in c["result"].items() if k != "text_head"}
            row = sanitize(keep)
            share_calls.append(row)
            fh.write(json.dumps(row) + "\n")

    analysis = analyze(
        sessions, calls, lambda c: f"{sid(c['participant'])}/{sid(c['session_id'])}#{c['call_idx']}"
    )
    manifest = sanitize(
        {
            "schema_version": SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scope": scope,
            # Row counts come from the rows written (bundle.row_counts); `hub_calls` is the
            # extraction total and also counts mentions in text-scanned sessions (no rows).
            "counts": {"hub_calls": summary["hub_calls"], **row_counts(share_calls, share_sessions)},
            "harnesses": summary["by_harness"],
            "needs_manual_read": summary["needs_manual_read"],
            "files": list(BUNDLE_FILES),
            "removed": [
                "transcript text", "shell commands", "file paths (replaced with <path>)",
                "other tools' output (error class names kept)", "session/participant ids (hashed)",
                "raw non-JSON result text",
            ],
            "redaction": "best-effort regexes for keys/tokens/home paths; queries are verbatim",
            "analysis": analysis,
            "caveats": [
                "flags are heuristics, not verdicts",
                "downstream_error is correlation only",
                "latency includes harness overhead",
            ],
        }
    )  # fmt: skip
    (share / "report.md").write_text(render_report(manifest, share_calls))
    (share / "SCHEMA.md").write_text(
        "# Context Hub session bundle: schema v" + str(SCHEMA_VERSION) + "\n\n" + FIELDS_MD
        + "\n## Flag glossary (severity weight)\n\n"
        + "\n".join(f"- `{k}` ({f.weight}): {f.glossary}" for k, f in FLAGS.items())
        + "\n"
    )  # fmt: skip
    (share / "manifest.json").write_text(json.dumps(manifest, indent=2))
    leaks = verify_share(share, BUNDLE_FILES)
    if leaks:
        for name in BUNDLE_FILES:  # fail closed: keep nothing that failed the check
            (share / name).unlink(missing_ok=True)
        raise ShareLeakError(
            f"share bundle failed the leak check and was not kept: {'; '.join(leaks)}"
        )
    load_files(_read_share(share))  # what extract writes is what ingest accepts, or the build fails
    return share, manifest


@dataclasses.dataclass
class ExtractResult:
    summary: dict[str, Any]
    share: Path
    tarball: Path
    sha256: str
    warnings: list[str]
    counts: dict[str, Any]  # the bundle manifest's counts (``bundle.has_content`` reads them)


_LOCAL_OUTPUTS = (
    "summary.json",
    "sessions.jsonl",
    "calls.jsonl",
    "share.tar.gz",
    "share.tar.gz.sha256",
)


def generated_paths(out_dir: Path) -> list[Path]:
    """Exactly the files ``run_extract`` writes into ``out_dir`` (nothing else is ever listed).

    Refuses a directory that does not look like extract output, and symlinks, so a typo'd path
    cannot delete someone's files.
    """
    if (
        not (out_dir / "summary.json").is_file()
        or not (out_dir / "share" / "manifest.json").is_file()
    ):
        raise ValueError(f"{out_dir.name or out_dir} does not look like `audit extract` output")
    if (out_dir / "share").is_symlink():  # its files would be deleted in the link's target
        raise ValueError("refusing to delete: share/ is a symlink")
    candidates = [out_dir / n for n in _LOCAL_OUTPUTS] + [
        out_dir / "share" / n for n in BUNDLE_FILES
    ]
    found = [p for p in candidates if p.exists() or p.is_symlink()]
    if any(p.is_symlink() or not p.is_file() for p in found):
        raise ValueError("refusing to delete: output contains a symlink or non-file")
    return found


def _read_share(share: Path, names: tuple[str, ...] = BUNDLE_FILES) -> dict[str, bytes]:
    return {n: (share / n).read_bytes() for n in names}


def repack(out_dir: Path) -> tuple[Path, str]:
    """Rebuild ``share.tar.gz`` from an (edited) ``out_dir/share/``.

    Every row must pass the bundle contract's row validator (``bundle.parse_rows``), the manifest's
    row counts are re-derived from the rows that remain, and ``report.md`` is regenerated from
    ``calls.jsonl`` and ``manifest.json`` rather than patched, so a call deleted from
    ``calls.jsonl`` disappears from everything derived from it. The finished files must pass the
    leak check and the whole-bundle validator ``ingest`` runs (``bundle.load_files``): a bundle
    repack accepts is one ingest accepts.
    """
    share = out_dir / "share"
    missing = [n for n in BUNDLE_FILES if not (share / n).is_file() or (share / n).is_symlink()]
    if missing:
        raise ValueError(f"cannot repack: {share.name}/ is missing {missing}")
    try:
        rows = parse_rows((share / "calls.jsonl").read_text(), "calls.jsonl")
        sessions = parse_rows((share / "sessions.jsonl").read_text(), "sessions.jsonl")
        manifest = decode_manifest((share / "manifest.json").read_text())
    except (BundleError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot repack: {exc}") from None
    manifest["counts"].update(row_counts(rows, sessions))  # gist and ingest decide with these
    if isinstance(manifest.get("analysis"), dict):
        prune_refs(manifest["analysis"], {r["ref"] for r in rows})
    (share / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (share / "report.md").write_text(render_report(manifest, rows))
    leaks = verify_share(share, BUNDLE_FILES)
    if leaks:
        raise ValueError(
            f"cannot repack: the bundle still holds a secret or local path ({'; '.join(leaks)}); "
            "remove it from the file named and run repack again"
        )
    try:
        load_files(_read_share(share))
    except BundleError as exc:
        raise ValueError(f"cannot repack: {exc}") from None
    return pack_share(share)


def decode_manifest(text: str) -> dict[str, Any]:
    """A hand-edited manifest whose row counts are about to be re-derived: version and counts object
    are checked here, the counts themselves after ``repack`` rewrites them."""
    manifest = decode_json(text, "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("counts"), dict):
        raise BundleError("manifest.json is not an object with a counts object")
    err = manifest_error({**manifest, "counts": dict.fromkeys(ROW_COUNT_KEYS, 0)})
    if err:
        raise BundleError(err)
    return manifest


def run_extract(
    inputs: Sequence[Path], out: Path, *, anonymize: bool = False, since: str | None = None
) -> ExtractResult:
    """Extract every transcript under ``inputs`` into ``out`` and build the share bundle."""
    for root in inputs:
        if not root.is_dir():
            raise ValueError(f"not a directory: {root.name or root}")
    refuse_unsafe_outputs(out)
    out.mkdir(parents=True, exist_ok=True)
    sessions: list[dict[str, Any]] = []
    all_calls: list[dict[str, Any]] = []
    warnings: list[str] = []
    aliases: dict[str, str] = {}
    out_resolved = out.resolve()
    for root in inputs:
        for f in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES):
            rel = f.relative_to(root)
            if (
                NOT_SESSION_DIRS.intersection(rel.parts[:-1])
                or f.name.endswith(NOT_SESSION_SUFFIXES)
                or out_resolved
                in f.resolve().parents  # a previous run's output, when out is inside -i
            ):
                continue
            # One sub-directory per participant when collecting. A Claude Code project directory
            # holds `<session-id>/subagents/...`: that first component is a session, not a person.
            nested = len(rel.parts) > 1 and rel.parts[1] != "subagents"
            label = rel.parts[0] if nested else root.name
            if anonymize:  # sequential aliases: nothing derived from the label survives
                label = aliases.setdefault(label, f"participant-{len(aliases) + 1}")
            try:
                s, c = process_session(f, label)
            except Exception as exc:  # one bad file must not sink the batch
                warnings.append(f"skipped {f.name}: {type(exc).__name__}")
                continue
            if s["tool_calls_total"] == 0 and s.get("fidelity") != "text_scan":
                continue
            if since:
                started = (s.get("start") or "")[:10] or datetime.fromtimestamp(
                    f.stat().st_mtime, timezone.utc
                ).strftime("%Y-%m-%d")
                if started < since:
                    continue
            sessions.append(s)
            all_calls.extend(c)

    link_parents(sessions)
    annotate_calls(all_calls)
    with (out / "sessions.jsonl").open("w") as fh:
        for s in sessions:
            fh.write(json.dumps(s) + "\n")
    with (out / "calls.jsonl").open("w") as fh:
        for row in all_calls:
            fh.write(json.dumps(row) + "\n")

    flag_totals: Counter[str] = Counter()
    tool_totals: Counter[str] = Counter()
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
    share, manifest = build_share_bundle(
        out, summary, sessions, all_calls, {"since": since, "anonymized": anonymize}
    )
    summary["analysis"] = manifest["analysis"]
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    tar_path, digest = pack_share(share)
    return ExtractResult(summary, share, tar_path, digest, warnings, manifest["counts"])
