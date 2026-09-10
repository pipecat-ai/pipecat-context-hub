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
import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _REPO_ROOT / "justfile"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_UV_LOCK = _REPO_ROOT / "uv.lock"

# Match the flag form only (`--ignore-vuln PYSEC-2026-139`), and constrain the
# captured token to a real advisory-ID shape (PYSEC/CVE/GHSA-...). This skips
# prose like "the --ignore-vuln set and rationale live in ci.yml", where the
# word after the flag is not an ID.
_IGNORE_FLAG_RE = re.compile(r"--ignore-vuln\s+((?:PYSEC|CVE|GHSA)-[A-Za-z0-9-]+)")


def _ignored_vulns(path: Path) -> set[str]:
    return set(_IGNORE_FLAG_RE.findall(path.read_text(encoding="utf-8")))


def _project_data() -> dict[str, Any]:
    with _PYPROJECT.open("rb") as stream:
        return tomllib.load(stream)


def _lock_data() -> dict[str, Any]:
    with _UV_LOCK.open("rb") as stream:
        return tomllib.load(stream)


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


def test_project_declares_the_supported_mcp_major_range() -> None:
    project = _project_data()["project"]
    dependencies = project["dependencies"]
    requirements = [Requirement(value) for value in dependencies]

    mcp_requirements = [requirement for requirement in requirements if requirement.name == "mcp"]

    assert mcp_requirements == [Requirement("mcp>=2.0,<3.0")]


def test_lock_freezes_one_in_range_mcp_sdk_and_matching_types_package() -> None:
    project = _project_data()["project"]
    dependencies = project["dependencies"]
    mcp_requirement = next(
        Requirement(value) for value in dependencies if Requirement(value).name == "mcp"
    )
    supported_range = SpecifierSet(str(mcp_requirement.specifier))

    lock = _lock_data()
    packages = lock["package"]
    mcp_packages = [package for package in packages if package["name"] == "mcp"]
    mcp_types_packages = [package for package in packages if package["name"] == "mcp-types"]

    assert len(mcp_packages) == 1
    assert len(mcp_types_packages) == 1
    mcp_version = Version(mcp_packages[0]["version"])
    mcp_types_version = Version(mcp_types_packages[0]["version"])
    assert mcp_version in supported_range
    assert mcp_types_version == mcp_version

    root_package = next(
        package for package in packages if package.get("source") == {"editable": "."}
    )
    root_dependency_names = {dependency["name"] for dependency in root_package["dependencies"]}
    assert "mcp" in root_dependency_names


def test_lock_keeps_mcp_2x_transitives_and_starlette_security_floor() -> None:
    project = _project_data()
    constraints = project["tool"]["uv"]["constraint-dependencies"]
    starlette_requirement = next(
        Requirement(value) for value in constraints if Requirement(value).name == "starlette"
    )
    assert starlette_requirement == Requirement("starlette>=1.0.1")

    lock = _lock_data()
    assert lock["manifest"]["constraints"] == [{"name": "starlette", "specifier": ">=1.0.1"}]

    packages_by_name = {package["name"]: package for package in lock["package"]}
    assert Version(packages_by_name["starlette"]["version"]) in starlette_requirement.specifier
    assert {"httpcore2", "httpx2", "truststore"} <= packages_by_name.keys()

    mcp_dependencies = {
        dependency["name"] for dependency in packages_by_name["mcp"]["dependencies"]
    }
    assert {"httpx2", "mcp-types", "opentelemetry-api", "starlette"} <= mcp_dependencies
