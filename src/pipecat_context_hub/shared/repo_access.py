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

# The patterns, matched case-insensitively, that mean "GitHub would not show us this
# repo" (404 masquerading for private, bad/absent credentials, token scope, SSO).
# This tuple is the one source: `_ACCESS_RE` is compiled from it and the tests
# check each pattern against its own case.
#
# A bare "permission denied" is deliberately NOT a pattern: git and the filesystem
# say it for a local failure too (`fatal: could not create work tree dir '...':
# Permission denied`, `[Errno 13] Permission denied: '.../repos'`), which this hint
# would misdiagnose as a GitHub account problem. GitHub's own wording is
# "Permission to <repo> denied to <user>" (a token without access), and ssh's is
# "Permission denied (publickey)".
_ACCESS_PATTERNS: Final = (
    re.escape("repository not found"),
    re.escape("authentication failed"),
    re.escape("could not read username"),
    re.escape("could not read password"),
    re.escape("terminal prompts disabled"),
    re.escape("permission denied (publickey"),
    re.escape("returned error: 401"),
    re.escape("returned error: 403"),
    re.escape("returned error: 404"),
    # `The '<org>' organization has enabled or enforced SAML SSO.`: over SSH this is
    # the only refusal text (over HTTPS a 403 follows it).
    re.escape("enabled or enforced saml sso"),
    # `fatal: repository '<url>' not found`, which git prints without the
    # preceding `remote: Repository not found.` line when the server (a
    # proxy, GitHub Enterprise) does not send one. Anchored on the quote
    # so it cannot match "Tag 'x' not found in repository".
    r"repository '[^']*' not found",
    # `remote: Permission to <repo> denied to <user>.`
    r"permission to \S+ denied to",
)
_ACCESS_RE: Final = re.compile("|".join(_ACCESS_PATTERNS), re.IGNORECASE)

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


def _github_said(exc: BaseException) -> str:
    """The text in ``exc`` that can be GitHub's answer: only git's stderr.

    Only a git ``CommandError`` holds text git received from the remote, so any
    other exception contributes nothing: an ``OSError`` names local paths, and a
    ``ValueError`` such as ``Invalid repo slug: <slug>`` echoes configured input,
    either of which can hold any words. A wrapper whose cause is a git failure
    still counts, through that cause in the chain. The stderr is read without the
    command's last argument (the clone target, a local directory), because git
    repeats it there (``fatal: could not create work tree dir '<target>': ...``);
    ``str()`` is not read at all, since it also carries the command line.

    Residual: a ``git fetch`` in an existing clone names that clone's directory
    in some local errors (a lock file, say), and the directory is not in the
    exception, so a refusal phrase inside the data-dir path can still match there.
    """
    from git.exc import CommandError  # deferred: keeps this module a leaf at import

    if not isinstance(exc, CommandError):
        return ""
    stderr = str(exc.stderr)
    target = str(exc.command[-1]) if exc.command else ""
    return stderr.replace(target, "") if target else stderr


def _looks_like_access_failure(exc: BaseException) -> bool:
    return any(_ACCESS_RE.search(_github_said(link)) is not None for link in _chain(exc))


def _access_hint(slug: str) -> str:
    return (
        f"{shown(slug)} could not be read. The hub clones with the GitHub account in this "
        "environment (git's credential helper, e.g. gh after `gh auth setup-git`), so it "
        "can only index repos that account can read; GitHub shows a private repo you "
        "cannot access as 'not found'. Check `gh auth status` and that the account can "
        "open the repo in a browser. For a token, prefer a fine-grained one with "
        "read-only Contents access to just this repo (classic `repo` scope also works, "
        "but grants the account's full access to every private repo it can reach, "
        "including write where it has write), authorised for the org if it uses SSO; a "
        "fine-grained token may not see an org repo where you are only an outside "
        "collaborator. If the repo is public, check the spelling."
    )


def clone_failure_message(slug: str, exc: BaseException) -> str:
    """``Failed to clone/fetch <slug>: <exc>``, plus the access hint when it applies.

    The hint applies when a git ``CommandError`` in ``exc``'s cause/context chain
    has one of GitHub's refusal texts in its stderr (see :func:`_github_said`). The
    first line is the same with or without it; the hint, when present, follows on an
    indented line.
    """
    message = f"Failed to clone/fetch {shown(slug)}: {exc}"
    if _looks_like_access_failure(exc):
        message += f"\n  {_access_hint(slug)}"
    return message
