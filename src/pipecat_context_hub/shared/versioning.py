"""Shared version-tag string helpers."""


def strip_v_prefix(tag: str) -> str:
    """Strip a single leading literal 'v', not the character set {'v'}.

    ``str.lstrip("v")`` strips a character set, not a fixed prefix, so
    ``"vv1.0.0".lstrip("v")`` incorrectly collapses to ``"1.0.0"`` — the same
    result as the well-formed tag ``"v1.0.0"``. This strips at most one
    leading 'v'.
    """
    return tag.removeprefix("v")
