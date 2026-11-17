"""The one place text becomes safe to leave the machine.

Everything a share bundle carries that came from a participant's transcript (queries, argument
names and values, hit ids, error text, version strings) goes through :func:`sanitize` at write time,
and the finished files are checked by :func:`verify_files`, which fails closed. Sanitising at each
call site that happens to touch text is how holes appeared (dict keys, the report table); a single
egress plus a tripwire means a new field or sink is covered, or the build refuses to finish.

Redaction is a denylist of known shapes and stays best-effort. This module makes it *complete over
sinks*, not over secrets: the participant still reads the report before sending anything.

Representation parity: ``sanitize`` cleans parsed values, so ``verify_files`` checks parsed values
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
import string
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, Protocol

# The item separator of every text a text sink renders (lists, line breaks; see `value_text`).
_ITEM_SEP = " · "

# Secrets must never reach the output files. Over-redact rather than under-redact.
# `\b` treats `_` as a word character, so `key_sk-...`, `mcp__sk-...` and `API_KEY_<hex>` have no
# boundary and would escape. A secret is delimited by anything that is not a letter or digit.
_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"
REDACTED = "<redacted>"

# ---- The label model: a credential is recognised by the LABEL in front of it, never by a list of
# label shapes. No value pattern can recognise an arbitrary password, and a hub call's arguments are
# agent-chosen (``{"api_key": "..."}``), so the label decides. One recogniser (:func:`is_credential_
# name`) serves a dict key, a command-line flag and a label in free text; one value model
# (:func:`_value_span`) decides what a label's value is; ``sanitize`` and the tripwire each locate
# labels with their own code over the same grammar (below), so a weakened locator on one side is
# caught by the other.
#
# Rule 1, the recogniser, over the label's word SEGMENTS: NFKC (fullwidth letters fold), split at
# every non-letter (`_ - . `, spaces, digits) and at camel-case humps, ASCII-lowered by a table.
#   1a. A segment that STARTS with a different word sharing a stem's letters
#       (`NON_CREDENTIAL_SEGMENTS`: `tokenizer`, `secretary`) has that prefix blanked, wherever the
#       segment sits: blanking a non-credential word cannot hide a stem in another segment, and the
#       rest of the segment is still read (`TOKENIZERSECRET` is a credential).
#   1b. A label that is exactly one of `BENIGN_WHOLE_LABELS` (`tokens`, `keys`) is not a credential.
#   1c. When the label's TRAILING segments spell a benign phrase (`BENIGN_LABEL_WORDS`: a count, a
#       hint, a file or a policy about a credential: `max_tokens`, `token_count`, `tokens_used`,
#       `password_hint`, `password_file`, `secret_santa`), those segments are dropped. A benign
#       phrase anywhere else in the label is not dropped, and `tokens` alone is never a benign
#       phrase: `API_TOKENS` and `MY_TOKENS` are plurals of credential labels.
#   1d. What is left names a credential when its letters, joined, CONTAIN a credential stem
#       (`password1`, `api_key_prod`, `OPENAI_API_KEY_2`, `secret_key_base`, `X-Api-Key`,
#       `db.password`, `API key`, `SECRETS`, `PASSWORDS`), or when its last segment is `key` or
#       `keys` (`STRIPE_KEY`, `SSH_KEYS`, a bare `key`): an env-style key the stems do not cover.
#       `MONKEY` (no `key` segment), `KeyError` and `key_id` (`key` not last) are not.
# Known cost: a value under a label that merely contains a stem (`next_token`, `authorization_url`,
# `primary_key`, `cookie_policy`, `api_tokens_used`) is redacted too; over-redacting is the safe
# direction. Invariant (tested): every label the earlier `KEY|TOKEN|SECRET|PASSWORD`-substring rule
# redacted, whose stem is a whole segment, is still redacted, except the benign phrases and whole
# labels above and a label whose `key` segment is not its last.
#
# Rule 2, the separator. In text a label is one word (a maximal run of word characters, `.` and
# `-`, except the `-` of `->`), optionally preceded by one more word and a single space or tab (`API key`), optionally
# followed by a quote (`"`, `'`, each possibly backslash-escaped: stringified JSON), then spaces
# and one of `:` `=` `=>` `->` and their fullwidth forms. A flag (a word starting with `-`) may
# also be followed by spaces alone (`--password x`).
#
# Rule 3, the value (:func:`_value_span`). A quoted value runs to the matching closing quote: the
# same quote character after exactly as many backslashes, so `\"` inside `"..."` and `\\\"` inside
# `\"...\"` are not the end. An unterminated quote is a value to the end of the text. A flag's
# unquoted value is one word. Any other unquoted value runs to the end of its segment: a line break,
# a sentence end (`.` then whitespace), two blanks, or a separator the report renders between
# values (` | `, ` · `, ` → `), so a passphrase with spaces loses its tail too; a value with no
# blank in it goes whole, whatever punctuation it holds. Two terminators depend on the context
# before the label: `&` (and `#`, a blank) ends the value of a URL-query key (a label right after
# `?` or `&`, separated by `=`: `a=b&password=x&c=d` loses exactly `x`); `)` `]` `}` end it when
# they close a bracket opened before the label, and `,` `;` when any bracket opened before the
# label is still open (`(password: abc)`, `{"password": abc, ...}`). Elsewhere they are part of the
# value (`password: abc&def123`, `pwd: ab,cd1234`). There is no minimum length: any non-empty value
# is redacted except the redaction marker and a placeholder (:func:`is_placeholder`).
#
# Rule 4, URL userinfo: `scheme://user:pass@host` and `scheme://:pass@host` are credentials;
# `scheme://user@host` is kept (a value-shape rule in ``SECRET_RES``).
_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
CREDENTIAL_STEMS = (
    "password", "passwd", "passphrase", "pwd", "secret", "token", "apikey", "accesskey",
    "privatekey", "secretkey", "credential", "authorization", "bearer", "cookie",
)  # fmt: skip
# Rule 1c: phrases (letters of whole trailing segments, joined) that qualify a stem into something
# else: an LLM token count, a hint, file or policy about a credential, a gift exchange.
BENIGN_LABEL_WORDS = (
    "maxtokens", "mintokens", "numtokens", "totaltokens", "prompttokens", "completiontokens",
    "inputtokens", "outputtokens", "cachedtokens", "reasoningtokens", "tokencount",
    "tokenscount", "tokenlimit", "tokenbudget", "tokensused", "tokensremaining", "tokenusage",
    "passwordhint", "passwordfile", "passwordpolicy", "secretsanta",
)  # fmt: skip
# Rule 1a: segment prefixes of other words that share a stem's letters.
NON_CREDENTIAL_SEGMENTS = ("tokeniz", "secretar")
# Rule 1b: a label that is exactly this one segment is not a credential (a count, a keyboard term).
BENIGN_WHOLE_LABELS = ("tokens", "keys")
_KEY_SEGMENTS = ("key", "keys")
_SEGMENT_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")


def label_segments(name: str) -> list[str]:
    """``name``'s word segments for recognition (rule 1): NFKC, split at non-letters and camel-case
    humps, ASCII-lowered, letters a-z only."""
    norm = unicodedata.normalize("NFKC", name)
    return [w.translate(_FOLD) for w in _SEGMENT_RE.findall(norm) if w.isalpha()]


def _blank_non_credential(seg: str) -> str:
    for word in NON_CREDENTIAL_SEGMENTS:
        if seg.startswith(word):
            return "|" + seg[len(word) :]
    return seg


def is_credential_name(name: str) -> bool:
    """True when ``name`` (a dict key, a flag or a label in text) names a credential (rule 1)."""
    segs = [_blank_non_credential(s) for s in label_segments(name)]
    if len(segs) == 1 and segs[0] in BENIGN_WHOLE_LABELS:
        return False
    for i in range(len(segs)):
        if "".join(segs[i:]) in BENIGN_LABEL_WORDS:
            segs = segs[:i]
            break
    letters = "".join(segs)
    return any(stem in letters for stem in CREDENTIAL_STEMS) or (
        bool(segs) and segs[-1] in _KEY_SEGMENTS
    )


# ---- Placeholders (rule 3's exemption): text that stands for a credential without being one. The
# one rule, shared by both locators:
#   * an ellipsis or mask: `..`, `…`, `xxx`, `***` (any length);
#   * `$VAR` or `${VAR}` with VAR upper-case letters, digits and `_`, under 33 characters;
#   * `<name>` with name lower-case words (`a-z`, digits after the first letter) joined by `_` `-`;
#   * `YOUR_...` / `your-...`: `YOUR` or `your`, then words each all upper- or all lower-case;
# and in every form the name holds no secret shape (``SECRET_RES``), does not start with a token
# prefix followed by 8 more characters (`AKIA...`, `ghp_...`, `sk-...`), and has no run of 12+
# letters and digits mixing both. Anything else after a credential label is a value.
_ELLIPSIS_RE = re.compile(r"\.{2,}|…+|[xX*]{3,}")
_PLACEHOLDER_NAME_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Z_][A-Z0-9_]{0,31})\}|(?P<var>[A-Z_][A-Z0-9_]{0,31}))"
    r"|<(?P<angle>[a-z][a-z0-9]*(?:[_\-][a-z][a-z0-9]*)*)>"
    r"|(?P<your>(?:YOUR|your)(?:[_\-](?:[A-Z0-9]+|[a-z0-9]+))+)"
)
# The token kinds of ``SECRET_RES``'s prefixed-token rule, shared so the two cannot drift.
_PREFIX_KINDS = r"sk|pk|rk|dg|gsk|hf|xox[abprs]|ghp|gho|ghs|ghu|ghr|github_pat|glpat"
_TOKEN_PREFIX_RE = re.compile(
    r"(?:(?:" + _PREFIX_KINDS + r")[-_]|pypi-|AKIA|AIza|eyJ)[A-Za-z0-9_\-]{8}"
)
_MIXED_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{12,}(?![A-Za-z0-9])"
)


def is_placeholder(value: str) -> bool:
    """True when ``value`` stands for a credential rather than being one (the rule above)."""
    if _ELLIPSIS_RE.fullmatch(value):
        return True
    m = _PLACEHOLDER_NAME_RE.fullmatch(value)
    if m is None:
        return False
    name = next(g for g in m.groups() if g is not None)
    return not (
        _TOKEN_PREFIX_RE.match(name)
        or _MIXED_RUN_RE.search(name)
        or any(rx.search(name) for rx in SECRET_RES)
    )


_QUOTE_TOKEN_RE = re.compile(r"\\*[\"']")
_RENDER_SEPS = frozenset("|·→")
_SPACE_RE = re.compile(r"\s")
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _open_brackets(text: str, end: int) -> list[str]:
    """The brackets opened in ``text[:end]`` and still open there (a closer pops only the bracket
    it matches on top; any other closer is ignored)."""
    stack: list[str] = []
    for ch in text[:end]:
        if ch in "([{":
            stack.append(ch)
        elif ch in _CLOSERS and stack and stack[-1] == _CLOSERS[ch]:
            stack.pop()
    return stack


def _is_query_key(text: str, label_start: int, sep: str) -> bool:
    """A URL-query key: the label follows `?` or `&` and is separated from its value by `=`."""
    return sep == "=" and label_start > 0 and text[label_start - 1] in "?&"


def _bare_end(text: str, pos: int, opened: list[str], query: bool) -> int:
    """Where an unquoted, non-flag value starting at ``pos`` ends (rule 3), trailing blanks off."""
    n, i, inner = len(text), pos, []
    while i < n:
        ch = text[i]
        if ch in "\r\n":
            break
        end_follows = i + 1 == n or text[i + 1].isspace()
        if ch in " \t":
            if query or (i + 1 < n and text[i + 1] in " \t"):  # two blanks
                break
        elif ch in _RENDER_SEPS and i > 0 and text[i - 1] in " \t" and end_follows:
            break  # a separator the report renders between values: ` | ` ` · ` ` → `
        elif (ch == "." and end_follows) or (query and ch in "&#"):
            break
        elif ch in "([{":
            inner.append(ch)
        elif ch in _CLOSERS:
            if inner and inner[-1] == _CLOSERS[ch]:
                inner.pop()
            elif not inner and opened and opened[-1] == _CLOSERS[ch]:
                break
        elif ch in ",;" and not inner and opened:
            break
        i += 1
    while i > pos and text[i - 1] in " \t":
        i -= 1
    return i


def _closing_quote(text: str, start: int, token: str) -> int | None:
    """Index of the quote closing a value opened by ``token`` (``k`` backslashes and a quote) whose
    content starts at ``start``: the same quote character after exactly ``k`` backslashes."""
    quote, k = token[-1], len(token) - 1
    pos = start
    while (pos := text.find(quote, pos)) >= 0:
        run = 0
        while pos - run - 1 >= start and text[pos - run - 1] == "\\":
            run += 1
        if run == k:
            return pos - k
        pos += 1
    return None


def _value_span(
    text: str, pos: int, word: bool, label_start: int, sep: str
) -> tuple[int, int, str] | None:
    """The value of a label starting at ``label_start`` whose separator ``sep`` (empty for a flag
    followed by blanks) ends, blanks skipped, at ``pos`` (rule 3): ``(start, end, kind)`` or None
    when there is none. ``kind`` is ``quoted`` (the span is the content between the quotes),
    ``open`` (an unterminated quote: the span starts at the quote) or ``bare``."""
    if m := _QUOTE_TOKEN_RE.match(text, pos):
        close = _closing_quote(text, m.end(), m.group(0))
        if close is None:
            return pos, len(text), "open"
        return m.end(), close, "quoted"
    if word:
        m = _SPACE_RE.search(text, pos)
        end = m.start() if m else len(text)
        if text[pos:end] in _RENDER_SEPS:
            end = pos
    else:
        query = _is_query_key(text, label_start, sep)
        end = _bare_end(text, pos, _open_brackets(text, label_start), query)
    return (pos, end, "bare") if end > pos else None


def _is_secret_value(text: str, span: tuple[int, int, str]) -> bool:
    """False for the marker, a placeholder and an empty value; an unterminated quote is always a
    value (closed in a later cell of the report it could take in another value's text)."""
    start, end, kind = span
    if kind == "open":
        return True
    value = text[start:end]
    if kind == "bare":  # quotes, backslashes and a cut mark (`…`) around a bare value are not it
        value = value.strip(" \t\"'\\…")
    return not (value in ("", REDACTED) or is_placeholder(value))


# Locator A (the sanitizer's): the grammar of rule 2 as two patterns.
# A label word: letters, digits, `_`, `.` and `-`, except the `-` of a `->` separator.
_WC = r"(?:[\w.]|-(?!>))"
_W = _WC + r"+(?!" + _WC + r")"
_SEP = r"(?:=>|->|[:=：﹕＝])"
_SEP_LABEL_RE = re.compile(
    r"(?<![\w.\-])(?:(?P<w0>" + _W + r")[ \t])?(?P<w1>" + _W + r")(?:\\*[\"'])?[ \t]*(?P<sep>"
    + _SEP + r")[ \t]*"
)  # fmt: skip
_FLAG_LABEL_RE = re.compile(
    r"(?<![\w.\-])(?P<flag>-"
    + _WC
    + r"*(?!"
    + _WC
    + r"))(?:\\*[\"'])?[ \t]+(?![ \t]|"
    + _SEP
    + r")"
)


def _label_spans(text: str) -> list[tuple[int, int, str]]:
    """Every labelled value in ``text`` (locator A, rules 1-3), as value spans."""
    spans: list[tuple[int, int, str]] = []
    pos = 0
    while (m := _SEP_LABEL_RE.search(text, pos)) is not None:
        names = [m.group("w1")] + ([m.group(0)[: m.end("w1") - m.start()]] if m.group("w0") else [])
        if any(is_credential_name(n) for n in names):
            w1 = m.group("w1")
            span = _value_span(text, m.end(), w1.startswith("-"), m.start("w1"), m.group("sep"))
            if span is not None and _is_secret_value(text, span):
                spans.append(span)
        pos = m.end()
    for m in _FLAG_LABEL_RE.finditer(text):
        if is_credential_name(m.group("flag")):
            span = _value_span(text, m.end(), True, m.start("flag"), "")
            if span is not None and _is_secret_value(text, span):
                spans.append(span)
    return spans


def redact_labelled(text: str) -> str:
    """``text`` with every labelled credential value replaced by ``REDACTED`` (the label and any
    quotes around a closed value are kept; an unterminated quote goes with its value)."""
    spans = sorted(_label_spans(text))
    if not spans:
        return text
    merged: list[list[int]] = []
    for start, end, _ in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, last = [], 0
    for start, end in merged:
        out += [text[last:start], REDACTED]
        last = end
    return "".join(out) + text[last:]


# Locator B (the tripwire's): the same grammar walked character by character from each separator
# and each flag, sharing only the recogniser and the value model with locator A.
_WORD_CHAR_RE = re.compile(r"[\w.\-]")
_SEP_CHARS = ":=：﹕＝"


def _is_word_char(text: str, i: int) -> bool:
    """A label character (``_WC``): the `-` of a `->` separator is not one."""
    if not (0 <= i < len(text) and _WORD_CHAR_RE.match(text[i])):
        return False
    return not text.startswith("->", i)


def _word_start(text: str, end: int) -> int:
    start = end
    while _is_word_char(text, start - 1):
        start -= 1
    return start


def _sep_at(text: str, i: int) -> int:
    """Length of the separator at ``i``, or 0."""
    if text.startswith(("=>", "->"), i):
        return 2
    return 1 if i < len(text) and text[i] in _SEP_CHARS else 0


def _skip_blanks(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def labelled_value_leaks(text: str) -> bool:
    """True when ``text`` holds a credential label followed by a value (locator B, rules 1-3)."""
    n = len(text)
    for i in range(n):
        if sep := _sep_at(text, i):
            j = i
            while j > 0 and text[j - 1] in " \t":
                j -= 1
            if j > 0 and text[j - 1] in "\"'":
                j -= 1
                while j > 0 and text[j - 1] == "\\":
                    j -= 1
            if _is_word_char(text, j) or (start := _word_start(text, j)) == j:
                continue
            names = [text[start:j]]
            if start >= 2 and text[start - 1] in " \t" and _is_word_char(text, start - 2):
                names.append(text[_word_start(text, start - 1) : j])
            if any(is_credential_name(w) for w in names):
                v = _skip_blanks(text, i + sep)
                span = _value_span(text, v, names[0].startswith("-"), start, text[i : i + sep])
                if span is not None and _is_secret_value(text, span):
                    return True
        if text[i] == "-" and not _is_word_char(text, i - 1):
            end = i
            while _is_word_char(text, end):
                end += 1
            k = end
            while k < n and text[k] == "\\":
                k += 1
            k = k + 1 if k < n and text[k] in "\"'" else end
            v = _skip_blanks(text, k)
            if v == k or _sep_at(text, v):
                continue
            if is_credential_name(text[i:end]):
                span = _value_span(text, v, True, i, "")
                if span is not None and _is_secret_value(text, span):
                    return True
    return False


class _Rule(Protocol):
    """A leak rule: a compiled pattern, or anything with a ``search`` that finds a hit."""

    def search(self, string: str, /) -> object: ...


class _LabelRule:
    """The tripwire's label check (locator B) as a leak rule."""

    def search(self, string: str, /) -> object:
        return True if labelled_value_leaks(string) else None


SECRET_RES: list[re.Pattern[str]] = [
    # A whole PEM private key, header to footer, or to the end of the text when the footer was cut
    # off. First, because a later rule that eats only the header would leave the key body behind.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    ),
    re.compile(_B + r"(" + _PREFIX_KINDS + r")[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(_B + r"pypi-AgE[A-Za-z0-9_\-]{16,}"),  # PyPI API token (a macaroon: always AgE...)
    # Credentials in a URL (rule 4): scheme://user:password@host, scheme://:password@host (an
    # empty user: the Redis shape); a user without a password is kept. The password runs to the
    # LAST `@` before the host, so an `@` inside it is not left behind.
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/:@\"'<>]*:[^\s/\"'<>]+@"),
    re.compile(r"(?i)" + _B + r"(bearer|token)\s+[A-Za-z0-9._\-]{20,}"),
    # Bare hex keys (Deepgram 40, Twilio/AssemblyAI 32, Daily 64) and git SHAs alike: a commit
    # SHA in a query is worth nothing, a key that escapes is worth a lot.
    re.compile(_B + r"[0-9a-fA-F]{32,}" + _E),
    re.compile(_B + r"AKIA[0-9A-Z]{16}" + _E),  # AWS access key id
    re.compile(_B + r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(_B + r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    # Labelled credentials are not a pattern here: the label model above (`redact_labelled`).
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
    return redact_labelled(text)


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
# text no pattern should be trusted to cover, so the bundle carries an allowlist, not the name. The
# allowlist is the one harness tool vocabulary: the shell and read-only groups the extractor's
# heuristics use are named here (lower-case, the way `extract` compares them) and are part of it.
SHELL_TOOLS = frozenset(
    {"bash", "shell", "exec", "exec_command", "run_shell_command", "local_shell", "terminal"}
)
READONLY_TOOLS = frozenset({"read", "grep", "glob", "ls", "find"})
STANDARD_TOOLS = (
    frozenset(
        {
            # Claude Code
            "Bash", "Read", "Grep", "Glob", "LS", "Edit", "MultiEdit", "Write", "NotebookEdit",
            "WebFetch", "WebSearch", "ToolSearch", "Task", "Agent", "TodoWrite",
            # pi, Codex and others (exact spellings: no case folding)
            "edit", "write", "apply_patch", "update_plan", "view_image", "write_stdin",
        }
    )
    | SHELL_TOOLS
    | READONLY_TOOLS
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
    shape the tripwire looks for, so ``sanitize`` succeeding implies ``verify_files`` passes. A text
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
    judged on the key as written and as cleaned, so ``verify_files`` (which sees the cleaned key)
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
    "credential label": _LabelRule(),
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


def verify_files(files: Mapping[str, bytes]) -> list[str]:
    """``["file: rule", ...]`` for every bundle file in ``files`` (name to bytes) that trips a leak
    rule; empty when clean.

    JSON and JSONL files are checked decoded (see the module docstring), a markdown file as the
    text its reader sees (the table-cell escape undone, :func:`md_decoded`), anything else as text.
    It checks the bytes it is given, never a path: the caller holds the bytes it will write or post.
    """
    found: list[str] = []
    for n, data in files.items():
        text = data.decode("utf-8", "replace")
        found.extend(f"{n}: {rule}" for rule in dict.fromkeys(_file_leaks(n, text)))
    return found
