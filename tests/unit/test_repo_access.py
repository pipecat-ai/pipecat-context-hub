"""The clone-failure message: when the access hint fires and what it says.

Everything goes through ``clone_failure_message``, the one function both clone sites
call. The rule: only git's stderr, minus the clone target, can be GitHub's answer.
The state space is exception type (git, other, OSError) x refusal text x where the
text sits (git's stderr, the clone target that stderr repeats, the git command line,
another exception's message) x where in the exception chain; the table below covers
every cell.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from git.exc import GitCommandError

from pipecat_context_hub.shared import repo_access
from pipecat_context_hub.shared.repo_access import clone_failure_message

_SLUG = "acme/private"
_CLONE_ARGV = ["git", "clone", "-v", "--", "https://github.com/acme/private.git", "/data/r"]

# One string per pattern; each matches that pattern and no other, so dropping any
# one pattern fails its case (see the first test). Beside each, how the trigger
# sentence in docs/configuration.md's "Private repos" names that pattern.
_ACCESS_CASES = [
    ("remote: Repository not found.", "`Repository not found`"),
    (
        "fatal: Authentication failed for 'https://github.com/acme/private.git/'",
        "`Authentication failed`",
    ),
    (
        "fatal: could not read Username for 'https://github.com': No such device or address",
        "`could not read Username`",
    ),
    (
        "fatal: could not read Password for 'https://octocat@github.com': No such device",
        "or `Password`",
    ),
    ("fatal: terminal prompts disabled", "`terminal prompts disabled`"),
    # ssh's method list, containing publickey (other forms are checked below).
    (
        "git@github.com: Permission denied (keyboard-interactive,publickey).",
        "any method list containing `publickey`",
    ),
    (
        "Received disconnect from 140.82.112.3 port 22:2: Too many authentication failures",
        "`Too many authentication failures`",
    ),
    # GitHub's SSH key audit: the key is suspended until re-approved; nothing else
    # in the output names the cause.
    (
        "ERROR: We're doing an SSH key audit.\n"
        "Reactivate this key at https://github.com/settings/keys\n"
        "Once you've done so, you'll be able to continue using this key.\n"
        "fatal: Could not read from remote repository.",
        "`We're doing an SSH key audit`",
    ),
    (
        "error: RPC failed; HTTP 401 curl 22 The requested URL returned error: 401",
        "`returned error: 401`",
    ),
    ("error: The requested URL returned error: 403", "`returned error: 403`"),
    ("error: The requested URL returned error: 404", "`returned error: 404`"),
    # GitHub's SAML SSO refusal over SSH, where nothing else names the cause.
    (
        "ERROR: The 'acme' organization has enabled or enforced SAML SSO. To access this "
        "repository, you must use the HTTPS remote with a personal access token or SSH key "
        "and password that has been allowed access to this organization.\n"
        "fatal: Could not read from remote repository.",
        "SAML SSO refusal",
    ),
    # Without the `remote:` line (a proxy, GitHub Enterprise).
    (
        "fatal: repository 'https://github.com/acme/private.git/' not found",
        "`repository '<url>' not found`",
    ),
    (
        "remote: Permission to acme/private.git denied to octocat.",
        "`Permission to <repo> denied to <user>`",
    ),
]
_ACCESS_TEXTS = [text for text, _ in _ACCESS_CASES]

_UNRELATED_TEXTS = [
    "fatal: unable to access 'https://github.com/acme/x.git/': Could not resolve host: github.com",
    "network error",
    # A local disk failure is not a GitHub account problem. Frozen from real
    # `git clone` output into a read-only directory.
    "fatal: could not create work tree dir 'ro/out2': Permission denied",
    "[Errno 13] Permission denied: '/home/u/.local/share/pipecat-context-hub/repos'",
    "Permission denied (os error 13)",
    "Tag 'v9' not found in repository. Available tags (latest 5): ['v1.8.1']",
    # Only the quote anchor rejects this: an unanchored `repository .* not found`
    # would match it.
    "Tag 'v9' not found in repository acme/x; tag 'v9.0' not found on origin either",
]


def test_every_pattern_is_pinned_by_its_own_case() -> None:
    """The pattern tuple is the matcher's one source, and each pattern has exactly
    one case that trips it and nothing else."""
    patterns = repo_access._ACCESS_PATTERNS
    assert repo_access._ACCESS_RE.pattern == "|".join(patterns)
    assert repo_access._ACCESS_RE.flags & re.IGNORECASE
    hits = {
        text: [p for p in patterns if re.search(p, text, re.IGNORECASE)] for text in _ACCESS_TEXTS
    }
    assert all(len(h) == 1 for h in hits.values()), hits
    assert sorted(h[0] for h in hits.values()) == sorted(patterns)


@pytest.mark.parametrize(
    "text",
    [
        "git@github.com: Permission denied (publickey).",
        "git@github.com: Permission denied (publickey,password).",
        "git@github.com: Permission denied (keyboard-interactive,publickey).",
    ],
)
def test_ssh_refusal_matches_by_shape_not_by_example(text: str) -> None:
    """Any ssh method list containing publickey is a refusal; a bare or OS-level
    "Permission denied" (in _UNRELATED_TEXTS) is not."""
    assert repo_access._ACCESS_RE.search(text) is not None


def _pattern_of(text: str) -> str:
    hits = [p for p in repo_access._ACCESS_PATTERNS if re.search(p, text, re.IGNORECASE)]
    assert len(hits) == 1, (text, hits)
    return hits[0]


def test_docs_trigger_list_names_every_pattern() -> None:
    """The docs list the triggers by hand. Each docs phrase sits beside a sample
    text, and the sample finds its pattern. So this fails when a pattern has no
    case row, when a row's phrase is missing from the docs sentence, or when a
    regex stops matching its sample. It checks one direction only: a pattern
    dropped together with its case row passes even if the docs still name it,
    since the docs sentence is prose and is not parsed."""
    by_pattern = {_pattern_of(text): phrase for text, phrase in _ACCESS_CASES}
    assert sorted(by_pattern) == sorted(repo_access._ACCESS_PATTERNS)
    docs = (Path(__file__).resolve().parents[2] / "docs" / "configuration.md").read_text()
    start = docs.index("The hint fires when")
    sentence = docs[start : docs.index(". ", start)]
    missing = [phrase for phrase in by_pattern.values() if phrase not in sentence]
    assert not missing, missing


# --- the exception shapes text can reach the clone sites in ------------------


def _git(text: str) -> BaseException:
    """The text is git's stderr."""
    return GitCommandError(_CLONE_ARGV, 128, text.encode())


def _git_target(text: str) -> BaseException:
    """The text is inside the clone target, which git repeats in its stderr."""
    target = f"/Volumes/{text}/hub/repos/acme_private"
    stderr = f"fatal: could not create work tree dir '{target}': Permission denied"
    return GitCommandError([*_CLONE_ARGV[:-1], target], 128, stderr.encode())


def _git_command_line(text: str) -> BaseException:
    """The text is in the command line only (``str()`` shows it, stderr does not)."""
    target = f"/Volumes/{text}/hub/repos/acme_private"
    stderr = b"fatal: unable to access: Could not resolve host"
    return GitCommandError([*_CLONE_ARGV[:-1], target], 128, stderr)


def _plain(text: str) -> BaseException:
    """Not git: e.g. ``Invalid repo slug: <slug>`` echoing configured input."""
    return ValueError(text)


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


def _non_git_caused_by_git(text: str) -> BaseException:
    """The text is in the wrapper's message; the git failure it wraps is unrelated."""
    try:
        try:
            raise GitCommandError(_CLONE_ARGV, 128, b"fatal: early EOF")
        except GitCommandError as inner:
            raise ValueError(text) from inner
    except ValueError as outer:
        return outer


def _caused_by_non_git(text: str) -> BaseException:
    try:
        try:
            raise RuntimeError(text)
        except RuntimeError as inner:
            raise ValueError("clone failed") from inner
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
    (_git, True),
    (_caused_by_git, True),
    (_during_git, True),
    (_git_suppressed, False),
    (_git_target, False),
    (_git_command_line, False),
    (_plain, False),
    (_non_git_caused_by_git, False),
    (_caused_by_non_git, False),
    (_local, False),
    (_caused_by_local, False),
]


@pytest.mark.parametrize(("make", "hinted"), _SHAPES, ids=[m.__name__ for m, _ in _SHAPES])
@pytest.mark.parametrize("text", _ACCESS_TEXTS + _UNRELATED_TEXTS)
def test_hint_fires_exactly_for_github_refusals(
    make: Callable[[str], BaseException], hinted: bool, text: str
) -> None:
    exc = make(text)
    message = clone_failure_message(_SLUG, exc)
    first = f"Failed to clone/fetch {_SLUG}: {exc}"
    if hinted and text in _ACCESS_TEXTS:
        assert message.startswith(first + "\n  ")
        assert message.count("\n  ") == first.count("\n  ") + 1
    else:
        # Byte-identical to the message before the hint existed.
        assert message == first


_MARKED_TARGET = "/Volumes/Authentication Failed/hub/repos/a_b"


def test_a_marker_in_stderr_beside_the_clone_target_still_gets_the_hint() -> None:
    stderr = (
        f"Cloning into '{_MARKED_TARGET}'...\n"
        "remote: Repository not found.\n"
        "fatal: repository 'https://github.com/acme/private.git/' not found"
    )
    exc = GitCommandError([*_CLONE_ARGV[:-1], _MARKED_TARGET], 128, stderr.encode())
    assert clone_failure_message(_SLUG, exc).startswith(f"Failed to clone/fetch {_SLUG}: {exc}\n  ")


def test_a_cyclic_chain_terminates() -> None:
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert clone_failure_message(_SLUG, a) == f"Failed to clone/fetch {_SLUG}: a"


def test_hint_states_the_account_requirement_first_then_the_fix() -> None:
    exc = _git("remote: Repository not found.")
    message = clone_failure_message(_SLUG, exc)
    first = f"Failed to clone/fetch {_SLUG}: {exc}"
    assert message.startswith(first + "\n  ")
    hint = message[len(first) + 3 :]
    assert "\n" not in hint  # one paragraph
    assert hint.startswith(f"{_SLUG} could not be read.")
    account = hint.index("GitHub account in this environment")
    assert "can only index repos that account can read" in hint
    assert "gh auth setup-git" in hint
    # git sends whatever credentials it is configured with, not only gh's, so the
    # hint names the usual sources without claiming they are the only ones, and
    # gh's account is not assumed to be the one git used.
    assert (
        "using whatever credentials git is configured with (usually its credential "
        "helper or `GIT_ASKPASS`" in hint
    )
    gh_status = hint.index(
        "Check that the account git actually used can open the repo in a browser "
        "(`gh auth status` names gh's account, which is not always the one git used)."
    )
    # An `insteadOf` rule can turn the hub's HTTPS clone into SSH, where the HTTPS
    # credential helper is not involved: the hint points at the SSH key too. A failed
    # first clone leaves no checkout, so its URL comes from `ls-remote --get-url`
    # (which expands the rewrite with no checkout); a refresh of an existing clone
    # fetches that clone's `origin`, which its own config can rewrite. A host alias
    # or GitHub Enterprise is not github.com, and ssh's defaults (local username,
    # port 22) are not what `ssh://git@host:2222/...` uses, so the check names all
    # three. git's own ssh command can choose another key than the test.
    ssh = hint.index(
        "If your git config rewrites the HTTPS URL to SSH, "
        "`git ls-remote --get-url https://github.com/<owner>/<repo>.git` shows the URL "
        "a first clone uses; an existing clone fetches its own `origin` "
        "(`git -C <clone dir> remote get-url origin`; the clone is under `repos/` in the "
        "hub's data dir). Test that URL's key with `ssh -T -p <port> <user>@<host>`, "
        "using the user, host and port it names (not always github.com; drop `-p` if it "
        "names no port); ssh reads the `~/.ssh/config` entry for that host, and git's "
        "`core.sshCommand`, if set, can pick a different key."
    )
    # Least privilege first: read-only Contents on the one repo; classic `repo`
    # named as the broader option, bounded by what the account itself may do.
    fine_grained = hint.index("fine-grained one with read-only Contents access to just this repo")
    classic = hint.index("classic `repo` scope")
    assert account < gh_status < ssh < fine_grained < classic
    assert "full access to every private repo it can reach" in hint
    assert "including write where it has write" in hint
    # An org can block classic tokens: then a 403 even though the browser shows it.
    blocked = hint.index(
        "an org can block classic tokens, which then get a 403 even when the browser shows the repo"
    )
    assert classic < blocked
    assert "read and write" not in hint
    assert "SSO" in hint and "outside collaborator" in hint
    assert hint.endswith("If the repo is public, check the spelling.")


def test_hint_never_names_a_configured_slug_raw() -> None:
    """The slug is a configured value; a non-printable one is flagged, not echoed,
    in the first line and in the hint alike."""
    hostile = "acme/\x1b[2Jprivate"
    exc = _git("remote: Repository not found.")
    message = clone_failure_message(hostile, exc)
    assert "\x1b" not in message
    first = f"Failed to clone/fetch {hostile!r} [invalid]: {exc}"
    assert message.startswith(first + "\n  ")
    hint = message[len(first) + 3 :]
    assert hint.startswith(f"{hostile!r} [invalid] could not be read.")
