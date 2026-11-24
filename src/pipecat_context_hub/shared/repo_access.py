"""What to tell the user when a repo cannot be cloned: the one hint `refresh` prints.

GitHub answers "not found" for a repo that does not exist *and* for a private repo
the caller cannot see, so the two cannot be told apart from outside. The hint states
the one thing the user controls: the hub clones with the GitHub account in this
environment, so it can only index repos that account can read.
"""

from __future__ import annotations

import re
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


def looks_like_access_failure(text: str) -> bool:
    return _ACCESS_RE.search(text) is not None


def access_hint(slug: str) -> str:
    """Tell the user what to check when ``slug`` cannot be read."""
    return (
        f"{shown(slug)} could not be read. The hub clones with the GitHub account in this "
        "environment (git's credential helper, e.g. gh after `gh auth setup-git`), so it "
        "can only index repos that account can read; GitHub shows a private repo you "
        "cannot access as 'not found'. Check `gh auth status`, that the account can open "
        "the repo in a browser, and, for a token, contents-read access (classic `repo` "
        "scope or fine-grained Contents: read, plus org SSO authorisation). If the repo "
        "is public, check the spelling."
    )
