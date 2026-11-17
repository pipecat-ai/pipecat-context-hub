"""The one place text becomes safe to leave the machine.

Everything a share bundle carries that came from a participant's transcript (queries, argument
names and values, hit ids, error text, version strings) goes through :func:`sanitize` at write time,
and the finished files are checked by :func:`verify_share`, which fails closed. Sanitising at each
call site that happens to touch text is how holes appeared (dict keys, the report table); a single
egress plus a tripwire means a new field or sink is covered, or the build refuses to finish.

Redaction is a denylist of known shapes and stays best-effort. This module makes it *complete over
sinks*, not over secrets: the participant still reads the report before sending anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Secrets must never reach the output files. Over-redact rather than under-redact.
# `\b` treats `_` as a word character, so `key_sk-...`, `mcp__sk-...` and `API_KEY_<hex>` have no
# boundary and would escape. A secret is delimited by anything that is not a letter or digit.
_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"
SECRET_RES = [
    re.compile(_B + r"(sk|pk|rk|dg|gsk|hf|xox[abprs]|ghp|gho|github_pat)[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)" + _B + r"(bearer|token)\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\b([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*[=:]\s*\S{8,}"),
    # Bare hex keys (Deepgram 40, Twilio/AssemblyAI 32, Daily 64) and git SHAs alike: a commit
    # SHA in a query is worth nothing, a key that escapes is worth a lot.
    re.compile(_B + r"[0-9a-fA-F]{32,}" + _E),
    re.compile(_B + r"AKIA[0-9A-Z]{16}" + _E),  # AWS access key id
    re.compile(_B + r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(_B + r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
HOME_RE = re.compile(r"/(Users|home)/[^/\s\"']+")
WIN_HOME_RE = re.compile(r"([A-Za-z]:\\{1,2}Users\\{1,2})[^\\/\s\"']+")


SHARE_PATH_RE = re.compile(
    r"(?:/(?:Users|home|private|tmp|var|mnt|opt|root|Volumes|etc)/[^\s\"'`;|)\]]+|[A-Za-z]:\\[^\s\"'`;|)\]]+)"
)


def scrub_paths(v: Any) -> Any:
    """Replace local absolute paths in every string of a share row (doc paths like /guides/x stay)."""
    return map_strings(v, lambda text: SHARE_PATH_RE.sub("<path>", text))


def redact(text: str) -> str:
    text = HOME_RE.sub(r"/\1/<user>", text)
    text = WIN_HOME_RE.sub(r"\1<user>", text)
    for rx in SECRET_RES:
        text = rx.sub("<redacted>", text)
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


def clean_text(text: str) -> str:
    """Redact secrets and home paths, then replace local absolute paths. Idempotent."""
    return SHARE_PATH_RE.sub("<path>", redact(text))


def sanitize(v: Any) -> Any:
    """``v`` with every string (and dict key) cleaned: the egress for every share JSON row."""
    return map_strings(v, clean_text)


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


def verify_share(share: Path, names: tuple[str, ...]) -> list[str]:
    """``["file: rule", ...]`` for every bundle file that trips a leak rule; empty when clean."""
    found: list[str] = []
    for n in names:
        f = share / n
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        found.extend(f"{n}: {rule}" for rule in leaks_in(text))
    return found
