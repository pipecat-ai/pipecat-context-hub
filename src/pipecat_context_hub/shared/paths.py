"""Home-path redaction helpers for user-facing log/stderr output.

Startup telemetry and one-shot CLI error messages are included in the "share
this with maintainers" guidance, so stripping usernames out of absolute paths
keeps routine bug reports from leaking local filesystem layout.

Two helpers, because the leak has two shapes:

- :func:`redact_home` — the *whole argument* is a path (banner / cache-dir
  sites). A prefix match suffices.
- :func:`redact_home_in_text` — the path is embedded *inside* an arbitrary
  message string (e.g. a ``{exc}`` rendering whose ``__str__`` carries an
  absolute path). A prefix-only helper cannot redact this, so the home dir is
  matched and replaced wherever it appears in the text.

Lives in ``shared/`` (not ``cli.py``) because ``cli.py`` imports ``cli_query``;
importing the helper back from ``cli.py`` would be a cycle. ``shared/`` peers
import neither CLI module, so the layer stays leaf-level.
"""

from __future__ import annotations

import os
from pathlib import Path


def redact_home(path: Path | str) -> str:
    """Replace the user's home-directory prefix with ``~`` for logs.

    Startup telemetry is included in the "share this with maintainers"
    guidance, so stripping usernames out of absolute paths keeps routine
    bug reports from leaking local filesystem layout.
    """
    try:
        s = str(path)
        home = str(Path.home())
        if home and (s == home or s.startswith(home + os.sep)):
            return "~" + s[len(home) :]
        return s
    except Exception:
        return str(path)


def redact_home_in_text(text: str) -> str:
    """Replace home-directory occurrences embedded anywhere in ``text`` with ``~``.

    Unlike :func:`redact_home` (whole-arg-is-a-path, prefix match), this handles
    a path embedded mid-message — e.g. an absolute path inside a ``{exc}``
    rendering such as ``"Error: ... '/Users/x/.../chroma.sqlite3'"``.

    Guards against partial-username over-match: a naive ``text.replace(home,
    "~")`` would corrupt ``/Users/xavier`` when home is ``/Users/x``. Only the
    exact ``home`` token and the ``home + os.sep`` prefix-within-a-path form are
    replaced. Returns the original text on any failure, mirroring
    :func:`redact_home`'s defensive style.
    """
    try:
        home = str(Path.home())
        if not home:
            return text
        # Replace `home + sep` (a path continuing past home — the common case)
        # with `~/`. Anchoring on the trailing separator avoids over-matching a
        # longer sibling like `/Users/xavier` when home is `/Users/x`: that
        # path has no separator right after `home`, so it is left intact.
        result = text.replace(home + os.sep, "~" + os.sep)
        # Handle a bare `home` occurrence (no trailing separator) only when it
        # is the exact whole message — the `== home` case. A raw substring
        # replace here would reintroduce the partial-username over-match, so it
        # is deliberately scoped to equality.
        if result == home:
            result = "~"
        return result
    except Exception:
        return text
