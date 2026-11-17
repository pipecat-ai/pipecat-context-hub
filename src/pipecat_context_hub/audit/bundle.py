"""The share bundle contract, defined once: what ``extract`` writes, ``repack`` rewrites, ``gist``
posts and ``ingest`` reads.

A leaf module (stdlib only, imports nothing from the rest of ``audit``), so producer and consumer
cannot drift: the schema version, the five filenames, the required keys and types of every row
and of the manifest, the one JSON decoder, the one whole-bundle validator (:func:`load_files`) and
the predicates every command decides with (:func:`has_content`, :func:`missing_file_allowed`,
:func:`omitted_when_posting`).

Counts. ``manifest.counts`` holds two kinds of number:

- **Row counts** (:data:`ROW_COUNT_KEYS`): ``call_rows``, ``sessions``, ``participants``,
  ``hub_used`` and ``missed_opportunity``, each derived from the rows actually present
  (:func:`row_counts`). ``repack`` re-derives them after a hand edit, and :func:`load_files`
  rejects a bundle whose row counts disagree with its rows. Every predicate reads only these.
- ``hub_calls``: the extraction-time total of Context Hub calls, which also counts mentions in
  text-scanned sessions (no rows), so it may exceed ``call_rows``. Informational; no decision
  reads it.

"Nothing to share" is :func:`has_content` false: no call rows, no session that used Context Hub
(a text-scanned session can, without rows) and no session that did Pipecat work without it.

Empty. A JSONL file is *empty* (:func:`is_empty`) when its text has no non-whitespace character,
which is exactly when it holds no line to parse. One predicate decides it everywhere: ``gist``
leaves an empty JSONL file out (a gist cannot hold an empty file), :func:`content_digest` hashes it
as absent, and :func:`load_files` (``repack``, ``gist``, ``ingest``) reads it as zero rows. A
missing ``calls.jsonl`` or ``sessions.jsonl`` is accepted exactly when the manifest says that file
has zero rows; every other file is required.

Manifest. :func:`decode_manifest` is the one manifest decoder: the version, a ``counts`` object
and the ``analysis`` block's shape (:data:`ANALYSIS_SHAPE`: every key ``report.md`` reads, with
its container type). :func:`manifest_error` adds the row-count values, which ``repack`` re-derives
and so checks only after it has rewritten them.

Size. A bundle file is at most :data:`MAX_FILE_BYTES` and a bundle at most
:data:`MAX_BUNDLE_BYTES`, however it is read (directory, tarball member).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Callable, Mapping
from typing import Any

SCHEMA_VERSION = 1
BUNDLE_FILES = ("manifest.json", "SCHEMA.md", "report.md", "calls.jsonl", "sessions.jsonl")
# A JSONL file may be missing from a posted bundle when it has zero rows (see the module docstring).
_OPTIONAL_FILES = {"calls.jsonl": "call_rows", "sessions.jsonl": "sessions"}
MAX_FILE_BYTES = 50 * 1024 * 1024  # one bundle file (a directory file or a tarball member)
MAX_BUNDLE_BYTES = (
    64 * 1024 * 1024
)  # every bundle file together (and a tarball's decompressed stream)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_latency(v: Any) -> bool:
    """Seconds to a result: a finite, non-negative number, or null (no result timestamp)."""
    return v is None or (_is_number(v) and math.isfinite(v) and v >= 0)


_Check = Callable[[Any], bool]
# Required keys of a calls.jsonl row and the type each must have. Other keys are allowed.
CALL_FIELDS: dict[str, tuple[_Check, str]] = {
    "ref": (lambda v: isinstance(v, str), "a string"),
    "tool": (lambda v: isinstance(v, str), "a string"),
    "via": (lambda v: isinstance(v, str), "a string"),
    "flags": (_is_str_list, "a list of strings"),
    "severity": (_is_number, "a number"),
    "result": (lambda v: isinstance(v, dict), "an object"),
    "followups": (lambda v: isinstance(v, list), "a list"),
    "latency_s": (_is_latency, "a finite non-negative number or null"),
    "input": (lambda v: v is None or isinstance(v, dict), "an object or null"),
}
# Required keys of a sessions.jsonl row; ``participant`` and ``session`` are its identity.
SESSION_FIELDS: dict[str, tuple[_Check, str]] = {
    "participant": (lambda v: isinstance(v, str), "a string"),
    "session": (lambda v: isinstance(v, str), "a string"),
    "harness": (lambda v: isinstance(v, str), "a string"),
    "hub_used": (lambda v: isinstance(v, bool), "a boolean"),
    "missed_opportunity": (lambda v: isinstance(v, bool), "a boolean"),
    "hub_calls": (_is_int, "an integer"),
    "flag_counts": (lambda v: isinstance(v, dict), "an object"),
}
ROW_COUNT_KEYS = ("call_rows", "sessions", "participants", "hub_used", "missed_opportunity")


class BundleError(ValueError):
    """A bundle that breaks the contract. The message names the file and line, never row text."""


class _DuplicateKey(ValueError):
    pass


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _DuplicateKey
        out[k] = v
    return out


def decode_json(text: str, where: str) -> Any:
    """``text`` decoded as one JSON document; a duplicate key or bad JSON raises ``BundleError``.

    A duplicate key is refused, not resolved: ``json.loads`` keeps the last of two equal keys while
    a person reading the gist sees both, so the two would disagree about what the bundle says.
    """
    try:
        return json.loads(text, object_pairs_hook=_unique_pairs)
    except _DuplicateKey:
        raise BundleError(f"{where}: an object repeats a key") from None
    except (ValueError, RecursionError):
        raise BundleError(f"{where}: not valid JSON") from None


def row_error(row: Any, fields: Mapping[str, tuple[_Check, str]]) -> str | None:
    """Why ``row`` is not a valid row under ``fields``, or None."""
    if not isinstance(row, dict):
        return "not a JSON object"
    missing = sorted(k for k in fields if k not in row)
    if missing:
        return f"missing keys {missing}"
    for key, (ok, kind) in fields.items():
        if not ok(row[key]):
            return f"{key} must be {kind}"
    return None


def parse_rows(text: str, name: str) -> list[dict[str, Any]]:
    """The rows of ``calls.jsonl`` or ``sessions.jsonl``, each validated; raises ``BundleError``."""
    fields = CALL_FIELDS if name == "calls.jsonl" else SESSION_FIELDS
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        row = decode_json(line, f"{name} line {i}")
        err = row_error(row, fields)
        if err:
            raise BundleError(f"{name} line {i}: {err}")
        rows.append(row)
    return rows


# The ``analysis`` block, as ``report.md`` reads it: a dict is an object with (at least) these keys,
# ``[shape]`` a list whose every item has ``shape``, ``dict`` / ``str`` that type, and ``None`` any
# JSON value (a leaf, rendered through its canonical text). The renderer reads no key not named here.
ANALYSIS_SHAPE: dict[str, Any] = {
    "usage": {
        "used": None, "sessions": None, "session_trees": None, "subagent_sessions": None,
        "pipecat_work_and_used": None, "with_pipecat_work": None,
        "adoption_pct_of_pipecat_work": None, "hub_tools_listed_deferred": None,
        "tool_search_ever_used": None, "missed_opportunity": None, "why_not_used": dict,
    },
    "efficiency": {
        "calls": None, "clean_pct": None, "median_latency_s": None,
        "sessions_searching_3plus_times_never_fetching": None,
        "sessions_with_3plus_status_calls": None,
        "versioned_search_calls_without_pipecat_version": {"of": None, "without": None},
    },
    "correctness": {"note": None, "outcomes": dict, "read_these_first": [str]},
    "improvement_backlog": [
        {
            "target": None, "calls": None, "sessions": None, "participants": None,
            "top_signals": dict, "example_refs": [str],
        }
    ],
}  # fmt: skip
_KIND: dict[type, str] = {dict: "an object", str: "a string"}


def shape_error(value: Any, shape: Any, where: str) -> str | None:
    """Why ``value`` does not have ``shape`` (see :data:`ANALYSIS_SHAPE`), or None."""
    if shape is None:
        return None
    if isinstance(shape, type):
        return None if isinstance(value, shape) else f"{where} must be {_KIND[shape]}"
    if isinstance(shape, list):
        if not isinstance(value, list):
            return f"{where} must be a list"
        for i, item in enumerate(value):
            err = shape_error(item, shape[0], f"{where}[{i}]")
            if err:
                return err
        return None
    if not isinstance(value, dict):
        return f"{where} must be an object"
    for key, sub in shape.items():
        if key not in value:
            return f"{where} has no {key!r}"
        err = shape_error(value[key], sub, f"{where}.{key}")
        if err:
            return err
    return None


def _manifest_shape_error(manifest: Any) -> str | None:
    if not isinstance(manifest, dict):
        return "manifest.json is not an object"
    version = manifest.get("schema_version")
    if not _is_int(version) or version != SCHEMA_VERSION:
        return f"schema_version {version!r} unsupported (expected {SCHEMA_VERSION})"
    if not isinstance(manifest.get("counts"), dict):
        return "manifest.json has no counts object"
    return None


def _analysis_error(manifest: dict[str, Any]) -> str | None:
    err = shape_error(manifest.get("analysis"), ANALYSIS_SHAPE, "analysis")
    return f"manifest.json {err}" if err else None


def manifest_error(manifest: Any) -> str | None:
    """Why ``manifest`` is not a valid manifest (version, row counts, analysis shape), or None."""
    err = _manifest_shape_error(manifest)
    if err:
        return err
    counts = manifest["counts"]
    bad = [k for k in ROW_COUNT_KEYS if not _is_int(counts.get(k)) or counts[k] < 0]
    if bad:
        return f"manifest.json counts {bad} missing or not a non-negative integer"
    return _analysis_error(manifest)


def decode_manifest(data: bytes | None) -> dict[str, Any]:
    """The one manifest decoder: ``manifest.json``'s bytes as a manifest whose version, ``counts``
    object and ``analysis`` shape are valid. The row-count values are not checked here: ``repack``
    re-derives them from the rows, and :func:`load_files` checks them (:func:`manifest_error`)."""
    if data is None:
        raise BundleError("missing ['manifest.json']")
    manifest = decode_json(_text("manifest.json", data), "manifest.json")
    err = _manifest_shape_error(manifest) or _analysis_error(manifest)
    if err:
        raise BundleError(err)
    return dict(manifest)


def row_counts(calls: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[str, int]:
    """The manifest row counts the rows themselves imply (see the module docstring)."""
    return {
        "call_rows": len(calls),
        "sessions": len(sessions),
        "participants": len({s["participant"] for s in sessions}),
        "hub_used": sum(s["hub_used"] is True for s in sessions),
        "missed_opportunity": sum(s["missed_opportunity"] is True for s in sessions),
    }


def has_content(counts: Mapping[str, Any]) -> bool:
    """False when there is nothing worth sharing: no call rows, no session that used Context Hub,
    and no session that did Pipecat work without it (the one "nothing to share" rule)."""
    return any(counts.get(k) for k in ("call_rows", "hub_used", "missed_opportunity"))


def missing_file_allowed(name: str, manifest: Mapping[str, Any]) -> bool:
    """True when ``name`` may be absent: a JSONL file the manifest says has zero rows."""
    key = _OPTIONAL_FILES.get(name)
    counts = manifest.get("counts")
    return key is not None and isinstance(counts, dict) and counts.get(key) == 0


def is_empty(data: bytes) -> bool:
    """True when ``data`` has no non-whitespace character: for a JSONL file, exactly when it holds
    no line to parse (every line break is whitespace). The one "empty file" rule."""
    return not data.decode("utf-8", "replace").strip()


def omitted_when_posting(name: str, data: bytes) -> bool:
    """True when ``gist`` leaves ``name`` out: an empty JSONL file (a gist cannot hold an empty
    file). The mirror of :func:`missing_file_allowed` on a bundle :func:`load_files` accepts."""
    return name in _OPTIONAL_FILES and is_empty(data)


def content_digest(files: Mapping[str, bytes]) -> str:
    """One bundle's identity, the same for its tarball, its share/ directory and its gist.

    Every bundle file in a fixed order, length-prefixed; a JSONL file that is absent or empty
    (:func:`is_empty`, so what ``gist`` leaves out) counts as zero bytes, so the gist of a bundle
    has the digest of the bundle.
    """
    h = hashlib.sha256()
    for name in BUNDLE_FILES:
        data = files.get(name, b"")
        if omitted_when_posting(name, data):
            data = b""
        h.update(f"{name}\0{len(data)}\0".encode())
        h.update(data)
    return h.hexdigest()


@dataclasses.dataclass
class Bundle:
    manifest: dict[str, Any]
    calls: list[dict[str, Any]]
    sessions: list[dict[str, Any]]


def _text(name: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise BundleError(f"{name} is not UTF-8") from None


def jsonl_rows(files: Mapping[str, bytes], name: str) -> list[dict[str, Any]]:
    """The validated rows of the JSONL file ``name`` in ``files``: none when it is absent or
    empty (:func:`is_empty`); whether it may be absent is :func:`load_files`'s call."""
    data = files.get(name, b"")
    return [] if is_empty(data) else parse_rows(_text(name, data), name)


def load_files(files: Mapping[str, bytes]) -> Bundle:
    """The one whole-bundle validator, used by ``extract`` (on what it wrote), ``repack``, ``gist``
    and ``ingest``. ``files`` maps bundle filenames to their bytes; a name not in it is absent.

    Raises ``BundleError`` for an unknown or missing file, a manifest without the version or row
    counts, a row that fails its validator, or row counts that disagree with the rows.
    """
    unknown = sorted(set(files) - set(BUNDLE_FILES))
    if unknown:
        raise BundleError(f"unexpected files {unknown}")
    if "manifest.json" not in files:
        raise BundleError("missing ['manifest.json']")
    manifest = decode_json(_text("manifest.json", files["manifest.json"]), "manifest.json")
    err = manifest_error(manifest)
    if err:
        raise BundleError(err)
    missing = [n for n in BUNDLE_FILES if n not in files and not missing_file_allowed(n, manifest)]
    if missing:
        raise BundleError(f"missing {missing}")
    blank = [n for n in ("SCHEMA.md", "report.md") if is_empty(files[n])]
    if blank:
        raise BundleError(f"{blank} empty")
    calls = jsonl_rows(files, "calls.jsonl")
    sessions = jsonl_rows(files, "sessions.jsonl")
    implied = row_counts(calls, sessions)
    stale = {
        k: (manifest["counts"][k], v) for k, v in implied.items() if manifest["counts"][k] != v
    }
    if stale:
        detail = ", ".join(f"{k} says {said}, rows say {rows}" for k, (said, rows) in stale.items())
        raise BundleError(
            f"manifest.json counts disagree with the rows ({detail}); run `audit repack`"
        )
    return Bundle(manifest, calls, sessions)
