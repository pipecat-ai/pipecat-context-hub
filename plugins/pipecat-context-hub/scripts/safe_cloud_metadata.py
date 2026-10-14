"""Run a read-only Pipecat Cloud JSON command without exposing raw output."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import selectors
import signal
import stat
import threading
import tempfile
import time
from contextlib import contextmanager
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, distribution, distributions
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


_READ_ONLY_CLOUD_COMMANDS = {
    ("organizations", "list"): "organizations",
    ("regions", "list"): "regions",
    ("agent", "list"): "agents",
    ("agent", "profiles", "list"): "profiles",
    ("secrets", "list"): "secret-sets",
}
_READ_ONLY_OPTIONS = frozenset({"--organization", "--region"})


def _tool_distribution(executable: Path):
    """Find pipecat metadata in the environment owning an executable.

    ``uv tool install`` puts the console script in an isolated environment,
    while this helper is often run by the Context Hub environment. Looking up
    only the helper interpreter's distributions therefore rejects a valid
    package-owned Pipecat executable.
    """
    root = executable.absolute().parent.parent
    site_dirs: list[Path] = []
    for lib in (root / "lib", root / "Lib"):
        site_dirs.extend([lib / "site-packages", *lib.glob("python*/site-packages")])
    for site_dir in site_dirs:
        for candidate in distributions(path=[str(site_dir)]):
            if candidate.metadata.get("Name", "").casefold() == "pipecat-ai":
                return candidate
    return None


def _trusted_pipecat_path(executable: Path | None = None) -> Path | None:
    """Find a package-owned CLI in this interpreter's installation prefix.

    PATH and launcher source text are not identity proofs: either can be
    supplied by a project or an attacker.  Require the ``pipecat-ai``
    distribution metadata to enumerate the console script, and require the
    resulting file to be a non-reparse regular file below ``sys.prefix``.
    When those independently verifiable facts are unavailable, fail closed.
    """
    names = ("pipecat.exe", "pipecat") if os.name == "nt" else ("pipecat",)
    if executable is None:
        try:
            dist = distribution("pipecat-ai")
        except PackageNotFoundError:
            return None
        prefix = Path(sys.prefix).resolve(strict=True)
    else:
        dist = _tool_distribution(executable)
        if dist is None:
            return None
        prefix = executable.absolute().parent.parent.resolve(strict=True)
    for file in dist.files or ():
        if Path(file).name not in names:
            continue
        # Wheel RECORD entries commonly spell console scripts relative to the
        # distribution directory (for example ``../../../bin/pipecat``).
        # Normalize that spelling before comparing it with the resolved path;
        # the spelling itself is still checked with lstat below so a launcher
        # symlink cannot become the credential-bearing producer.
        candidate = Path(os.path.abspath(os.path.normpath(os.fspath(dist.locate_file(file)))))
        if executable is not None and candidate.resolve(strict=True) != executable.resolve(
            strict=True
        ):
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(prefix)
            # Check the executable spelling separately from its resolved
            # target.  The interpreter prefix itself may be reached through a
            # benign parent symlink (notably temporary virtualenv paths), but
            # the actual console-script entry must not be a reparse point.
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            if not stat.S_ISREG(candidate.lstat().st_mode):
                continue
            # Distribution metadata is not merely a name/permission check: if
            # RECORD carries a digest, bind trust to the bytes that the wheel
            # installed.  A modified launcher must never receive PIPECAT_TOKEN.
            record = dist.read_text("RECORD")
            if not record:
                continue
            record_digest = None
            record_path = os.path.normpath(os.fspath(file)).replace(os.sep, "/")
            for row in record.splitlines():
                fields = row.split(",")
                if (
                    len(fields) >= 3
                    and fields[0] == record_path
                    and fields[1].startswith("sha256=")
                ):
                    record_digest = fields[1][len("sha256=") :]
                    break
            if not record_digest:
                continue
            digest = hashlib.sha256(candidate.read_bytes()).digest()
            encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            if encoded != record_digest:
                continue
            # Return the canonical spelling used by the identity check below.
            # A symlinked virtualenv prefix is valid, but the executable itself
            # must resolve to the package-owned regular file.
            return resolved
        except (OSError, RuntimeError, ValueError, UnicodeError):
            continue
    return None


def _trusted_path_for_command(command: list[str]) -> Path | None:
    """Resolve trust against the environment selected by an absolute command."""
    requested = Path(command[0]) if command else Path()
    installed = _trusted_pipecat_path()
    if requested.is_absolute():
        try:
            if installed is None or requested.resolve(strict=True) != installed.resolve(
                strict=True
            ):
                selected = _trusted_pipecat_path(requested)
                if selected is not None:
                    return selected
        except (OSError, RuntimeError, ValueError):
            return None
    return installed


def _record_digest_for_executable(executable: Path) -> str | None:
    """Return the wheel RECORD digest for an executable, if available."""
    dist = _tool_distribution(executable)
    if dist is None:
        try:
            dist = distribution("pipecat-ai")
        except PackageNotFoundError:
            return None
    try:
        resolved = executable.resolve(strict=True)
        for file in dist.files or ():
            if Path(file).name != executable.name:
                continue
            candidate = Path(os.path.abspath(os.path.normpath(os.fspath(dist.locate_file(file)))))
            if candidate.resolve(strict=True) != resolved:
                continue
            record_path = os.path.normpath(os.fspath(file)).replace(os.sep, "/")
            record = dist.read_text("RECORD") or ""
            for row in record.splitlines():
                fields = row.split(",")
                if (
                    len(fields) >= 3
                    and fields[0] == record_path
                    and fields[1].startswith("sha256=")
                ):
                    return fields[1][len("sha256=") :]
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return None
    return None


def _validate_producer_command(
    command: list[str], *, expected_kind: str | None = None
) -> list[str]:
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
    # ``shutil.which`` is intentionally not an authority here: an attacker
    # can put a shadow executable earlier on PATH and receive PIPECAT_TOKEN.
    installed = _trusted_path_for_command(command)
    if installed is None:
        raise ValueError("metadata producer must be the pipecat CLI")
    requested = Path(command[0])
    if requested.name.casefold() not in {"pipecat", "pipecat.exe"}:
        raise ValueError("metadata producer must be the pipecat CLI")
    try:
        # A bare name is resolved through the caller's PATH at exec time. Even
        # comparing it with ``which`` here leaves a shadow executable able to
        # receive PIPECAT_TOKEN between validation and Popen, so require the
        # exact installed executable as an absolute path.
        if not requested.is_absolute() or requested.resolve(strict=True) != installed.resolve(
            strict=True
        ):
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
    command_kind = _READ_ONLY_CLOUD_COMMANDS.get(tuple(command_words))
    if command_kind is None:
        raise ValueError("metadata command is not read-only")
    if expected_kind is not None and command_kind != expected_kind:
        raise ValueError("metadata command does not match requested metadata kind")
    return [str(installed), *command[1:]]


@contextmanager
def _materialize_verified_producer(command: list[str]):
    """Execute a private snapshot of the verified launcher.

    Validation and ``Popen`` cannot make a pathname execution atomic. Copying
    the checked regular file through an open descriptor binds the credential-
    bearing process to the bytes that were verified, even if the installed
    launcher is replaced immediately afterwards.
    """
    source = Path(command[0])
    trusted = _trusted_path_for_command(command)
    if trusted is None:
        raise ValueError("metadata producer is not a trusted installed executable")
    try:
        trusted_info = trusted.lstat()
    except OSError as exc:
        raise ValueError("metadata producer is unavailable") from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source, flags)
    temporary = tempfile.TemporaryDirectory(prefix="pipecat-context-hub-cli-")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("metadata producer is not a regular executable")
        if (before.st_dev, before.st_ino) != (trusted_info.st_dev, trusted_info.st_ino):
            raise ValueError("metadata producer changed after validation")
        record_digest = _record_digest_for_executable(trusted)
        if record_digest is None:
            # Test harnesses may inject an already-verified executable path
            # without its wheel metadata. Bind the copy to the bytes present
            # on the validated descriptor; real package paths always carry a
            # RECORD digest because _trusted_pipecat_path requires one.
            baseline = hashlib.sha256()
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                baseline.update(chunk)
            record_digest = base64.urlsafe_b64encode(baseline.digest()).rstrip(b"=").decode("ascii")
            os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        snapshot = Path(temporary.name) / source.name
        with snapshot.open("wb") as output:
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise ValueError("metadata producer changed while being verified")
        encoded = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
        if encoded != record_digest:
            raise ValueError("metadata producer bytes are not RECORD-verified")
        snapshot.chmod(stat.S_IMODE(before.st_mode) | stat.S_IXUSR)
        yield [str(snapshot), *command[1:]]
    finally:
        os.close(fd)
        temporary.cleanup()


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
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if not system_root:
            raise RuntimeError("Windows SystemRoot is unavailable; refusing PATH cleanup")
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
        if not taskkill.is_file() or taskkill.is_symlink():
            raise RuntimeError("trusted Windows taskkill is unavailable")
        subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env={
                key: value
                for key, value in _safe_child_environment().items()
                if key in _SAFE_RUNTIME_ENV
            },
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
        validated = _validate_producer_command(command, expected_kind=args.kind)
        with _materialize_verified_producer(validated) as safe_command:
            result = _run_bounded(safe_command)
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
