"""The clone-failure message: when the access hint fires and what it says.

Everything goes through ``clone_failure_message``, the one function both clone sites
call. The state space is exception type x refusal text x where in the exception
chain the text sits; the table below covers every cell.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from git.exc import GitCommandError

from pipecat_context_hub.shared import repo_access
from pipecat_context_hub.shared.repo_access import clone_failure_message

_SLUG = "acme/private"
_CLONE_ARGV = ["git", "clone", "-v", "--", "https://github.com/acme/private.git", "/data/r"]

# One string per marker; each holds that marker and no other, so dropping any one
# marker fails its case. Keyed by the marker (or pattern) it pins.
_ACCESS_TEXTS: dict[str, str] = {
    "repository not found": "remote: Repository not found.",
    "authentication failed": "fatal: Authentication failed for 'https://github.com/acme/private.git/'",
    "could not read username": (
        "fatal: could not read Username for 'https://github.com': No such device or address"
    ),
    "could not read password": (
        "fatal: could not read Password for 'https://octocat@github.com': No such device"
    ),
    "terminal prompts disabled": "fatal: terminal prompts disabled",
    "permission denied (publickey": "git@github.com: Permission denied (publickey).",
    "returned error: 401": (
        "error: RPC failed; HTTP 401 curl 22 The requested URL returned error: 401"
    ),
    "returned error: 403": "error: The requested URL returned error: 403",
    "returned error: 404": "error: The requested URL returned error: 404",
    # Without the `remote:` line (a proxy, GitHub Enterprise).
    r"repository '[^']*' not found": (
        "fatal: repository 'https://github.com/acme/private.git/' not found"
    ),
    r"permission to \S+ denied to": "remote: Permission to acme/private.git denied to octocat.",
}

_UNRELATED_TEXTS = [
    "fatal: unable to access 'https://github.com/acme/x.git/': Could not resolve host: github.com",
    "network error",
    # A local disk failure is not a GitHub account problem. Frozen from real
    # `git clone` output into a read-only directory.
    "fatal: could not create work tree dir 'ro/out2': Permission denied",
    "[Errno 13] Permission denied: '/home/u/.local/share/pipecat-context-hub/repos'",
    "Tag 'v9' not found in repository. Available tags (latest 5): ['v1.8.1']",
    # Only the quote anchor rejects this: an unanchored `repository .* not found`
    # would match it.
    "Tag 'v9' not found in repository acme/x; tag 'v9.0' not found on origin either",
]


_ACCESS_TEXTS_PATTERNS = [
    *(re.escape(m) for m in repo_access._ACCESS_MARKERS),
    r"repository '[^']*' not found",
    r"permission to \S+ denied to",
]


def test_every_marker_is_pinned_by_its_own_case() -> None:
    """Each marker has a case, and each case trips exactly its own marker."""
    assert repo_access._ACCESS_RE.pattern == "|".join(_ACCESS_TEXTS_PATTERNS)
    keyed = {re.escape(k) if k in repo_access._ACCESS_MARKERS else k for k in _ACCESS_TEXTS}
    assert keyed == set(_ACCESS_TEXTS_PATTERNS)
    for text in _ACCESS_TEXTS.values():
        hits = [p for p in _ACCESS_TEXTS_PATTERNS if re.search(p, text, re.IGNORECASE)]
        assert len(hits) == 1, (text, hits)


# --- the exception shapes text can reach the clone sites in ------------------


def _plain(text: str) -> BaseException:
    return RuntimeError(text)


def _git(text: str) -> BaseException:
    return GitCommandError(_CLONE_ARGV, 128, text.encode())


def _caused_by_git(text: str) -> BaseException:
    """`_origin_tag_commit`: ``raise ValueError(...) from GitCommandError``."""
    try:
        try:
            raise _git(text)
        except GitCommandError as inner:
            raise ValueError("Could not verify tag 'v1.2.0' against origin") from inner
    except ValueError as outer:
        return outer


def _during_git(text: str) -> BaseException:
    """An error raised while handling a git failure (implicit ``__context__``)."""
    try:
        try:
            raise _git(text)
        except GitCommandError:
            raise ValueError("cleanup after a failed fetch also failed")  # noqa: B904
    except ValueError as outer:
        return outer


def _git_suppressed(text: str) -> BaseException:
    """``raise ... from None`` hides the git failure on purpose."""
    try:
        try:
            raise _git(text)
        except GitCommandError:
            raise ValueError("replaced") from None
    except ValueError as outer:
        return outer


def _local(text: str) -> BaseException:
    return OSError(text)


def _caused_by_local(text: str) -> BaseException:
    try:
        try:
            raise OSError(text)
        except OSError as inner:
            raise ValueError("clone failed") from inner
    except ValueError as outer:
        return outer


# (shape, hint expected when the text is an access text)
_SHAPES: list[tuple[Callable[[str], BaseException], bool]] = [
    (_plain, True),
    (_git, True),
    (_caused_by_git, True),
    (_during_git, True),
    (_git_suppressed, False),
    (_local, False),
    (_caused_by_local, False),
]


@pytest.mark.parametrize(("make", "hinted"), _SHAPES, ids=[m.__name__ for m, _ in _SHAPES])
@pytest.mark.parametrize("text", list(_ACCESS_TEXTS.values()) + _UNRELATED_TEXTS)
def test_hint_fires_exactly_for_github_refusals(
    make: Callable[[str], BaseException], hinted: bool, text: str
) -> None:
    exc = make(text)
    message = clone_failure_message(_SLUG, exc)
    first = f"Failed to clone/fetch {_SLUG}: {exc}"
    expected = hinted and text in _ACCESS_TEXTS.values()
    if expected:
        assert message.startswith(first + "\n  ")
        assert message.count("\n  ") == first.count("\n  ") + 1
    else:
        # Byte-identical to the message before the hint existed.
        assert message == first


def test_local_permission_error_naming_a_marker_in_its_path_gets_no_hint() -> None:
    """A local ``OSError`` is never GitHub's answer, whatever its path says."""
    exc = PermissionError(13, "Permission denied", "/Volumes/Authentication Failed/hub/repos")
    assert str(exc) == "[Errno 13] Permission denied: '/Volumes/Authentication Failed/hub/repos'"
    assert clone_failure_message(_SLUG, exc) == f"Failed to clone/fetch {_SLUG}: {exc}"


def test_git_command_line_is_not_read_as_githubs_answer() -> None:
    """``str(GitCommandError)`` carries the command line, which names the local
    clone directory; only git's stderr is matched."""
    argv = [*_CLONE_ARGV[:-1], "/Volumes/Authentication Failed/hub/repos/acme_private"]
    exc = GitCommandError(argv, 128, b"fatal: unable to access: Could not resolve host")
    assert "Authentication Failed" in str(exc)
    assert clone_failure_message(_SLUG, exc) == f"Failed to clone/fetch {_SLUG}: {exc}"


def test_tag_verification_failure_keeps_the_hint_from_its_cause() -> None:
    """F3: ``Could not verify tag`` wraps the ``git ls-remote`` failure."""
    exc = _caused_by_git("remote: Repository not found.")
    assert "not found" not in str(exc)
    assert "\n  " in clone_failure_message(_SLUG, exc)


def test_a_cyclic_chain_terminates() -> None:
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert clone_failure_message(_SLUG, a) == f"Failed to clone/fetch {_SLUG}: a"


def test_hint_states_the_account_requirement_first_then_the_fix() -> None:
    message = clone_failure_message(_SLUG, RuntimeError("remote: Repository not found."))
    hint = message.split("\n  ", 1)[1]
    assert "\n" not in hint  # one paragraph
    assert hint.startswith(f"{_SLUG} could not be read.")
    account = hint.index("GitHub account in this environment")
    assert "can only index repos that account can read" in hint
    assert "gh auth status" in hint and "gh auth setup-git" in hint
    # Least privilege first: read-only Contents on the one repo; classic `repo`
    # named as the broader, read-and-write option.
    fine_grained = hint.index("fine-grained one with read-only Contents access to just this repo")
    classic = hint.index("classic `repo` scope")
    assert account < fine_grained < classic
    assert "read and write" in hint
    assert "SSO" in hint and "outside collaborator" in hint


def test_hint_never_names_a_configured_slug_raw() -> None:
    """The slug is a configured value; a non-printable one is flagged, not echoed,
    in the first line and in the hint alike."""
    hostile = "acme/\x1b[2Jprivate"
    message = clone_failure_message(hostile, RuntimeError("remote: Repository not found."))
    assert "\x1b" not in message
    first, hint = message.split("\n  ", 1)
    assert first == f"Failed to clone/fetch {hostile!r} [invalid]: remote: Repository not found."
    assert hint.startswith(f"{hostile!r} [invalid] could not be read.")
