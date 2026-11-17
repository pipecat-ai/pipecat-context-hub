"""Validate and merge Context Hub session bundles received from participants.

Bundles come from other people: they are treated as untrusted data. Tarballs are read member by
member in memory (never extracted to disk by path), only the five known flat filenames are
accepted, links and oversized members are rejected, and JSONL rows must parse and carry the
required keys. ``merge_bundles`` writes ``calls.jsonl`` and ``sessions.jsonl`` (merged,
de-duplicated by ``ref``, sorted by severity) and ``ingest.json`` listing every source with its
sha256 and any rejection.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA = 1
FILES = ("manifest.json", "SCHEMA.md", "report.md", "calls.jsonl", "sessions.jsonl")
MAX_MEMBER_BYTES = 50 * 1024 * 1024
CALL_KEYS = {"ref", "tool", "via", "flags", "severity", "result", "followups"}
SESSION_KEYS = {"harness", "hub_used", "hub_calls", "flag_counts"}


def read_tar(path: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as tf:
        for m in tf.getmembers():
            if m.name not in FILES:
                raise ValueError(f"unexpected member {m.name!r}")
            if not m.isreg():
                raise ValueError(f"member {m.name!r} is not a regular file")
            if m.size > MAX_MEMBER_BYTES:
                raise ValueError(f"member {m.name!r} too large ({m.size} bytes)")
            fh = tf.extractfile(m)
            if fh is None:
                raise ValueError(f"member {m.name!r} is unreadable")
            out[m.name] = fh.read(MAX_MEMBER_BYTES + 1)
    return out


def read_dir(path: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name in FILES:
        f = path / name
        if f.is_symlink() or not f.is_file():
            continue
        if f.stat().st_size > MAX_MEMBER_BYTES:
            raise ValueError(f"{name} too large")
        out[name] = f.read_bytes()
    return out


def parse_jsonl(data: bytes, required: set[str], name: str) -> list[dict[str, Any]]:
    rows = []
    for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{name} line {i}: not JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{name} line {i}: not a JSON object")
        if not required <= row.keys():
            raise ValueError(f"{name} line {i}: missing keys {sorted(required - row.keys())}")
        rows.append(row)
    return rows


def load(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], str]:
    if path.is_dir():
        files = read_dir(path)
        digest = hashlib.sha256(b"".join(files[n] for n in sorted(files))).hexdigest()
    else:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files = read_tar(path)
    missing = [n for n in ("manifest.json", "sessions.jsonl") if n not in files]
    if missing:
        raise ValueError(f"missing {missing}")
    manifest = json.loads(files["manifest.json"])
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json is not an object")
    if "calls.jsonl" not in files:
        # A gist cannot hold an empty file, so `audit gist` omits calls.jsonl when it is empty.
        # `call_rows` is the row count; `hub_calls` (older bundles) also counts text-scan mentions.
        counts = manifest.get("counts")
        counts = counts if isinstance(counts, dict) else {}
        if counts.get("call_rows", counts.get("hub_calls")) != 0:
            raise ValueError("missing ['calls.jsonl']")
        files["calls.jsonl"] = b""
    if manifest.get("schema_version") != SUPPORTED_SCHEMA:
        raise ValueError(
            f"schema_version {manifest.get('schema_version')!r} unsupported (expected {SUPPORTED_SCHEMA})"
        )
    calls = parse_jsonl(files["calls.jsonl"], CALL_KEYS, "calls.jsonl")
    sessions = parse_jsonl(files["sessions.jsonl"], SESSION_KEYS, "sessions.jsonl")
    for i, row in enumerate(calls, 1):
        # `ref` keys the merge and `severity` orders it: a wrong type would crash the whole run.
        sev = row.get("severity")
        if not isinstance(row["ref"], str):
            raise ValueError(f"calls.jsonl line {i}: ref must be a string")
        if sev is not None and (isinstance(sev, bool) or not isinstance(sev, int | float)):
            raise ValueError(f"calls.jsonl line {i}: severity must be a number")
    return manifest, calls, sessions, digest


def merge_bundles(bundles: Sequence[Path], out: Path) -> dict[str, Any]:
    """Merge ``bundles`` into ``out``; returns counts plus the per-source report."""
    calls: dict[str, dict[str, Any]] = {}
    sessions: dict[str, dict[str, Any]] = {}
    report: list[dict[str, Any]] = []
    for b in bundles:
        try:
            manifest, c, s, digest = load(b)
        except (ValueError, OSError, tarfile.TarError, EOFError, zlib.error, RecursionError) as exc:
            report.append({"source": b.name, "status": "rejected", "reason": str(exc)})
            continue
        new_calls = 0
        for row in c:
            if row["ref"] not in calls:
                calls[row["ref"]] = row
                new_calls += 1
        for row in s:
            sessions.setdefault(f"{row.get('participant')}/{row.get('session')}", row)
        report.append(
            {
                "source": b.name,
                "status": "ok",
                "sha256": digest,
                "calls": len(c),
                "new_calls": new_calls,
                "sessions": len(s),
                "extractor_version": manifest.get("extractor_version"),
                "scope": manifest.get("scope"),
            }
        )

    out.mkdir(parents=True, exist_ok=True)
    merged = sorted(calls.values(), key=lambda r: -(r.get("severity") or 0))
    (out / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in merged))
    (out / "sessions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sessions.values()))
    (out / "ingest.json").write_text(json.dumps(report, indent=2))
    ok = sum(r["status"] == "ok" for r in report)
    return {
        "bundles_ok": ok,
        "rejected": len(report) - ok,
        "calls": len(merged),
        "sessions": len(sessions),
        "report": report,
    }
