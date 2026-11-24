"""The one message `refresh` logs when a repo cannot be cloned or fetched.

GitHub answers "not found" for a repo that does not exist *and* for a private repo
the caller cannot see, so the two cannot be told apart from outside. When the failure
looks like that (or like any other refusal by GitHub), the message adds a hint stating
the one thing the user controls: the hub clones with the GitHub account in this
environment, so it can only index repos that account can read.

Both clone sites (``cli.py``'s refresh loop and ``github_ingest.py``'s ingester) call
:func:`clone_failure_message`; neither composes the message itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from pipecat_context_hub.shared.terminal import shown

# Substrings of a failed git clone/fetch that mean "GitHub would not show us this
# repo" (404 masquerading for private, bad/absent credentials, token scope).
#
# A bare "permission denied" is deliberately NOT a marker: git and the filesystem
# say it for a local failure too (`fatal: could not create work tree dir '...':
# Permission denied`, `[Errno 13] Permission denied: '.../repos'`), which this hint
# would misdiagnose as a GitHub account problem. GitHub's own wording is
# "Permission to <repo> denied to <user>" (matched below, a token without access), and
# ssh's is "Permission denied (publickey)".
_ACCESS_MARKERS: Final = (
    "repository not found",
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "permission denied (publickey",
    "returned error: 401",
    "returned error: 403",
    "returned error: 404",
)
_ACCESS_RE: Final = re.compile(
    "|".join(
        (
            *(re.escape(m) for m in _ACCESS_MARKERS),
            # `fatal: repository '<url>' not found`, which git prints without the
            # preceding `remote: Repository not found.` line when the server (a
            # proxy, GitHub Enterprise) does not send one. Anchored on the quote
            # so it cannot match "Tag 'x' not found in repository".
            r"repository '[^']*' not found",
            # `remote: Permission to <repo> denied to <user>.`
            r"permission to \S+ denied to",
        )
    ),
    re.IGNORECASE,
)

# How far down an exception chain to look. The deepest real chain today is two
# (`ValueError("Could not verify tag ...") from GitCommandError`).
_MAX_CHAIN: Final = 8


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and what caused it, in the order a traceback prints them, cycle-safe."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < _MAX_CHAIN:
        seen.add(id(current))
        yield current
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )


def _git_said(exc: BaseException) -> str | None:
    """The text of ``exc`` that can carry GitHub's answer, or ``None`` if none can.

    An ``OSError`` is a local failure (disk, permissions, a missing binary), never
    GitHub's answer, and its text names local paths that can hold any words. A git
    ``CommandError`` (``GitCommandError`` is not an ``OSError``) is read through its
    stderr only: its ``str()`` also carries the command line, which names the local
    clone directory.
    """
    if isinstance(exc, OSError):
        return None
    from git.exc import CommandError  # deferred: keeps this module a leaf at import

    if isinstance(exc, CommandError):
        return str(exc.stderr)
    return str(exc)


def _looks_like_access_failure(exc: BaseException) -> bool:
    return any(
        text is not None and _ACCESS_RE.search(text) is not None
        for text in map(_git_said, _chain(exc))
    )


def _access_hint(slug: str) -> str:
    return (
        f"{shown(slug)} could not be read. The hub clones with the GitHub account in this "
        "environment (git's credential helper, e.g. gh after `gh auth setup-git`), so it "
        "can only index repos that account can read; GitHub shows a private repo you "
        "cannot access as 'not found'. Check `gh auth status` and that the account can "
        "open the repo in a browser. For a token, prefer a fine-grained one with "
        "read-only Contents access to just this repo (classic `repo` scope also works, "
        "but grants read and write on every private repo you can reach), authorised for "
        "the org if it uses SSO; a fine-grained token may not see an org repo where you "
        "are only an outside collaborator. If the repo is public, check the spelling."
    )


def clone_failure_message(slug: str, exc: BaseException) -> str:
    """``Failed to clone/fetch <slug>: <exc>``, plus the access hint when it applies.

    The hint applies when ``exc`` or anything in its cause/context chain that is not
    an ``OSError`` carries one of GitHub's refusal texts. The first line is the same
    with or without it; the hint, when present, follows on an indented line.
    """
    message = f"Failed to clone/fetch {shown(slug)}: {exc}"
    if _looks_like_access_failure(exc):
        message += f"\n  {_access_hint(slug)}"
    return message
