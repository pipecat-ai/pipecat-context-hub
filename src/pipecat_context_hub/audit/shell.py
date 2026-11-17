"""Which commands a shell line actually runs: the hub CLI calls in an agent's shell input.

Moved out of ``extract.py`` unchanged. Stdlib only; imports nothing from the rest of ``audit``.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

# ---- shell: which commands a shell line actually runs
#
# A hub CLI call is a command whose argv starts with the hub binary, after any NAME=value
# assignments and launcher words (`uv run`, `uvx --from pkg`, `env`, `timeout 30`). The line is
# tokenised shell-style first: quotes and escapes are honoured, comments dropped, control operators
# (`; & | && || ( )` and newlines) separate commands, redirections and their targets are skipped,
# and `$(...)`, backticks, `bash -c "..."` and `eval` scripts are parsed as the commands they run.
# So `printf '%s' 'pipecat-context-hub search-docs x'` is not a call (the hub is an argument), and
# `search-docs "TTS & STT"` keeps its whole query. A line that cannot be tokenised (an unterminated
# quote) fails closed: the call is still counted by the regex, but no command text is shared.

CLI_SUBCOMMANDS = frozenset(
    {"search-api", "search-docs", "search-examples", "get-doc", "get-example",
     "get-code-snippet", "check-deprecation", "status"}
)  # fmt: skip
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# Words that may precede the hub binary in the command that runs it.
_LAUNCHERS = frozenset(
    {"uv", "uvx", "run", "tool", "pipx", "env", "time", "timeout", "nice", "nohup", "command",
     "exec", "sudo", "xargs", "{", "!", "if", "then", "else", "elif", "do", "while", "until"}
)  # fmt: skip
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_DURATION_RE = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_SHELL_C_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
_REDIRECT_RE = re.compile(r"\d*(?:>>|>&|>\||<<<|<<-|<<|<&|<>|>|<)|&>>|&>")
_CONTROL_RE = re.compile(r"\|\||&&|;;|\|&|[|&;()\n]")
_DQ_SUBST_RE = re.compile(r"\$\(|`")
_MAX_SHELL_DEPTH = 4


class ShellSyntaxError(ValueError):
    """A shell line that cannot be tokenised (unterminated quote or substitution)."""


def _read_balanced(text: str, i: int, close: str) -> int:
    """Index just past the ``close`` that ends a substitution body starting at ``i`` (quote-aware)."""
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
        if close == ")" and ch == "(":
            depth += 1
        elif ch == close:
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


def _shell_segments(text: str) -> tuple[list[list[str]], list[str]]:
    """(simple commands as argv lists, scripts nested in substitutions) of one shell line."""
    segments: list[list[str]] = []
    nested: list[str] = []
    argv: list[str] = []
    word: list[str] | None = None
    skip_target = False  # the next word is a redirection target
    heredocs: list[tuple[str, bool]] = []  # (delimiter, strip leading tabs)
    want_delim: bool | None = None  # the next word is a here-document delimiter

    def add(part: str) -> None:
        nonlocal word
        if word is None:
            word = []
        word.append(part)

    def end_word() -> None:
        nonlocal word, skip_target, want_delim
        if word is None:
            return
        w = "".join(word)
        word = None
        if want_delim is not None:
            heredocs.append((w, want_delim))
            want_delim = None
        elif skip_target:
            skip_target = False
        else:
            argv.append(w)

    def end_segment() -> None:
        nonlocal argv
        end_word()
        if argv:
            segments.append(argv)
        argv = []

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
            add(text[i + 1 : i + 2])
            i += 2
        elif ch == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise ShellSyntaxError("unterminated quote")
            add(text[i + 1 : end])
            i = end + 1
        elif ch == '"':
            end = _dquote_end(text, i + 1)
            if end < 0:
                raise ShellSyntaxError("unterminated quote")
            body = text[i + 1 : end]
            j = 0
            while (m := _DQ_SUBST_RE.search(body, j)) is not None:  # substitutions run in "..." too
                close = ")" if m.group(0) == "$(" else "`"
                k = _read_balanced(body, m.end(), close)
                nested.append(body[m.end() : k - 1])
                j = k
            add(re.sub(r'\\([$`"\\\n])', r"\1", body))
            i = end + 1
        elif ch == "$" and text[i + 1 : i + 2] == "(":
            end = _read_balanced(text, i + 2, ")")
            nested.append(text[i + 2 : end - 1])
            add(text[i:end])
            i = end
        elif ch == "`":
            end = _read_balanced(text, i + 1, "`")
            nested.append(text[i + 1 : end - 1])
            add(text[i:end])
            i = end
        elif (m := _REDIRECT_RE.match(text, i)) and (word is None or not m.group(0)[0].isdigit()):
            end_word()
            op = m.group(0)
            if op.endswith(("<<", "<<-")) and not op.endswith("<<<"):
                want_delim = op.endswith("-")
            else:  # the target (a file, an fd, a here-string) is never an argument
                skip_target = True
            i = m.end()
        elif m := _CONTROL_RE.match(text, i):
            end_segment()
            i = m.end()
            if m.group(0) == "\n" and heredocs:  # here-document bodies are data, not commands
                for delim, tabs in heredocs:
                    while i < n:
                        nl = text.find("\n", i)
                        line = text[i : n if nl < 0 else nl]
                        i = n if nl < 0 else nl + 1
                        if (line.lstrip("\t") if tabs else line) == delim:
                            break
                heredocs = []
        else:
            add(ch)
            i += 1
    end_segment()
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
        return " ".join(argv[i + 1 :])
    if _basename(argv[i]) in _SHELLS:
        for j in range(i + 1, len(argv) - 1):
            if _SHELL_C_RE.fullmatch(argv[j]):
                return argv[j + 1]
    return None


def shell_hub_calls(script: str, depth: int = 0) -> list[list[str]]:
    """Every hub CLI call ``script`` runs, as canonical argv. Raises ShellSyntaxError."""
    if depth > _MAX_SHELL_DEPTH:
        return []
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


def shell_source(inp: Any) -> str | None:
    """A shell call's script (an argv list is re-quoted, so its words stay words), else None."""
    if isinstance(inp, dict):
        cmd = inp.get("command") or inp.get("cmd")
        if isinstance(cmd, list) and all(isinstance(part, str) for part in cmd):
            return shlex.join(cmd)
        return cmd if isinstance(cmd, str) else None
    return inp if isinstance(inp, str) else None
