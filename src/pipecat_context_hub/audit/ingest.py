"""Validate and merge Context Hub session bundles received from participants.

Bundles come from other people: they are treated as untrusted data.

Reading. A tarball is read as a stream, member by member, and each member header is checked as it
arrives (a known flat filename, not seen before, a regular file, within the size limit) before its
data is read; the first violation stops the read. The work done to reject a hostile archive is
bounded by constants, never by what the archive claims: the compressed file size
(``MAX_ARCHIVE_BYTES``), the member count (one per bundle file), the per-member size
(``MAX_MEMBER_BYTES``) and the total decompressed bytes (``MAX_DECOMPRESSED_BYTES``, which also
bounds the tar metadata blocks the reader parses between members). Nothing is extracted to disk.
A directory (a share/ or a cloned gist) is read by the one share reader (``sharedir.read_share``),
the same one ``gist`` and ``repack`` use. The bytes are then validated by the bundle contract
(``bundle.load_files``): the same validator ``repack`` and ``gist`` use, so a bundle they accept is
one this accepts.

Merging. A row's identity is namespaced by its source bundle: calls are keyed by
``(bundle, ref)`` and sessions by ``(bundle, participant, session)``, where ``bundle`` is the
content digest (``bundle.content_digest``, the same for a bundle's tarball, directory and gist).
One bundle therefore cannot shadow another's rows: when two different bundles carry the same ref or
session, both are kept, each merged row says which bundle it came from (``source_bundle``), and the
collision is counted in ``ingest.json``. The same bundle given twice (equal digest) is merged once.

``merge_bundles`` writes ``calls.jsonl`` (sorted by severity), ``sessions.jsonl`` and
``ingest.json`` listing every source with its sha256, bundle digest, counts and any rejection.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO

from pipecat_context_hub.audit.analysis import triage_order
from pipecat_context_hub.audit.bundle import (
    BUNDLE_FILES,
    MAX_BUNDLE_BYTES,
    MAX_FILE_BYTES,
    BundleError,
    content_digest,
    has_content,
    load_files,
)
from pipecat_context_hub.audit.sharedir import SafeDir, read_share

MAX_ARCHIVE_BYTES = 50 * 1024 * 1024  # a tarball's own (compressed) size
MAX_MEMBER_BYTES = MAX_FILE_BYTES  # one member (a directory file: the same, in sharedir)
MAX_DECOMPRESSED_BYTES = MAX_BUNDLE_BYTES  # everything read from a tarball's stream
MAX_MEMBERS = len(BUNDLE_FILES)


class _CappedReader(io.RawIOBase):
    """A read-only stream that refuses to yield more than ``limit`` bytes in total."""

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self.count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        data = self._raw.read(len(b))
        self.count += len(data)
        if self.count > self._limit:
            raise BundleError(f"archive decompresses past {self._limit} bytes")
        b[: len(data)] = data
        return len(data)


def read_tar(path: Path) -> dict[str, bytes]:
    """The bundle files in the tarball at ``path``; raises at the first member that breaks a rule."""
    out: dict[str, bytes] = {}
    with path.open("rb") as raw, gzip.GzipFile(fileobj=raw, mode="rb") as gz:
        capped = _CappedReader(gz, MAX_DECOMPRESSED_BYTES)  # type: ignore[arg-type]
        with tarfile.open(fileobj=capped, mode="r|") as tf:
            for count, m in enumerate(tf, 1):  # one header at a time: never getmembers()
                if count > MAX_MEMBERS:
                    raise BundleError(f"more than {MAX_MEMBERS} members")
                if m.name not in BUNDLE_FILES:
                    raise BundleError(f"unexpected member {m.name!r}")
                if m.name in out:
                    raise BundleError(f"member {m.name!r} appears twice")
                if not m.isreg() or m.issparse():
                    raise BundleError(f"member {m.name!r} is not a regular file")
                if m.size > MAX_MEMBER_BYTES:
                    raise BundleError(f"member {m.name!r} too large ({m.size} bytes)")
                fh = tf.extractfile(m)
                if fh is None:
                    raise BundleError(f"member {m.name!r} is unreadable")
                out[m.name] = fh.read(m.size)
    return out


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], str, str]:
    """(manifest, calls, sessions, sha256, bundle digest) of one bundle; raises when it is invalid.

    ``sha256`` is the tarball file's own digest (what ``share.tar.gz.sha256`` names), or the bundle
    digest for a directory.
    """
    if path.is_dir():  # a share/ directory or a cloned gist: the one share reader
        files = read_share(path)
        digest = content_digest(files)
        sha = digest
    else:
        size = path.stat().st_size
        if size > MAX_ARCHIVE_BYTES:
            raise BundleError(f"archive is {size} bytes (limit {MAX_ARCHIVE_BYTES})")
        files = read_tar(path)
        sha = _file_sha256(path)
        digest = content_digest(files)
    b = load_files(files)
    return b.manifest, b.calls, b.sessions, sha, digest


def merge_bundles(bundles: Sequence[Path], out: Path) -> dict[str, Any]:
    """Merge ``bundles`` into ``out``; returns counts plus the per-source report."""
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    sessions: dict[tuple[str, str, str], dict[str, Any]] = {}
    ref_owner: dict[str, str] = {}  # ref -> the bundle digest that first carried it
    session_owner: dict[tuple[str, str], str] = {}
    seen: dict[str, str] = {}  # bundle digest -> the source it was first read from
    report: list[dict[str, Any]] = []
    for b in bundles:
        try:
            manifest, c, s, sha, digest = load(b)
        except (ValueError, OSError, tarfile.TarError, EOFError, zlib.error, RecursionError) as exc:
            report.append({"source": b.name, "status": "rejected", "reason": str(exc)})
            continue
        entry: dict[str, Any] = {
            "source": b.name,
            "status": "ok",
            "sha256": sha,
            "bundle": digest,
            "calls": len(c),
            "new_calls": 0,
            "sessions": len(s),
            "has_content": has_content(manifest["counts"]),
            "ref_collisions": 0,
            "session_collisions": 0,
            "extractor_version": manifest.get("extractor_version"),
            "scope": manifest.get("scope"),
        }
        report.append(entry)
        if digest in seen:  # the same bundle again (its tarball, its directory or its gist)
            entry["duplicate_of"] = seen[digest]
            continue
        seen[digest] = b.name
        for row in c:
            key = (digest, row["ref"])
            if key in calls:  # a repeated ref inside one bundle: its first row stands
                continue
            if ref_owner.setdefault(row["ref"], digest) != digest:
                entry["ref_collisions"] += 1
            calls[key] = {**row, "source_bundle": digest}
            entry["new_calls"] += 1
        for row in s:
            ident = (row["participant"], row["session"])
            skey = (digest, *ident)
            if skey in sessions:
                continue
            if session_owner.setdefault(ident, digest) != digest:
                entry["session_collisions"] += 1
            sessions[skey] = {**row, "source_bundle": digest}

    merged = triage_order(calls.values())
    with SafeDir.open(out, create=True) as d:  # never written through a planted link
        d.write("calls.jsonl", "".join(json.dumps(r) + "\n" for r in merged).encode())
        d.write("sessions.jsonl", "".join(json.dumps(r) + "\n" for r in sessions.values()).encode())
        d.write("ingest.json", json.dumps(report, indent=2).encode())
    ok = sum(r["status"] == "ok" for r in report)
    return {
        "bundles_ok": ok,
        "rejected": len(report) - ok,
        "calls": len(merged),
        "sessions": len(sessions),
        "ref_collisions": sum(r.get("ref_collisions", 0) for r in report),
        "session_collisions": sum(r.get("session_collisions", 0) for r in report),
        "report": report,
    }
