"""Structural guard: `just audit-deps` and the CI "Dependency Audit" step must
ignore the exact same pip-audit advisories.

Both `justfile` and `.github/workflows/ci.yml` carry a reciprocal KEEP-IN-SYNC
comment, but a prose convention does not fail a build. This test turns it into a
real check: a `--ignore-vuln` added to one location but not the other makes the
local `just audit-deps` pass/fail differently than the PR gate — the exact drift
recorded in the AGENTS.md Review Checklist (transformers CVE-2026-1839, where the
justfile kept a stale ignore CI had dropped). Catch it here instead.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _REPO_ROOT / "justfile"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Match the flag form only (`--ignore-vuln PYSEC-2026-139`), and constrain the
# captured token to a real advisory-ID shape (PYSEC/CVE/GHSA-...). This skips
# prose like "the --ignore-vuln set and rationale live in ci.yml", where the
# word after the flag is not an ID.
_IGNORE_FLAG_RE = re.compile(r"--ignore-vuln\s+((?:PYSEC|CVE|GHSA)-[A-Za-z0-9-]+)")


def _ignored_vulns(path: Path) -> set[str]:
    return set(_IGNORE_FLAG_RE.findall(path.read_text(encoding="utf-8")))


def test_justfile_and_ci_pip_audit_ignores_match() -> None:
    just_ids = _ignored_vulns(_JUSTFILE)
    ci_ids = _ignored_vulns(_CI_YML)

    assert just_ids, "no --ignore-vuln advisories found in the justfile audit-deps recipe"
    assert ci_ids, "no --ignore-vuln advisories found in the ci.yml Dependency Audit step"
    assert just_ids == ci_ids, (
        "justfile `audit-deps` and ci.yml 'Dependency Audit' pip-audit ignore sets have "
        "drifted — keep them in sync (see the KEEP-IN-SYNC notes in both files).\n"
        f"  justfile only: {sorted(just_ids - ci_ids)}\n"
        f"  ci.yml only:   {sorted(ci_ids - just_ids)}"
    )
