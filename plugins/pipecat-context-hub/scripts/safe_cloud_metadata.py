"""Run a read-only Pipecat Cloud JSON command without exposing raw output."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import selectors
import shutil
import signal
import threading
import time
from collections.abc import Mapping
from pathlib import Path
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
_SECRET_ENV = re.compile(
    r"(?:TOKEN|API[_-]?KEY|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY|CREDENTIAL|"
    r"ACCESS[_-]?KEY|AUTH(?:ORIZATION|[_-]?TOKEN)?|DOCKER[_-]?AUTH|"
    r"GOOGLE_APPLICATION_CREDENTIALS|SSH_AUTH_SOCK|DATABASE[_-]?URL|"
    r"DSN|JWT|COOKIE|SESSION)",
    re.IGNORECASE,
)
_PASSTHROUGH_ENV = frozenset({"PIPECAT_TOKEN"})
_SAFE_RUNTIME_ENV = frozenset(
    {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TMP", "TEMP"}
)


_READ_ONLY_CLOUD_COMMANDS = frozenset(
    {
        ("organizations", "list"),
        ("regions", "list"),
        ("agent", "list"),
        ("agent", "profiles", "list"),
        ("secrets", "list"),
    }
)
_READ_ONLY_OPTIONS = frozenset({"--organization", "--region"})


def _validate_producer_command(command: list[str]) -> None:
    """Allow only the installed Pipecat CLI and read-only Cloud commands.

    A basename check is not sufficient: ``/tmp/pipecat`` can be an arbitrary
    executable that exfiltrates ``PIPECAT_TOKEN``.  Resolve the command the
    same way the shell does and require it to be the installed executable.
    Then accept only the documented metadata list commands and their
    read-only selector options; mutating commands must never run under the
    credential-bearing environment.
    """
    if not command:
        raise ValueError("metadata producer must be the pipecat CLI")
    installed = shutil.which("pipecat")
    if installed is None:
        raise ValueError("metadata producer must be the pipecat CLI")
    requested = Path(command[0])
    if requested.name.casefold() not in {"pipecat", "pipecat.exe"}:
        raise ValueError("metadata producer must be the pipecat CLI")
    try:
        if requested.is_absolute():
            if requested.resolve() != Path(installed).resolve():
                raise ValueError("metadata producer must be the installed pipecat CLI")
        elif requested.parent != Path("."):
            raise ValueError("metadata producer must be the installed pipecat CLI")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("metadata producer"):
            raise
        raise ValueError("metadata producer must be the installed pipecat CLI") from exc

    args = command[1:]
    if args[:1] != ["cloud"]:
        raise ValueError("metadata command is not read-only")
    args = args[1:]
    # The output mode is a global Cloud option and must be explicitly JSON;
    # accepting the environment default could produce human-facing output.
    if args[:2] == ["--output", "json"]:
        args = args[2:]
    elif args[:1] == ["--output=json"]:
        args = args[1:]
    else:
        raise ValueError("metadata command must request JSON output")

    command_words: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        if word in _READ_ONLY_OPTIONS:
            if index + 1 >= len(args) or not args[index + 1] or args[index + 1].startswith("-"):
                raise ValueError("metadata command has an unsafe option")
            index += 2
            continue
        if word.startswith("-"):
            raise ValueError("metadata command has an unapproved option")
        command_words.append(word)
        index += 1
    if tuple(command_words) not in _READ_ONLY_CLOUD_COMMANDS:
        raise ValueError("metadata command is not read-only")


def _safe_child_environment() -> dict[str, str]:
    """Pass runtime settings plus only the documented PAT credential."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _PASSTHROUGH_ENV
        or key in _SAFE_RUNTIME_ENV
        or (key.startswith("LC_") and not _SECRET_ENV.search(key))
    }


def _run_bounded(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run a producer while retaining at most the allowlisted output size."""
    process: subprocess.Popen[bytes] | None = None
    stdout = None
    selector: selectors.BaseSelector | None = None
    deadline = time.monotonic() + 30
    try:
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        # The command is intentionally treated as an untrusted producer: even
        # an allowlisted JSON field must not become a side channel for a
        # credential inherited from the agent process.
        popen_kwargs["env"] = _safe_child_environment()
        process = subprocess.Popen(command, **popen_kwargs)
        stdout = process.stdout
        assert stdout is not None
        stdout_fd = stdout.fileno()
        output = bytearray()

        def read_blocking() -> subprocess.CompletedProcess[bytes]:
            """Drain an unselectable pipe without changing its blocking mode."""
            result_queue: queue.Queue[tuple[str, bytes | BaseException | None]] = queue.Queue(1)

            def read_pipe() -> None:
                data = bytearray()
                try:
                    while True:
                        chunk = stdout.read(64 * 1024)
                        if not chunk:
                            result_queue.put(("ok", bytes(data)))
                            return
                        data.extend(chunk)
                        if len(data) > _MAX_OUTPUT_BYTES:
                            result_queue.put(("too_large", None))
                            return
                except BaseException as exc:
                    result_queue.put(("error", exc))

            threading.Thread(target=read_pipe, daemon=True).start()
            try:
                kind, value = result_queue.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise subprocess.TimeoutExpired(command, 30)
            if kind == "too_large":
                raise ValueError("command output too large")
            if kind == "error":
                assert isinstance(value, BaseException)
                raise value
            assert isinstance(value, bytes)
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
            return subprocess.CompletedProcess(command, returncode, value, b"")

        # ``SelectSelector`` cannot wait on anonymous subprocess pipes on
        # Windows.  Choose the blocking reader before constructing a selector
        # so the platform never enters a selector loop that is guaranteed to
        # fail on its first ``select()`` call.
        if os.name == "nt":
            return read_blocking()

        try:
            os.set_blocking(stdout_fd, False)
            selector = selectors.DefaultSelector()
            selector.register(stdout, selectors.EVENT_READ)
        except (NotImplementedError, OSError):
            # Anonymous subprocess pipes are not selectable on Windows.
            # A bounded reader thread keeps draining the pipe while the main
            # thread enforces the timeout and terminates oversized producers.
            try:
                os.set_blocking(stdout_fd, True)
            except (NotImplementedError, OSError):
                # Windows 3.11 may reject both modes for anonymous pipes; they
                # are blocking by default, so the reader remains safe here.
                pass
            return read_blocking()

        stdout_fd = stdout.fileno()
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
    except BaseException:
        if process is not None:
            _terminate_process_tree(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                process.wait()
        raise
    finally:
        if selector is not None:
            selector.close()
        if stdout is not None:
            stdout.close()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate the bounded command and descendants as one process group."""
    if os.name == "nt":
        # ``CREATE_NEW_PROCESS_GROUP`` groups descendants on Windows; taskkill
        # is the supported way to terminate that tree from Python 3.11.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        if process.poll() is None:
            process.kill()


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
        _validate_producer_command(command)
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
