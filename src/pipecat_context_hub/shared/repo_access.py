"""What to tell the user when a repo cannot be cloned: the one hint `refresh` prints.

GitHub answers "not found" for a repo that does not exist *and* for a private repo
the caller cannot see, so the two cannot be told apart from outside. The hint states
the one thing the user controls: the hub clones with the GitHub account in this
environment, so it can only index repos that account can read.
"""

from __future__ import annotations

import re
from typing import Final

# Substrings of a failed git clone/fetch that mean "GitHub would not show us this
# repo" (404 masquerading for private, bad/absent credentials, token scope).
_ACCESS_MARKERS: Final = (
    "repository not found",
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "permission denied",
    "returned error: 403",
    "returned error: 404",
)
_ACCESS_RE: Final = re.compile("|".join(re.escape(m) for m in _ACCESS_MARKERS), re.IGNORECASE)


def looks_like_access_failure(text: str) -> bool:
    return _ACCESS_RE.search(text) is not None


def access_hint(slug: str) -> str:
    """Tell the user what to check when ``slug`` cannot be read."""
    return (
        f"{slug} could not be read. The hub clones with the GitHub account in this "
        "environment (git's credential helper, e.g. gh after `gh auth setup-git`), so it "
        "can only index repos that account can read; GitHub shows a private repo you "
        "cannot access as 'not found'. Check `gh auth status`, that the account can open "
        "the repo in a browser, and, for a token, contents-read access (classic `repo` "
        "scope or fine-grained Contents: read, plus org SSO authorisation). If the repo "
        "is public, check the spelling."
    )
