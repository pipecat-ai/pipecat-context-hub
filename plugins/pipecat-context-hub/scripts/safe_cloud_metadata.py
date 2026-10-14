"""Run a read-only Pipecat Cloud JSON command without exposing raw output."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import selectors
import time
from collections.abc import Mapping
from typing import Any

_FIELDS: dict[str, frozenset[str]] = {
    "organizations": frozenset({"id", "name"}),
    "regions": frozenset({"id", "name", "slug", "architecture"}),
    "profiles": frozenset({"id", "name", "cpu", "memory"}),
    "agents": frozenset({"id", "name", "status", "region"}),
    "secret-sets": frozenset({"id", "name", "keys"}),
}
_GENERIC_CONTAINERS = frozenset({"data", "items", "results"})
_SCALARS = (str, int, float, bool)
_MAX_OUTPUT_BYTES = 1_048_576
_MAX_RECORDS = 1_000
_MAX_DEPTH = 32


def _run_bounded(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run a producer while retaining at most the allowlisted output size."""
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True
    )
    assert process.stdout is not None
    stdout_fd = process.stdout.fileno()
    os.set_blocking(stdout_fd, False)
    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 30
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 30)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, 30)
            try:
                chunk = os.read(stdout_fd, 64 * 1024)
            except BlockingIOError:
                continue
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > _MAX_OUTPUT_BYTES:
                raise ValueError("command output too large")
        returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
        return subprocess.CompletedProcess(command, returncode, bytes(output), b"")
    except (Exception,):
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()


def _records(
    value: Any,
    fields: frozenset[str],
    kind: str,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Project allowlisted metadata with bounded nesting and record count."""
    if budget is None:
        budget = [_MAX_RECORDS]
    if depth > _MAX_DEPTH:
        raise ValueError("metadata nesting too deep")
    projected: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            projected.extend(_records(item, fields, kind, depth=depth + 1, budget=budget))
        return projected
    if not isinstance(value, Mapping):
        return projected

    # Envelope objects can themselves carry id/name fields (request IDs,
    # account metadata, etc.).  Only mappings that are not also containers may
    # be interpreted as a resource record.
    has_container = any(key in _GENERIC_CONTAINERS or key in _FIELDS for key in value)
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
    if not has_container and record and ("id" in record or "name" in record):
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("too many metadata records")
        projected.append(record)

    for key, candidate in value.items():
        # Generic API envelopes may contain the requested resource.  A
        # different resource-kind key is intentionally opaque: its records
        # must never be reinterpreted as this request's kind.
        if key in _GENERIC_CONTAINERS or key == kind:
            projected.extend(_records(candidate, fields, kind, depth=depth + 1, budget=budget))
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
        result = _run_bounded(command)
        if result.returncode != 0:
            raise ValueError("command failed")
        if len(result.stdout) > _MAX_OUTPUT_BYTES:
            raise ValueError("command output too large")
        payload = json.loads(result.stdout.decode("utf-8"))
        records = _records(payload, _FIELDS[args.kind], args.kind)
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
