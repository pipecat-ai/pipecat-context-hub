"""Which commands a shell line actually runs: the hub CLI calls in an agent's shell input.

Stdlib only; imports nothing from the rest of ``audit``. Also the one home of the hub's tool
vocabulary (:data:`HUB_TOOL_SUBCOMMANDS`): ``extract`` derives every table and regex that names a
hub tool or CLI subcommand from it.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

# The hub's tools and the CLI subcommand each one is reachable as: the one vocabulary. `extract`
# derives HUB_TOOLS, CLI_RE, MCP_RE and the CLI-to-tool mapping from it; a test pins the result
# key table and the search-tool set to these names.
HUB_TOOL_SUBCOMMANDS = {
    "search_docs": "search-docs",
    "search_examples": "search-examples",
    "search_api": "search-api",
    "get_doc": "get-doc",
    "get_example": "get-example",
    "get_code_snippet": "get-code-snippet",
    "check_deprecation": "check-deprecation",
    "get_hub_status": "status",
}
CLI_SUBCOMMANDS = frozenset(HUB_TOOL_SUBCOMMANDS.values())
CLI_TO_TOOL = {sub: tool for tool, sub in HUB_TOOL_SUBCOMMANDS.items()}

# ---- shell: which commands a shell line actually runs
#
# A hub CLI call is a command whose argv starts with the hub binary, after any NAME=value
# assignments and launcher words (`uv run`, `uvx --from pkg`, `env`, `timeout 30`). The line is
# tokenised shell-style first: quotes and escapes are honoured, comments dropped, control operators
# (`; & | && || ( )` and newlines) separate commands, redirections and their targets are skipped,
# and `$(...)`, backticks, `<(...)`, `>(...)`, `bash -c "..."`, `eval` and a here-document fed to a
# shell are parsed as the commands they run. So `printf '%s' 'pipecat-context-hub search-docs x'`
# is not a call (the hub is an argument), and `search-docs "TTS & STT"` keeps its whole query.
#
# Fail closed. A line is either fully understood or it is not parsed at all: any construct the
# tokenizer does not model (ANSI-C `$'...'`, `$"..."`, `${...}` other than a bare `${NAME}`,
# arithmetic `$((...))` / `((...))` / `$[...]`, brace expansion, a backslash inside backticks, a
# shell reading its script from a pipe, file or here-string, a here-document whose unquoted body
# the outer shell would expand into code, a `(` that does not start a command, anything
# unterminated or unbalanced) raises ShellSyntaxError. The caller then still counts the call by
# regex, but shares no command text from the line.

_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# Words that may precede the hub binary in the command that runs it.
_LAUNCHERS = frozenset(
    {"uv", "uvx", "run", "tool", "pipx", "env", "time", "timeout", "nice", "nohup", "command",
     "exec", "sudo", "xargs", "{", "!", "if", "then", "else", "elif", "do", "while", "until"}
)  # fmt: skip
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_DURATION_RE = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_SHELL_C_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
_SHELL_S_RE = re.compile(r"-[A-Za-z]*s[A-Za-z]*")
_REDIRECT_RE = re.compile(r"\d*(?:>>|>&|>\||<<<|<<-|<<|<&|<>|>|<)|&>>|&>")
_CONTROL_RE = re.compile(r"\|\||&&|;;|\|&|[|&;\n]")
_SIMPLE_PARAM_RE = re.compile(r"\$\{(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]|[@*#?$!-])\}")
_BRACE_EXPANSION_RE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_MAX_SHELL_DEPTH = 4


class ShellSyntaxError(ValueError):
    """A shell line the tokenizer does not fully understand (see "Fail closed" above)."""


class _Expanded(str):
    """A word holding a command or process substitution: its real value is that command's output,
    so its text is never shared (it is another command's text, not an argument)."""


def _read_balanced(text: str, i: int) -> int:
    """Index just past the ``)`` that ends a ``$(`` / ``<(`` body starting at ``i`` (quote-aware)."""
    depth = 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch in "'\"":
            end = text.find(ch, i + 1) if ch == "'" else _dquote_end(text, i + 1)
            if end < 0:
                raise ShellSyntaxError("unterminated quote")
            i = end + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ShellSyntaxError("unterminated substitution")


def _dquote_end(text: str, i: int) -> int:
    """Index of the ``"`` closing a double-quoted string whose body starts at ``i``, or -1."""
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == '"':
            return i
        else:
            i += 1
    return -1


def _backtick_body(text: str, i: int) -> tuple[str, int]:
    """(body, index past the closing backtick) of a backtick substitution whose body starts at ``i``.

    The body ends at the first unescaped backtick, quotes or not (as bash reads it). Inside
    backticks a backslash is de-escaped before the inner command runs (``\\``` nests another
    substitution), which is not modelled: a body holding a backslash fails closed.
    """
    end = text.find("`", i)
    if end < 0:
        raise ShellSyntaxError("unterminated backtick")
    body = text[i:end]
    if "\\" in body:
        raise ShellSyntaxError("backslash inside backticks")
    return body, end + 1


def _dollar(text: str, i: int, nested: list[str]) -> tuple[str, int, bool]:
    """(text, end, is_substitution) of the ``$`` construct at ``i``; raises on an unmodelled one."""
    if text.startswith("$((", i):
        raise ShellSyntaxError("arithmetic expansion")
    nxt = text[i + 1 : i + 2]
    if nxt == "(":
        end = _read_balanced(text, i + 2)
        nested.append(text[i + 2 : end - 1])
        return text[i:end], end, True
    if nxt == "{":
        m = _SIMPLE_PARAM_RE.match(text, i)
        if m is None:
            raise ShellSyntaxError("parameter expansion")
        return m.group(0), m.end(), False
    if nxt == "[":
        raise ShellSyntaxError("arithmetic expansion")
    return "$", i + 1, False


def _read_dquote(text: str, i: int, nested: list[str]) -> tuple[str, int, bool]:
    """(value, index past the closing quote, holds a substitution) of ``"..."`` whose body starts at
    ``i``. Substitutions run inside double quotes too; an escaped ``\\$`` or ``\\``` does not."""
    j, expanded = i, False
    while j < len(text):
        ch = text[j]
        if ch == "\\":
            j += 2
        elif ch == '"':
            body = re.sub(r"\\\n", "", text[i:j])
            return re.sub(r'\\([$`"\\])', r"\1", body), j + 1, expanded
        elif ch == "$":
            _, j, sub = _dollar(text, j, nested)
            expanded = expanded or sub
        elif ch == "`":
            body, j = _backtick_body(text, j + 1)
            nested.append(body)
            expanded = True
        else:
            j += 1
    raise ShellSyntaxError("unterminated quote")


def _has_live_substitution(body: str) -> bool:
    """True when the outer shell would run a command while expanding an unquoted here-document."""
    j = 0
    while j < len(body):
        if body[j] == "\\":
            j += 2
        elif body.startswith("$(", j) or body[j] == "`":
            return True
        else:
            j += 1
    return False


def _shell_segments(text: str) -> tuple[list[list[str]], list[str]]:
    """(simple commands as argv lists, scripts nested in substitutions) of one shell line.

    Raises ShellSyntaxError for anything not fully understood (see "Fail closed" above).
    """
    segments: list[list[str]] = []
    nested: list[str] = []
    argv: list[str] = []
    word: list[str] | None = None
    quoted = False  # part of the current word was quoted or escaped
    expanded = False  # the current word holds a command or process substitution
    bare: list[str] = []  # the current word's unquoted characters (brace expansion check)
    skip_target = False  # the next word is a redirection target
    stdin_redirected = False  # this segment's stdin comes from something other than a heredoc
    # Pending here-documents: (delimiter, strip leading tabs, delimiter quoted, its command's argv).
    heredocs: list[tuple[str, bool, bool, list[str]]] = []
    want_delim: bool | None = None  # the next word is a here-document delimiter (strip tabs?)
    depth = 0  # open `(` subshells

    def add(part: str, *, is_quoted: bool = False, is_bare: bool = False) -> None:
        nonlocal word, quoted
        if word is None:
            word = []
        word.append(part)
        quoted = quoted or is_quoted
        if is_bare:
            bare.append(part)

    def end_word() -> None:
        nonlocal word, skip_target, want_delim, quoted, expanded
        if word is None:
            return
        if _BRACE_EXPANSION_RE.search("".join(bare)):
            raise ShellSyntaxError("brace expansion")
        w = "".join(word)
        word, was_quoted, was_expanded = None, quoted, expanded
        quoted = expanded = False
        bare.clear()
        if want_delim is not None:
            heredocs.append((w, want_delim, was_quoted, argv))
            want_delim = None
        elif skip_target:
            skip_target = False
        else:
            argv.append(_Expanded(w) if was_expanded else w)

    def end_segment() -> None:
        nonlocal argv, stdin_redirected
        end_word()
        if skip_target or want_delim is not None:
            raise ShellSyntaxError("redirection without a target")
        if argv:
            has_heredoc = any(h[3] is argv for h in heredocs)
            if _reads_script_from_stdin(argv) and (stdin_redirected or not has_heredoc):
                raise ShellSyntaxError("a shell reads its script from stdin")
            segments.append(argv)
        argv = []
        stdin_redirected = False

    def read_heredocs(i: int) -> int:
        for delim, tabs, delim_quoted, owner in heredocs:
            lines: list[str] = []
            while True:
                if i >= len(text):
                    raise ShellSyntaxError("unterminated here-document")
                nl = text.find("\n", i)
                line = text[i : len(text) if nl < 0 else nl]
                i = len(text) if nl < 0 else nl + 1
                if (line.lstrip("\t") if tabs else line) == delim:
                    break
                lines.append(line)
            body = "\n".join(lines)
            if _reads_script_from_stdin(owner):  # the body is the script the shell runs
                if not delim_quoted and any(c in body for c in "$`\\"):
                    raise ShellSyntaxError("a here-document script the outer shell expands")
                nested.append(body)
            elif not delim_quoted and _has_live_substitution(body):
                raise ShellSyntaxError("a here-document whose expansion runs a command")
        heredocs.clear()
        return i

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r":
            end_word()
            i += 1
        elif ch == "#" and word is None:  # a comment runs to the end of the line
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
        elif ch == "\\":
            if text[i + 1 : i + 2] == "\n":  # line continuation
                i += 2
                continue
            add(text[i + 1 : i + 2], is_quoted=True)
            i += 2
        elif ch == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise ShellSyntaxError("unterminated quote")
            add(text[i + 1 : end], is_quoted=True)
            i = end + 1
        elif ch == '"':
            value, i, sub = _read_dquote(text, i + 1, nested)
            add(value, is_quoted=True)
            expanded = expanded or sub
        elif ch == "$":
            if text[i + 1 : i + 2] in ("'", '"'):
                raise ShellSyntaxError("ANSI-C or locale quoting")
            part, i, sub = _dollar(text, i, nested)
            add(part, is_bare=not sub)
            expanded = expanded or sub
        elif ch == "`":
            body, end = _backtick_body(text, i + 1)
            nested.append(body)
            add(text[i:end])
            expanded = True
            i = end
        elif ch in "<>" and text[i + 1 : i + 2] == "(":  # process substitution, not a redirect
            end = _read_balanced(text, i + 2)
            nested.append(text[i + 2 : end - 1])
            add(text[i:end])
            expanded = True
            i = end
        elif (
            (m := _REDIRECT_RE.match(text, i))
            and (word is None or not m.group(0)[0].isdigit())
            # `2>(cmd)` is the word `2` and a process substitution, as bash reads it
            and not (
                m.group(0).lstrip("0123456789") in ("<", ">") and text[m.end() : m.end() + 1] == "("
            )
        ):
            end_word()
            op = m.group(0)
            if op.endswith(("<<", "<<-")) and not op.endswith("<<<"):
                want_delim = op.endswith("-")
            else:  # the target (a file, an fd, a here-string) is never an argument
                skip_target = True
                if op.lstrip("0123456789").startswith("<") and op[:1] in "<0":
                    stdin_redirected = True  # `<`, `<<<`, `<&`, `<>`, `0<`: not a heredoc
            i = m.end()
        elif ch == "(":
            if word is not None or argv or skip_target or want_delim is not None:
                raise ShellSyntaxError("`(` that does not start a command")
            if text[i + 1 : i + 2] == "(":
                raise ShellSyntaxError("arithmetic command")
            depth += 1
            i += 1
        elif ch == ")":
            if depth == 0:
                raise ShellSyntaxError("unbalanced `)`")
            end_segment()
            depth -= 1
            i += 1
        elif m := _CONTROL_RE.match(text, i):
            if m.group(0) == "|" or m.group(0) == "|&":
                end_segment()
                stdin_redirected = True  # the next command reads the pipe
            else:
                end_segment()
            i = m.end()
            if m.group(0) == "\n" and heredocs:
                i = read_heredocs(i)
        else:
            add(ch, is_bare=True)
            i += 1
    end_segment()
    if heredocs:
        raise ShellSyntaxError("unterminated here-document")
    if depth:
        raise ShellSyntaxError("unbalanced `(`")
    return segments, nested


def _basename(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _hub_head(argv: list[str], i: int) -> int:
    """How many words of ``argv`` from ``i`` name the hub binary followed by a subcommand, else 0."""
    if _basename(argv[i]) == "pipecat-context-hub":
        return 1 if argv[i + 1 : i + 2] and argv[i + 1] in CLI_SUBCOMMANDS else 0
    if _basename(argv[i]) == "pipecat" and argv[i + 1 : i + 2] == ["context-hub"]:
        return 2 if argv[i + 2 : i + 3] and argv[i + 2] in CLI_SUBCOMMANDS else 0
    return 0


def _command_start(argv: list[str]) -> int:
    """Index of the command word: past NAME=value assignments and launcher words, or len(argv)."""
    i = 0
    while i < len(argv):
        w = argv[i]
        if _hub_head(argv, i):
            return i
        launcher_arg = i > 0 and (
            w.startswith("-")
            or _DURATION_RE.fullmatch(w) is not None
            or (argv[i - 1].startswith("--") and "=" not in argv[i - 1])  # `uvx --from pkg`
        )
        if _ASSIGN_RE.fullmatch(w) or _basename(w) in _LAUNCHERS or launcher_arg:
            i += 1
            continue
        return i
    return i


def _reads_script_from_stdin(argv: list[str]) -> bool:
    """True when ``argv`` runs a shell that reads its script from stdin (no ``-c``, no script file,
    or ``-s``): what it runs is whatever feeds its stdin."""
    i = _command_start(argv)
    if i >= len(argv) or _basename(argv[i]) not in _SHELLS:
        return False
    args = argv[i + 1 :]
    if any(_SHELL_C_RE.fullmatch(a) for a in args):
        return False
    if any(_SHELL_S_RE.fullmatch(a) for a in args):
        return True
    j = 0
    while j < len(args):
        if args[j] in ("-o", "+o", "-O", "+O"):
            j += 2
        elif args[j].startswith(("-", "+")):
            j += 1
        else:
            return False  # a script file: its stdin is data
    return True


def _hub_argv(argv: list[str]) -> list[str] | None:
    """The hub CLI call ``argv`` runs, canonicalised (no path, no launcher), or None."""
    i = _command_start(argv)
    if i >= len(argv) or not (k := _hub_head(argv, i)):
        return None
    head = ["pipecat-context-hub"] if k == 1 else ["pipecat", "context-hub"]
    return head + argv[i + k :]


def _nested_script(argv: list[str]) -> str | None:
    """The script a `bash -c SCRIPT` or `eval ...` command runs, else None."""
    i = _command_start(argv)
    if i >= len(argv):
        return None
    if _basename(argv[i]) == "eval":
        words = argv[i + 1 :]
    elif _basename(argv[i]) in _SHELLS:
        words = []
        for j in range(i + 1, len(argv) - 1):
            if _SHELL_C_RE.fullmatch(argv[j]):
                words = [argv[j + 1]]
                break
        if not words:
            return None
    else:
        return None
    if any(isinstance(w, _Expanded) for w in words):  # the script is another command's output
        raise ShellSyntaxError("a script built by a substitution")
    return " ".join(words)


def shell_hub_calls(script: str, depth: int = 0) -> list[list[str]]:
    """Every hub CLI call ``script`` runs, as canonical argv. Raises ShellSyntaxError."""
    if depth > _MAX_SHELL_DEPTH:
        raise ShellSyntaxError("nested too deeply")
    segments, nested = _shell_segments(script)
    calls: list[list[str]] = []
    for argv in segments:
        hub = _hub_argv(argv)
        if hub is not None:
            calls.append(hub)
            continue
        inner = _nested_script(argv)
        if inner is not None:
            calls.extend(shell_hub_calls(inner, depth + 1))
    for inner in nested:
        calls.extend(shell_hub_calls(inner, depth + 1))
    return calls


def literal_argv(argv: list[str]) -> list[str] | None:
    """``argv`` as plain literal words, or None when a word is a substitution's output (its text
    is another command's, not an argument, so the call's text is never shared)."""
    if any(isinstance(w, _Expanded) for w in argv):
        return None
    return [str(w) for w in argv]


def shell_source(inp: Any) -> str | None:
    """A shell call's script (an argv list is re-quoted, so its words stay words), else None."""
    if isinstance(inp, dict):
        cmd = inp.get("command") or inp.get("cmd")
        if isinstance(cmd, list) and all(isinstance(part, str) for part in cmd):
            return shlex.join(cmd)
        return cmd if isinstance(cmd, str) else None
    return inp if isinstance(inp, str) else None
