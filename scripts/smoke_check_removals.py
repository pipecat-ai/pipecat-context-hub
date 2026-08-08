#!/usr/bin/env python3
"""Live-hub smoke test for version-aware ``check_deprecation`` removal history.

Companion to ``scripts/smoke_check_deprecation.py``. Where that script asserts
the boolean ``deprecated`` verdict against the persisted map, this one exercises
the **removal lifecycle** added in PR #88: reading pipecat's ``removals.json``
ledger and answering ``check_deprecation`` relative to a pipecat *version*
(``current`` / ``deprecated`` / ``removed``).

It builds a map from the **real** ``deprecations.json`` in the cloned framework
repo (so the active-deprecation data is genuine), then merges a **synthetic**
``removals.json`` fixture. The synthetic fixture is necessary because upstream
``removals.json`` is still empty (pipecat is on 1.x — nothing has actually been
removed yet), so a live run could never reach the ``removed`` branch. The script
therefore prints the real file's state (to confirm the dormant no-op) and uses
the fixture only to drive the version-relative assertions.

It does **not** mutate the persisted ``deprecation_map.json`` or hit the network
— the merge happens in an in-memory map, so a running MCP server is unaffected.

Three things are checked, all the failure modes the feature can have:

* REMOVED lifecycle — a removed symbol reports ``current`` before its
  ``deprecated_in``, ``deprecated`` in the window, and ``removed`` at/after its
  ``removed_in``.
* SAFETY INVARIANT — an *active* deprecation (still in ``deprecations.json``)
  must NEVER report ``removed``, even past its *announced* ``removed_in``, since
  that version is only a promise, not evidence of removal. Checked on a real
  active deprecation pulled from the registry.
* CLOBBER GUARD — a removal whose bare name collides with a *different* active
  deprecation (same name, different module) must not shadow it on the bare key;
  the removal stays resolvable by its fully-qualified path.

Both ``status_for`` (pure) and the real ``handle_check_deprecation`` handler are
exercised, so the response-shaping path is covered too.

Usage::

    uv run python scripts/smoke_check_removals.py          # full run

Requires a prior ``refresh`` so the framework registry is present on disk.
Exit codes: 0 all checks pass, 1 one or more regressions, 2 registry not found.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

from pipecat_context_hub.server.tools.check_deprecation import handle_check_deprecation
from pipecat_context_hub.services.ingest.deprecation_map import (
    REGISTRY_RELATIVE_PATH,
    REMOVALS_RELATIVE_PATH,
    add_removals_from_registry,
    build_deprecation_map_from_registry,
    status_for,
)
from pipecat_context_hub.services.ingest.github_ingest import _FRAMEWORK_REPO
from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.env_loading import load_env_layers

# A removed symbol that is absent from the real active registry — mirrors how the
# producer (pipecat#4734) only emits removals for subjects no longer in
# deprecations.json. ``removed_in`` is the actual release; ``announced_removed_in``
# is the version the deprecation originally promised.
_REMOVED_SUBJECT = "OldGoneService"
_REMOVED_MODULE = "pipecat.services.old.gone"
_SYNTHETIC_REMOVALS = {
    "schema_version": 1,
    "removals": [
        {
            "subject": _REMOVED_SUBJECT,
            "module": _REMOVED_MODULE,
            "kind": "class",
            "deprecated_in": "1.0.0",
            "removed_in": "2.0.0",
            "announced_removed_in": "2.0.0",
            "relation": "use_existing",
            "replacement": "NewService",
            "message": f"`{_REMOVED_SUBJECT}` was deprecated since 1.0.0",
        }
    ],
}


def _bootstrap() -> HubConfig:
    """Load every config layer, then construct HubConfig.

    Same single bootstrap call as `cli.py:main()`, so this script's resolved
    config (in particular `PIPECAT_HUB_DATA_DIR`) matches refresh/serve
    instead of only seeing real env vars. The two-call ordering lives inside
    `load_env_layers()` rather than being hand-replicated per entry point.
    """
    load_env_layers()
    return HubConfig()


def _registry_path(config: HubConfig) -> Path:
    """Resolve the real ``deprecations.json`` in the cloned framework checkout.

    Takes the already-bootstrapped config rather than calling ``_bootstrap()``
    itself: mutating process-wide ``os.environ`` must not be a side effect of
    asking a leaf helper for a path. ``main()`` owns the bootstrap.
    """
    data_dir = config.storage.data_dir
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", _FRAMEWORK_REPO)
    return data_dir / "repos" / safe_name / REGISTRY_RELATIVE_PATH


def _record(failures: list[str], label: str, ok: bool, detail: str = "") -> None:
    tag = "ok  " if ok else "FAIL"
    print(f"  [{tag}] {label}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(f"{label}{(': ' + detail) if detail else ''}")


async def _status_via_handler(
    dep_map: object, symbol: str, version: str | None
) -> dict[str, object]:
    """Run the real handler and return the parsed verdict."""
    args: dict[str, object] = {"symbol": symbol}
    if version is not None:
        args["version"] = version
    verdict: dict[str, object] = json.loads(await handle_check_deprecation(args, dep_map))
    return verdict


async def run_checks(registry: Path) -> int:
    failures: list[str] = []

    dep_map = build_deprecation_map_from_registry(registry)
    print(f"Real registry: {len(dep_map.entries)} lookup keys from {registry.name}")

    # Confirm the real removals ledger is dormant (empty / absent) — the no-op case.
    real_removals = registry.parent.parent.parent / REMOVALS_RELATIVE_PATH
    if real_removals.exists():
        try:
            count = len(json.loads(real_removals.read_text()).get("removals", []))
        except Exception:
            count = -1
        print(f"Real removals.json present with {count} record(s) (0 = dormant no-op).")
    else:
        print("Real removals.json absent (dormant; merge is a no-op upstream).")

    # Merge the synthetic fixture into the in-memory map (never persisted).
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / "removals.json"
        fixture.write_text(json.dumps(_SYNTHETIC_REMOVALS))
        add_removals_from_registry(dep_map, fixture)

    # Pick a real active deprecation (still in deprecations.json) for the safety
    # invariant + clobber checks — robust to registry drift.
    active = next(
        (
            e
            for e in dep_map.entries.values()
            if e.status == "deprecated" and e.deprecated_in and e.removed_in
        ),
        None,
    )

    print("\n== REMOVED lifecycle (synthetic; expect status to track the version) ==")
    rem = dep_map.check(_REMOVED_SUBJECT)
    _record(
        failures,
        f"{_REMOVED_SUBJECT} resolves as removed",
        rem is not None and rem.status == "removed",
    )
    if rem is not None:
        _record(failures, "removed at removed_in (2.0.0)", status_for(rem, "2.0.0") == "removed")
        _record(failures, "removed past removal (2.5.0)", status_for(rem, "2.5.0") == "removed")
        _record(failures, "deprecated in window (1.5.0)", status_for(rem, "1.5.0") == "deprecated")
        _record(
            failures, "current before deprecation (0.9.0)", status_for(rem, "0.9.0") == "current"
        )
        _record(
            failures,
            "fully-qualified path resolves",
            dep_map.check(f"{_REMOVED_MODULE}.{_REMOVED_SUBJECT}") is not None,
        )
        r = await _status_via_handler(dep_map, _REMOVED_SUBJECT, "2.0.0")
        _record(
            failures,
            "handler @2.0.0 -> removed + migration note",
            r["status"] == "removed"
            and bool(r["deprecated"])
            and "was removed in 2.0.0" in str(r.get("note") or ""),
            detail=str(r),
        )

    print("\n== SAFETY INVARIANT (real active deprecation; never 'removed') ==")
    if active is None:
        _record(failures, "found a real active deprecation to test", False, "none in registry")
    else:
        fq = f"{active.old_path}"  # bare; also test via handler
        _record(
            failures,
            f"{active.old_path} stays 'deprecated' past announced removal (status_for @2.5.0)",
            status_for(active, "2.5.0") == "deprecated",
            detail=f"announced removed_in={active.removed_in}",
        )
        r = await _status_via_handler(dep_map, fq, "2.5.0")
        _record(
            failures,
            f"handler: {active.old_path} @2.5.0 -> deprecated (not removed)",
            r["status"] == "deprecated",
            detail=str(r.get("status")),
        )

    print("\n== CLOBBER GUARD (removal must not shadow an active deprecation) ==")
    if active is None:
        _record(failures, "found a real active deprecation to collide with", False)
    else:
        # Build a fresh map and merge a removal whose bare name collides with the
        # active deprecation but lives in a different module.
        dm2 = build_deprecation_map_from_registry(registry)
        collide = {
            "schema_version": 1,
            "removals": [
                {
                    "subject": active.old_path,
                    "module": "pipecat.unrelated.other",
                    "kind": "class",
                    "deprecated_in": "1.0.0",
                    "removed_in": "2.0.0",
                    "announced_removed_in": "2.0.0",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "removals.json"
            fixture.write_text(json.dumps(collide))
            add_removals_from_registry(dm2, fixture)
        bare = dm2.check(active.old_path)
        _record(
            failures,
            f"bare {active.old_path!r} still resolves to the active deprecation",
            bare is not None and bare.status == "deprecated",
            detail=f"got status={bare.status if bare else None}",
        )
        fq_removed = dm2.check(f"pipecat.unrelated.other.{active.old_path}")
        _record(
            failures,
            "colliding removal still resolves by its fully-qualified path",
            fq_removed is not None and fq_removed.status == "removed",
        )

    if failures:
        print(f"\n{len(failures)} regression(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll removal checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    registry = _registry_path(_bootstrap())
    if not registry.exists():
        print(
            f"Framework registry not found at {registry}.\n"
            "Run `uv run pipecat-context-hub refresh` first to clone the framework repo.",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(run_checks(registry))


if __name__ == "__main__":
    sys.exit(main())
