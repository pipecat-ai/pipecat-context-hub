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
files are scanned as the text a reader sees, with the one table-cell escape undone: ``report.md``
one table cell (or other line) at a time, the unit one rendered value occupies (:func:`report_units`),
``SCHEMA.md`` whole. A scan of serialised JSON sees escapes, not text: ``options:\\nvoice`` there
looks like a Windows path (a false failure) and ``\\u0073k-...`` hides a token (a miss).

Closure. Three properties hold, and ``tests/unit/test_audit_sessions.py`` checks each over
generated values: whatever ``sanitize`` emits passes the tripwire; ``report.md`` rendered from
rows that pass the tripwire passes it too (a value is one table cell, checked alone, and inside
a cell or line the renderer only joins row values with whitespace-bounded separators that end every
value and segment: :func:`value_text`, :func:`md_cell`); and a labelled credential in any form of
the label grammar (:data:`LABEL_FORMS`: ``password: ...``, ``--api-key ...``, ``Authorization:
Basic ...``, ``.netrc``, ``curl -u``, URL userinfo, a name/value pair, ...) is removed by
``sanitize`` and refused by the tripwire in a hand edit.
"""

from __future__ import annotations

import itertools
import json
import re
import shlex
import string
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# The item separator of every text a text sink renders (lists, line breaks; see `value_text`), and
# the pair separator (an object's key and value). Neither is a label separator: `Flags: password ↦ 1`
# (the report counting a flag string) is a count, not a labelled value. `→` IS a label separator
# (`api_key → v`), so the renderer must never join a key and its value with it.
_ITEM_SEP = " · "
_PAIR_SEP = " ↦ "

# Secrets must never reach the output files. Over-redact rather than under-redact.
# `\b` treats `_` as a word character, so `key_sk-...`, `mcp__sk-...` and `API_KEY_<hex>` have no
# boundary and would escape. A secret is delimited by anything that is not a letter or digit.
_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"
REDACTED = "<redacted>"

# ---- The label model: a credential is recognised by the LABEL in front of it, never by a list of
# value shapes. No value pattern can recognise an arbitrary password, and a hub call's arguments are
# agent-chosen (``{"api_key": "..."}``), so the label decides. One recogniser (:func:`is_credential_
# name`) serves a dict key, a command-line flag and a label in free text; one value model
# (:func:`_value_span`) decides what a label's value is; ``sanitize`` and the tripwire each locate
# labels with their own code (locator A: patterns; locator B: a character walk) over the same
# grammar, and both call the same per-form helpers, so a weakened locator on one side is caught by
# the other.
#
# The grammar is :data:`LABEL_FORMS` below: every syntactic form recognised, each with its rule and
# examples. ``tests/unit/test_audit_sessions.py`` generates its whole-space table from that list, so
# a new form is one row there. Anything outside these forms is not detected: redaction stays
# best-effort over free text, and the participant reads ``report.md`` before sharing.
#
# Rule 0, normalisation. Both locators read the text through :func:`normal_view`: every character
# NFKC-folded (fullwidth `：` is `:`, NBSP is a space), format characters (Unicode `Cf`: zero-width
# space, soft hyphen, BOM) dropped, and curly quotes read as `'` and `"`. Spans found in the view
# are mapped back to the original characters, so redaction removes exactly what was found.
#
# Rule 1, the recogniser, over the label's word SEGMENTS: split at every non-letter (`_ - . `,
# spaces, digits) and at camel-case humps, ASCII-lowered by a table.
#   1a. A segment that STARTS with a whole non-credential word sharing a stem's letters
#       (`NON_CREDENTIAL_SEGMENTS`: `tokenizer`, `tokenize`, `tokenization`, `secretary`, ...) has
#       those words blanked (repeatedly), and what remains is read as its own segment: `tokenizerkey`
#       is `key` (a credential), `TOKENIZERSECRET` is `secret`, `tokenizers` is nothing. When the
#       words can be cut more than one way (`tokenizer`+`s...` or `tokenizers`+`...`), the label is a
#       credential when ANY reading is one. A prefix that is not a whole word (`tokeniz`) blanks
#       nothing.
#   1b. A label that is exactly one of `BENIGN_WHOLE_LABELS` (`tokens`, `keys`) is not a credential.
#   1c. When the label's TRAILING segments spell a benign phrase (`BENIGN_LABEL_WORDS`: a count, a
#       hint, a file or a policy about a credential: `max_tokens`, `token_count`, `tokens_used`,
#       `password_hint`, `password_file`, `secret_santa`), those segments are dropped. A benign
#       phrase anywhere else in the label is not dropped, and `tokens` alone is never a benign
#       phrase: `API_TOKENS` and `MY_TOKENS` are plurals of credential labels.
#   1d. What is left names a credential when its letters, joined, CONTAIN a credential stem
#       (`password1`, `api_key_prod`, `OPENAI_API_KEY_2`, `secret_key_base`, `X-Api-Key`,
#       `db.password`, `API key`, `SECRETS`, `PASSWORDS`), when a WHOLE segment is a credential word
#       (`CREDENTIAL_WORDS`: `pass`, `pw`, `auth`, in any position: `SMTP_PASS`, `DB_PASS`,
#       `EMAIL_PW`, `X_AUTH`, `auth_token`), or when its last segment is `key` or `keys`
#       (`STRIPE_KEY`, `SSH_KEYS`, a bare `key`). `bypass`, `passed`, `passenger`, `author`,
#       `authority`, `authors`, `authored` and `authorize` are not credential words (whole segments
#       only); `MONKEY` (no `key` segment), `KeyError` and `key_id` (`key` not last) are not
#       credentials.
# Known cost: a value under a label that merely contains a stem or a credential word
# (`next_token`, `authorization_url`, `primary_key`, `cookie_policy`, `pass_rate`, `auth_method`,
# `ByPass`) is redacted too; over-redacting is the safe direction. Invariant (tested): every label
# the earlier `KEY|TOKEN|SECRET|PASSWORD`-substring rule redacted, whose stem is a whole segment, is
# still redacted, except the benign phrases and whole labels above and a label whose `key` segment
# is not its last.
#
# Rule 2, the value (:func:`_value_span`), for every form that has a separator:
#   * A quoted value runs to the matching closing quote: the same quote character after exactly as
#     many backslashes (`\"` inside `"..."` is not the end); a triple quote (`"""`, `'''`: TOML,
#     Python; escaped at any depth inside a JSON string) runs to the matching triple. An
#     unterminated quote is a value to the end of the text.
#   * A flag's unquoted value is one word.
#   * No value on the label's line (only blanks, or a YAML block indicator `|` `>` with chomping and
#     indent marks): the value is the next non-blank line, to its end (`password:\n  v`,
#     `password: |\n  v`). After a block indicator the next line must be indented. When that line is
#     indented deeper than the label's line, every following line indented deeper is part of the
#     value too, blank lines between included (a YAML block or plain multi-line scalar; it ends at a
#     dedent). A line break is a real one or an escaped `\n` / `\r` (a stringified JSON document);
#     a line ends at its break or a rendered separator, and after an escaped break also at the next
#     escape or `"`. The end of the text, and a rendered separator right after the break, mean no
#     value (`password: \n | x`): a report cell ends a value exactly where its row's text ended.
#     (So a value starting with `\n` or `\r` keeps those two characters.)
#   * Any other unquoted value runs to the end of its segment: a line break, a sentence end (`.`
#     then whitespace), two blanks, or a separator the report renders between values (a rendered
#     symbol ` | ` ` · ` ` ↦ ` with a blank before it and whitespace or the end after it: the one
#     definition of a segment boundary, :func:`_render_sep_at`), so a passphrase with spaces loses
#     its tail too; a value with no blank in it goes whole, whatever punctuation it holds. Context
#     before the label adds terminators:
#       - a URL-query key (the label right after `?` or `&`, separated by `=`): `&` ends the value,
#         and `#` too when the blank-free token holding the label is a real `scheme://` URL; a
#         blank does not (`a&password=correct horse battery` loses all three words);
#       - an open bracket: `)` `]` `}` end the value when they close a bracket still open at the
#         value, and `,` `;` when any bracket is still open there (`(password: abc)`,
#         `{"password": abc, ...}`). Only brackets in the label's segment count (since the last
#         line break or rendered separator), brackets opened before a quote that is still open at
#         the label do not (a label inside a quoted string is bounded by its string), and a bracket
#         the label itself closes (`os.environ["X"] = ...`) is closed;
#       - a label opened by a double quote that is not closed before the separator
#         (`-H "Authorization: Bearer x"`): the value ends at the matching closing quote, or at
#         the segment's end when that comes first;
#       - a cookie label (`Cookie`, `Set-Cookie`): `;` and `,` never end the value (cookie pairs,
#         and a `Set-Cookie` date, are the value: the whole header value is redacted, pair names
#         included; inside brackets it runs to the closing bracket), and neither do two blanks.
#   * An authorization label (`Authorization`, `Proxy-Authorization`, an `auth` segment): a leading
#     scheme word (`Basic`, `Bearer`, `Token`, `ApiKey`, `Digest`, `Negotiate`, `NTLM`, `OAuth`)
#     and its run of blanks (any length) are not the value; a scheme with nothing after it is no
#     value, and two blanks do not end the value (`Bearer  x` keeps nothing of `x`).
#   * No minimum length: any non-empty value is redacted except the redaction marker, a placeholder
#     (:func:`is_placeholder`) and a bare YAML block indicator.
#
# Rule 3, URL userinfo (a value-shape rule in ``SECRET_RES``): in `scheme://AUTHORITY`, the
# authority runs to the LAST `@` before the first blank or quote; when it holds a `:`, the part
# after the first `:` up to that `@` is a password (it may hold `/ ? # @`; the user may hold `@`),
# and the whole `scheme://user:password@` is removed. `scheme://user@host`, `mailto:a@b.c` and
# `git@github.com:org/repo` are kept, and so is `host:PORT/path@x`: a `:` followed by 1-5 digits
# and then `/`, `?` or `#` is a port, not a password (a password that is 1-5 digits before a `/`
# is therefore missed).
#
# Rule 4, the keyword forms (one helper each, both locators): netrc, SQL (`IDENTIFIED BY v`,
# `PASSWORD 'v'`), prose (`LABEL is v`), `set` / `export` / `setenv LABEL v`, a glued MySQL `-pv`,
# `sshpass -p v`, `curl -u user:v`, an XML element (`<LABEL>v</LABEL>`, closing tag in the same
# segment) and a name/value pair (a `value` member whose sibling `name` / `key` / `variable` /
# `env` member, with only blanks, breaks, quotes, `,` `;` between and no rendered separator, names
# a credential; in a parsed object, :func:`names_credential_pair`). Their costs: `token is invalid`
# loses `invalid`, `set token mode` loses `mode`, an object `{"key": ..., "value": ...}` loses both.
_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
CREDENTIAL_STEMS = (
    "password", "passwd", "passphrase", "pwd", "secret", "token", "apikey", "accesskey",
    "privatekey", "secretkey", "credential", "authorization", "bearer", "cookie",
)  # fmt: skip
# Rule 1d: whole segments that name a credential on their own (too short to match as substrings).
CREDENTIAL_WORDS = ("pass", "pw", "auth")
# Rule 1c: phrases (letters of whole trailing segments, joined) that qualify a stem into something
# else: an LLM token count, a hint, file or policy about a credential, a gift exchange.
BENIGN_LABEL_WORDS = (
    "maxtokens", "mintokens", "numtokens", "totaltokens", "prompttokens", "completiontokens",
    "inputtokens", "outputtokens", "cachedtokens", "reasoningtokens", "tokencount",
    "tokenscount", "tokenlimit", "tokenbudget", "tokensused", "tokensremaining", "tokenusage",
    "passwordhint", "passwordfile", "passwordpolicy", "secretsanta",
)  # fmt: skip
# Rule 1a: whole words that share a stem's letters without naming a credential (not prefixes).
NON_CREDENTIAL_SEGMENTS = (
    "tokenize", "tokenizes", "tokenized", "tokenizing", "tokenizer", "tokenizers",
    "tokenization", "tokenizations", "tokenise", "tokenises", "tokenised", "tokenising",
    "tokeniser", "tokenisers", "tokenisation", "secretary", "secretaries", "secretariat",
    "secretarial",
)  # fmt: skip
# Rule 1b: a label that is exactly this one segment is not a credential (a count, a keyboard term).
BENIGN_WHOLE_LABELS = ("tokens", "keys")
_KEY_SEGMENTS = ("key", "keys")
_SEGMENT_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")
_MAX_READINGS = 64  # rule 1a readings of one label; past this the label counts as a credential

# Rule 0: characters read as another before label detection.
_QUOTE_FOLD = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
}  # fmt: skip


def _normal_char(ch: str) -> str:
    if ch in _QUOTE_FOLD:
        return _QUOTE_FOLD[ch]
    if unicodedata.category(ch) == "Cf":
        return ""
    return unicodedata.normalize("NFKC", ch)


def normal_view(text: str) -> tuple[str, list[int] | None]:
    """``text`` as both locators read it (rule 0), and for each character of that view the index
    of the ``text`` character it came from (None when the view is ``text`` itself). Normalised one
    character at a time, so the view of a concatenation is the concatenation of the views."""
    if text.isascii():
        return text, None
    parts: list[str] = []
    starts: list[int] = []
    for i, ch in enumerate(text):
        out = ch if ch.isascii() else _normal_char(ch)
        parts.append(out)
        starts.extend([i] * len(out))
    return "".join(parts), starts


def label_segments(name: str) -> list[str]:
    """``name``'s word segments for recognition (rule 1): normalised (rule 0), split at non-letters
    and camel-case humps, ASCII-lowered, letters a-z only."""
    norm = normal_view(name)[0]
    return [w.translate(_FOLD) for w in _SEGMENT_RE.findall(norm) if w.isalpha()]


def _segment_readings(seg: str) -> list[list[str]]:
    """Rule 1a: ``seg`` with its leading whole non-credential words blanked (``|``), every way they
    can be cut, each reading ending in what remains as its own segment."""
    if not any(seg.startswith(w) for w in NON_CREDENTIAL_SEGMENTS):
        return [[seg]]
    readings: list[list[str]] = []
    seen, todo = {0}, [0]
    while todo:
        p = todo.pop()
        nxt = [p + len(w) for w in NON_CREDENTIAL_SEGMENTS if seg.startswith(w, p)]
        if not nxt:
            readings.append(["|", seg[p:]] if p < len(seg) else ["|"])
        for q in nxt:
            if q not in seen:
                seen.add(q)
                todo.append(q)
    return readings


def _names_credential(segs: list[str]) -> bool:
    """Rules 1b-1d over one reading's segments."""
    if len(segs) == 1 and segs[0] in BENIGN_WHOLE_LABELS:
        return False
    for i in range(len(segs)):
        if "".join(segs[i:]) in BENIGN_LABEL_WORDS:
            segs = segs[:i]
            break
    letters = "".join(segs)
    return (
        any(stem in letters for stem in CREDENTIAL_STEMS)
        or any(s in CREDENTIAL_WORDS for s in segs)
        or (bool(segs) and segs[-1] in _KEY_SEGMENTS)
    )


def is_credential_name(name: str) -> bool:
    """True when ``name`` (a dict key, a flag or a label in text) names a credential (rule 1)."""
    options = [_segment_readings(s) for s in label_segments(name)]
    count = 1
    for o in options:
        count *= len(o)
    if count > _MAX_READINGS:  # only a hostile label cuts this many ways: fail closed
        return True
    return any(
        _names_credential([w for reading in combo for w in reading])
        for combo in itertools.product(*options)
    )


def _is_auth_label(name: str) -> bool:
    segs = label_segments(name)
    return "authorization" in "".join(segs) or "auth" in segs


def _is_cookie_label(name: str) -> bool:
    return "cookie" in "".join(label_segments(name))


# ---- Placeholders (rule 2's exemption): text that stands for a credential without being one. The
# one rule, shared by both locators:
#   * an ellipsis or mask: `..`, `…`, `xxx`, `***` (any length);
#   * `$VAR` or `${VAR}` with VAR upper-case letters, digits and `_`, under 33 characters;
#   * `<name>` with name lower-case words (`a-z`, digits after the first letter) joined by `_` `-`;
#   * `YOUR_...` / `your-...`: `YOUR` or `your`, then words each all upper- or all lower-case,
#     which together name a credential (rule 1): `YOUR_API_KEY`, `your-api-key`,
#     `your_password_here` stand for one, `your-dog-rex` and `YOUR_PROJECT_ID` are values;
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
    if m.group("your") is not None and not is_credential_name(name[len("your") + 1 :]):
        return False
    return not (
        _TOKEN_PREFIX_RE.match(name)
        or _MIXED_RUN_RE.search(name)
        or any(rx.search(name) for rx in SECRET_RES)
    )


@dataclass(frozen=True)
class LabelForm:
    """One syntactic form of the label grammar. ``examples`` are templates: ``{label}`` is any
    credential label (a form without it names its own), ``{value}`` the value, which is exactly
    what sanitize removes (``expected``, when given, is the output with ``{redacted}`` for the
    marker, for a form that removes more than the value). ``values`` names the value shapes the
    form carries: ``ONE_TOKEN`` (no blank) or ``ANY_VALUE`` (blanks too). ``structured`` are JSON
    templates of the form as a parsed value (a share row's dict), whose ``{value}`` sanitize must
    replace with the marker and the tripwire's decoded walk must refuse."""

    name: str
    rule: str
    examples: tuple[str | tuple[str, str], ...]
    values: tuple[str, ...]
    structured: tuple[str, ...] = ()


# The value shapes the forms carry (``tests/unit/test_audit_sessions.py`` gives each a concrete
# value): an alphanumeric word, one character, a word of punctuation, non-ASCII letters, and words
# separated by single blanks.
ONE_TOKEN = ("word", "one", "punct", "unicode")
ANY_VALUE = (*ONE_TOKEN, "spaced")

LABEL_FORMS: tuple[LabelForm, ...] = (
    LabelForm(
        "separator",
        "a label word (letters, digits, `_ . -`), blanks, one of `: = => -> → ⇒`, blanks, the "
        "value (a fullwidth `：` `＝` is `:` `=` by rule 0: see `unicode spelling`)",
        ("{label}: {value}", "{label}={value}", "{label} = {value}", "{label} => {value}",
         "{label}->{value}", "{label} → {value}", "{label}→{value}", "{label} ⇒ {value}"),
        ANY_VALUE,
    ),
    LabelForm(
        "two-word label",
        "one more word and a single blank before the label word (`API key`, `secret phrase`)",
        ("API key: {value}", "access token = {value}", "client secret: {value}",
         "secret phrase: {value}", "auth code = {value}"),
        ANY_VALUE,
    ),
    LabelForm(
        "quoted value",
        "the value in quotes (optionally backslash-escaped, any depth) or triple quotes",
        ('{label}: "{value}"', "{label}='{value}'", '{{"{label}": "{value}"}}',
         '{{\\"{label}\\": \\"{value}\\"}}', '{label} = """{value}"""',
         "{label} = '''{value}'''"),
        ANY_VALUE,
    ),
    LabelForm(
        "flag",
        "a credential-named flag (a word starting with `-`), blanks or `=`, one word",
        ("--{label} {value}", "--{label}={value}", "-{label} {value}"),
        ONE_TOKEN,
    ),
    LabelForm(
        "flag with a quoted value",
        "a credential-named flag, blanks or `=`, a quoted value",
        ('--{label} "{value}"', "--{label}='{value}'"),
        ANY_VALUE,
    ),
    LabelForm(
        "markdown label",
        "quotes, `*`, backticks, `]` or `~` between the label and the separator",
        ("**{label}**: {value}", "`{label}`: {value}", "_{label}_: {value}", "[{label}]: {value}",
         "~~{label}~~: {value}", "*{label}*: {value}"),
        ANY_VALUE,
    ),
    LabelForm(
        "subscript or attribute assignment",
        "the label in a subscript (`x[\"LABEL\"] = v`) or after a `.`; a read (`getenv(\"X\")`) "
        "has no separator and is not one",
        ('os.environ["{label}"] = "{value}"', "headers['{label}'] = '{value}'",
         'config.{label} = "{value}"', 'os.environ["{label}"] = {value}'),
        ANY_VALUE,
    ),
    LabelForm(
        "unicode spelling",
        "curly quotes, NBSP, zero-width characters and fullwidth forms around the label and as "
        "the separator (rule 0)",
        ("\u201c{label}\u201d: {value}", "\u2018{label}\u2019 = {value}",
         "{label}\u00a0=\u00a0{value}", "{label}\u200b: {value}", "ｐａｓｓｗｏｒｄ: {value}",
         "{label}\uff1a{value}", "{label}\uff1d{value}", "{label}\ufe55{value}"),
        ANY_VALUE,
    ),
    LabelForm(
        "value on the next line",
        "nothing (or a YAML block indicator) after the separator: the next non-blank line, and "
        "when that line is indented deeper than the label's, every following line indented "
        "deeper too (a block ends at a dedent)",
        ("{label}:\n  {value}", "{label}: |\n  {value}", "{label}: >-\n  {value}",
         "{label}:\n\n{value}", "**{label}**:\n  {value}",
         ("{label}: |\n  {value}\n  {value}", "{label}: |\n  {redacted}"),
         ("{label}: >2\n   {value}\n\n   {value}\nnext: 1", "{label}: >2\n   {redacted}\nnext: 1"),
         ("  {label}: |-\n    {value}\n    {value}\n  other: x",
          "  {label}: |-\n    {redacted}\n  other: x"),
         ("{label}: |\\n  {value}\\n  {value}", "{label}: |\\n  {redacted}"),
         ("{label}:\n  {value}\n  {value}", "{label}:\n  {redacted}")),
        ANY_VALUE,
    ),
    LabelForm(
        "URL query",
        "a key after `?` or `&` with `=`: `&` ends the value, `#` too inside a real URL",
        ("https://h/p?a=1&{label}={value}&b=2", "https://h/p?{label}={value}#frag",
         "a=b&{label}={value}"),
        ANY_VALUE,
    ),
    LabelForm(
        "authorization scheme",
        "an authorization label's scheme word (`Basic`, `Bearer`, ...) is kept, the rest goes",
        ("Authorization: Basic {value}", "Proxy-Authorization: Bearer {value}",
         '-H "Authorization: Token {value}"', "X-Auth: ApiKey {value}"),
        ANY_VALUE,
    ),
    LabelForm(
        "cookie header",
        "a cookie label: the whole header value, `;` and `,` included, is the value",
        ("Cookie: {value}", "Set-Cookie: {value}", "{{'Cookie': {value}}}"),
        ANY_VALUE,
    ),
    LabelForm(
        "netrc",
        "the word `password`/`passwd`, blanks, one word, after a `machine`/`login`/`account` word "
        "pair in the same segment, or alone on its segment (`  password v`)",
        ("machine h login bob password {value}", "login bob password {value}",
         "machine h\n  login bob\n  password {value}\n"),
        ONE_TOKEN,
    ),
    LabelForm(
        "user flag",
        "`-u`, `-U`, `--user`, `--proxy-user` (blanks, `=`, or glued to a short flag) with a "
        "value `user:password`: the part after the first `:`",
        ("curl -u alice:{value} https://h", "curl --user alice:{value} https://h",
         "curl --user=alice:{value}", "curl -ualice:{value} https://h",
         "curl --proxy-user bob:{value} https://h"),
        ONE_TOKEN,
    ),
    LabelForm(
        "glued password flag",
        "`-p` glued to its value after a MySQL-family command in the same segment",
        ("mysql -u root -p{value} app", "mysqldump -p{value} app"),
        ONE_TOKEN,
    ),
    LabelForm(
        "URL userinfo",
        "rule 3: `scheme://user:password@host`; the whole userinfo prefix goes",
        (("postgres://admin:{value}@db:5432/app", "{redacted}db:5432/app"),
         ("redis://:{value}@h:6379", "{redacted}h:6379"),
         ("https://u@x.com:{value}@h/p", "{redacted}h/p")),
        ONE_TOKEN,
    ),
    LabelForm(
        "name/value pair",
        "a `value` member whose sibling `name`, `key`, `variable` or `env` member (before or after "
        "it, with only blanks, line breaks, quotes, `,` and `;` between) names a credential or "
        "holds the marker: in text, and as a parsed object",
        ('{{"name": "{label}", "value": "{value}"}}', '{{"value": "{value}", "name": "{label}"}}',
         "- name: {label}\n  value: {value}", '{{\\"env\\": \\"{label}\\", \\"value\\": \\"{value}\\"}}',
         ('<add key="{label}" value="{value}"/>', '<add key="{redacted}" value="{redacted}"/>')),
        ANY_VALUE,
        ('{{"name": "{label}", "value": "{value}"}}', '[{{"Name": "{label}", "Value": "{value}"}}]',
         '{{"env": [{{"variable": "{label}", "value": "{value}"}}]}}'),
    ),
    LabelForm(
        "XML element",
        "`<label>`, the text, and the matching `</label>` in the same segment (an attribute "
        "`label=\"v\"` is the quoted-value form)",
        ("<{label}>{value}</{label}>", "<config><{label}> {value} </{label}></config>",
         '<user {label}="{value}"/>'),
        ANY_VALUE,
    ),
    LabelForm(
        "SQL password",
        "`IDENTIFIED BY` or the word `PASSWORD`, blanks, a quoted value (`WITH PASSWORD = 'v'` is "
        "the separator form)",
        ("CREATE USER bob IDENTIFIED BY '{value}'", "ALTER ROLE r PASSWORD '{value}'",
         "create role r with login password '{value}'", "ALTER ROLE r WITH PASSWORD = '{value}'"),
        ANY_VALUE,
    ),
    LabelForm(
        "SQL identified by",
        "`IDENTIFIED BY`, blanks, one unquoted word",
        ("CREATE USER bob IDENTIFIED BY {value}", "alter user bob identified  by {value}"),
        ONE_TOKEN,
    ),
    LabelForm(
        "prose is",
        "a credential label word, blanks, `is`, blanks, one word",
        ("{label} is {value}", "my {label} IS {value}"),
        ONE_TOKEN,
    ),
    LabelForm(
        "set or export",
        "`set`, `export` or `setenv`, blanks, a credential label word, blanks, one word "
        "(`export X=v` is the separator form)",
        ("set {label} {value}", "export {label} {value}", "setenv {label} {value}"),
        ONE_TOKEN,
    ),
    LabelForm(
        "sshpass",
        "`-p` after an `sshpass` command in the same segment, glued or followed by blanks",
        ("sshpass -p {value} ssh h", "sshpass -p{value} ssh h"),
        ONE_TOKEN,
    ),
)  # fmt: skip

Span = tuple[int, int, str]

_QUOTE_TOKEN_RE = re.compile(r"\\*[\"']")
# What the report renders between values: a table cell boundary, the item and the pair separator.
_RENDER_SEP_TEXTS = (" | ", _ITEM_SEP, _PAIR_SEP)
_RENDER_SEPS = frozenset(s.strip() for s in _RENDER_SEP_TEXTS)
_SPACE_RE = re.compile(r"\s")
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_BLOCK_INDICATOR_RE = re.compile(r"[|>][+\-]?[1-9]?[+\-]?")
_LINE_BREAK_RE = re.compile(r"\r\n?|\n|\\+[nr]")
_ESCAPED_VALUE_END_RE = re.compile(r"\\+[nrt']|\\*\"")
_URL_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_AUTH_SCHEME_RE = re.compile(
    r"(?ai)(?:basic|bearer|token|apikey|digest|negotiate|ntlm|oauth)(?:[ \t]+|$)"
)


def _skip_blanks(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _render_sep_at(text: str, i: int) -> bool:
    """A separator the report renders between values (` | ` ` · ` ` ↦ `) has its symbol at ``i``:
    a blank before it, whitespace or the end of the text after it."""
    return (
        i < len(text)
        and text[i] in _RENDER_SEPS
        and i > 0
        and text[i - 1] in " \t"
        and (i + 1 == len(text) or text[i + 1].isspace())
    )


# A rendered separator as `_render_sep_at` sees it, located by its leading blank: one definition
# for where a value ends and where a segment starts or ends, so a separator the report creates
# (`x |` then a line break joined with ` · `) ends a segment in the row text too.
_SEGMENT_END_RE = re.compile(r"[\r\n]|[ \t](?=[" + "".join(sorted(_RENDER_SEPS)) + r"](?:\s|\Z))")


def _segment_start(text: str, i: int) -> int:
    """Where the segment holding ``i`` starts: after the last line break or rendered separator
    (and the one blank after it) before ``i``."""
    cut = max(text.rfind("\n", 0, i), text.rfind("\r", 0, i)) + 1
    for m in _SEGMENT_END_RE.finditer(text, cut):  # no endpos: the lookahead reads past ``i``
        s = m.start() + 1  # the separator's symbol
        if s >= i:
            break
        cut = s + 2 if s + 1 < len(text) and text[s + 1] in " \t" else s + 1
    return min(cut, i)


def _segment_end(text: str, i: int) -> int:
    """Where the segment holding ``i`` ends: the next line break, or the blank before the next
    rendered separator, at or after ``i``."""
    m = _SEGMENT_END_RE.search(text, i)
    return m.start() if m else len(text)


def _attached_quote_start(text: str, label_start: int) -> int:
    """``label_start``, or the start of a quote token (a quote and its backslashes) right before it."""
    j = label_start
    if j > 0 and text[j - 1] in "\"'":
        j -= 1
        while j > 0 and text[j - 1] == "\\":
            j -= 1
    return j


def _open_brackets(text: str, label_start: int, pos: int) -> list[str]:
    """The brackets open at a value starting at ``pos`` (rule 2): opened in the label's segment
    before the label (its own opening quote aside) and still open, those opened before a quote
    still open at the label left out, then updated by the brackets between the label and the value
    (`["X"] = `). Only the segment counts: a report line joins values with rendered separators,
    so nothing in one value may change where another value ends."""
    stack: list[str] = []
    quote, base = "", 0
    for ch in text[_segment_start(text, label_start) : _attached_quote_start(text, label_start)]:
        if ch in "\"'":
            if not quote:
                quote, base = ch, len(stack)
            elif ch == quote:
                del stack[base:]  # brackets inside a closed string were its text
                quote = ""
        elif ch in "([{":
            stack.append(ch)
        elif ch in _CLOSERS and len(stack) > (base if quote else 0) and stack[-1] == _CLOSERS[ch]:
            stack.pop()
    if quote:
        del stack[:base]
    for ch in text[label_start:pos]:
        if ch in "([{":
            stack.append(ch)
        elif ch in _CLOSERS and stack and stack[-1] == _CLOSERS[ch]:
            stack.pop()
    return stack


def _is_query_key(text: str, label_start: int, sep: str) -> bool:
    """A URL-query key: the label follows `?` or `&` and is separated from its value by `=`."""
    return sep == "=" and label_start > 0 and text[label_start - 1] in "?&"


def _in_url(text: str, label_start: int) -> bool:
    """The blank-free token holding the label is a real `scheme://` URL."""
    token_start = max(text.rfind(c, 0, label_start) for c in " \t\r\n") + 1
    return _URL_SCHEME_RE.search(text, token_start, label_start) is not None


def _bare_end(
    text: str,
    pos: int,
    opened: list[str],
    *,
    query: bool,
    url: bool,
    cookie: bool,
    blanks: bool = True,
) -> int:
    """Where an unquoted, non-flag value starting at ``pos`` ends (rule 2), trailing blanks off.
    ``blanks``: whether two blanks end it (not for an authorization or cookie value)."""
    n, i, inner = len(text), pos, []
    while i < n:
        ch = text[i]
        if ch in "\r\n":
            break
        end_follows = i + 1 == n or text[i + 1].isspace()
        if ch in " \t":
            if blanks and i + 1 < n and text[i + 1] in " \t":  # two blanks
                break
        elif _render_sep_at(text, i):
            break  # a separator the report renders between values: ` | ` ` · ` ` ↦ `
        elif (ch == "." and end_follows) or (query and (ch == "&" or (url and ch == "#"))):
            break
        elif ch in "([{":
            inner.append(ch)
        elif ch in _CLOSERS:
            if inner and inner[-1] == _CLOSERS[ch]:
                inner.pop()
            elif not inner and opened and opened[-1] == _CLOSERS[ch]:
                break
        elif ch in ",;" and not inner and opened and not cookie:
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


def _next_content(text: str, j: int) -> tuple[int, int]:
    """From just past a line break at ``j``: (the start of the next non-blank line, where its text
    starts); blank lines and their breaks are skipped."""
    line = j
    while (j := _skip_blanks(text, j)) < len(text) and (more := _LINE_BREAK_RE.match(text, j)):
        j = line = more.end()
    return line, j


def _line_end(text: str, j: int, escaped: bool) -> tuple[int, int | None]:
    """Where the value line starting at ``j`` ends (a line break or a rendered separator, and after
    an escaped break also the next escape or `"`), and, when a line break ends it (so a deeper
    indented line may continue the value), where the next line starts."""
    end = _segment_end(text, j)
    if escaped and (m := _ESCAPED_VALUE_END_RE.search(text, j, end)):
        end = m.start()
    br = _LINE_BREAK_RE.match(text, end)
    return end, (br.end() if br else None)


def _label_indent(text: str, label_start: int) -> int:
    """The blanks before the first character of the label's line: the line starts after the last
    line break (real or escaped) or rendered separator before the label."""
    start = _segment_start(text, label_start)
    for br in ("\\n", "\\r"):
        k = text.rfind(br, start, label_start)
        if k >= 0:
            start = max(start, k + 2)
    return _skip_blanks(text, start) - start


def _next_line(text: str, pos: int, label_start: int) -> tuple[bool, Span | None]:
    """Rule 2's next-line value: (whether the label's line holds no value of its own, the value or
    None). The value is the next non-blank line; when that line is indented deeper than the label's
    line, every following line indented deeper is part of it too (a YAML block, a plain multi-line
    scalar), blank lines between included. The end of the text and a rendered separator end the
    value wherever they occur, so a report cell ends it exactly where the end of its row value did."""
    n, i = len(text), pos
    block = _BLOCK_INDICATOR_RE.match(text, i)
    if block:
        i = _skip_blanks(text, block.end())
    if i >= n:
        return block is not None, None
    br = _LINE_BREAK_RE.match(text, i)
    if br is None:
        return False, None
    escaped = text[br.start()] == "\\"
    line, j = _next_content(text, br.end())
    if j >= n or _render_sep_at(text, j) or (block and j == line):  # a block's content is indented
        return True, None
    indent = _label_indent(text, label_start)
    start, deeper = j, j - line > indent
    last, more = _line_end(text, j, escaped)
    while deeper and more is not None:
        line, j = _next_content(text, more)
        if j >= n or _render_sep_at(text, j) or j - line <= indent:
            break  # the end of the text or a cell, or a dedent: the block is over
        last, more = _line_end(text, j, escaped)
    while last > start and text[last - 1] in " \t":
        last -= 1
    return True, ((start, last, "bare") if last > start else None)


def _value_span(
    text: str,
    pos: int,
    *,
    flag: bool,
    label_start: int = 0,
    label_end: int = 0,
    sep: str = "",
    name: str = "",
) -> Span | None:
    """The value of a label (``text[label_start:label_end]``, whose credential name is ``name``)
    whose separator ``sep`` (empty for a flag followed by blanks) ends, blanks skipped, at ``pos``
    (rule 2): ``(start, end, kind)`` or None when there is none. ``kind`` is ``quoted`` (the span is
    the content between the quotes), ``open`` (an unterminated quote: the span starts at the quote)
    or ``bare``."""
    n = len(text)
    span: Span | None
    if m := _QUOTE_TOKEN_RE.match(text, pos):
        token = m.group(0)
        if text.startswith(token * 3, pos):  # a triple-quoted string, escaped at any depth
            close: int | None = text.find(token * 3, pos + 3 * len(token))
            span = (
                (pos + 3 * len(token), close, "quoted")
                if close is not None and close >= 0
                else None
            )
            if span is None:
                span = (pos, n, "open")
        else:
            close = _closing_quote(text, m.end(), token)
            span = (pos, n, "open") if close is None else (m.end(), close, "quoted")
    elif flag:
        sp = _SPACE_RE.search(text, pos)
        end = sp.start() if sp else n
        if text[pos:end] in _RENDER_SEPS:
            end = pos
        span = (pos, end, "bare")
    else:
        empty_line, span = _next_line(text, pos, label_start)
        if not empty_line:
            cookie = _is_cookie_label(name)
            end = _bare_end(
                text,
                pos,
                _open_brackets(text, label_start, pos),
                query=(query := _is_query_key(text, label_start, sep)),
                url=query and _in_url(text, label_start),
                cookie=cookie,
                blanks=not (cookie or _is_auth_label(name)),
            )
            j = _attached_quote_start(text, label_start)
            if j < label_start and text[label_start - 1] == '"' and '"' not in text[label_end:pos]:
                close = _closing_quote(text, pos, text[j:label_start])
                if close is not None:  # `"Authorization: Bearer x"`: the quote bounds the value
                    end = min(end, close)
            span = (pos, end, "bare")
    if span is not None and span[2] != "open" and not flag and _is_auth_label(name):
        if sm := _AUTH_SCHEME_RE.match(text, span[0], span[1]):
            span = (sm.end(), span[1], span[2])
    return span if span is not None and span[1] > span[0] else None


def _is_secret_value(text: str, span: Span) -> bool:
    """False for the marker, a placeholder, an empty value and a bare YAML block indicator; an
    unterminated quote is always a value (closed in a later cell of the report it could take in
    another value's text)."""
    start, end, kind = span
    if kind == "open":
        return True
    value = text[start:end]
    if (
        kind == "bare"
    ):  # quotes, backslashes and a cut mark (`…`, read as `...`) around it are not it
        value = value.strip(" \t\"'\\….")
        if _BLOCK_INDICATOR_RE.fullmatch(value):
            return False
    return not (value in ("", REDACTED) or is_placeholder(value))


def _secret(text: str, span: Span | None) -> Span | None:
    return span if span is not None and _is_secret_value(text, span) else None


# ---- Keyword forms (netrc, user flag, glued password flag, SQL, prose `is`, `set`/`export`,
# sshpass, XML element, name/value pair): one helper each, called by both locators with the
# keyword's position.
_NETRC_CONTEXT_RE = re.compile(r"(?ai)(?<![\w.\-])(?:machine|login|account)[ \t]+\S")
_NETRC_WORDS = ("password", "passwd")
_MYSQL_RE = re.compile(
    r"(?<![\w.\-])(?:mysql|mysqldump|mysqladmin|mysqlimport|mysqlcheck|mysqlpump|mysqlsh|mariadb"
    r"|mariadb-dump|mariadb-admin)(?![\w.\-])"
)
_SSHPASS_RE = re.compile(r"(?<![\w.\-])sshpass(?![\w.\-])")
# The user-flag form's flags: the one list both locators are built from.
USER_FLAGS = ("-u", "-U", "--user", "--proxy-user")
_SHORT_USER_FLAGS = tuple(f for f in USER_FLAGS if len(f) == 2)
_LONG_USER_FLAGS = tuple(f for f in USER_FLAGS if len(f) > 2)
_IDENTIFIED, _BY = "identified", "by"
_IS = "is"
_ASSIGN_WORDS = ("set", "export", "setenv")
# A name/value pair: the members that name the pair, and the member holding its value.
PAIR_NAME_KEYS = ("name", "key", "variable", "env")
PAIR_VALUE_KEY = "value"
_PAIR_WINDOW = 256  # how far before a `value` label its sibling name is looked for


def _word_value_span(text: str, v: int) -> Span | None:
    """A keyword form's value at ``v``: a quoted value or one word, not a separator."""
    if v >= len(text) or _sep_at(text, v):
        return None
    return _secret(text, _value_span(text, v, flag=True))


def _netrc_span(text: str, kw_start: int, kw_end: int) -> Span | None:
    """The netrc form's value for the keyword ``text[kw_start:kw_end]`` (followed by a blank)."""
    v = _skip_blanks(text, kw_end)
    if v == kw_end or v >= len(text) or _sep_at(text, v):
        return None
    span = _value_span(text, v, flag=True)
    if span is None:
        return None
    seg = _segment_start(text, kw_start)
    if _NETRC_CONTEXT_RE.search(text, seg, kw_start):
        return _secret(text, span)
    after = span[1]
    if span[2] == "quoted" and (q := _QUOTE_TOKEN_RE.match(text, after)):
        after = q.end()
    before, rest = text[seg:kw_start], text[after : _segment_end(text, after)]
    return _secret(text, span) if not before.strip(" \t") and not rest.strip(" \t") else None


def _quoted_password_span(text: str, kw_end: int) -> Span | None:
    """The SQL form's `PASSWORD 'v'`: the keyword (followed by a blank), blanks, a quoted value."""
    v = _skip_blanks(text, kw_end)
    if v == kw_end or not _QUOTE_TOKEN_RE.match(text, v):
        return None
    return _word_value_span(text, v)


def _identified_by_span(text: str, by_end: int) -> Span | None:
    """The SQL form's `IDENTIFIED BY v` (``by_end`` just past `BY`, a blank after it)."""
    v = _skip_blanks(text, by_end)
    return _word_value_span(text, v) if v > by_end else None


def _user_flag_span(text: str, v: int) -> Span | None:
    """The user-flag form's password: the part after the first `:` of the value at ``v``."""
    span = _value_span(text, v, flag=True)
    if span is None:
        return None
    start, end, kind = span
    if kind == "open":
        return span
    colon = text.find(":", start, end)
    return _secret(text, (colon + 1, end, "bare")) if 0 <= colon < end - 1 else None


def _glued_password_span(text: str, flag_start: int) -> Span | None:
    """The glued password flag's value (`-p<value>` at ``flag_start``) after a MySQL-family command
    in the same segment."""
    v = flag_start + 2
    if v >= len(text) or text[v].isspace():
        return None
    if not _MYSQL_RE.search(text, _segment_start(text, flag_start), flag_start):
        return None
    return _secret(text, _value_span(text, v, flag=True))


def _sshpass_span(text: str, flag_start: int) -> Span | None:
    """The sshpass form's value (`-p v` or `-pv` at ``flag_start``) after `sshpass` in the same
    segment."""
    if not _SSHPASS_RE.search(text, _segment_start(text, flag_start), flag_start):
        return None
    v = flag_start + 2
    if v < len(text) and text[v] in " \t":
        return _word_value_span(text, _skip_blanks(text, v))
    if v >= len(text) or text[v].isspace():
        return None
    return _secret(text, _value_span(text, v, flag=True))


def _element_span(text: str, start: int, tag: str) -> Span | None:
    """The XML element form's value: the text from ``start`` (just past `<tag>`) to the matching
    `</tag>` in the same segment, blanks around it off."""
    close = text.find(f"</{tag}>", start, _segment_end(text, start))
    if close < 0:
        return None
    s, e = _skip_blanks(text, start), close
    while e > s and text[e - 1] in " \t":
        e -= 1
    return _secret(text, (s, e, "bare")) if e > s else None


def _is_pair_key(name: str, keys: tuple[str, ...]) -> bool:
    """``name`` (a dict key or a label word) is exactly one of ``keys``, in any spelling."""
    segs = label_segments(name)
    return len(segs) == 1 and segs[0] in keys


def _pair_name_is_credential(value: object) -> bool:
    """A pair's name member names a credential (rule 1), or holds the marker."""
    return isinstance(value, str) and (value == REDACTED or is_credential_name(value))


def names_credential_pair(pairs: Iterable[tuple[Any, Any]]) -> bool:
    """An object (its key/value pairs) whose name member (``PAIR_NAME_KEYS``) names a credential:
    its ``value`` member is then a credential too. The one rule ``sanitize`` and the tripwire's
    decoded walk both apply."""
    return any(
        isinstance(k, str) and _is_pair_key(k, PAIR_NAME_KEYS) and _pair_name_is_credential(x)
        for k, x in pairs
    )


_PAIR_Q = r"(?:\\*[\"'])?"
_PAIR_JOIN = r"(?:\\+[nr]|[\s,;\"'\\])*"
_PAIR_MEMBER = (
    r"(?<![\w.\-])(?P<k>[\w.\-]+)" + _PAIR_Q + r"[ \t]*[:=][ \t]*" + _PAIR_Q
    + r"(?P<n><redacted>|[\w.\-]+)(?![\w.\-])"
)  # fmt: skip
_PAIR_BEFORE_RE = re.compile(_PAIR_Q + _PAIR_MEMBER + _PAIR_Q + _PAIR_JOIN + r"\Z")
_PAIR_AFTER_RE = re.compile(_PAIR_JOIN + _PAIR_Q + _PAIR_MEMBER)


def _pair_value_span(text: str, span: Span | None, label_start: int) -> Span | None:
    """The name/value pair form: the value ``span`` of a `value` label at ``label_start`` when a
    sibling name member, just before the label or just after the value with only joiners between,
    names a credential."""
    if span is None:
        return None
    q = _attached_quote_start(text, label_start)
    before = _PAIR_BEFORE_RE.search(text, max(0, q - _PAIR_WINDOW), q)
    after_at = span[1]
    if span[2] == "quoted" and (tok := _QUOTE_TOKEN_RE.match(text, after_at)):
        after_at = tok.end()
    after = _PAIR_AFTER_RE.match(text, after_at)
    for m, a, b in (
        (before, before.start() if before else 0, label_start),
        (after, label_start, after.end() if after else 0),
    ):
        if m and _pair_names_credential(m) and not _crosses_render_sep(text, a, b):
            return _secret(text, span)
    return None


def _pair_names_credential(m: re.Match[str]) -> bool:
    return _is_pair_key(m.group("k"), PAIR_NAME_KEYS) and _pair_name_is_credential(m.group("n"))


def _crosses_render_sep(text: str, a: int, b: int) -> bool:
    """A rendered separator lies in ``text[a:b]``: a pair never spans two rendered values."""
    m = _SEGMENT_END_RE.search(text, a)
    while m is not None and m.start() < b:
        if text[m.start()] not in "\r\n":
            return True
        m = _SEGMENT_END_RE.search(text, m.end())
    return False


# Locator A (the sanitizer's): the grammar as patterns.
# A label word: letters, digits, `_`, `.` and `-`, except the `-` of a `->` separator.
_WC = r"(?:[\w.]|-(?!>))"
_W = _WC + r"+(?!" + _WC + r")"
_SEP = r"(?:=>|->|[:=→⇒])"  # `：` `＝` `﹕` are `:` `=` in the normal view (rule 0)
_CLOSE_RUN = r"(?:\\*[\"']|[*`\]~])*"  # quotes, markdown and a subscript between label and sep
_SEP_LABEL_RE = re.compile(
    r"(?<![\w.\-])(?:(?P<w0>" + _W + r")[ \t])?(?P<w1>" + _W + r")" + _CLOSE_RUN
    + r"[ \t]*(?P<sep>" + _SEP + r")[ \t]*"
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
_NETRC_RE = re.compile(r"(?<![\w.\-])[Pp][Aa][Ss][Ss][Ww](?:[Oo][Rr])?[Dd](?=[ \t])")
_USER_FLAG_RE = re.compile(
    r"(?<![\w.\-])(?:(?:" + "|".join(map(re.escape, _LONG_USER_FLAGS)) + r")(?:=|[ \t]+)"
    + r"|(?:" + "|".join(map(re.escape, _SHORT_USER_FLAGS)) + r")(?:[ \t]+|(?=[\w.\-])))"
)  # fmt: skip
_P_FLAG_RE = re.compile(r"(?<![\w.\-])-p")
_IDENTIFIED_BY_RE = re.compile(
    r"(?<![\w.\-])(?ai:" + _IDENTIFIED + r")[ \t]+(?ai:" + _BY + r")(?=[ \t])"
)  # ASCII case-insensitive keywords; the boundary is Unicode, as `_is_word_char`'s
_IS_RE = re.compile(r"(?<![\w.\-])(?P<w>" + _W + r")[ \t]+(?ai:" + _IS + r")(?=[ \t])")
_ASSIGN_RE = re.compile(
    r"(?<![\w.\-])(?ai:" + "|".join(_ASSIGN_WORDS) + r")(?=[ \t]+(?P<w>" + _W + r")[ \t])"
)
_ELEMENT_RE = re.compile(r"<(?P<tag>" + _WC + r"+)>")


def _spans_a(text: str) -> list[Span]:
    """Every labelled value in the normalised ``text`` (locator A)."""
    spans: list[Span | None] = []
    pos = 0
    while (m := _SEP_LABEL_RE.search(text, pos)) is not None:
        w1 = m.group("w1")
        names = [w1] + ([text[m.start() : m.end("w1")]] if m.group("w0") else [])
        credential = any(is_credential_name(w) for w in names)
        if credential or _is_pair_key(w1, (PAIR_VALUE_KEY,)):
            span = _value_span(
                text, m.end(), flag=w1.startswith("-"), label_start=m.start("w1"),
                label_end=m.end("w1"), sep=m.group("sep"), name=names[-1],
            )  # fmt: skip
            spans.append(
                _secret(text, span) if credential else _pair_value_span(text, span, m.start("w1"))
            )
        pos = m.end()
    for m in _FLAG_LABEL_RE.finditer(text):
        if is_credential_name(m.group("flag")):
            spans.append(_secret(text, _value_span(text, m.end(), flag=True)))
    for m in _NETRC_RE.finditer(text):
        spans += [_netrc_span(text, m.start(), m.end()), _quoted_password_span(text, m.end())]
    spans += [_user_flag_span(text, m.end()) for m in _USER_FLAG_RE.finditer(text)]
    for m in _P_FLAG_RE.finditer(text):
        spans += [_glued_password_span(text, m.start()), _sshpass_span(text, m.start())]
    spans += [_identified_by_span(text, m.end()) for m in _IDENTIFIED_BY_RE.finditer(text)]
    for m in _IS_RE.finditer(text):
        if is_credential_name(m.group("w")):
            spans.append(_word_value_span(text, _skip_blanks(text, m.end())))
    for m in _ASSIGN_RE.finditer(text):
        if is_credential_name(m.group("w")):
            spans.append(_word_value_span(text, _skip_blanks(text, m.end("w"))))
    for m in _ELEMENT_RE.finditer(text):
        if is_credential_name(m.group("tag")):
            spans.append(_element_span(text, m.end(), m.group("tag")))
    return [s for s in spans if s is not None]


def _label_spans(text: str) -> list[Span]:
    """Every labelled value in ``text`` (locator A, rules 0-2), as spans of ``text`` itself."""
    norm, starts = normal_view(text)
    spans = _spans_a(norm)
    if starts is None:
        return spans
    n = len(text)
    return [(starts[s] if s < len(starts) else n, starts[e - 1] + 1, kind) for s, e, kind in spans]


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


# Locator B (the tripwire's): the same grammar walked character by character from each separator,
# each `-` and each keyword, sharing only the recogniser, the value model and the per-form helpers
# with locator A.
_WORD_CHAR_RE = re.compile(r"[\w.\-]")
_SEP_CHARS = ":=→⇒"  # as `_SEP`: the fullwidth forms are folded by rule 0


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


def _word_end(text: str, start: int) -> int:
    end = start
    while _is_word_char(text, end):
        end += 1
    return end


def _keyword_at(text: str, i: int, words: tuple[str, ...]) -> int:
    """The end of whichever of ``words`` (ASCII case-insensitive) starts at ``i`` with no label
    character before it and a blank after it, or -1."""
    if _is_word_char(text, i - 1):
        return -1
    for w in words:
        end = i + len(w)
        if text[i:end].translate(_FOLD) == w and end < len(text) and text[end] in " \t":
            return end
    return -1


def _sep_at(text: str, i: int) -> int:
    """Length of the separator at ``i``, or 0."""
    if text.startswith(("=>", "->"), i):
        return 2
    return 1 if i < len(text) and text[i] in _SEP_CHARS else 0


def _close_run_start(text: str, j: int) -> int:
    """Walking back from ``j`` over a run of quote tokens, `*`, backticks, `]` and `~`."""
    while j > 0:
        if text[j - 1] in "\"'":
            j -= 1
            while j > 0 and text[j - 1] == "\\":
                j -= 1
        elif text[j - 1] in "*`]~":
            j -= 1
        else:
            break
    return j


def _sep_label_leaks(text: str, i: int, sep: int) -> bool:
    j = i
    while j > 0 and text[j - 1] in " \t":
        j -= 1
    j = _close_run_start(text, j)
    if _is_word_char(text, j) or (start := _word_start(text, j)) == j:
        return False
    names = [text[start:j]]
    if start >= 2 and text[start - 1] in " \t" and _is_word_char(text, start - 2):
        names.append(text[_word_start(text, start - 1) : j])
    credential = any(is_credential_name(w) for w in names)
    if not credential and not _is_pair_key(names[0], (PAIR_VALUE_KEY,)):
        return False
    span = _value_span(
        text, _skip_blanks(text, i + sep), flag=names[0].startswith("-"), label_start=start,
        label_end=j, sep=text[i : i + sep], name=names[-1],
    )  # fmt: skip
    if credential:
        return _secret(text, span) is not None
    return _pair_value_span(text, span, start) is not None


def _flag_leaks(text: str, i: int) -> bool:
    """The forms that start at a `-` with no label character before it."""
    n = len(text)
    end = _word_end(text, i)
    word = text[i:end]
    if word in _LONG_USER_FLAGS and end < n and (text[end] == "=" or text[end] in " \t"):
        v = end + 1 if text[end] == "=" else _skip_blanks(text, end)
        if _user_flag_span(text, v):
            return True
    if word in _SHORT_USER_FLAGS and end < n and text[end] in " \t":
        if _user_flag_span(text, _skip_blanks(text, end)):
            return True
    if len(word) > 2 and word[:2] in _SHORT_USER_FLAGS and _user_flag_span(text, i + 2):
        return True
    if text.startswith("-p", i) and (_glued_password_span(text, i) or _sshpass_span(text, i)):
        return True
    k = end
    while k < n and text[k] == "\\":
        k += 1
    k = k + 1 if k < n and text[k] in "\"'" else end
    v = _skip_blanks(text, k)
    if v == k or _sep_at(text, v) or not is_credential_name(word):
        return False
    return _secret(text, _value_span(text, v, flag=True)) is not None


def _keyword_leaks(text: str, i: int) -> bool:
    """The forms that start at a keyword: netrc and SQL `PASSWORD`, `IDENTIFIED BY`, `set` /
    `export` / `setenv`, and the prose `is` (whose label is the word before it)."""
    if (end := _keyword_at(text, i, _NETRC_WORDS)) >= 0:
        if _netrc_span(text, i, end) or _quoted_password_span(text, end):
            return True
    if (end := _keyword_at(text, i, (_IDENTIFIED,))) >= 0:
        by = _skip_blanks(text, end)
        if (by_end := _keyword_at(text, by, (_BY,))) >= 0 and _identified_by_span(text, by_end):
            return True
    if (end := _keyword_at(text, i, _ASSIGN_WORDS)) >= 0:
        v = _skip_blanks(text, end)
        e = _word_end(text, v)
        if e > v and e < len(text) and text[e] in " \t" and is_credential_name(text[v:e]):
            if _word_value_span(text, _skip_blanks(text, e)):
                return True
    if i > 0 and text[i - 1] in " \t" and (end := _keyword_at(text, i, (_IS,))) >= 0:
        j = i
        while j > 0 and text[j - 1] in " \t":
            j -= 1
        start = _word_start(text, j)
        if start < j and is_credential_name(text[start:j]):
            if _word_value_span(text, _skip_blanks(text, end)):
                return True
    return False


def _element_leaks(text: str, i: int) -> bool:
    """The XML element form at a `<`."""
    end = _word_end(text, i + 1)
    if end == i + 1 or end >= len(text) or text[end] != ">":
        return False
    tag = text[i + 1 : end]
    return is_credential_name(tag) and _element_span(text, end + 1, tag) is not None


def labelled_value_leaks(text: str) -> bool:
    """True when ``text`` holds a credential label followed by a value (locator B, rules 0-2)."""
    text = normal_view(text)[0]
    for i in range(len(text)):
        if (sep := _sep_at(text, i)) and _sep_label_leaks(text, i, sep):
            return True
        ch = text[i]
        if ch == "-" and not _is_word_char(text, i - 1) and _flag_leaks(text, i):
            return True
        if ch in "pPiIsSeE" and _keyword_leaks(text, i):
            return True
        if ch == "<" and _element_leaks(text, i):
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
    # Credentials in a URL (rule 3 of the label model): scheme://user:password@host and
    # scheme://:password@host (the Redis shape). The userinfo runs to the LAST `@` before a blank
    # or quote, so the password may hold `/ ? # @` and the user `@`; a `:PORT` followed by `/ ? #`
    # is a port, and a user without a password is kept.
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:\"'<>]*:(?!\d{1,5}[/?#])[^\s\"'<>]*@"),
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

    Each break becomes the item separator `` · `` (see :func:`value_text`), never a plain space:
    ``--password`` and a value on the next line are not a match (a flag's value is on its line),
    and joining them with a space would make one the row never had. A separator no rule matches
    across keeps every match inside one of the original lines. (A label with no value on its line
    does take the next line's, but only in the row: the separator ends a value and a segment.)
    """
    return _ITEM_SEP.join(text.splitlines())


def clip(text: str, n: int) -> str:
    text = redact(text)
    return text if len(text) <= n else text[:n] + f"…[+{len(text) - n}]"


# Names of the participant's other tools can identify their setup (an internal MCP server) and are
# text no pattern should be trusted to cover, so the bundle carries an allowlist, not the name. The
# allowlist is the one harness tool vocabulary: the shell, read-only and JS-batch groups the
# extractor uses are named here (lower-case, the way `extract` compares them) and are part of it.
SHELL_TOOLS = frozenset(
    {"bash", "shell", "exec", "exec_command", "run_shell_command", "local_shell", "terminal"}
)
READONLY_TOOLS = frozenset({"read", "grep", "glob", "ls", "find"})
# Codex's code-mode tool: its input is a JavaScript batch that calls MCP tools as `tools.<name>(...)`.
JS_BATCH_TOOLS = frozenset({"exec"})
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

    A lone surrogate (a transcript string cut inside an emoji, which JSON allows as ``\\ud83d``) has
    no UTF-8 form: it is replaced by ``?`` first, so every shared string can be written.
    """
    if not text.isascii():
        text = text.encode("utf-8", "replace").decode("utf-8")
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
    agrees; so is the ``value`` member of a name/value pair whose name member names a credential
    (:func:`names_credential_pair`, judged on the object as written and as cleaned). A number whose
    canonical text (:func:`value_text`) would trip a rule becomes ``REDACTED``; every other number
    is kept as it is.
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
        if names_credential_pair(v.items()) or names_credential_pair(out.items()):
            for ck, x in out.items():
                if isinstance(ck, str) and _is_pair_key(ck, (PAIR_VALUE_KEY,)) and x is not None:
                    out[ck] = REDACTED
        return out
    return v


# ---- One canonical text form for every value a text sink (``report.md``) renders.
#
# A string is itself; null is empty; a boolean or number is its JSON literal; a list or object is
# its parts joined with separators that no rule can match across (each is whitespace, a symbol no
# rule takes as a label, value or path character, whitespace). So a rule match in the text of a
# value lies inside one of its strings, keys or numbers: if those pass the tripwire, so does the
# text. Python's ``str()`` / ``repr`` is never used: its ``\n`` escapes read as paths (``s:\n``).


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
    credential-named key, or under the ``value`` member of a name/value pair naming a credential
    (:func:`names_credential_pair`), must be ``REDACTED`` or null, whatever it looks like.
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
            pair = names_credential_pair(v)
            for k, x in v:
                if isinstance(k, str):
                    rules.extend(leaks_in(k))
                    named = is_credential_name(k) or (pair and _is_pair_key(k, (PAIR_VALUE_KEY,)))
                    if named and x is not None and x != REDACTED:
                        rules.append("credential field")
                stack.append(x)
        elif isinstance(v, list):
            stack.extend(v)
    return rules


# The markdown file rendered from rows: checked one table cell (or one other line) at a time.
_RENDERED_MD = "report.md"
_CELL_SEP_RE = re.compile(r"(?<!\\)\|")  # a cell boundary: the one `|` `md_cell` leaves unescaped


def report_units(text: str) -> list[str]:
    """The texts ``report.md`` is checked as: each table line split at its cell boundaries, every
    other line whole, each with the cell escape undone (:func:`md_decoded`). A cell is exactly one
    rendered value (``md_decoded(md_cell(t)) == one_line(t)``), so no match can join text from two
    values: a cell ending in `password:` and the next cell, or the next line."""
    units: list[str] = []
    for line in text.splitlines():
        cells = _CELL_SEP_RE.split(line) if line.startswith("|") else [line]
        units += [md_decoded(c) for c in cells]
    return units


def _file_leaks(name: str, text: str) -> list[str]:
    if name == _RENDERED_MD:
        # Its content is pinned to the rows (`sharedir.check_share` re-renders it byte for byte),
        # and the rows are checked whole; here every value is checked in the unit it renders as.
        return [r for unit in report_units(text) for r in leaks_in(unit)]
    if name.endswith(".md"):
        # Any other markdown (SCHEMA.md: text the extractor writes, kept as it is) is checked whole,
        # as sanitize reads a text: a value on the line after its label is a value.
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
