"""Run a read-only Pipecat Cloud JSON command without exposing raw output."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

_FIELDS: dict[str, frozenset[str]] = {
    "organizations": frozenset({"id", "name"}),
    "regions": frozenset({"id", "name", "slug", "architecture"}),
    "profiles": frozenset({"id", "name", "cpu", "memory"}),
    "agents": frozenset({"id", "name", "status", "region"}),
    "secret-sets": frozenset({"id", "name", "keys"}),
}
_CONTAINERS = frozenset({"data", "items", "results", *_FIELDS})
_SCALARS = (str, int, float, bool)


def _records(value: Any, fields: frozenset[str]) -> list[dict[str, Any]]:
    """Project recognized JSON objects onto one allowlisted metadata schema."""
    projected: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            projected.extend(_records(item, fields))
        return projected
    if not isinstance(value, Mapping):
        return projected

    record: dict[str, Any] = {}
    for key in fields:
        candidate = value.get(key)
        if isinstance(candidate, _SCALARS) and not isinstance(candidate, bytes):
            record[key] = candidate
        elif (
            key == "keys"
            and isinstance(candidate, list)
            and all(isinstance(item, str) for item in candidate)
        ):
            record[key] = candidate
    if record and ("id" in record or "name" in record):
        projected.append(record)

    for key, candidate in value.items():
        if key in _CONTAINERS:
            projected.extend(_records(candidate, fields))
    return projected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=sorted(_FIELDS))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("command failed")
        payload = json.loads(result.stdout)
        records = _records(payload, _FIELDS[args.kind])
        if not records:
            raise ValueError("no allowlisted records")
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError):
        print(
            "Cloud metadata unavailable: command failed or returned an unsafe schema.",
            file=sys.stderr,
        )
        return 1

    json.dump({args.kind: records}, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
