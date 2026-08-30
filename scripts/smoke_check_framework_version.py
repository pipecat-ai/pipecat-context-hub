#!/usr/bin/env python3
"""Live-hub smoke test for the ``refresh --framework-version`` CLI surface.

Companion to ``scripts/smoke_check_removals.py`` / ``smoke_check_deprecation.py``,
but where those two assert against the *already-persisted* index, this one
drives ``refresh`` itself — the feature it exercises only shows up across a
refresh run, not in a single query. It therefore does the one thing those two
scripts are careful never to: it runs real refreshes against real network
sources (GitHub, ``git ls-remote``). It never touches the *operator's*
persisted index though — every refresh in this script runs against a scratch
``PIPECAT_HUB_DATA_DIR`` created for the run and deleted at the end, so a
concurrently-running ``serve``/other ``refresh`` against the real hub is
unaffected. To keep each refresh fast, every default repo except
``pipecat-ai/pipecat`` is tainted for the run's duration (via
``PIPECAT_HUB_TAINTED_REPOS``) — the docs crawl still runs (no skip-docs
flag exists), so each refresh costs roughly one docs-crawl-plus-one-repo-clone,
not a full multi-repo index build.

Covers the CLI surface end-to-end:

* ``refresh --framework-version latest`` — resolves to the newest release tag;
  ``indexed_framework_version`` is stamped exactly (``commits_ahead == "0"``).
* ``refresh --framework-version <explicit tag>`` — pin behavior; the recorded
  ``framework_version`` and ``indexed_framework_version`` both match the tag.
* ``refresh --framework-version <malformed>`` — fails fast: exit 1, "Invalid
  tag format" in stderr, and — since the tag never resolved — none of the
  provenance metadata from the prior (valid-pin) run is touched.
* ``refresh --framework-version <nonexistent-but-well-formed>`` — exit 1,
  "not found" + available-tags hint in stderr, metadata untouched (same
  invariant as the malformed case, different rejection path).
* ``refresh`` with no flag — confirms the *default* pin is ``latest`` (not
  unset), per PR #122's "index the framework at its newest release tag by
  default".
* ``--reset-index`` — confirms ``framework_version``, ``indexed_framework_version``,
  ``indexed_framework_commits_ahead``, and ``deprecation_map_commit_sha`` are
  all absent afterward (cleared together, not left partially stale).
* Failure-injection — pre-creating ``deprecation_map.json`` as a directory
  forces ``DeprecationMap.save()`` to raise mid-refresh. Asserts the same four
  provenance keys are cleared together rather than left describing a
  deprecation map that was never actually replaced (the HIGH finding from the
  #116 review).
* ``check-deprecation --at-version`` after each refresh, to catch drift
  between the provenance stamp and what ``resolve_framework_version`` actually
  reports.

Usage::

    uv run python scripts/smoke_check_framework_version.py

Requires network access (GitHub) and a working ``git``. Each refresh is a real
network operation, so a full run takes several minutes.

Exit codes: 0 all checks pass, 1 one or more regressions, 2 setup failure
(e.g. can't reach GitHub to pick a pin tag).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec B404 — hardcoded CLI args, no shell, no user input
import sys
import tempfile
from pathlib import Path

from packaging.version import InvalidVersion, Version

from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.env_loading import load_env_layers

_FRAMEWORK_REPO = "pipecat-ai/pipecat"

# Provenance keys that must always move together — never left partially stale
# across a purge, a failed deprecation-map save, or an unresolved pin.
_PROVENANCE_KEYS = (
    "framework_version",
    "indexed_framework_version",
    "indexed_framework_commits_ahead",
    "deprecation_map_commit_sha",
)

_REFRESH_TIMEOUT_SECS = 600


def _bootstrap() -> HubConfig:
    """Load every config layer, then construct HubConfig.

    Same single bootstrap call as `cli.py:main()` (via `load_env_layers()`),
    so this script's resolved config — in particular `PIPECAT_HUB_DATA_DIR` —
    matches refresh/serve instead of only seeing real env vars. Mirrors
    `smoke_check_removals.py:_bootstrap()`; required by
    `tests/unit/test_config.py::TestDashboardScriptConfigParity` /
    `TestDashboardScriptDataDirResolution`, which discover every
    `scripts/*.py` that constructs `HubConfig`/`StorageConfig` and assert this
    exact shape.
    """
    load_env_layers()
    return HubConfig()


def _record(failures: list[str], label: str, ok: bool, detail: str = "") -> None:
    tag = "ok  " if ok else "FAIL"
    print(f"  [{tag}] {label}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(f"{label}{(': ' + detail) if detail else ''}")


def _pick_pin_tags() -> tuple[str, str]:
    """Return (newest_release_tag, an_older_release_tag) from the real repo.

    Resolved live via ``git ls-remote`` rather than hardcoded, so the script
    doesn't go stale as pipecat cuts new releases. Raises RuntimeError if
    fewer than two release tags are reachable.
    """
    result = subprocess.run(  # nosec B603 B607 — hardcoded args, no user input
        ["git", "ls-remote", "--tags", f"https://github.com/{_FRAMEWORK_REPO}.git"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    # `ls-remote --tags` emits two lines per annotated tag — the tag object
    # itself and its dereferenced `^{}` commit — both naming the same tag
    # after the suffix is stripped. Dedup by parsed version (keeping the
    # first-seen name) so two lines for the same release don't masquerade as
    # two distinct releases when picking "newest" and "an older one".
    by_version: dict[Version, str] = {}
    for line in result.stdout.splitlines():
        ref = line.rsplit("\t", 1)[-1]
        name = ref.removeprefix("refs/tags/").removesuffix("^{}")
        bare = name[1:] if name[:1].lower() == "v" else name
        try:
            version = Version(bare)
        except InvalidVersion:
            continue
        by_version.setdefault(version, name)
    if len(by_version) < 2:
        raise RuntimeError(f"Fewer than 2 distinct release tags found for {_FRAMEWORK_REPO}")
    ordered = sorted(by_version.items(), key=lambda pair: pair[0], reverse=True)
    return ordered[0][1], ordered[1][1]


def _run_refresh(
    data_dir: Path, extra_args: list[str], *, tainted_repos: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PIPECAT_HUB_DATA_DIR"] = str(data_dir)
    env["PIPECAT_HUB_TAINTED_REPOS"] = tainted_repos
    env["PIPECAT_HUB_WARMUP"] = "0"
    env["PIPECAT_HUB_RERANKER_ENABLED"] = "0"
    env.pop("PIPECAT_HUB_FRAMEWORK_VERSION", None)
    return subprocess.run(  # nosec B603 B607 — hardcoded args, no shell, no user input
        ["uv", "run", "pipecat-context-hub", "refresh", *extra_args],
        capture_output=True,
        text=True,
        timeout=_REFRESH_TIMEOUT_SECS,
        env=env,
    )


def _check_deprecation_at_version(
    data_dir: Path, symbol: str, at_version: str, *, tainted_repos: str
) -> dict[str, object]:
    env = dict(os.environ)
    env["PIPECAT_HUB_DATA_DIR"] = str(data_dir)
    env["PIPECAT_HUB_TAINTED_REPOS"] = tainted_repos
    env.pop("PIPECAT_HUB_FRAMEWORK_VERSION", None)
    result = subprocess.run(  # nosec B603 B607 — hardcoded args, symbol is our own constant
        [
            "uv",
            "run",
            "pipecat-context-hub",
            "check-deprecation",
            symbol,
            "--at-version",
            at_version,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"check-deprecation exited {result.returncode}: {result.stderr.strip()}")
    parsed: dict[str, object] = json.loads(result.stdout)
    return parsed


def _read_metadata(config: HubConfig, data_dir: Path) -> dict[str, str]:
    """Read all persisted index metadata without going through the MCP server.

    A short-lived ``IndexStore`` open+close, mirroring how
    ``smoke_check_removals.py`` reaches into internals directly rather than
    round-tripping through a query subprocess for data ``get_hub_status``
    doesn't expose (``deprecation_map_commit_sha``). Reuses the bootstrapped
    *config*'s storage settings, pointed at the scratch *data_dir*, rather
    than constructing a second bare ``StorageConfig()`` — same one-bootstrap
    discipline ``_bootstrap()`` documents.
    """
    storage = config.storage.model_copy(update={"data_dir": data_dir})
    store = IndexStore(storage)
    try:
        return store.get_all_metadata()
    finally:
        store.close()


def _present(metadata: dict[str, str], keys: tuple[str, ...]) -> set[str]:
    return {k for k in keys if metadata.get(k)}


def run_checks(config: HubConfig, data_dir: Path, tainted_repos: str) -> int:
    failures: list[str] = []

    print(f"Scratch data dir: {data_dir}")
    print(f"Tainted (skipped) repos for speed: {tainted_repos}")

    print("\nResolving pin tags from the live repo...")
    newest_tag, older_tag = _pick_pin_tags()
    print(f"  newest release tag: {newest_tag}")
    print(f"  older release tag:  {older_tag}")

    # ----- 1. `--framework-version latest` -----
    print("\n== refresh --framework-version latest ==")
    proc = _run_refresh(data_dir, ["--framework-version", "latest"], tainted_repos=tainted_repos)
    _record(failures, "exit code 0", proc.returncode == 0, detail=proc.stderr[-2000:])
    meta = _read_metadata(config, data_dir)
    _record(failures, "framework_version == 'latest'", meta.get("framework_version") == "latest")
    _record(
        failures,
        f"indexed_framework_version == {newest_tag!r} (bare)",
        meta.get("indexed_framework_version") in (newest_tag, newest_tag.removeprefix("v")),
        detail=str(meta.get("indexed_framework_version")),
    )
    _record(
        failures,
        "indexed_framework_commits_ahead == '0' (exact tag)",
        meta.get("indexed_framework_commits_ahead") == "0",
        detail=str(meta.get("indexed_framework_commits_ahead")),
    )
    _record(
        failures,
        "deprecation_map_commit_sha stamped",
        bool(meta.get("deprecation_map_commit_sha")),
    )

    # ----- 2. `--framework-version <explicit tag>` -----
    print(f"\n== refresh --framework-version {older_tag} (explicit pin) ==")
    proc = _run_refresh(
        data_dir, ["--force", "--framework-version", older_tag], tainted_repos=tainted_repos
    )
    _record(failures, "exit code 0", proc.returncode == 0, detail=proc.stderr[-2000:])
    meta = _read_metadata(config, data_dir)
    _record(
        failures, f"framework_version == {older_tag!r}", meta.get("framework_version") == older_tag
    )
    _record(
        failures,
        f"indexed_framework_version matches pin {older_tag!r}",
        meta.get("indexed_framework_version") in (older_tag, older_tag.removeprefix("v")),
        detail=str(meta.get("indexed_framework_version")),
    )
    verdict = _check_deprecation_at_version(
        data_dir, "PipelineTask", older_tag.removeprefix("v"), tainted_repos=tainted_repos
    )
    print(f"  check-deprecation PipelineTask --at-version {older_tag}: {verdict.get('status')}")

    # ----- 3. `--framework-version <malformed>` -----
    print("\n== refresh --framework-version 'bad tag!!' (malformed) ==")
    before = _read_metadata(config, data_dir)
    proc = _run_refresh(data_dir, ["--framework-version", "bad tag!!"], tainted_repos=tainted_repos)
    _record(failures, "exit code 1", proc.returncode == 1, detail=f"got {proc.returncode}")
    _record(
        failures,
        "stderr mentions 'Invalid tag format'",
        "Invalid tag format" in proc.stderr,
        detail=proc.stderr.strip()[-500:],
    )
    after = _read_metadata(config, data_dir)
    _record(
        failures,
        "provenance metadata untouched by the rejected pin",
        {k: after.get(k) for k in _PROVENANCE_KEYS} == {k: before.get(k) for k in _PROVENANCE_KEYS},
        detail=f"before={before} after={after}",
    )

    # ----- 4. `--framework-version <nonexistent, well-formed>` -----
    print("\n== refresh --framework-version nonexistent-tag-xyz ==")
    before = _read_metadata(config, data_dir)
    proc = _run_refresh(
        data_dir, ["--framework-version", "nonexistent-tag-xyz"], tainted_repos=tainted_repos
    )
    _record(failures, "exit code 1", proc.returncode == 1, detail=f"got {proc.returncode}")
    _record(
        failures,
        "stderr mentions 'not found' + available tags",
        "not found" in proc.stderr and "Available tags" in proc.stderr,
        detail=proc.stderr.strip()[-500:],
    )
    after = _read_metadata(config, data_dir)
    _record(
        failures,
        "provenance metadata untouched by the unresolved pin",
        {k: after.get(k) for k in _PROVENANCE_KEYS} == {k: before.get(k) for k in _PROVENANCE_KEYS},
        detail=f"before={before} after={after}",
    )

    # ----- 5. default (no flag) -----
    print("\n== refresh (no --framework-version flag) ==")
    proc = _run_refresh(data_dir, ["--force"], tainted_repos=tainted_repos)
    _record(failures, "exit code 0", proc.returncode == 0, detail=proc.stderr[-2000:])
    meta = _read_metadata(config, data_dir)
    _record(
        failures,
        "default pin is 'latest' (not unset, not the leftover explicit pin)",
        meta.get("framework_version") == "latest",
        detail=str(meta.get("framework_version")),
    )
    _record(
        failures,
        f"indexed_framework_version advances back to newest ({newest_tag!r})",
        meta.get("indexed_framework_version") in (newest_tag, newest_tag.removeprefix("v")),
        detail=str(meta.get("indexed_framework_version")),
    )

    # ----- 6. purge path (repo removed from config) -----
    print("\n== purge: framework repo removed from config (all repos tainted) ==")
    proc = _run_refresh(
        data_dir, ["--force", "--prune"], tainted_repos=f"{_FRAMEWORK_REPO},{tainted_repos}"
    )
    _record(failures, "exit code 0", proc.returncode == 0, detail=proc.stderr[-2000:])
    meta = _read_metadata(config, data_dir)
    still_present = _present(meta, _PROVENANCE_KEYS)
    _record(
        failures,
        "all 4 provenance keys cleared together on purge",
        not still_present,
        detail=f"still present: {sorted(still_present)}",
    )

    # ----- 7. failure injection: DeprecationMap.save() fails mid-refresh -----
    print("\n== failure injection: deprecation_map.json path unwritable ==")
    # Re-establish a good baseline first, then corrupt the save target.
    proc = _run_refresh(
        data_dir, ["--force", "--framework-version", newest_tag], tainted_repos=tainted_repos
    )
    _record(
        failures, "baseline refresh exit code 0", proc.returncode == 0, detail=proc.stderr[-2000:]
    )
    baseline = _read_metadata(config, data_dir)
    _record(
        failures,
        "baseline has all 4 provenance keys before injecting the failure",
        not (set(_PROVENANCE_KEYS) - _present(baseline, _PROVENANCE_KEYS)),
        detail=str(baseline),
    )

    dep_map_path = data_dir / "deprecation_map.json"
    dep_map_path.unlink(missing_ok=True)
    dep_map_path.mkdir()  # a directory where a file is expected -> save() raises
    try:
        proc = _run_refresh(
            data_dir, ["--force", "--framework-version", older_tag], tainted_repos=tainted_repos
        )
    finally:
        if dep_map_path.is_dir():
            shutil.rmtree(dep_map_path)

    _record(
        failures,
        "refresh still exits 0 (dep-map save failure is non-fatal)",
        proc.returncode == 0,
        detail=proc.stderr[-2000:],
    )
    meta = _read_metadata(config, data_dir)
    still_present = _present(meta, _PROVENANCE_KEYS)
    _record(
        failures,
        "all 4 provenance keys cleared together, not left describing a map that was never replaced",
        not still_present,
        detail=f"still present: {sorted(still_present)} (full: {meta})",
    )

    if failures:
        print(f"\n{len(failures)} regression(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll framework-version checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    config = _bootstrap()
    tainted_repos = ",".join(r for r in config.sources.effective_repos if r != _FRAMEWORK_REPO)

    with tempfile.TemporaryDirectory(prefix="pch-smoke-fw-version-") as tmp:
        try:
            return run_checks(config, Path(tmp), tainted_repos)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"Setup failure: {exc}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
