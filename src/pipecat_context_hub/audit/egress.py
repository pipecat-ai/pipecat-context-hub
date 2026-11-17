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
and key is scanned, and every number through its canonical text (the number rule); the markdown
files (``SCHEMA.md``, ``report.md``) are scanned as the text a reader sees, with the one table-cell
escape undone. A scan of serialised JSON sees escapes, not text: ``options:\\nvoice`` there looks
like a Windows path (a false failure) and ``\\u0073k-...`` hides a token (a miss).

Closure. Three properties hold, and ``tests/unit/test_audit_sessions.py`` checks each over
generated values: whatever ``sanitize`` emits passes the tripwire; ``report.md`` rendered from
rows that pass the tripwire passes it too (the renderer only joins row values with whitespace-
bounded separators, :func:`value_text` and :func:`md_cell`); and a labelled credential
(``password: ...``, ``--api-key ...``, ``Authorization: Basic ...``, URL userinfo) is removed by
``sanitize`` and refused by the tripwire in a hand edit.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

# The item separator of every text a text sink renders (lists, line breaks; see `value_text`).
_ITEM_SEP = " · "

# Secrets must never reach the output files. Over-redact rather than under-redact.
# `\b` treats `_` as a word character, so `key_sk-...`, `mcp__sk-...` and `API_KEY_<hex>` have no
# boundary and would escape. A secret is delimited by anything that is not a letter or digit.
_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"
REDACTED = "<redacted>"

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


class _Rule(Protocol):
    """A redaction rule: a compiled pattern, or one that also checks what it matched."""

    def search(self, string: str, /) -> object: ...

    def sub(self, repl: str, string: str, /) -> str: ...


class _LabelledCredential:
    """A value written after a label that names a credential, in free text.

    The label must pass :func:`is_credential_name`, the same rule a dict key does, so
    ``"password": "..."``, ``--api-key ...``, ``X-Api-Key=...`` and ``Authorization: Basic ...`` are
    caught while ``password_hint: ...``, ``--password-file ...`` and ``max_tokens: ...`` are not.
    The whole match (label and value) is replaced. A value is a quoted string, or 4+ non-space
    characters (an optional auth scheme first); an already redacted value is not a value, so the
    result is stable.
    """

    def __init__(self, rx: re.Pattern[str]) -> None:
        self._rx = rx

    def _hits(self, text: str) -> list[re.Match[str]]:
        found: list[re.Match[str]] = []
        pos = 0
        while (m := self._rx.search(text, pos)) is not None:
            if is_credential_name(m.group("name")):
                found.append(m)
                pos = m.end()
            else:  # a rejected label must not hide a credential label inside its value
                pos = m.start() + 1
        return found

    def search(self, string: str, /) -> object:
        hits = self._hits(string)
        return hits[0] if hits else None

    def sub(self, repl: str, string: str, /) -> str:
        out, last = [], 0
        for m in self._hits(string):
            out += [string[last : m.start()], repl]
            last = m.end()
        return "".join(out) + string[last:]


# One line only (`[ \t]`, never `\s`), and a quoted value never runs across ` | `, ` · ` or ` → `:
# those are the separators the report renders between values, so no match can span two values.
_LABEL_QUOTED = r"""(?:"(?:(?! [|·→] )[^"\n])+"|'(?:(?! [|·→] )[^'\n])+')"""
_LABEL_VALUE = (
    r"""(?!["']?<redacted>)(?:(?:basic|bearer|digest|token)[ \t]+(?!["']?<redacted>))?"""
    r"(?:" + _LABEL_QUOTED + r"|\S{4,})"
)
_LABEL_START = r"(?<![A-Za-z0-9_.\-])"
_LABEL_NAME = r"(?P<name>[A-Za-z][A-Za-z0-9_.\-]*)"

SECRET_RES: list[_Rule] = [
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
    # Credentials in a URL: scheme://user:password@host (a user without a password is kept). The
    # password runs to the LAST `@` before the host, so an `@` inside it is not left behind.
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/:@\"'<>]+:[^\s/\"'<>]+@"),
    re.compile(r"(?i)" + _B + r"(bearer|token)\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\b([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*[=:]\s*\S{8,}"),
    # Bare hex keys (Deepgram 40, Twilio/AssemblyAI 32, Daily 64) and git SHAs alike: a commit
    # SHA in a query is worth nothing, a key that escapes is worth a lot.
    re.compile(_B + r"[0-9a-fA-F]{32,}" + _E),
    re.compile(_B + r"AKIA[0-9A-Z]{16}" + _E),  # AWS access key id
    re.compile(_B + r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(_B + r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    # Labelled credentials in free text: `name: value`, `"name": "value"`, `name=value`
    _LabelledCredential(
        re.compile(
            r"(?i)"
            + _LABEL_START
            + r"""(?P<q>["']?)"""
            + _LABEL_NAME
            + r"(?P=q)[ \t]*[=:][ \t]*"
            + _LABEL_VALUE
        )
    ),
    # ... and as a command-line flag: `--name value`, `--name=value`
    _LabelledCredential(
        re.compile(r"(?i)" + _LABEL_START + r"--?" + _LABEL_NAME + r"(?:=|[ \t]+)" + _LABEL_VALUE)
    ),
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

    Each break becomes the item separator `` · `` (see :func:`value_text`), never a plain space: a
    labelled-credential rule reads one line at a time, so ``--password`` and a value on the next
    line are not a match, and joining them with a space would make one the row never had. A
    separator no rule matches across keeps every match inside one of the original lines.
    """
    return _ITEM_SEP.join(text.splitlines())


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


def _number_text(v: float) -> str | None:
    """A number's canonical text (its JSON form), or None when it has none (an int past the
    interpreter's digit limit)."""
    try:
        return json.dumps(v)
    except ValueError:
        return None


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _number_is_clean(v: float) -> bool:
    """The number rule: a number is shared as itself only when its canonical text trips no rule
    (in practice: fewer than 32 digits, so it cannot be a bare hex key)."""
    text = _number_text(v)
    return text is not None and clean_text(text) == text


def sanitize(v: Any) -> Any:
    """``v`` with every string (and dict key) cleaned: the egress for every share JSON row.

    The value of a credential-named key is replaced by ``REDACTED`` whole (``None`` stays ``None``),
    judged on the key as written and as cleaned, so ``verify_share`` (which sees the cleaned key)
    agrees. A number whose canonical text (:func:`value_text`) would trip a rule becomes
    ``REDACTED``; every other number is kept as it is.
    """
    if isinstance(v, str):
        return clean_text(v)
    if _is_number(v):
        return v if _number_is_clean(v) else REDACTED
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


# ---- One canonical text form for every value a text sink (``report.md``) renders.
#
# A string is itself; null is empty; a boolean or number is its JSON literal; a list or object is
# its parts joined with separators that no rule can match across (each is whitespace, a symbol no
# rule takes as a label, value or path character, whitespace). So a rule match in the text of a
# value lies inside one of its strings, keys or numbers: if those pass the tripwire, so does the
# text. Python's ``str()`` / ``repr`` is never used: its ``\n`` escapes read as paths (``s:\n``).
_PAIR_SEP = " → "


def value_text(v: Any) -> str:
    """The canonical text of a decoded JSON value (see above)."""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if _is_number(v):
        return _number_text(v) or REDACTED
    if isinstance(v, list | tuple):
        return _ITEM_SEP.join(value_text(x) for x in v)
    if isinstance(v, dict):
        return _ITEM_SEP.join(f"{value_text(k)}{_PAIR_SEP}{value_text(x)}" for k, x in v.items())
    return ""


# ---- Markdown table cells. The one escape the renderer applies is ``|`` to ``\|``; the tripwire
# reads ``.md`` files with exactly that escape undone (the text a reader sees), so escaping can
# never create a match the value did not have (``TOKEN=a|b|c|d`` stays 7 characters long).
_MD_PIPE = "\\|"


def md_cell(text: str) -> str:
    """A table cell for ``text``: one line, ``|`` escaped. No redaction (see :func:`md_decoded`)."""
    return one_line(text).replace("|", _MD_PIPE)


def md_decoded(text: str) -> str:
    """``text`` (a markdown file) with the cell escape undone: ``md_decoded(md_cell(t)) ==
    one_line(t)`` for every ``t``."""
    return text.replace(_MD_PIPE, "|")


# ---- A hub CLI call's shared text.
_ARGV_WORD_CHARS = 200


def argv_text(argv: list[str]) -> str:
    """``argv`` as the one inert command a bundle may carry, or ``""``.

    The value after a credential-named flag (``--api-key X``, ``--password=X``) is replaced by
    structure, every word is cleaned and bounded on its own, and the words are re-quoted with
    ``shlex.join``. If cleaning the joined text would still change it, a later rewrite could break
    the quoting (an unquoted ``<redacted>`` is a redirection), so nothing is shared (fail closed).
    """
    words: list[str] = []
    redact_next = False
    for w in argv:
        if redact_next:
            w, redact_next = REDACTED, False
        elif w.startswith("-"):
            name, eq, _ = w.lstrip("-").partition("=")
            if is_credential_name(name):
                if eq:
                    w = w.split("=", 1)[0] + "=" + REDACTED
                else:
                    redact_next = True
        w = clean_text(w)
        if len(w) > _ARGV_WORD_CHARS:
            w = w[:_ARGV_WORD_CHARS] + "…"
        words.append(w)
    text = shlex.join(words)
    return text if clean_text(text) == text else ""


# What the tripwire looks for in a finished file. These are the shapes `clean_text` removes, so on
# sanitised output they must find nothing; a hit means a sink bypassed the egress. A home path that
# has already been rewritten to `<user>` is clean.
_LEAK_RES: dict[str, _Rule] = {
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
    """Rules tripped by a decoded JSON value: every string and key, every number's canonical text
    (the number rule ``sanitize`` applies), and every credential-named key.

    The credential check is structural and independent of the patterns: a value under a
    credential-named key must be ``REDACTED`` or null, whatever it looks like.
    """
    rules: list[str] = []
    stack = [value]
    while stack:  # iterative: a deeply nested hand edit must not crash the check
        v = stack.pop()
        if isinstance(v, str):
            rules.extend(leaks_in(v))
        elif _is_number(v):
            text = _number_text(v)
            rules.extend(leaks_in(text) if text is not None else ["number without a text form"])
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
    if name.endswith(".md"):
        return leaks_in(md_decoded(text))
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

    JSON and JSONL files are checked decoded (see the module docstring), a markdown file as the
    text its reader sees (the table-cell escape undone, :func:`md_decoded`), anything else as text.
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
