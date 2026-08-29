"""Shared version-tag string helpers.

Every module that reasons about a framework tag *as a string* — the ingester
resolving a pin, the CLI normalising one for metadata, the deprecation map
comparing versions — goes through this module, so the sentinel spelling and the
tag-to-``Version`` parse have exactly one definition each.
"""

from __future__ import annotations

from typing import Final

from packaging.version import InvalidVersion, Version

# Sentinel accepted wherever a framework tag is expected, meaning "newest
# release tag". Note this shadows any tag matching "latest" case-insensitively
# after trimming whitespace (e.g. "Latest", " LATEST "); no pipecat-ai repo
# publishes one, and an escape syntax would cost more than it buys.
LATEST_SENTINEL: Final[str] = "latest"

# Sentinel meaning "the framework repo's default branch", spelled either
# ``head`` or ``main``. A pin is otherwise validated as a version-like tag, and
# an operator who names no pin gets :data:`DEFAULT_FRAMEWORK_PIN`, so the
# default branch needs a spelling of its own to be reachable at all. Neither
# spelling shadows a real tag in any pipecat-ai repo.
HEAD_SENTINEL: Final[str] = "head"
_HEAD_SPELLINGS: Final[frozenset[str]] = frozenset({HEAD_SENTINEL, "main"})

# The framework pin applied when an operator names none. ``latest`` keeps the
# framework aligned with every other indexed source: the docs site publishes
# from a release-time promotion and the example repos depend on released
# wheels, so a default-branch framework checkout would be the only source
# contributing unreleased APIs — and git-describe floor semantics would stamp
# them with the previous release's number, which reads as "compatible" to a
# caller running that release.
DEFAULT_FRAMEWORK_PIN: Final[str] = LATEST_SENTINEL


def strip_v_prefix(tag: str) -> str:
    """Strip a single leading literal 'v', not the character set {'v'}.

    ``str.lstrip("v")`` strips a character set, not a fixed prefix, so
    ``"vv1.0.0".lstrip("v")`` incorrectly collapses to ``"1.0.0"`` — the same
    result as the well-formed tag ``"v1.0.0"``. This strips at most one
    leading 'v'.
    """
    return tag.removeprefix("v")


def is_latest_sentinel(tag: str | None) -> bool:
    """True when *tag* is the ``latest`` sentinel, case- and whitespace-insensitive."""
    return tag is not None and tag.strip().lower() == LATEST_SENTINEL


def is_head_sentinel(tag: str | None) -> bool:
    """True when *tag* is a ``head`` sentinel spelling, case- and whitespace-insensitive."""
    return tag is not None and tag.strip().lower() in _HEAD_SPELLINGS


def is_sentinel_pin(tag: str | None) -> bool:
    """True when *tag* is any sentinel spelling rather than a concrete tag.

    The single derived "is this a pin rather than a version?" predicate, so
    callers don't open-code ``is_latest_sentinel(x) or is_head_sentinel(x)`` —
    which would have to be found and edited again if a third sentinel is ever
    added. Same case- and whitespace-insensitivity as its constituents.
    """
    return is_latest_sentinel(tag) or is_head_sentinel(tag)


def canonicalize_framework_pin(pin: str) -> str:
    """The canonical spelling of an operator's framework pin.

    Returns the canonical spelling for either sentinel in any casing or
    surrounding whitespace (``main`` canonicalizes to ``head``), and the pin
    verbatim otherwise — so a pin recorded in index metadata compares equal to
    what :func:`is_latest_sentinel` / :func:`is_head_sentinel` accept.
    """
    if is_latest_sentinel(pin):
        return LATEST_SENTINEL
    if is_head_sentinel(pin):
        return HEAD_SENTINEL
    return pin


def parse_release_version(tag: str) -> Version:
    """Parse *tag* as a release version after stripping exactly one leading 'v' or 'V'.

    ``Version()`` performs its own PEP 440 normalisation that tolerates a
    leading 'v' — so a naive single strip of ``"vv1.0.0"`` yields ``"v1.0.0"``,
    which ``Version()`` then happily accepts as ``1.0.0``, silently undoing the
    single-prefix guarantee. Rejecting any leading 'v'/'V' left after the strip
    closes that gap: a tag needs a real prefix mismatch (``"vv1.0.0"``,
    ``"vV1.0.0"``, ``"Vv1.0.0"``, ``"VV1.0.0"``) to be excluded, not just an
    unlucky double-normalisation.

    The strip and the guard are deliberately both case-insensitive. An
    asymmetric pair — lowercase strip, either-case guard — rejects the
    well-formed ``"V1.2.0"`` as unparseable, which would silently drop a repo's
    newest release out of ``latest`` candidacy. Note this is *not* the same
    widening as making :func:`strip_v_prefix` case-insensitive: that helper is
    used for display normalisation, where strip precision is the point, and it
    stays lowercase-only.

    Raises ``InvalidVersion`` — same contract as ``Version()`` itself — so
    every existing ``except InvalidVersion`` call site needs no changes.
    """
    stripped = tag[1:] if tag[:1] in ("v", "V") else tag
    if stripped[:1] in ("v", "V"):
        raise InvalidVersion(f"Invalid version: {tag!r}")
    return Version(stripped)


def exact_release_version(tag: str | None) -> str | None:
    """The normalized release version *tag* names, or ``None`` when it is not a release.

    A tag that parses as a PEP 440 release identifies the checkout exactly
    (``commits_ahead == 0``). A tag that does not — a branch-shaped or feature
    tag such as ``some-feature-tag``, which git accepts and this tool's tag-input
    validation permits — carries no version identity, so callers must fall back
    to git-describe floor semantics rather than stamping the tag verbatim.
    """
    if tag is None:
        return None
    try:
        return str(parse_release_version(tag.strip()))
    except InvalidVersion:
        return None
