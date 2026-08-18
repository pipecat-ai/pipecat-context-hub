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


def canonicalize_framework_pin(pin: str) -> str:
    """The canonical spelling of an operator's framework pin.

    Returns the canonical ``latest`` for the sentinel in any casing or
    surrounding whitespace, and the pin verbatim otherwise — so a pin recorded
    in index metadata compares equal to what :func:`is_latest_sentinel` accepts.
    """
    return LATEST_SENTINEL if is_latest_sentinel(pin) else pin


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
