"""Tests for ``pipecat-context-hub audit`` (extract, share bundle, repack, gist, discard, ingest).

Transcripts are synthetic. The leak tests freeze the secret, token and home-path shapes the share
bundle must never carry; the hostile-tarball cases freeze what ``ingest`` must reject.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from pipecat_context_hub.audit import extract as audit_extract
from pipecat_context_hub.cli import main

SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV123456"
HOME_PATH = "/Users/alice/secret-project"
BEARER = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
BUNDLE = ["manifest.json", "SCHEMA.md", "report.md", "calls.jsonl", "sessions.jsonl"]


def _jl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _use(i: int, ts: str, name: str, inp: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "sessionId": "s1",
        "cwd": HOME_PATH,
        "timestamp": ts,
        "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]},
    }


def _result(i: int, ts: str, text: str, is_error: bool = False) -> dict[str, Any]:
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
    empty = json.dumps({"hits": [], "evidence": {"confidence": 0.1, "low_confidence": True}})
    cli_json = json.dumps({"hits": [{"doc_id": "d1"}], "evidence": {"confidence": 0.9}})
    use1 = _use(
        1, "2026-11-18T10:00:00Z", "mcp__pipecat-context-hub__search_docs", {"query": "TTS"}
    )
    cmd = (
        'uv run pipecat-context-hub search-api "Foo" --limit 2 2>&1 | head -c 400; '
        f"echo token={SECRET} {HOME_PATH}"
    )
    rows = [
        use1,
        {**use1, "type": "api-request-blob"},  # duplicate of a real message: must not double-count
        _result(1, "2026-11-18T10:00:01Z", empty),
        _use(2, "2026-11-18T10:00:02Z", "Bash", {"command": cmd}),
        _result(2, "2026-11-18T10:00:03Z", f"exit=0\n{cli_json}\nleak {SECRET} {BEARER}"),
        _use(
            3,
            "2026-11-18T10:00:04Z",
            "Bash",
            {"command": "grep -r 'pipecat_context_hub__search_docs' n"},
        ),
        _result(3, "2026-11-18T10:00:05Z", "no match"),
        _use(4, "2026-11-18T10:00:06Z", "mcp__pipecat-context-hub__get_doc", {"path": "/guides/x"}),
        _result(
            4,
            "2026-11-18T10:00:07Z",
            f"InputValidationError: get_doc was called with input that could not be parsed {HOME_PATH}",
            True,
        ),
        _use(
            5,
            "2026-11-18T10:00:08Z",
            "Write",
            {
                "file_path": f"{HOME_PATH}/bot.py",
                "content": f"from pipecat.pipeline import P  # {SECRET}",
            },
        ),
        _result(5, "2026-11-18T10:00:09Z", "ok"),
        _use(6, "2026-11-18T10:00:10Z", "Bash", {"command": "python bot.py"}),
        _result(
            6,
            "2026-11-18T10:00:11Z",
            f"Traceback (most recent call last):\n  {HOME_PATH}\nImportError: x",
        ),
    ]
    _jl(path, rows)


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["audit", *args])


def _extract(src: Path, out: Path, *extra: str) -> Result:
    return _invoke("extract", "-i", str(src), "--out", str(out), *extra)


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def audited(tmp_path: Path) -> Path:
    _claude_session(tmp_path / "in" / "alice" / "s1.jsonl")
    out = tmp_path / "out"
    result = _extract(tmp_path / "in", out)
    assert result.exit_code == 0, result.output
    return out


class TestClaudeExtraction:
    def test_counts_only_real_calls(self, audited: Path) -> None:
        # blob duplicate ignored; a `grep ... pipecat_context_hub__search_docs` mention ignored
        assert [(c["tool"], c["via"]) for c in _rows(audited / "calls.jsonl")] == [
            ("search_docs", "mcp"),
            ("search_api", "cli"),
            ("get_doc", "mcp"),
        ]

    def test_flags(self, audited: Path) -> None:
        by_tool = {c["tool"]: c for c in _rows(audited / "calls.jsonl")}
        assert {"zero_hits", "low_confidence"} <= set(by_tool["search_docs"]["flags"])
        assert {"input_validation_error", "downstream_error"} <= set(by_tool["get_doc"]["flags"])

    def test_cli_result_parsed_from_wrapped_shell_output(self, audited: Path) -> None:
        cli = next(c for c in _rows(audited / "calls.jsonl") if c["via"] == "cli")
        assert cli["result"]["parse"] == "embedded" and cli["result"]["n_hits"] == 1
        assert "non_json_result" not in cli["flags"]
        assert cli["hub_invocation"].startswith("pipecat-context-hub search-api")
        assert "echo" not in cli["hub_invocation"] and SECRET not in cli["hub_invocation"]

    def test_session_flags(self, audited: Path) -> None:
        (s,) = _rows(audited / "sessions.jsonl")
        assert s["harness"] == "claude" and s["hub_used"] and s["pipecat_work"]
        assert not s["missed_opportunity"]

    def test_stdout_is_the_summary_json(self, tmp_path: Path) -> None:
        _claude_session(tmp_path / "in" / "alice" / "s1.jsonl")
        result = CliRunner().invoke(
            main, ["audit", "extract", "-i", str(tmp_path / "in"), "--out", str(tmp_path / "o")]
        )
        assert json.loads(result.stdout)["hub_calls"] == 3

    def test_bad_input_dir_and_bad_since_exit_nonzero(self, tmp_path: Path) -> None:
        assert _extract(tmp_path / "missing", tmp_path / "o").exit_code == 1
        _claude_session(tmp_path / "in" / "a" / "s.jsonl")
        assert _extract(tmp_path / "in", tmp_path / "o", "--since", "yesterday").exit_code != 0

    def test_since_filters_old_sessions(self, tmp_path: Path) -> None:
        _claude_session(tmp_path / "in" / "alice" / "s1.jsonl")  # starts 2026-11-18
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out, "--since", "2026-11-19").exit_code == 0
        assert _rows(out / "sessions.jsonl") == []


class TestShareBundle:
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

    def test_full_local_output_is_redacted_too(self, audited: Path) -> None:
        text = (audited / "calls.jsonl").read_text()
        assert SECRET not in text and "/Users/alice" not in text

    def test_tarball_is_exactly_the_five_flat_files(self, audited: Path) -> None:
        with tarfile.open(audited / "share.tar.gz") as tf:
            assert sorted(tf.getnames()) == sorted(BUNDLE)
            assert all(m.isreg() and m.uid == 0 and m.mtime == 0 for m in tf.getmembers())

    def test_refs_unique_and_manifest_versioned(self, audited: Path) -> None:
        refs = [c["ref"] for c in _rows(audited / "share" / "calls.jsonl")]
        assert len(refs) == len(set(refs)) == 3
        assert json.loads((audited / "share" / "manifest.json").read_text())["schema_version"] == 1

    def test_ids_are_not_dictionary_reversible(self, audited: Path, tmp_path: Path) -> None:
        """Low-entropy labels must not be recoverable by hashing guesses, and ids differ per run."""
        (call, *_) = _rows(audited / "share" / "calls.jsonl")
        for guess in ("alice", "s1"):
            assert hashlib.sha256(guess.encode()).hexdigest()[:10] not in json.dumps(call)
        second = tmp_path / "out2"
        assert _extract(tmp_path / "in", second).exit_code == 0
        assert _rows(second / "share" / "calls.jsonl")[0]["ref"] != call["ref"]

    def test_anonymize_replaces_labels_with_aliases(self, tmp_path: Path) -> None:
        _claude_session(tmp_path / "in" / "alice" / "s1.jsonl")
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out, "--anonymize").exit_code == 0
        assert {r["participant"] for r in _rows(out / "sessions.jsonl")} == {"participant-1"}

    def test_repack_after_edit_changes_digest(self, audited: Path) -> None:
        before = (audited / "share.tar.gz.sha256").read_text()
        calls = audited / "share" / "calls.jsonl"
        calls.write_text(calls.read_text().splitlines()[0] + "\n")
        assert _invoke("repack", str(audited)).exit_code == 0
        assert (audited / "share.tar.gz.sha256").read_text() != before

    def test_repack_refuses_incomplete_dir(self, tmp_path: Path) -> None:
        assert _invoke("repack", str(tmp_path)).exit_code == 1


class TestOtherHarnesses:
    def test_pi_cli_call(self, tmp_path: Path) -> None:
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
        assert _extract(tmp_path / "in", out).exit_code == 0
        (call,) = _rows(out / "calls.jsonl")
        assert (call["tool"], call["via"]) == ("check_deprecation", "cli")
        assert _rows(out / "sessions.jsonl")[0]["harness"] == "pi"

    @staticmethod
    def _codex(call: dict[str, Any], output: str) -> list[dict[str, Any]]:
        return [
            {"timestamp": "2026-11-18T10:00:00Z", "type": "session_meta", "payload": {"id": "k1"}},
            {
                "timestamp": "2026-11-18T10:00:01Z",
                "type": "response_item",
                "payload": {"call_id": "c1", **call},
            },
            {
                "timestamp": "2026-11-18T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output"
                    if call["type"] == "function_call"
                    else "custom_tool_call_output",
                    "call_id": "c1",
                    "output": [{"type": "input_text", "text": output}],
                },
            },
        ]

    def test_codex_cli_call(self, tmp_path: Path) -> None:
        call = {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": 'pipecat-context-hub search-docs "TTS + STT"'}),
        }
        _jl(
            tmp_path / "in" / "carol" / "r.jsonl", self._codex(call, '{"hits": [], "evidence": {}}')
        )
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        (c,) = _rows(out / "calls.jsonl")
        assert (c["tool"], c["via"]) == ("search_docs", "cli") and "zero_hits" in c["flags"]

    def test_codex_js_batch_never_leaks_its_input(self, tmp_path: Path) -> None:
        """A JS `exec` batch carries unrelated code; only the one call expression may be shared."""
        js = (
            f'const k = "{SECRET}"; const p = "{HOME_PATH}";\n'
            'const r = await tools.mcp__pipecat_context_hub__search_docs({query: "TTS (fast)"});\n'
            f'console.log(k, p, "{BEARER}")'
        )
        call = {"type": "custom_tool_call", "name": "exec", "input": js}
        _jl(tmp_path / "in" / "dan" / "r.jsonl", self._codex(call, '{"hits": [], "evidence": {}}'))
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        (c,) = _rows(out / "share" / "calls.jsonl")
        assert c["via"] == "mcp_js" and c["input"] == {}
        assert c["hub_invocation"].endswith('{query: "TTS (fast)"})')
        shared = "".join(p.read_text() for p in (out / "share").iterdir())
        for needle in (SECRET, "/Users/alice", BEARER):
            assert needle not in shared

    @pytest.mark.parametrize(
        "tail",
        ["`unterminated ${x", '"never closed', "(((((("],
        ids=["template-literal", "open-string", "paren-storm"],
    )
    def test_unterminated_js_call_fails_closed(self, tmp_path: Path, tail: str) -> None:
        """No closing paren found: share the call prefix only, never the code that follows."""
        js = (
            f"const r = await tools.mcp__pipecat_context_hub__search_docs({tail}\n"
            f'const k = "{SECRET}"; fetch("https://x.example/?t={BEARER}")'
        )
        call = {"type": "custom_tool_call", "name": "exec", "input": js}
        _jl(tmp_path / "in" / "erin" / "r.jsonl", self._codex(call, '{"hits": [], "evidence": {}}'))
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        (c,) = _rows(out / "share" / "calls.jsonl")
        assert c["via"] == "mcp_js" and c["hub_invocation"].endswith("(...)")
        shared = "".join(p.read_text() for p in (out / "share").iterdir())
        assert SECRET not in shared and BEARER not in shared
        rows = (out / "share" / "calls.jsonl").read_text()
        assert "fetch" not in rows and "x.example" not in rows  # unrelated code never shared

    def test_direct_mcp_call_keeps_its_arguments(self, audited: Path) -> None:
        first = _rows(audited / "share" / "calls.jsonl")[0]
        assert first["via"] == "mcp" and first["input"] == {"query": "TTS"}

    def test_unknown_format_is_text_scanned_and_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "in" / "dave").mkdir(parents=True)
        (tmp_path / "in" / "dave" / "chat.md").write_text(
            "I ran `pipecat-context-hub search-docs TTS` then wrote from pipecat import x"
        )
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        (s,) = _rows(out / "sessions.jsonl")
        assert s["fidelity"] == "text_scan" and s["needs_manual_read"] and s["hub_used"]


class TestSharing:
    def test_gist_preview_creates_nothing(
        self, audited: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("must not run gh without --yes")

        monkeypatch.setattr(subprocess, "run", boom)
        result = _invoke("gist", str(audited))
        assert result.exit_code == 0 and "SECRET gist" in result.output

    def test_gist_yes_runs_gh_secret_never_public(
        self, audited: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_run(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "https://gist.github.com/x/abc\n", "")

        monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(subprocess, "run", fake_run)
        result = _invoke("gist", str(audited), "--yes")
        assert result.exit_code == 0
        # whole-line match on the fake gh's URL (a hostname substring check is what CodeQL flags)
        assert (
            "Gist created (secret, unlisted): https://gist.github.com/x/abc"
            in result.output.splitlines()
        )
        (argv,) = seen
        assert argv[1:3] == ["gist", "create"] and "--public" not in argv
        assert {Path(a).name for a in argv if a.endswith((".md", ".json", ".jsonl"))} == set(BUNDLE)

    def test_zero_call_bundle_says_so_and_gist_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _jl(
            tmp_path / "in" / "zed" / "s.jsonl",
            [_use(1, "2026-11-18T10:00:00Z", "Bash", {"command": "ls"})],
        )
        out = tmp_path / "out"
        extracted = _extract(tmp_path / "in", out)
        assert extracted.exit_code == 0 and "nothing to share" in extracted.output

        def boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("must not run gh for an empty bundle")

        monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(subprocess, "run", boom)
        for extra in ([], ["--yes"]):
            refused = _invoke("gist", str(out), *extra)
            assert refused.exit_code == 1 and "no Context Hub calls" in refused.output

    def test_bundle_with_calls_does_not_say_nothing_to_share(self, audited: Path) -> None:
        assert _invoke("gist", str(audited)).exit_code == 0

    def test_gist_leaves_out_an_empty_file_and_ingest_accepts_the_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real `gh` rejects blank files (frozen from a real failure): no Context Hub calls means an
        empty calls.jsonl, which must be left out of the gist and still ingest as a zero-call bundle."""
        _jl(
            tmp_path / "in" / "zed" / "s.jsonl",
            [_use(1, "2026-11-18T10:00:00Z", "Bash", {"command": "ls"})],
        )
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        assert (out / "share" / "calls.jsonl").stat().st_size == 0
        seen: list[list[str]] = []

        def fake_run(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "https://gist.github.com/x/abc\n", "")

        monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _invoke("gist", str(out), "--yes", "--allow-empty").exit_code == 0
        posted = {Path(a).name for a in seen[0] if a.endswith((".md", ".json", ".jsonl"))}
        assert posted == set(BUNDLE) - {"calls.jsonl"}
        # a cloned gist is the posted files only: ingest must accept it
        gist_dir = tmp_path / "gist"
        gist_dir.mkdir()
        for n in posted:
            (gist_dir / n).write_bytes((out / "share" / n).read_bytes())
        merged = tmp_path / "merged"
        assert _invoke("ingest", str(gist_dir), "--out", str(merged)).exit_code == 0
        assert json.loads((merged / "ingest.json").read_text())[0]["status"] == "ok"

    def test_ingest_still_requires_calls_when_the_manifest_claims_calls(
        self, audited: Path, tmp_path: Path
    ) -> None:
        gist_dir = tmp_path / "gist"
        gist_dir.mkdir()
        for n in set(BUNDLE) - {"calls.jsonl"}:
            (gist_dir / n).write_bytes((audited / "share" / n).read_bytes())
        result = _invoke("ingest", str(gist_dir), "--out", str(tmp_path / "m"))
        assert result.exit_code == 1 and "REJECTED" in result.output

    def test_gist_yes_without_gh_fails_cleanly(
        self, audited: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _n: None)
        assert _invoke("gist", str(audited), "--yes").exit_code == 1

    def test_discard_previews_then_deletes_only_generated_files(self, audited: Path) -> None:
        keep = audited / "my-notes.txt"
        keep.write_text("mine")
        preview = _invoke("discard", str(audited))
        assert preview.exit_code == 0 and (audited / "share.tar.gz").exists()
        assert _invoke("discard", str(audited), "--yes").exit_code == 0
        assert not (audited / "share").exists() and not (audited / "calls.jsonl").exists()
        assert keep.read_text() == "mine"  # unrelated file survives, so does its directory

    def test_discard_refuses_a_directory_that_is_not_extract_output(self, tmp_path: Path) -> None:
        precious = tmp_path / "precious"
        precious.mkdir()
        (precious / "calls.jsonl").write_text("not ours")
        assert _invoke("discard", str(precious), "--yes").exit_code == 1
        assert (precious / "calls.jsonl").exists()

    def test_discard_refuses_symlinked_output(self, audited: Path, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere.txt"
        target.write_text("x")
        (audited / "calls.jsonl").unlink()
        (audited / "calls.jsonl").symlink_to(target)
        assert _invoke("discard", str(audited), "--yes").exit_code == 1
        assert target.exists()

    def test_generated_paths_lists_only_known_names(self, audited: Path) -> None:
        (audited / "extra.txt").write_text("x")
        names = {p.name for p in audit_extract.generated_paths(audited)}
        assert "extra.txt" not in names and "share.tar.gz" in names


def _tar(path: Path, members: list[tuple[str, bytes, str]]) -> Path:
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


class TestIngest:
    def test_merge_dedupes_and_sorts(self, audited: Path, tmp_path: Path) -> None:
        out = tmp_path / "merged"
        result = _invoke(
            "ingest", str(audited / "share.tar.gz"), str(audited / "share"), "--out", str(out)
        )
        assert result.exit_code == 0
        merged = _rows(out / "calls.jsonl")
        assert len(merged) == 3  # the same bundle twice adds nothing
        sev = [c["severity"] for c in merged]
        assert sev == sorted(sev, reverse=True)
        assert [r["new_calls"] for r in json.loads((out / "ingest.json").read_text())] == [3, 0]

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
    def test_hostile_bundles_rejected_and_nothing_escapes(
        self, tmp_path: Path, members: list[tuple[str, bytes, str]]
    ) -> None:
        bad = _tar(tmp_path / "bad.tar.gz", members)
        out = tmp_path / "merged"
        result = _invoke("ingest", str(bad), "--out", str(out))
        assert result.exit_code == 1 and "REJECTED" in result.output
        assert json.loads((out / "ingest.json").read_text())[0]["status"] == "rejected"
        assert not (tmp_path.parent / "evil.txt").exists()

    def test_hostile_member_name_is_escaped_in_the_message(self, tmp_path: Path) -> None:
        bad = _tar(tmp_path / "bad.tar.gz", [("evil\x1b[31m.txt", b"x", "f")])
        result = _invoke("ingest", str(bad), "--out", str(tmp_path / "m"))
        assert "\x1b" not in result.output
