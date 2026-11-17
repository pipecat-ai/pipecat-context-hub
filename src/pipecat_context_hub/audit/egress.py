"""The one place text becomes safe to leave the machine.

Everything a share bundle carries that came from a participant's transcript (queries, argument
names and values, hit ids, error text, version strings) goes through :func:`sanitize` at write time,
and the finished files are checked by :func:`verify_share`, which fails closed. Sanitising at each
call site that happens to touch text is how holes appeared (dict keys, the report table); a single
egress plus a tripwire means a new field or sink is covered, or the build refuses to finish.

Redaction is a denylist of known shapes and stays best-effort. This module makes it *complete over
sinks*, not over secrets: the participant still reads the report before sending anything.

Representation parity: ``sanitize`` cleans parsed values, so ``verify_share`` checks parsed values
too. A JSON file is decoded (every row, every pair, duplicates included) and each decoded string
and key is scanned; only the non-JSON files (``SCHEMA.md``, ``report.md``) are scanned as text. A
scan of serialised JSON sees escapes, not text: ``options:\\nvoice`` there looks like a Windows
path (a false failure) and ``\\u0073k-...`` hides a token (a miss).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Secrets must never reach the output files. Over-redact rather than under-redact.
# `\b` treats `_` as a word character, so `key_sk-...`, `mcp__sk-...` and `API_KEY_<hex>` have no
# boundary and would escape. A secret is delimited by anything that is not a letter or digit.
_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"
REDACTED = "<redacted>"
SECRET_RES = [
    # A whole PEM private key, header to footer, or to the end of the text when the footer was cut
    # off. First, because a later rule that eats only the header would leave the key body behind.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    ),
    re.compile(
        _B + r"(sk|pk|rk|dg|gsk|hf|xox[abprs]|ghp|gho|ghs|ghu|ghr|github_pat|glpat)"
        r"[-_][A-Za-z0-9_\-]{16,}"
    ),
    re.compile(_B + r"pypi-AgE[A-Za-z0-9_\-]{16,}"),  # PyPI API token (a macaroon: always AgE...)
    # Credentials in a URL: scheme://user:password@host (a user without a password is kept).
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/:@\"'<>]+:[^\s/@\"'<>]+@"),
    re.compile(r"(?i)" + _B + r"(bearer|token)\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\b([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*[=:]\s*\S{8,}"),
    # Bare hex keys (Deepgram 40, Twilio/AssemblyAI 32, Daily 64) and git SHAs alike: a commit
    # SHA in a query is worth nothing, a key that escapes is worth a lot.
    re.compile(_B + r"[0-9a-fA-F]{32,}" + _E),
    re.compile(_B + r"AKIA[0-9A-Z]{16}" + _E),  # AWS access key id
    re.compile(_B + r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(_B + r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
]
HOME_RE = re.compile(r"/(Users|home)/[^/\s\"']+")
WIN_HOME_RE = re.compile(r"([A-Za-z]:\\{1,2}Users\\{1,2})[^\\/\s\"']+")


SHARE_PATH_RE = re.compile(
    r"(?:/(?:Users|home|private|tmp|var|mnt|opt|root|Volumes|etc)/[^\s\"'`;|)\]]+|[A-Za-z]:\\[^\s\"'`;|)\]]+)"
)


def redact(text: str) -> str:
    text = HOME_RE.sub(r"/\1/<user>", text)
    text = WIN_HOME_RE.sub(r"\1<user>", text)
    for rx in SECRET_RES:
        text = rx.sub(REDACTED, text)
    return text


def map_strings(v: Any, fn: Callable[[str], str]) -> Any:
    """``v`` with ``fn`` applied to every string value and every string dict key, structure untouched.

    Keys matter: a hub call's argument names are chosen by the agent, so a secret or a path can sit
    in a key as easily as in a value.

    Redaction must walk the parsed value, never the serialised text: a secret pattern's ``\\S{8,}``
    swallows the closing quote and brace of a JSON dump, and the result no longer parses.
    """
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, list):
        return [map_strings(x, fn) for x in v]
    if isinstance(v, dict):
        return {(fn(k) if isinstance(k, str) else k): map_strings(x, fn) for k, x in v.items()}
    return v


def one_line(text: str) -> str:
    """``text`` on a single line under EVERY line break ``str.splitlines`` honours.

    ``repack`` finds report rows with ``splitlines()``; a row built with a narrower rule (just
    ``\\n``) splits there on ``\\r``, ``\\x0b``, ``\\u2028`` and friends, and a deleted call's
    query survives in the fragment.
    """
    return " ".join(text.splitlines())


def clip(text: str, n: int) -> str:
    text = redact(text)
    return text if len(text) <= n else text[:n] + f"…[+{len(text) - n}]"


# Names of the participant's other tools can identify their setup (an internal MCP server) and are
# text no pattern should be trusted to cover, so the bundle carries an allowlist, not the name.
STANDARD_TOOLS = frozenset(
    {
        # Claude Code
        "Bash", "Read", "Grep", "Glob", "LS", "Edit", "MultiEdit", "Write", "NotebookEdit",
        "WebFetch", "WebSearch", "ToolSearch", "Task", "Agent", "TodoWrite",
        # pi, Codex and others (exact spellings: no case folding)
        "bash", "shell", "exec", "exec_command", "run_shell_command", "local_shell", "terminal",
        "read", "grep", "glob", "ls", "find", "edit", "write", "apply_patch", "update_plan",
        "view_image", "write_stdin",
    }
)  # fmt: skip


def public_tool_name(name: str, hub_tool: str | None) -> str:
    """The hub tool's own name, a standard harness tool's name, or ``other``; never anything else."""
    if hub_tool:
        return hub_tool
    return name if name in STANDARD_TOOLS else "other"


_MAX_CLEAN_PASSES = 8


def clean_text(text: str) -> str:
    """Redact secrets and home paths, then replace local absolute paths, until nothing changes.

    One pass is not a fixpoint: a later rule's ``<redacted>`` can complete an earlier rule's match
    (``KEY=`` then a hex key). Repeating until stable makes the result idempotent and free of every
    shape the tripwire looks for, so ``sanitize`` succeeding implies ``verify_share`` passes. A text
    that never settles is replaced whole (fail closed).
    """
    for _ in range(_MAX_CLEAN_PASSES):
        cleaned = SHARE_PATH_RE.sub("<path>", redact(text))
        if cleaned == text:
            return text
        text = cleaned
    return REDACTED


# A field whose NAME says it holds a credential has its value replaced whole, whatever the value
# looks like: no value pattern can recognise an arbitrary password, and a hub call's arguments are
# agent-chosen (``{"api_key": "..."}``). The rule: drop every character that is not an ASCII letter
# or digit, then the name must END in one of these words, any case. So `api_key`, `apiKey`,
# `X-Api-Key`, `OPENAI_API_KEY`, `client_secret`, `access_token`, `db.password` are credentials;
# `max_tokens`, `token_count`, `secret_santa`, `keys` are not. Over-redacting a pagination
# `next_token` is the accepted cost.
_CREDENTIAL_WORDS = (
    "apikey", "token", "secret", "password", "passwd", "pwd", "passphrase", "authorization",
    "bearer", "accesskey", "privatekey", "secretkey", "credential", "credentials", "cookie",
)  # fmt: skip
_CREDENTIAL_NAME_RE = re.compile(r"(?i)[a-z0-9]*(?:" + "|".join(_CREDENTIAL_WORDS) + r")")
_NOT_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


def is_credential_name(name: str) -> bool:
    """True when ``name`` (a dict key) names a credential under the rule above."""
    return bool(_CREDENTIAL_NAME_RE.fullmatch(_NOT_ALNUM_RE.sub("", name)))


def sanitize(v: Any) -> Any:
    """``v`` with every string (and dict key) cleaned: the egress for every share JSON row.

    The value of a credential-named key is replaced by ``REDACTED`` whole (``None`` stays ``None``),
    judged on the key as written and as cleaned, so ``verify_share`` (which sees the cleaned key)
    agrees.
    """
    if isinstance(v, str):
        return clean_text(v)
    if isinstance(v, list):
        return [sanitize(x) for x in v]
    if isinstance(v, dict):
        out: dict[Any, Any] = {}
        for k, x in v.items():
            if not isinstance(k, str):
                out[k] = sanitize(x)
                continue
            ck = clean_text(k)
            credential = is_credential_name(k) or is_credential_name(ck)
            out[ck] = REDACTED if credential and x is not None else sanitize(x)
        return out
    return v


# What the tripwire looks for in a finished file. These are the shapes `clean_text` removes, so on
# sanitised output they must find nothing; a hit means a sink bypassed the egress. A home path that
# has already been rewritten to `<user>` is clean.
_LEAK_RES: dict[str, re.Pattern[str]] = {
    **{f"secret pattern {i}": rx for i, rx in enumerate(SECRET_RES, 1)},
    "home path": re.compile(r"/(?:Users|home)/(?!<user>)[^/\s\"']+"),
    "windows home path": re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}(?!<user>)[^\\/\s\"']+"),
    "local absolute path": SHARE_PATH_RE,
}


class ShareLeakError(ValueError):
    """A finished share file still holds something `sanitize` should have removed."""


def leaks_in(text: str) -> list[str]:
    """Names of the leak rules ``text`` trips. Never returns the matched text itself."""
    return [name for name, rx in _LEAK_RES.items() if rx.search(text)]


class _Pairs(list[tuple[Any, Any]]):
    """A decoded JSON object as all of its (key, value) pairs: a duplicate key is still scanned.

    ``json.loads`` keeps only the last of two equal keys, but a person reading the gist sees both.
    """


def _decoded_leaks(value: Any) -> list[str]:
    """Rules tripped by a decoded JSON value: every string and key, and every credential-named key.

    The credential check is structural and independent of the patterns: a value under a
    credential-named key must be ``REDACTED`` or null, whatever it looks like.
    """
    rules: list[str] = []
    stack = [value]
    while stack:  # iterative: a deeply nested hand edit must not crash the check
        v = stack.pop()
        if isinstance(v, str):
            rules.extend(leaks_in(v))
        elif isinstance(v, _Pairs):
            for k, x in v:
                if isinstance(k, str):
                    rules.extend(leaks_in(k))
                    if is_credential_name(k) and x is not None and x != REDACTED:
                        rules.append("credential field")
                stack.append(x)
        elif isinstance(v, list):
            stack.extend(v)
    return rules


def _file_leaks(name: str, text: str) -> list[str]:
    if not name.endswith((".json", ".jsonl")):
        return leaks_in(text)
    jsonl = name.endswith(".jsonl")
    docs = text.splitlines() if jsonl else [text]
    rules: list[str] = []
    for i, doc in enumerate(docs, 1):
        if not doc.strip():
            continue
        try:
            value = json.loads(doc, object_pairs_hook=_Pairs)
        except (ValueError, RecursionError):  # cannot be checked decoded: refuse, never skip
            rules.append(f"line {i} is not valid JSON" if jsonl else "not valid JSON")
            continue
        rules.extend(_decoded_leaks(value))
    return rules


def verify_share(share: Path, names: tuple[str, ...]) -> list[str]:
    """``["file: rule", ...]`` for every bundle file that trips a leak rule; empty when clean.

    JSON and JSONL files are checked decoded (see the module docstring), every other file as text.
    """
    found: list[str] = []
    for n in names:
        f = share / n
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        found.extend(f"{n}: {rule}" for rule in dict.fromkeys(_file_leaks(n, text)))
    return found
