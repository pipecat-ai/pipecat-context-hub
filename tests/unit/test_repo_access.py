"""The clone-failure hint: when it fires and what it says."""

from __future__ import annotations

import pytest

from pipecat_context_hub.shared.repo_access import access_hint, looks_like_access_failure


@pytest.mark.parametrize(
    "text",
    [
        "remote: Repository not found.\nfatal: repository 'x' not found",
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        "fatal: Authentication failed for 'https://github.com/acme/private.git/'",
        "error: The requested URL returned error: 403",
        # Without the `remote:` line (a proxy, GitHub Enterprise).
        "fatal: repository 'https://github.com/acme/private.git/' not found",
        "remote: Permission to acme/private.git denied to octocat.",
        "git@github.com: Permission denied (publickey).",
    ],
)
def test_access_failures_are_recognised(text: str) -> None:
    assert looks_like_access_failure(text)


@pytest.mark.parametrize(
    "text",
    [
        "fatal: unable to access: Could not resolve host: github.com",
        "network error",
        # A local disk failure is not a GitHub account problem. Frozen from real
        # `git clone` output into a read-only directory.
        "fatal: could not create work tree dir 'ro/out2': Permission denied",
        "[Errno 13] Permission denied: '/home/u/.local/share/pipecat-context-hub/repos'",
        "Tag 'v9' not found in repository. Available tags (latest 5): ['v1.8.1']",
    ],
)
def test_unrelated_failures_are_not(text: str) -> None:
    assert not looks_like_access_failure(text)


def test_hint_states_the_account_requirement_and_the_fix() -> None:
    hint = access_hint("acme/private")
    assert "acme/private" in hint
    assert "GitHub account in this environment" in hint
    assert "gh auth status" in hint and "gh auth setup-git" in hint
    assert "Contents: read" in hint and "SSO" in hint


def test_hint_never_names_a_configured_slug_raw() -> None:
    """The slug is a configured value; a non-printable one is flagged, not echoed."""
    hostile = "acme/\x1b[2Jprivate"
    hint = access_hint(hostile)
    assert "\x1b" not in hint
    assert f"{hostile!r} [invalid]" in hint
