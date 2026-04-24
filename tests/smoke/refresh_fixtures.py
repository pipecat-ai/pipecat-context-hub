#!/usr/bin/env python3
"""Regenerate vendored smoke-test fixtures from live upstream clones.

Manual-use script — NOT invoked by CI. Refresh the snapshots whenever the
upstream ``examples/`` layout changes (every release at minimum; on every
drift-job failure). See ``tests/smoke/README.md`` for policy.

Usage
-----

    uv run python tests/smoke/refresh_fixtures.py

Behaviour
---------

For each repo in the vendored ``FIXTURE_PINS.json``:

1. Shallow-clone the repo at ``main`` into a scratch directory.
2. Copy over ``examples/`` (if present), ``pyproject.toml``, and ``README.md``
   into the vendored fixture root.
3. Strip ``.git/``, binaries (anything not matching the allowed suffix list),
   and any stray build artefacts.
4. Update ``FIXTURE_PINS.json`` with the new SHA + capture date.

The vendored fixture tree is tree-only: no ``.git`` directory and no history.
Target size ≤ 2 MB per repo.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_REF_RE = re.compile(r"^(?!-)[A-Za-z0-9._/+-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_CLONE_TIMEOUT_S = 300

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURES_ROOT = _REPO_ROOT / "tests" / "fixtures" / "smoke"
_PINS_PATH = _FIXTURES_ROOT / "FIXTURE_PINS.json"

# File suffixes considered "text-ish" and safe to vendor. Everything else is
# stripped (binaries, images, archives).
_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".md",
        ".rst",
        ".txt",
        ".cfg",
        ".ini",
    }
)

# Repo slug → vendored fixture dir name.
_REPOS: dict[str, str] = {
    "pipecat-ai/pipecat": "pipecat",
    "pipecat-ai/pipecat-examples": "pipecat-examples",
}


def _run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> str:
    result = subprocess.run(
        cmd, cwd=cwd, check=True, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout.strip()


def _shallow_clone(repo_slug: str, dest: Path, ref: str = "main") -> str:
    """Clone ``repo_slug`` at ``ref`` into ``dest`` and return the resolved SHA.

    Named refs (branch/tag) go through ``git clone --depth 1 --branch``.
    Commit-SHA refs use ``git init`` + ``git fetch --depth 1`` +
    ``git checkout FETCH_HEAD`` because ``--branch`` rejects SHAs.
    """
    if not _SLUG_RE.match(repo_slug):
        raise ValueError(f"Invalid repo slug: {repo_slug!r}")
    if not _REF_RE.match(ref):
        raise ValueError(f"Invalid git ref: {ref!r}")
    url = f"https://github.com/{repo_slug}.git"
    if _SHA_RE.match(ref):
        dest.mkdir(parents=True, exist_ok=True)
        _run(["git", "init", "--quiet", str(dest)], timeout=_CLONE_TIMEOUT_S)
        _run(
            ["git", "-C", str(dest), "remote", "add", "origin", "--", url],
            timeout=_CLONE_TIMEOUT_S,
        )
        _run(
            ["git", "-C", str(dest), "fetch", "--depth", "1", "origin", ref],
            timeout=_CLONE_TIMEOUT_S,
        )
        _run(
            ["git", "-C", str(dest), "checkout", "--quiet", "FETCH_HEAD"],
            timeout=_CLONE_TIMEOUT_S,
        )
    else:
        _run(
            ["git", "clone", "--depth", "1", "--branch", ref, "--", url, str(dest)],
            timeout=_CLONE_TIMEOUT_S,
        )
    return _run(["git", "rev-parse", "HEAD"], cwd=dest)


def _copy_filtered(src: Path, dst: Path) -> None:
    """Copy ``src`` into ``dst``, stripping ``.git``, symlinks, and non-text files.

    Symlinks in upstream clones are never followed — a malicious (or just
    buggy) example directory containing ``link -> /etc`` could otherwise
    cause the copy to escape the clone root.
    """
    if not src.exists():
        return
    if src.is_symlink():
        return
    if src.is_file():
        if src.suffix in _TEXT_SUFFIXES or src.name in {"README", "LICENSE"}:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return
    for entry in src.iterdir():
        if entry.name == ".git":
            continue
        _copy_filtered(entry, dst / entry.name)


# Top-level dirs that must never be vendored even in root-layout repos —
# mirrors ``TaxonomyBuilder.build_from_examples_repo`` packaged-project skip
# list so the fixture never captures source/tests/CI trees.
_ROOT_LAYOUT_SKIP: frozenset[str] = frozenset(
    {"src", "tests", "docs", "scripts", "dashboard", ".github", ".claude", ".git"}
)


def _rebuild_fixture(repo_slug: str, fixture_name: str, clone_root: Path) -> None:
    fixture_dir = _FIXTURES_ROOT / fixture_name
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture_dir.mkdir(parents=True, exist_ok=True)

    # Always copy root-level manifests when present.
    for top_level in ("pyproject.toml", "README.md"):
        _copy_filtered(clone_root / top_level, fixture_dir / top_level)

    examples_dir = clone_root / "examples"
    if examples_dir.is_dir():
        # Topic/foundational layout (pipecat-ai/pipecat): all examples live
        # under ``examples/``.
        _copy_filtered(examples_dir, fixture_dir / "examples")
        return

    # Root-level layout (pipecat-ai/pipecat-examples): each top-level
    # directory IS an example. Mirror ``build_from_examples_repo``'s
    # packaged-project skip list so we don't vendor source trees, CI
    # config, or hidden metadata.
    for entry in clone_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name in _ROOT_LAYOUT_SKIP:
            continue
        _copy_filtered(entry, fixture_dir / entry.name)


def refresh(ref: str = "main", dry_run: bool = False) -> None:
    pins: dict[str, dict[str, str]] = {}
    if _PINS_PATH.is_file():
        pins = json.loads(_PINS_PATH.read_text(encoding="utf-8"))

    today = date.today().isoformat()
    with tempfile.TemporaryDirectory() as scratch:
        scratch_root = Path(scratch)
        for repo_slug, fixture_name in _REPOS.items():
            clone_path = scratch_root / fixture_name
            sha = _shallow_clone(repo_slug, clone_path, ref=ref)
            print(f"Cloned {repo_slug} @ {sha[:8]}")
            if dry_run:
                continue
            _rebuild_fixture(repo_slug, fixture_name, clone_path)
            pins[fixture_name] = {
                "repo": repo_slug,
                "sha": sha,
                "date": today,
                "ref": ref,
                "note": f"vendored snapshot of {repo_slug}@{ref}",
            }

    if not dry_run:
        _PINS_PATH.write_text(
            json.dumps(pins, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {_PINS_PATH}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main", help="Git ref to clone (default: main)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Clone but do not overwrite fixtures."
    )
    args = parser.parse_args(argv)
    refresh(ref=args.ref, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
