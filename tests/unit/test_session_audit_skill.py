"""Tests for the context-hub-session-audit skill scripts (extract.py, ingest.py).

The scripts live under .claude/skills/ (not packaged), so they are loaded by path. Transcripts are
synthetic: the leak test freezes the secret/path shapes the share bundle must never carry.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / ".claude/skills/context-hub-session-audit/scripts"

SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV123456"
HOME_PATH = "/Users/alice/secret-project"
BEARER = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_skill_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def extract() -> ModuleType:
    return _load("extract")


@pytest.fixture(scope="module")
def ingest() -> ModuleType:
    return _load("ingest")


def _jl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _claude_use(i: int, ts: str, name: str, inp: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "sessionId": "s1",
        "cwd": HOME_PATH,
        "timestamp": ts,
        "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]},
    }


def _claude_result(i: int, ts: str, text: str, is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": "s1",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"t{i}",
                    "is_error": is_error,
                    "content": [{"type": "text", "text": text}],
                }
            ]
        },
    }


def _claude_session(path: Path) -> None:
    empty = json.dumps(
        {"hits": [], "evidence": {"confidence": 0.1, "low_confidence": True, "unknown": []}}
    )
    cli_json = json.dumps({"hits": [{"doc_id": "d1"}], "evidence": {"confidence": 0.9}})
    use1 = _claude_use(
        1, "2026-11-18T10:00:00Z", "mcp__pipecat-context-hub__search_docs", {"query": "TTS"}
    )
    rows: list[dict[str, Any]] = [
        use1,
        {**use1, "type": "api-request-blob"},  # duplicate of a real message: must not double-count
        _claude_result(1, "2026-11-18T10:00:01Z", empty),
        _claude_use(
            2,
            "2026-11-18T10:00:02Z",
            "Bash",
            {
                "command": f'uv run pipecat-context-hub search-api "Foo" --limit 2 2>&1 | head -c 400; '
                f"echo token={SECRET} {HOME_PATH}"
            },
        ),
        _claude_result(2, "2026-11-18T10:00:03Z", f"exit=0\n{cli_json}\nleak {SECRET} {BEARER}"),
        _claude_use(
            3,
            "2026-11-18T10:00:04Z",
            "Bash",
            {"command": "grep -r 'pipecat_context_hub__search_docs' n.txt"},
        ),
        _claude_result(3, "2026-11-18T10:00:05Z", "no match"),
        _claude_use(
            4, "2026-11-18T10:00:06Z", "mcp__pipecat-context-hub__get_doc", {"path": "/guides/x"}
        ),
        _claude_result(
            4,
            "2026-11-18T10:00:07Z",
            f"InputValidationError: get_doc was called with input that could not be parsed {HOME_PATH}",
            True,
        ),
        _claude_use(
            5,
            "2026-11-18T10:00:08Z",
            "Write",
            {
                "file_path": f"{HOME_PATH}/bot.py",
                "content": f"from pipecat.pipeline import Pipeline  # {SECRET}",
            },
        ),
        _claude_result(5, "2026-11-18T10:00:09Z", "ok"),
        _claude_use(6, "2026-11-18T10:00:10Z", "Bash", {"command": "python bot.py"}),
        _claude_result(
            6,
            "2026-11-18T10:00:11Z",
            f"Traceback (most recent call last):\n  File {HOME_PATH}/bot.py\nImportError: x",
        ),
    ]
    _jl(path, rows)


def _run_extract(extract: ModuleType, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["extract.py", *args])
    return int(extract.main())


@pytest.fixture
def audited(extract: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _claude_session(tmp_path / "in" / "alice" / "s1.jsonl")
    out = tmp_path / "out"
    assert (
        _run_extract(extract, monkeypatch, "--input", str(tmp_path / "in"), "--out", str(out)) == 0
    )
    return out


def _calls(out: Path, name: str = "calls.jsonl") -> list[dict[str, Any]]:
    return [json.loads(line) for line in (out / name).read_text().splitlines()]


class TestClaudeExtraction:
    def test_counts_only_real_calls(self, audited: Path) -> None:
        calls = _calls(audited)
        # blob duplicate ignored, `grep ... pipecat_context_hub__search_docs` mention ignored
        assert [(c["tool"], c["via"]) for c in calls] == [
            ("search_docs", "mcp"),
            ("search_api", "cli"),
            ("get_doc", "mcp"),
        ]

    def test_flags(self, audited: Path) -> None:
        by_tool = {c["tool"]: c for c in _calls(audited)}
        assert {"zero_hits", "low_confidence"} <= set(by_tool["search_docs"]["flags"])
        assert "input_validation_error" in by_tool["get_doc"]["flags"]
        assert "downstream_error" in by_tool["get_doc"]["flags"]

    def test_cli_result_parsed_from_wrapped_shell_output(self, audited: Path) -> None:
        cli = next(c for c in _calls(audited) if c["via"] == "cli")
        assert cli["result"]["parse"] == "embedded"
        assert cli["result"]["n_hits"] == 1
        assert "non_json_result" not in cli["flags"]

    def test_hub_invocation_excludes_rest_of_shell_line(self, audited: Path) -> None:
        cli = next(c for c in _calls(audited) if c["via"] == "cli")
        assert cli["hub_invocation"].startswith("pipecat-context-hub search-api")
        assert "echo" not in cli["hub_invocation"]
        assert SECRET not in cli["hub_invocation"]

    def test_session_flags(self, audited: Path) -> None:
        (s,) = _calls(audited, "sessions.jsonl")
        assert s["harness"] == "claude" and s["hub_used"] and s["pipecat_work"]
        assert not s["missed_opportunity"]


class TestShareBundleLeaks:
    def test_no_secret_or_local_path_in_any_shared_byte(self, audited: Path) -> None:
        blobs = [p.read_bytes() for p in (audited / "share").iterdir()]
        with tarfile.open(audited / "share.tar.gz") as tf:
            for m in tf.getmembers():
                fh = tf.extractfile(m)
                assert fh is not None
                blobs.append(fh.read())
        joined = b"\n".join(blobs).decode(errors="replace")
        for needle in (SECRET, "/Users/alice", "alice", BEARER, "secret-project"):
            assert needle not in joined, needle

    def test_full_output_is_redacted_too(self, audited: Path) -> None:
        text = (audited / "calls.jsonl").read_text()
        assert SECRET not in text and "/Users/alice" not in text

    def test_tarball_has_exactly_the_five_flat_files(self, audited: Path) -> None:
        with tarfile.open(audited / "share.tar.gz") as tf:
            assert sorted(tf.getnames()) == sorted(
                ["manifest.json", "SCHEMA.md", "report.md", "calls.jsonl", "sessions.jsonl"]
            )
            assert all(m.isreg() and m.uid == 0 and m.mtime == 0 for m in tf.getmembers())

    def test_refs_unique_and_manifest_versioned(self, audited: Path) -> None:
        refs = [c["ref"] for c in _calls(audited / "share")]
        assert len(refs) == len(set(refs)) == 3
        assert json.loads((audited / "share" / "manifest.json").read_text())["schema_version"] == 1

    def test_repack_after_edit_changes_digest(
        self, extract: ModuleType, monkeypatch: pytest.MonkeyPatch, audited: Path
    ) -> None:
        before = (audited / "share.tar.gz.sha256").read_text()
        calls = audited / "share" / "calls.jsonl"
        calls.write_text(calls.read_text().splitlines()[0] + "\n")
        assert _run_extract(extract, monkeypatch, "--repack", str(audited)) == 0
        assert (audited / "share.tar.gz.sha256").read_text() != before


class TestOtherHarnesses:
    def test_pi_cli_call(
        self, extract: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rows: list[dict[str, Any]] = [
            {"type": "session", "id": "p1", "cwd": HOME_PATH, "timestamp": "2026-11-18T10:00:00Z"},
            {
                "type": "message",
                "parentId": "x",
                "timestamp": "2026-11-18T10:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "c1",
                            "name": "bash",
                            "arguments": {
                                "command": "pipecat-context-hub check-deprecation PipelineTask"
                            },
                        }
                    ],
                },
            },
            {
                "type": "message",
                "parentId": "y",
                "timestamp": "2026-11-18T10:00:02Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "c1",
                    "content": [{"type": "text", "text": '{"deprecated": false}'}],
                },
            },
        ]
        _jl(tmp_path / "in" / "bob" / "s.jsonl", rows)
        out = tmp_path / "out"
        assert (
            _run_extract(extract, monkeypatch, "--input", str(tmp_path / "in"), "--out", str(out))
            == 0
        )
        (call,) = _calls(out)
        assert (call["tool"], call["via"]) == ("check_deprecation", "cli")
        assert _calls(out, "sessions.jsonl")[0]["harness"] == "pi"

    def test_codex_cli_call(
        self, extract: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rows: list[dict[str, Any]] = [
            {
                "timestamp": "2026-11-18T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "k1", "cwd": HOME_PATH},
            },
            {
                "timestamp": "2026-11-18T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": 'pipecat-context-hub search-docs "TTS + STT"'}),
                },
            },
            {
                "timestamp": "2026-11-18T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": [
                        {
                            "type": "input_text",
                            "text": '{"hits": [], "evidence": {"confidence": 0.2}}',
                        }
                    ],
                },
            },
        ]
        _jl(tmp_path / "in" / "carol" / "r.jsonl", rows)
        out = tmp_path / "out"
        assert (
            _run_extract(extract, monkeypatch, "--input", str(tmp_path / "in"), "--out", str(out))
            == 0
        )
        (call,) = _calls(out)
        assert (call["tool"], call["via"]) == ("search_docs", "cli")
        assert "zero_hits" in call["flags"]

    def test_unknown_format_is_text_scanned_and_flagged(
        self, extract: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "in" / "dave").mkdir(parents=True)
        (tmp_path / "in" / "dave" / "chat.md").write_text(
            "I ran `pipecat-context-hub search-docs TTS` then wrote from pipecat import x"
        )
        out = tmp_path / "out"
        assert (
            _run_extract(extract, monkeypatch, "--input", str(tmp_path / "in"), "--out", str(out))
            == 0
        )
        (s,) = _calls(out, "sessions.jsonl")
        assert s["fidelity"] == "text_scan" and s["needs_manual_read"] and s["hub_used"]


class TestIngest:
    def _tar(self, path: Path, members: list[tuple[str, bytes, str]]) -> Path:
        with tarfile.open(path, "w:gz") as tf:
            for name, data, kind in members:
                ti = tarfile.TarInfo(name)
                if kind == "sym":
                    ti.type = tarfile.SYMTYPE
                    ti.linkname = "/etc/passwd"
                    tf.addfile(ti)
                else:
                    ti.size = len(data)
                    tf.addfile(ti, io.BytesIO(data))
        return path

    def _run(
        self, ingest: ModuleType, monkeypatch: pytest.MonkeyPatch, out: Path, *bundles: Path
    ) -> int:
        monkeypatch.setattr(sys, "argv", ["ingest.py", *map(str, bundles), "--out", str(out)])
        return int(ingest.main())

    def test_merge_dedupes_and_sorts(
        self, ingest: ModuleType, monkeypatch: pytest.MonkeyPatch, audited: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "merged"
        rc = self._run(ingest, monkeypatch, out, audited / "share.tar.gz", audited / "share")
        assert rc == 0
        merged = _calls(out)
        assert len(merged) == 3  # same bundle twice adds nothing
        sev = [c["severity"] for c in merged]
        assert sev == sorted(sev, reverse=True)
        report = json.loads((out / "ingest.json").read_text())
        assert [r["new_calls"] for r in report] == [3, 0]

    @pytest.mark.parametrize(
        "members",
        [
            [("../../evil.txt", b"x", "f")],
            [("calls.jsonl", b"", "sym")],
            [("manifest.json", b'{"schema_version": 1}', "f"), ("payload.sh", b"x", "f")],
            [
                ("manifest.json", b'{"schema_version": 2}', "f"),
                ("calls.jsonl", b"", "f"),
                ("sessions.jsonl", b"", "f"),
            ],
            [
                ("manifest.json", b'{"schema_version": 1}', "f"),
                ("calls.jsonl", b'{"nope": 1}\n', "f"),
                ("sessions.jsonl", b"", "f"),
            ],
        ],
        ids=["traversal", "symlink", "extra-member", "wrong-schema", "bad-rows"],
    )
    def test_hostile_bundles_rejected_and_nothing_written_outside(
        self,
        ingest: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        members: list[tuple[str, bytes, str]],
    ) -> None:
        bad = self._tar(tmp_path / "bad.tar.gz", members)
        out = tmp_path / "merged"
        assert self._run(ingest, monkeypatch, out, bad) == 1
        report = json.loads((out / "ingest.json").read_text())
        assert report[0]["status"] == "rejected"
        assert not (tmp_path.parent / "evil.txt").exists()
