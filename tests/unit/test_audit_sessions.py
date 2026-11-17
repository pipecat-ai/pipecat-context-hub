"""Tests for ``pipecat-context-hub audit`` (extract, share bundle, repack, gist, discard, ingest).

Transcripts are synthetic. The leak tests freeze the secret, token and home-path shapes the share
bundle must never carry; the hostile-tarball cases freeze what ``ingest`` must reject.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from pipecat_context_hub.audit import egress
from pipecat_context_hub.audit import extract as audit_extract
from pipecat_context_hub.audit.report import render_report
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


HUB_TOOL_NAMES = [
    f"mcp__pipecat-context-hub__{t}" for t in ("search_docs", "get_doc", "get_hub_status")
]


def _deferred(sid: str) -> dict[str, Any]:
    """Claude Code's tool-list attachment: the hub tools listed as *deferred* (callable only after ToolSearch)."""
    att = {"type": "deferred_tools_delta", "addedNames": [*HUB_TOOL_NAMES, "Read"]}
    return {"type": "attachment", "sessionId": sid, "attachment": att}


def _prompt(sid: str, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": sid,
        "timestamp": "2026-11-18T10:00:00Z",
        "message": {"content": text},
    }


def _pipecat_work(sid: str) -> list[dict[str, Any]]:
    write = _use(
        1,
        "2026-11-18T10:00:01Z",
        "Write",
        {"file_path": "bot.py", "content": "from pipecat.pipeline import P"},
    )
    write["sessionId"] = sid
    done = _result(1, "2026-11-18T10:00:02Z", "ok")
    done["sessionId"] = sid
    return [write, done]


class TestAnalysis:
    """The extractor answers the four audit questions itself, with coded evidence only."""

    def _tree(self, root: Path, *, tool_search: bool = False) -> None:
        # Claude Code layout: <project>/<parent>.jsonl and <project>/<parent>/subagents/agent-x.jsonl
        parent = [_deferred("p1"), _prompt("p1", "build a bot"), *_pipecat_work("p1")]
        if tool_search:
            ts = _use(
                9,
                "2026-11-18T10:00:03Z",
                "ToolSearch",
                {"query": "select:mcp__pipecat-context-hub__search_docs"},
            )
            ts["sessionId"] = "p1"
            parent.append(ts)
        sub = [_deferred("p1"), _prompt("p1", "write the pipeline"), *_pipecat_work("p1")]
        _jl(root / "p1.jsonl", parent)
        _jl(root / "p1" / "subagents" / "agent-x.jsonl", sub)

    def test_unused_hub_is_explained_and_a_session_tree_is_one_participant(
        self, tmp_path: Path
    ) -> None:
        self._tree(tmp_path / "proj")
        out = tmp_path / "out"
        assert _extract(tmp_path / "proj", out).exit_code == 0
        summary = json.loads((out / "summary.json").read_text())
        usage = summary["analysis"]["usage"]
        assert summary["participants"] == 1  # a session directory is not a person
        assert usage["session_trees"] == 1 and usage["subagent_sessions"] == 1
        assert usage["hub_tools_listed_deferred"] == 2 and usage["tool_search_ever_used"] == 0
        assert usage["missed_opportunity"] == 2
        rows = {
            r["is_subagent"]: r
            for r in map(json.loads, (out / "sessions.jsonl").read_text().splitlines())
        }
        assert "hub_tools_deferred_never_loaded" in rows[False]["no_use_signals"]
        assert "parent_also_unused" in rows[True]["no_use_signals"]
        assert "parent_also_unused" not in rows[False]["no_use_signals"]
        report = (out / "share" / "report.md").read_text()
        assert (
            "Answers to the four questions" in report
            and "hub_tools_deferred_never_loaded" in report
        )

    def test_loading_the_tools_with_toolsearch_clears_the_deferred_signal(
        self, tmp_path: Path
    ) -> None:
        self._tree(tmp_path / "proj", tool_search=True)
        out = tmp_path / "out"
        assert _extract(tmp_path / "proj", out).exit_code == 0
        rows = [json.loads(r) for r in (out / "sessions.jsonl").read_text().splitlines()]
        parent = next(r for r in rows if not r["is_subagent"])
        assert parent["hub_tool_search_calls"] == 1
        assert "hub_tools_deferred_never_loaded" not in parent["no_use_signals"]

    def test_calls_carry_outcome_and_target_and_the_manifest_the_answers(
        self, audited: Path
    ) -> None:
        calls = [
            json.loads(r) for r in (audited / "share" / "calls.jsonl").read_text().splitlines()
        ]
        by_tool = {c["tool"]: c for c in calls}
        assert (by_tool["search_docs"]["outcome"], by_tool["search_docs"]["suspect_target"]) == (
            "miss",
            "hub-retrieval",
        )
        assert (by_tool["get_doc"]["outcome"], by_tool["get_doc"]["suspect_target"]) == (
            "failed",
            "hub-tools",
        )
        manifest = json.loads((audited / "share" / "manifest.json").read_text())
        analysis = manifest["analysis"]
        assert analysis["heuristic"] is True
        assert analysis["usage"]["used"] == 1
        targets = {b["target"] for b in analysis["improvement_backlog"]}
        assert {"hub-retrieval", "hub-tools"} <= targets
        # example refs are the share bundle's own (hashed) refs, and nothing sensitive leaks into the answers
        refs = {c["ref"] for c in calls}
        assert all(r in refs for b in analysis["improvement_backlog"] for r in b["example_refs"])
        blob = json.dumps(analysis)
        assert SECRET not in blob and "/Users/" not in blob

    def test_outcome_priority_is_failed_then_miss_then_the_rest(self) -> None:
        from pipecat_context_hub.audit.analysis import classify_call

        assert classify_call({"flags": ["zero_hits", "error"]}) == ("failed", "hub-tools")
        assert classify_call({"flags": ["corpus_hint", "zero_hits"]}) == ("miss", "hub-corpus")
        assert classify_call({"flags": ["slow", "downstream_error"]}) == (
            "suspect",
            "needs-judgment",
        )
        assert classify_call({"flags": ["exact_repeat"]}) == ("inefficient", "agent-misuse")
        assert classify_call({"flags": []}) == ("no_negative_signal", None)


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
        """A JS `exec` batch carries unrelated code; only the call's literal arguments are shared.

        Changed with round-2 F27: the call expression as written is no longer shared (it can carry
        comments and credential values); the tool name plus its parsed literal arguments are."""
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
        assert c["hub_invocation"] == 'search_docs({"query": "TTS (fast)"})'
        shared = "".join(p.read_text() for p in (out / "share").iterdir())
        for needle in (SECRET, "/Users/alice", BEARER):
            assert needle not in shared

    @pytest.mark.parametrize(
        "tail",
        ["`unterminated ${x", '"never closed', "(((((("],
        ids=["template-literal", "open-string", "paren-storm"],
    )
    def test_unterminated_js_call_fails_closed(self, tmp_path: Path, tail: str) -> None:
        """No closing paren found: share nothing, never the code that follows (round-2 F27: the
        call prefix `...(...)` is no longer shared either; only parsed literal arguments are)."""
        js = (
            f"const r = await tools.mcp__pipecat_context_hub__search_docs({tail}\n"
            f'const k = "{SECRET}"; fetch("https://x.example/?t={BEARER}")'
        )
        call = {"type": "custom_tool_call", "name": "exec", "input": js}
        _jl(tmp_path / "in" / "erin" / "r.jsonl", self._codex(call, '{"hits": [], "evidence": {}}'))
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        (c,) = _rows(out / "share" / "calls.jsonl")
        assert c["via"] == "mcp_js" and c["hub_invocation"] == ""
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

    def test_zero_call_bundle_that_explains_non_use_is_shareable(self, tmp_path: Path) -> None:
        """Zero calls but Pipecat work without Context Hub is the adoption data organizers need."""
        _jl(tmp_path / "in" / "zed" / "s.jsonl", _pipecat_work("s1"))
        out = tmp_path / "out"
        extracted = _extract(tmp_path / "in", out)
        assert extracted.exit_code == 0
        assert "nothing to share" not in extracted.output
        assert "worth sending" in extracted.output
        preview = _invoke("gist", str(out))
        assert preview.exit_code == 0 and "manifest.json" in preview.output

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
        from pipecat_context_hub.audit.sharedir import generated_paths

        names = {p.name for p in generated_paths(audited)}
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


def _hub_session(path: Path, query: str = "TTS", confident: bool = True) -> None:
    body = {"hits": [{"doc_id": "d"}], "evidence": {"confidence": 0.9}}
    if not confident:
        body = {"hits": [], "evidence": {"confidence": 0.1, "low_confidence": True}}
    _jl(
        path,
        [
            _use(
                1, "2026-11-18T10:00:00Z", "mcp__pipecat-context-hub__search_docs", {"query": query}
            ),
            _result(1, "2026-11-18T10:00:01Z", json.dumps(body)),
        ],
    )


class TestReviewRegressions:
    """Each case freezes a defect found in review, with the input that exposed it."""

    def test_pch_acronym_in_a_prompt_counts_as_a_mention(self) -> None:
        assert audit_extract.HUB_MENTION_RE.search("please use PCH for this")
        assert not audit_extract.HUB_MENTION_RE.search("a PCHE value")

    def test_codex_shell_argv_list_is_a_cli_call_not_a_skipped_session(
        self, tmp_path: Path
    ) -> None:
        argv = ["bash", "-lc", "pipecat-context-hub search-docs TTS"]
        rows = [
            {"type": "session_meta", "timestamp": "2026-11-18T10:00:00Z", "payload": {"id": "c1"}},
            {
                "type": "response_item",
                "timestamp": "2026-11-18T10:00:01Z",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "k1",
                    "arguments": json.dumps({"command": argv}),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-18T10:00:02Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "k1",
                    "output": '{"hits": []}',
                },
            },
        ]
        _jl(tmp_path / "in" / "carol" / "r.jsonl", rows)
        result = _extract(tmp_path / "in", tmp_path / "out")
        assert result.exit_code == 0 and "skipped" not in result.output
        (c,) = _rows(tmp_path / "out" / "calls.jsonl")
        assert (c["tool"], c["via"]) == ("search_docs", "cli")
        assert c["hub_invocation"] == "pipecat-context-hub search-docs TTS"

    @pytest.mark.parametrize(
        "inp",
        [
            {"command": "pipecat-context-hub search-docs x; export TOKEN=abcdefghij"},
            {"query": "why does API_KEY=abcdefghijkl fail"},
            {"query": 'a "quoted" /Users/bob"x path'},
        ],
    )
    def test_redacting_an_input_never_corrupts_it(
        self, tmp_path: Path, inp: dict[str, str]
    ) -> None:
        """A secret pattern's `\\S{8,}` used to swallow a JSON dump's closing quote and brace, so the
        whole session was skipped as unparseable."""
        name = "mcp__pipecat-context-hub__search_docs"
        _jl(
            tmp_path / "in" / "zed" / "s.jsonl",
            [_use(1, "2026-11-18T10:00:00Z", name, inp), _result(1, "2026-11-18T10:00:01Z", "{}")],
        )
        result = _extract(tmp_path / "in", tmp_path / "out")
        assert result.exit_code == 0 and "skipped" not in result.output
        (c,) = _rows(tmp_path / "out" / "calls.jsonl")
        assert set(c["input"]) == set(inp)

    def test_bare_hex_keys_are_redacted_even_beside_the_word_commit(self) -> None:
        for key in ("a" * 40, "b" * 32, "c" * 64):
            assert key not in egress.redact(f"key {key} commit")
        assert "C:\\Users\\alice" not in egress.redact("C:\\Users\\alice\\proj\\bot.py")

    def test_report_md_scrubs_paths_like_the_jsonl_rows(self, tmp_path: Path) -> None:
        q = r"error in C:\Users\alice\proj\bot.py and /tmp/xyz/secret.py"
        _hub_session(tmp_path / "in" / "zed" / "s.jsonl", q, confident=False)
        assert _extract(tmp_path / "in", tmp_path / "out").exit_code == 0
        report = (tmp_path / "out" / "share" / "report.md").read_text()
        assert "alice" not in report and "/tmp/xyz" not in report and "<path>" in report

    def test_claude_project_siblings_are_not_sessions(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        _hub_session(proj / "s1.jsonl")
        note = "from pipecat.pipeline import Pipeline\nuv add pipecat-ai"
        for rel in ("memory/MEMORY.md", "s1/tool-results/abc.txt", "s1/subagents/a.meta.json"):
            (proj / rel).parent.mkdir(parents=True, exist_ok=True)
            (proj / rel).write_text(note)
        out = tmp_path / "out"
        summary = json.loads(_extract(proj, out).stdout)
        assert (summary["sessions"], summary["participants"]) == (1, 1)
        assert summary["missed_opportunity"] == 0

    def test_output_inside_the_input_is_refused(self, tmp_path: Path) -> None:
        """Round 3 F19 changed this: an --out inside an input directory used to be skipped while
        scanning; it is now refused before anything is read or written (K2)."""
        src = tmp_path / "in"
        _hub_session(src / "alice" / "s1.jsonl")
        result = _extract(src, src / "audit")
        assert result.exit_code == 1 and "input directory" in result.output, result.output
        assert not (src / "audit").exists()

    def test_text_scan_only_gist_ingests(self, tmp_path: Path) -> None:
        """A text-scanned session counts mentions in `hub_calls` but writes no calls.jsonl rows; the
        gist omits the empty file and ingest must still accept it."""
        chat = tmp_path / "in" / "alice" / "chat.md"
        chat.parent.mkdir(parents=True)
        chat.write_text("ran mcp__pipecat-context-hub__search_docs\nfrom pipecat.pipeline import X")
        out = tmp_path / "out"
        assert json.loads(_extract(tmp_path / "in", out).stdout)["hub_calls"] == 1
        assert (out / "share" / "calls.jsonl").stat().st_size == 0
        gist_dir = tmp_path / "gist"
        gist_dir.mkdir()
        for n in set(BUNDLE) - {"calls.jsonl"}:
            (gist_dir / n).write_bytes((out / "share" / n).read_bytes())
        merged = tmp_path / "merged"
        assert _invoke("ingest", str(gist_dir), "--out", str(merged)).exit_code == 0

    def test_session_without_a_first_timestamp_still_has_a_start(self, tmp_path: Path) -> None:
        first = _use(1, "", "Bash", {"command": "ls"})
        del first["timestamp"]
        later = _use(2, "2026-11-18T10:00:00Z", "Bash", {"command": "ls"})
        _jl(tmp_path / "s.jsonl", [first, later])
        session, _ = audit_extract.process_session(tmp_path / "s.jsonl", "p")
        assert session["start"] == "2026-11-18T10:00:00Z"

    def test_text_block_prompts_are_counted_and_injected_ones_are_not(self, tmp_path: Path) -> None:
        def prompt(text: str, **extra: Any) -> dict[str, Any]:
            return {
                "type": "user",
                "sessionId": "s1",
                "timestamp": "2026-11-18T10:00:00Z",
                "message": {"content": [{"type": "text", "text": text}]},
                **extra,
            }

        rows = [
            prompt("use pipecat-context-hub please"),
            prompt("<system-reminder>context hub</system-reminder>", isMeta=True),
            _use(1, "2026-11-18T10:00:01Z", "Bash", {"command": "ls"}),
        ]
        _jl(tmp_path / "s.jsonl", rows)
        session, _ = audit_extract.process_session(tmp_path / "s.jsonl", "p")
        assert session["user_prompts"] == 1 and session["prompt_mentions_hub"] is True

    def test_only_the_directory_named_subagents_marks_a_subagent(self, tmp_path: Path) -> None:
        _jl(tmp_path / "subagents" / "p" / "s.jsonl", [_use(1, "", "Bash", {"command": "ls"})])
        session, _ = audit_extract.process_session(tmp_path / "subagents" / "p" / "s.jsonl", "p")
        assert session["is_subagent"] is False
        _jl(tmp_path / "s9" / "subagents" / "a.jsonl", [_use(1, "", "Bash", {"command": "ls"})])
        session, _ = audit_extract.process_session(tmp_path / "s9" / "subagents" / "a.jsonl", "p")
        assert session["is_subagent"] is True

    def test_a_downstream_error_past_the_first_200_characters_keeps_its_class(
        self, tmp_path: Path
    ) -> None:
        rows = [
            _use(
                1, "2026-11-18T10:00:00Z", "mcp__pipecat-context-hub__search_docs", {"query": "x"}
            ),
            _result(1, "2026-11-18T10:00:01Z", "{}"),
            _use(2, "2026-11-18T10:00:02Z", "Bash", {"command": "python bot.py"}),
            _result(2, "2026-11-18T10:00:03Z", "." * 400 + "\nImportError: boom"),
        ]
        _jl(tmp_path / "in" / "zed" / "s.jsonl", rows)
        assert _extract(tmp_path / "in", tmp_path / "out").exit_code == 0
        (c,) = _rows(tmp_path / "out" / "share" / "calls.jsonl")
        assert [d["error"] for d in c["downstream_errors"]] == ["ImportError"]

    @pytest.mark.parametrize(
        "query",
        [
            "Deepgram, Cartesia, OpenAI pipeline setup example now",
            "TTS and STT latency tuning for the pipeline today",
        ],
    )
    def test_multi_concept_query_with_commas_is_flagged(self, tmp_path: Path, query: str) -> None:
        _hub_session(tmp_path / "in" / "zed" / "s.jsonl", query)
        assert _extract(tmp_path / "in", tmp_path / "out").exit_code == 0
        (c,) = _rows(tmp_path / "out" / "calls.jsonl")
        assert "multi_concept_without_delimiter" in c["flags"]

    def test_discard_refuses_a_symlinked_share_directory(
        self, audited: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        (audited / "share").rename(elsewhere)
        (audited / "share").symlink_to(elsewhere)
        assert _invoke("discard", str(audited), "--yes").exit_code == 1
        assert (elsewhere / "manifest.json").exists()

    def test_repack_drops_report_rows_of_deleted_calls(self, tmp_path: Path) -> None:
        _hub_session(tmp_path / "in" / "zed" / "s.jsonl", "my private acme-secret", confident=False)
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        assert "acme-secret" in (out / "share" / "report.md").read_text()
        (out / "share" / "calls.jsonl").write_text("")
        assert _invoke("repack", str(out)).exit_code == 0
        assert "acme-secret" not in (out / "share" / "report.md").read_text()
        assert json.loads((out / "share" / "manifest.json").read_text())["counts"]["call_rows"] == 0

    def test_repack_rejects_invalid_calls_jsonl(self, audited: Path) -> None:
        (audited / "share" / "calls.jsonl").write_text("not json\n")
        result = _invoke("repack", str(audited))
        assert result.exit_code == 1 and "calls.jsonl line 1" in result.output

    @pytest.mark.parametrize(
        "calls",
        [
            [
                {
                    "ref": ["x"],
                    "tool": "t",
                    "via": "mcp",
                    "flags": [],
                    "severity": 1,
                    "result": {},
                    "followups": [],
                }
            ],
            [
                {
                    "ref": "a",
                    "tool": "t",
                    "via": "mcp",
                    "flags": [],
                    "severity": "hi",
                    "result": {},
                    "followups": [],
                }
            ],
            [5],
        ],
        ids=["ref-list", "severity-string", "row-not-object"],
    )
    def test_ingest_rejects_wrongly_typed_rows_instead_of_crashing(
        self, tmp_path: Path, calls: list[Any]
    ) -> None:
        bundle = tmp_path / "b"
        bundle.mkdir()
        (bundle / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "counts": {"hub_calls": len(calls)}})
        )
        (bundle / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in calls))
        (bundle / "sessions.jsonl").write_text("")
        result = _invoke("ingest", str(bundle), "--out", str(tmp_path / "m"))
        assert (
            result.exit_code == 1
            and "REJECTED" in result.output
            and isinstance(result.exception, SystemExit)
        )

    def test_ingest_rejects_a_non_object_manifest(self, tmp_path: Path) -> None:
        bundle = tmp_path / "b"
        bundle.mkdir()
        (bundle / "manifest.json").write_text("[1]")
        (bundle / "sessions.jsonl").write_text("")
        result = _invoke("ingest", str(bundle), "--out", str(tmp_path / "m"))
        assert result.exit_code == 1 and isinstance(result.exception, SystemExit)

    def test_ingest_rejects_a_truncated_tarball_and_deeply_nested_json(
        self, tmp_path: Path
    ) -> None:
        good = _tar(
            tmp_path / "good.tar.gz",
            [
                ("manifest.json", b'{"schema_version": 1, "counts": {"hub_calls": 0}}', "f"),
                ("sessions.jsonl", b"x" * 200_000, "f"),
            ],
        )
        data = good.read_bytes()
        truncated = tmp_path / "trunc.tar.gz"
        truncated.write_bytes(data[: len(data) // 2])
        deep = tmp_path / "deep"
        deep.mkdir()
        (deep / "manifest.json").write_text("[" * 100_000)
        (deep / "sessions.jsonl").write_text("")
        result = _invoke("ingest", str(truncated), str(deep), "--out", str(tmp_path / "m"))
        assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
        assert result.output.count("REJECTED") == 2

    @pytest.mark.parametrize("sep", ["\r", "\x0b", "\x0c", "\x1c", "\x85", "\u2028"])
    def test_repack_removes_a_deleted_calls_query_whatever_line_break_it_holds(
        self, tmp_path: Path, sep: str
    ) -> None:
        """`repack` finds report rows with str.splitlines(); a query with any of its line breaks
        must not leave a fragment behind once its call row is deleted (security review)."""
        empty = json.dumps({"hits": [], "evidence": {"confidence": 0.1, "low_confidence": True}})
        rows = [
            _use(
                1,
                "2026-11-18T10:00:00Z",
                "mcp__pipecat-context-hub__search_docs",
                {"query": f"alpha{sep}LEAKME"},
            ),
            _result(1, "2026-11-18T10:00:01Z", empty),
        ]
        _jl(tmp_path / "in" / "zed" / "s.jsonl", rows)
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        assert "LEAKME" in (out / "share" / "report.md").read_text()  # the row is there to delete
        (out / "share" / "calls.jsonl").write_text("")  # the participant deletes the call
        assert _invoke("repack", str(out)).exit_code == 0
        assert "LEAKME" not in (out / "share" / "report.md").read_text()
        with tarfile.open(out / "share.tar.gz") as tf:
            assert b"LEAKME" not in tf.extractfile("report.md").read()  # type: ignore[union-attr]

    def test_secrets_and_paths_in_input_keys_are_redacted_too(self, tmp_path: Path) -> None:
        """Redaction walked dict values but not keys; a hub call's argument names are agent-chosen."""
        inp = {"query": "TTS", SECRET: "x", f"{HOME_PATH}/f.py": "y"}
        _jl(
            tmp_path / "in" / "zed" / "s.jsonl",
            [
                _use(1, "2026-11-18T10:00:00Z", "mcp__pipecat-context-hub__search_docs", inp),
                _result(1, "2026-11-18T10:00:01Z", json.dumps({"hits": [{"doc_id": "d"}]})),
            ],
        )
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        shared = (out / "share" / "calls.jsonl").read_text()
        assert SECRET not in shared and HOME_PATH not in shared
        assert (
            SECRET not in (out / "calls.jsonl").read_text()
        )  # the local detail file is redacted too


def _canary(name: str, *, path: bool = True) -> str:
    """A secret-shaped token (and optionally a home path) unique to one field."""
    s = f"sk-CANARY{name}ABCDEFGHIJKLMNOP"
    return f"{s} /Users/CANARYP{name}/proj" if path else s


# Every field a transcript can put text into that reaches the share bundle in some form.
CANARY_FIELDS = (
    "query", "path", "argkey", "argval", "hit", "unknown", "nextq", "corpus", "errtext", "stale",
    "cli", "version", "ts", "label", "sid", "file", "downstream", "toolname", "prompt",
)  # fmt: skip


class TestEgress:
    """One egress, one tripwire: no participant text reaches share/ except through `sanitize`."""

    def _canary_transcript(self, root: Path) -> Path:
        c = _canary
        sid = c("sid", path=False)
        base = {"sessionId": sid, "cwd": "/Users/CANARYPcwd/proj", "version": c("version")}

        def use(i: int, ts: str, name: str, inp: dict[str, Any]) -> dict[str, Any]:
            block = {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}
            return {**base, "type": "assistant", "timestamp": ts, "message": {"content": [block]}}

        def res(i: int, ts: str, *texts: str, err: bool = False) -> dict[str, Any]:
            content = [{"type": "text", "text": t} for t in texts]
            block = {
                "type": "tool_result",
                "tool_use_id": f"t{i}",
                "is_error": err,
                "content": content,
            }
            return {**base, "type": "user", "timestamp": ts, "message": {"content": [block]}}

        ts0, ts1 = f"2026-11-18T10:00:00Z {c('ts')}", "2026-11-18T10:00:05Z"
        hub = json.dumps(
            {
                "hits": [{"doc_id": c("hit")}],
                "evidence": {
                    "confidence": 0.1,
                    "low_confidence": True,
                    "unknown": [c("unknown")],
                    "next_retrieval_queries": [c("nextq")],
                },
                "corpus_hint": {"note": c("corpus")},
            }
        )
        rows = [
            {**base, "type": "user", "timestamp": ts0, "message": {"content": c("prompt")}},
            use(
                1,
                ts0,
                "mcp__pipecat-context-hub__search_docs",
                {"query": c("query"), "path": c("path"), c("argkey"): c("argval")},
            ),
            res(1, ts1, hub, f"index stale since {c('stale')}"),
            use(2, ts1, "mcp__pipecat-context-hub__search_api", {"query": "x"}),
            res(2, ts1, json.dumps({"error": c("errtext")})),
            use(3, ts1, "Bash", {"command": f"pipecat-context-hub search-docs '{c('cli')}'"}),
            res(3, ts1, "exit=0\n" + hub),
            use(4, ts1, f"mcp__{c('toolname', path=False)}__x", {"a": 1}),
            res(4, ts1, f"Traceback (most recent call last):\nImportError: {c('downstream')}"),
        ]
        src = root / "in" / c("label", path=False)
        _jl(src / f"{c('file', path=False)}.jsonl", rows)
        return root / "in"

    def _shared_text(self, out: Path) -> str:
        parts = [(out / "share" / n).read_text() for n in BUNDLE]
        with tarfile.open(out / "share.tar.gz") as tf:
            parts += [tf.extractfile(m).read().decode() for m in tf.getmembers() if m.isfile()]  # type: ignore[union-attr]
        return "\n".join(parts)

    def test_no_canary_reaches_any_share_file_or_the_tarball(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = _extract(self._canary_transcript(tmp_path), out)
        assert result.exit_code == 0, result.output
        blob = self._shared_text(out)
        leaked = [f for f in CANARY_FIELDS if f"CANARY{f}" in blob]
        assert not leaked, f"fields reached the share bundle: {leaked}"
        assert "CANARYP" not in blob  # no canary home path either
        # the hub calls were really extracted (the test would pass vacuously on an empty bundle)
        rows = [json.loads(r) for r in (out / "share" / "calls.jsonl").read_text().splitlines()]
        assert {r["via"] for r in rows} == {"mcp", "cli"}

    def test_other_tool_names_are_not_shared_only_an_allowlist(self, tmp_path: Path) -> None:
        """Names of the participant's other MCP servers identify their setup: share `other`, not the name."""
        rows = [
            _use(
                1, "2026-11-18T10:00:00Z", "mcp__pipecat-context-hub__search_docs", {"query": "TTS"}
            ),
            _result(1, "2026-11-18T10:00:01Z", json.dumps({"hits": [{"doc_id": "d"}]})),
            _use(2, "2026-11-18T10:00:02Z", "mcp__acme-internal-db__query", {"q": "x"}),
            _result(2, "2026-11-18T10:00:03Z", "ok"),
            _use(3, "2026-11-18T10:00:04Z", "Bash", {"command": "ls"}),
            _result(3, "2026-11-18T10:00:05Z", "ok"),
        ]
        _jl(tmp_path / "in" / "zed" / "s.jsonl", rows)
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        row = json.loads((out / "share" / "calls.jsonl").read_text().splitlines()[0])
        assert [w["tool"] for w in row["followups"]] == ["other", "Bash"]
        assert "acme" not in self._shared_text(out)
        local = (out / "calls.jsonl").read_text()
        assert "acme-internal-db" in local  # the participant's own local detail keeps it

    def test_a_sink_that_bypasses_sanitize_fails_the_build_not_the_participant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation of the contract: if rows skip the egress, the tripwire refuses to keep anything."""
        monkeypatch.setattr(audit_extract, "sanitize", lambda v: v)
        out = tmp_path / "out"
        result = _extract(self._canary_transcript(tmp_path), out)
        assert result.exit_code == 1 and "leak check" in result.output
        assert "CANARY" not in result.output  # the error names rules, never the matched text
        assert not any((out / "share" / n).exists() for n in BUNDLE)

    @pytest.mark.parametrize("cmd", ["gist", "repack"])
    def test_a_hand_edit_that_adds_a_secret_is_refused_before_it_leaves(
        self, audited: Path, cmd: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("must not run gh")

        monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(subprocess, "run", boom)
        sessions = audited / "share" / "sessions.jsonl"
        # a valid session row (so only the leak check can refuse it) carrying a secret
        (row,) = _rows(sessions)
        sessions.write_text(sessions.read_text() + json.dumps({**row, "note": SECRET}) + "\n")
        args = ("gist", str(audited), "--yes") if cmd == "gist" else ("repack", str(audited))
        refused = _invoke(*args)
        # refused by the leak check itself (a raising fake `gh` would also exit 1), naming the file
        assert isinstance(refused.exception, SystemExit) and refused.exit_code == 1, refused.output
        reason = "leak check" if cmd == "gist" else "secret or local path"
        assert reason in refused.output and "sessions.jsonl: " in refused.output, refused.output
        assert SECRET not in refused.output

    def test_sanitize_removes_every_known_secret_shape_however_it_is_glued(self) -> None:
        """Ground truth, not the patterns' own opinion: the planted payload must be gone.

        The tripwire shares the redactor's patterns, so `leaks_in(...) == []` alone would pass when
        both are weakened together (mutation-checked). Underscore-glued shapes are the regression.
        """
        from pipecat_context_hub.audit.egress import leaks_in, sanitize

        hexkey = "0123456789abcdef" * 4
        # (text, the payload that must not survive)
        cases = [
            (SECRET, SECRET), (BEARER, "abcdefghijklmnopqrstuvwxyz0123456789"),
            (HOME_PATH, "secret-project"), ("C:\\Users\\alice\\proj\\bot.py", "alice"),
            ("TOKEN=abcdefghij12", "abcdefghij12"), ("a" * 40, "a" * 40), (hexkey, hexkey),
            ("API_KEY_" + hexkey, hexkey), ("key_" + "b" * 40, "b" * 40),
            ("AKIA" + "A" * 16, "A" * 16), ("x_AKIA" + "Q" * 16, "Q" * 16),
            ("mcp__sk-" + "A" * 20, "A" * 20), ("key_sk-" + "B" * 20, "B" * 20),
            ("my_ghp_" + "C" * 20, "C" * 20),
            ("eyJabcdefgh.eyJabcdefgh.abcdefghij", "abcdefghij"),
            ("-----BEGIN RSA PRIVATE KEY-----", "BEGIN RSA PRIVATE KEY"),
        ]  # fmt: skip
        for text, payload in cases:
            once = sanitize({text: [text, {"k": text}]})
            blob = json.dumps(once)
            assert payload not in blob, text
            assert sanitize(once) == once  # idempotent
            # the tripwire's own representation: decoded values (round 3: a scan of the serialised
            # blob reads `TOKEN=<redacted>": "` as a value, which no share file is checked as)
            assert egress._file_leaks("calls.jsonl", blob) == [], text
            assert leaks_in(text) or text in (HOME_PATH,), text
        benign = sanitize("see /guides/x and plain words and /home/<user>/y")
        assert "/guides/x" in benign and "plain words" in benign


# ---- Contract E1: the sanitizer and the tripwire see the same representation ---------------------

PEM_BODY = "MC4CAQAwBQYDK2VwBCIEIAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8g"
PEM = f"-----BEGIN PRIVATE KEY-----\n{PEM_BODY}\n-----END PRIVATE KEY-----"
STRUCTURED = "mYVerySecretValue12345!"  # no value pattern can know this is a credential

# (text, the payload that must not survive): every shape the egress claims to remove.
SECRET_SHAPES = [
    (SECRET, SECRET),
    (BEARER, "abcdefghijklmnopqrstuvwxyz0123456789"),
    ("TOKEN=abcdefghij12", "abcdefghij12"),
    ("0123456789abcdef" * 4, "0123456789abcdef" * 4),
    ("AKIA" + "Q" * 16, "Q" * 16),
    ("AIza" + "G" * 35, "G" * 35),
    ("eyJabcdefgh.eyJabcdefgh.abcdefghij", "abcdefghij"),
    (PEM, PEM_BODY),
    ("ghs_" + "S" * 36, "S" * 36),
    ("ghu_" + "U" * 36, "U" * 36),
    ("ghr_" + "R" * 36, "R" * 36),
    ("glpat-" + "L" * 20, "L" * 20),
    ("pypi-AgEIcHlwaS5vcmc" + "P" * 40, "P" * 40),
    ("postgres://admin:Sup3rS3cretPw@db.internal/app", "Sup3rS3cretPw"),
    (HOME_PATH, "secret-project"),
    ("C:\\Users\\alice\\proj\\bot.py", "alice"),
    ("/tmp/xyz/secret.py", "xyz/secret"),
    # Round-2 F21: labelled credentials in free text (the label, not the value, says it is one)
    ('{"password": "hunter2hunter2"}', "hunter2hunter2"),
    ("--password hunter2hunter2", "hunter2hunter2"),
    ("--api-key=cobalt-lantern-private", "cobalt-lantern-private"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("postgres://user:p@ss@host/db", "ss@host"),
]
# How a transcript can carry a string into a share row.
REPRESENTATIONS: dict[str, Any] = {
    "value": lambda t: {"query": t},
    "dict-key": lambda t: {t: "x"},
    "nested": lambda t: {"a": [{"b": t}, t]},
    "credential-key": lambda t: {"client_secret": t},
    "path-like": lambda t: {"path": f"see {t} there"},
}
JSON_SINKS = ("calls.jsonl", "sessions.jsonl", "manifest.json")
# Text that resembles a leak shape but is not one: none of it may fail a build.
BENIGN = [
    "TTS options:\nvoice",  # a JSON `\n` escape after `X:` read as a Windows path (F3)
    "trailing drive D:\\",
    "tab after x:\tvalue",
    'quote after x:"',
    "accent after x:é",  # serialised as a \u escape
    "a|tmp|b",  # `|` swapped for `/` made a path (F17)
    "|home|x",
    "|Users|x",
    "/guides/x and plain words",
    "git@github.com:org/repo",
    "ssh://git@github.com/org/repo",
    "https://docs.pipecat.ai/guides/x",
    "http://localhost:8080/x@y",
    "pypi-simple and ghs short and glpat",
    # Round-2 F21: labels that are not credential names, and values too short to be one
    "password_hint: your pet",
    "--password-file ./pw.txt",
    "max_tokens: 4096",
    "token_count=12",
    # (round 3: "a token: is" moved out: a value has no minimum length after a credential label)
    "Authorization header docs",
]


def _escape_all(v: Any) -> str:
    """JSON for ``v`` with every character of every string written as a ``\\uXXXX`` escape."""
    if isinstance(v, str):
        return '"' + "".join(f"\\u{ord(ch):04x}" for ch in v) + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_escape_all(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_escape_all(k)}: {_escape_all(x)}" for k, x in v.items()) + "}"
    return json.dumps(v)


def _write_sink(share: Path, sink: str, text: str) -> None:
    share.mkdir(parents=True, exist_ok=True)
    (share / sink).write_text(text + ("\n" if sink.endswith(".jsonl") else ""))


def _decoded_text(path: Path) -> str:
    """Every decoded value of a JSON / JSONL file, as text a reader of the decoded data sees."""
    text = path.read_text()
    docs = text.splitlines() if path.suffix == ".jsonl" else [text]
    return "\n".join(json.dumps(json.loads(d), ensure_ascii=False) for d in docs if d.strip())


class TestEgressParity:
    """Contract E1: what `sanitize` produces passes `verify_share`; what it misses, the check sees.

    State space: secret shape x representation x sink x encoding (sanitised / raw hand edit /
    `\\uXXXX`-escaped hand edit), plus benign look-alikes that must never fail a build.
    """

    @pytest.mark.parametrize("sink", JSON_SINKS)
    @pytest.mark.parametrize("rep", list(REPRESENTATIONS))
    @pytest.mark.parametrize("shape", SECRET_SHAPES, ids=[p for _, p in SECRET_SHAPES])
    def test_every_shape_in_every_representation_and_json_sink(
        self, tmp_path: Path, shape: tuple[str, str], rep: str, sink: str
    ) -> None:
        text, payload = shape
        row = REPRESENTATIONS[rep](text)
        share = tmp_path / "share"
        _write_sink(share, sink, json.dumps(egress.sanitize(row)))
        assert _verify_share(share, (sink,)) == []
        assert payload not in _decoded_text(share / sink)
        for encoded in (json.dumps(row), _escape_all(row)):  # a hand edit, plain or escaped
            _write_sink(share, sink, encoded)
            assert _verify_share(share, (sink,)), (rep, sink, encoded[:40])

    @pytest.mark.parametrize("shape", SECRET_SHAPES, ids=[p for _, p in SECRET_SHAPES])
    def test_every_shape_in_the_text_sinks(self, tmp_path: Path, shape: tuple[str, str]) -> None:
        text, payload = shape
        share = tmp_path / "share"
        row = {
            "ref": "r",
            "tool": "search_docs",
            "flags": ["zero_hits"],
            "result": {},
            "input": egress.sanitize({"query": f"x {text} y"}),
        }
        report = render_report({}, [row])
        _write_sink(share, "report.md", report)
        assert _verify_share(share, ("report.md",)) == []
        assert payload not in report
        _write_sink(share, "report.md", f"| q | {text} |")  # a hand edit to report.md
        assert _verify_share(share, ("report.md",))

    @pytest.mark.parametrize("sink", JSON_SINKS)
    @pytest.mark.parametrize("rep", list(REPRESENTATIONS))
    @pytest.mark.parametrize("text", BENIGN)
    def test_benign_look_alikes_pass_in_every_representation(
        self, tmp_path: Path, text: str, rep: str, sink: str
    ) -> None:
        share = tmp_path / "share"
        row = REPRESENTATIONS[rep](text)
        clean = egress.sanitize(row)
        _write_sink(share, sink, json.dumps(clean))
        assert _verify_share(share, (sink,)) == []
        if rep == "dict-key" and egress.is_credential_name(text):
            # the label model's stated cost: a KEY containing a stem (`Authorization ...`) names a
            # credential, so its value goes; the key text itself is kept
            assert clean == {text: egress.REDACTED}
        elif rep != "credential-key":
            assert clean == row  # nothing benign is rewritten

    def test_benign_look_alikes_survive_extract_and_the_report(self, tmp_path: Path) -> None:
        """F3 / F17: a benign query used to fail the whole extract with ShareLeakError."""
        empty = json.dumps({"hits": [], "evidence": {"confidence": 0.1, "low_confidence": True}})
        name = "mcp__pipecat-context-hub__search_docs"
        rows: list[dict[str, Any]] = []
        for i, q in enumerate(BENIGN):
            rows += [
                _use(i, "2026-11-18T10:00:00Z", name, {"query": q, "max_tokens": 5}),
                _result(i, "2026-11-18T10:00:01Z", empty),
            ]
        _jl(tmp_path / "in" / "zed" / "s.jsonl", rows)
        out = tmp_path / "out"
        result = _extract(tmp_path / "in", out)
        assert result.exit_code == 0, result.output
        shared = _rows(out / "share" / "calls.jsonl")
        assert [r["input"] for r in shared] == [{"query": q, "max_tokens": 5} for q in BENIGN]
        report = (out / "share" / "report.md").read_text()
        assert "a\\|tmp\\|b" in report and "a/tmp/b" not in report
        assert _verify_share(out / "share", tuple(BUNDLE)) == []

    def test_a_long_query_is_cut_at_whitespace_never_inside_a_token(self, tmp_path: Path) -> None:
        """Cutting a 78-char hex run followed by `g` at 80 chars made a bare hex key appear."""
        q = "x " + "a" * 78 + "ggggg"
        _hub_session(tmp_path / "in" / "zed" / "s.jsonl", q, confident=False)
        result = _extract(tmp_path / "in", tmp_path / "out")
        assert result.exit_code == 0, result.output
        assert "| x … |" in (tmp_path / "out" / "share" / "report.md").read_text()

    @pytest.mark.parametrize("sink", [*JSON_SINKS, "report.md"])
    @pytest.mark.parametrize("cmd", ["gist", "repack"])
    def test_an_escaped_token_hand_edited_into_any_sink_is_refused(
        self, audited: Path, cmd: str, sink: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F25: the tripwire scanned serialised bytes, so `\\u0073k-...` passed repack and gist."""

        def boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("must not run gh")

        if cmd == "repack" and sink == "report.md":
            pytest.skip("repack regenerates report.md")
        monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(subprocess, "run", boom)
        target = audited / "share" / sink
        escaped = "\\u0073k-QQQQQQQQQQQQQQQQQQQQQQQQ"
        if sink == "report.md":
            target.write_text(target.read_text() + "\nsk-QQQQQQQQQQQQQQQQQQQQQQQQ\n")
        else:
            text = target.read_text() or "{}\n"
            target.write_text(text.replace("{", '{"note": "' + escaped + '", ', 1))
        args = ("gist", str(audited), "--yes") if cmd == "gist" else ("repack", str(audited))
        refused = _invoke(*args)
        # refused by the leak check itself (not by `gh` failing), naming the file
        assert isinstance(refused.exception, SystemExit), refused.output
        reason = "leak check" if cmd == "gist" else "secret or local path"
        assert reason in refused.output and f"{sink}: " in refused.output, refused.output
        assert "QQQQQQQQ" not in refused.output

    def test_a_structured_credential_is_redacted_and_checked_independently(
        self, tmp_path: Path
    ) -> None:
        """F24: key and value were cleaned separately, so `{"api_key": "..."}` survived."""
        inp = {"query": "TTS", "api_key": STRUCTURED, "token_count": 3, "password": None}
        _jl(
            tmp_path / "in" / "zed" / "s.jsonl",
            [
                _use(1, "2026-11-18T10:00:00Z", "mcp__pipecat-context-hub__search_docs", inp),
                _result(1, "2026-11-18T10:00:01Z", json.dumps({"hits": [{"doc_id": "d"}]})),
            ],
        )
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        (row,) = _rows(out / "share" / "calls.jsonl")
        assert row["input"] == {
            "query": "TTS",
            "api_key": egress.REDACTED,
            "token_count": 3,
            "password": None,
        }
        # the check is structural: it does not need a pattern to recognise the value
        share = tmp_path / "hand"
        _write_sink(share, "calls.jsonl", json.dumps({"input": {"apiKey": STRUCTURED}}))
        assert _verify_share(share, ("calls.jsonl",)) == ["calls.jsonl: credential field"]

    @pytest.mark.parametrize(
        ("name", "credential"),
        [
            ("api_key", True), ("apiKey", True), ("X-Api-Key", True), ("OPENAI_API_KEY", True),
            ("client_secret", True), ("access_token", True), ("db.password", True),
            ("passwd", True), ("Authorization", True), ("private_key", True),
            ("aws_secret_access_key", True), ("secretKey", True), ("cookie", True),
            ("credentials", True), ("query", False), ("max_tokens", False),
            ("token_count", False), ("secret_santa", False), ("keys", False),
            ("password_hint", False), ("doc_id", False), ("hub_tool_search_calls", False),
        ],
    )  # fmt: skip
    def test_the_credential_name_rule(self, name: str, credential: bool) -> None:
        assert egress.is_credential_name(name) is credential

    def test_no_field_the_extractor_writes_is_credential_named(self, audited: Path) -> None:
        """The rule must never eat the bundle's own schema."""
        keys: set[str] = set()

        def walk(v: Any) -> None:
            if isinstance(v, dict):
                keys.update(k for k in v if isinstance(k, str))
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

        for n in JSON_SINKS:
            for line in (audited / "share" / n).read_text().splitlines():
                if n.endswith(".jsonl"):
                    walk(json.loads(line))
            if n.endswith(".json"):
                walk(json.loads((audited / "share" / n).read_text()))
        assert keys and not [k for k in keys if egress.is_credential_name(k)]

    def test_the_whole_private_key_is_removed_not_just_its_header(self) -> None:
        """F23: only `BEGIN PRIVATE KEY` was replaced; body and footer survived."""
        for text in (PEM, f"key: {PEM} after", f"-----BEGIN RSA PRIVATE KEY-----\n{PEM_BODY}"):
            once = egress.sanitize({"q": text})
            assert PEM_BODY not in json.dumps(once) and "END PRIVATE" not in json.dumps(once)
            assert egress.sanitize(once) == once

    def test_a_duplicate_key_or_an_unparsable_line_is_refused(self, tmp_path: Path) -> None:
        share = tmp_path / "share"
        _write_sink(share, "calls.jsonl", '{"query": "' + SECRET + '", "query": "x"}')
        assert _verify_share(share, ("calls.jsonl",))  # json.loads keeps only the last
        _write_sink(share, "sessions.jsonl", "not json")
        assert _verify_share(share, ("sessions.jsonl",)) == [
            "sessions.jsonl: line 1 is not valid JSON"
        ]

    def test_the_report_renders_rows_verbatim_and_leaves_detection_to_the_tripwire(
        self, tmp_path: Path
    ) -> None:
        """F11: the renderer ran its own redaction on rows that had already passed the egress."""
        row = {
            "ref": "r",
            "tool": "search_docs",
            "flags": ["zero_hits"],
            "result": {},
            "input": {"query": f"{SECRET} a|b\nc"},
        }
        report = render_report({}, [row])
        # escaped and joined, never rewritten (round-2 G1: a line break renders as ` · `, a
        # separator no rule matches across, instead of a space that could join a label to a value)
        assert f"{SECRET} a\\|b · c" in report
        _write_sink(tmp_path / "share", "report.md", report)
        assert _verify_share(tmp_path / "share", ("report.md",))

    def test_sanitize_is_a_fixpoint_the_tripwire_accepts(self) -> None:
        """Property (no finding): `clean_text` repeats until stable, so its output never trips a rule."""
        for text in ("API_KEY= " + "f" * 40, "password: Bearer " + "z" * 30, "token " + "a" * 40):
            once = egress.sanitize(text)
            assert egress.leaks_in(once) == [] and egress.sanitize(once) == once


# ---- Contract E2: a shared invocation comes from the argv of an executed hub command ------------

SHELL_CASES = [
    # (shell line, the hub tool it runs or None, the invocation shared)
    (
        'uv run pipecat-context-hub search-docs "TTS & STT" --limit 3 | head',
        "search_docs",
        "pipecat-context-hub search-docs 'TTS & STT' --limit 3",
    ),
    (
        "pipecat-context-hub search-docs TTS # Acquisition target: Acme at $42m",
        "search_docs",
        "pipecat-context-hub search-docs TTS",
    ),
    (
        "printf '%s' 'pipecat-context-hub search-docs TTS' 'Acquisition target: Acme at $42m'",
        None,
        "",
    ),
    ('echo "pipecat-context-hub search-docs x"', None, ""),
    ("grep -r 'pipecat-context-hub search-docs' .", None, ""),
    ("cat <<'EOF'\npipecat-context-hub search-docs x\nEOF", None, ""),
    ("x=$(pipecat-context-hub search-api Foo) && echo $x", "search_api", "pipecat-context-hub search-api Foo"),
    ("bash -lc 'pipecat-context-hub status'", "get_hub_status", "pipecat-context-hub status"),
    (
        "cd /x && uvx --from pipecat-ai-context-hub pipecat-context-hub search-docs a 2>&1",
        "search_docs",
        "pipecat-context-hub search-docs a",
    ),
    (
        f"{HOME_PATH}/.venv/bin/pipecat-context-hub search-docs a > /tmp/out.json",
        "search_docs",
        "pipecat-context-hub search-docs a",
    ),
    (
        "timeout 30 env FOO=1 uv run pipecat-context-hub get-doc --path /guides/x; echo done",
        "get_doc",
        "pipecat-context-hub get-doc --path /guides/x",
    ),
    (
        "pipecat context-hub check-deprecation PipelineTask",
        "check_deprecation",
        "pipecat context-hub check-deprecation PipelineTask",
    ),
    ('pipecat-context-hub search-docs x <<< "data"', "search_docs", "pipecat-context-hub search-docs x"),
    ("pipecat-context-hub search-docs a\nrm -rf b", "search_docs", "pipecat-context-hub search-docs a"),
    ("pipecat-context-hub search-docs a#b", "search_docs", "pipecat-context-hub search-docs 'a#b'"),
    # cannot be tokenised: still counted (regex), but no command text is shared (fail closed)
    ("pipecat-context-hub search-docs 'unterminated", "search_docs", ""),
]  # fmt: skip


class TestShellInvocation:
    @pytest.mark.parametrize(("line", "tool", "invocation"), SHELL_CASES)
    def test_only_executed_hub_commands_are_calls_and_their_argv_is_shared(
        self, line: str, tool: str | None, invocation: str
    ) -> None:
        """F16 / F26: the line was split on `& | ; >` inside quotes and kept comments and mentions."""
        found, via, _ = audit_extract.detect_hub("Bash", {"command": line})
        assert (found, via) == ((tool, "cli") if tool else (None, None))
        assert audit_extract.hub_invocation({"command": line}, "cli") == invocation

    def test_codex_argv_and_nested_substitution(self) -> None:
        argv = ["bash", "-lc", "cd /x && pipecat-context-hub search-docs 'a b'"]
        assert audit_extract.hub_invocation({"command": argv}, "cli") == (
            "pipecat-context-hub search-docs 'a b'"
        )
        line = 'pipecat-context-hub search-docs "a $(pipecat-context-hub status)"'
        assert audit_extract.detect_hub("Bash", {"command": line})[2] == 2

    def test_a_comment_or_a_mention_never_reaches_the_bundle(self, tmp_path: Path) -> None:
        cli_json = json.dumps({"hits": [{"doc_id": "d"}], "evidence": {"confidence": 0.9}})
        rows = [
            _use(1, "2026-11-18T10:00:00Z", "Bash", {"command": SHELL_CASES[1][0]}),
            _result(1, "2026-11-18T10:00:01Z", cli_json),
            _use(2, "2026-11-18T10:00:02Z", "Bash", {"command": SHELL_CASES[2][0]}),
            _result(2, "2026-11-18T10:00:03Z", "printed"),
        ]
        _jl(tmp_path / "in" / "zed" / "s.jsonl", rows)
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        (row,) = _rows(out / "share" / "calls.jsonl")  # the printf mention is not a call
        assert row["hub_invocation"] == "pipecat-context-hub search-docs TTS"
        shared = "".join((out / "share" / n).read_text() for n in BUNDLE)
        assert "Acquisition" not in shared and "Acme" not in shared

    def test_another_commands_json_never_becomes_the_hub_result(self, tmp_path: Path) -> None:
        """F19: the first `{` anywhere in shell output was taken as the hub's response."""
        log = json.dumps({"message": "auth failed for user alice at db.internal.corp"})
        line = "tail -1 app.log; pipecat-context-hub status"
        rows = [
            _use(1, "2026-11-18T10:00:00Z", "Bash", {"command": line}),
            _result(1, "2026-11-18T10:00:01Z", log),
            _use(2, "2026-11-18T10:00:02Z", "Bash", {"command": line}),
            _result(2, "2026-11-18T10:00:03Z", log + "\n" + json.dumps({"server_version": "1"})),
        ]
        _jl(tmp_path / "in" / "zed" / "s.jsonl", rows)
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        first, second = _rows(out / "share" / "calls.jsonl")
        assert first["result"]["parse"] == "cli_unparsed" and "error_text" not in first["result"]
        assert "error" not in first["flags"]
        assert second["result"]["parse"] == "embedded"
        assert "db.internal" not in "".join((out / "share" / n).read_text() for n in BUNDLE)

    @pytest.mark.parametrize(
        ("output", "parse", "flags"),
        [
            ('TTS results:\n{"hits": []}', "embedded", ["zero_hits"]),
            ("the script printed something else", "js_unparsed", []),
            ('{"message": "from the script"}', "js_unparsed", []),
        ],
    )
    def test_a_js_batch_output_is_never_blamed_on_the_hub(
        self, tmp_path: Path, output: str, parse: str, flags: list[str]
    ) -> None:
        """F15: a JS batch's own stdout was flagged non_json_result and classed failed/hub-tools."""
        js = 'const r = await tools.mcp__pipecat_context_hub__search_docs({query: "x"});'
        call = {"type": "custom_tool_call", "name": "exec", "input": js}
        _jl(tmp_path / "in" / "dan" / "r.jsonl", TestOtherHarnesses._codex(call, output))
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        (c,) = _rows(out / "share" / "calls.jsonl")
        assert (c["result"]["parse"], c["flags"]) == (parse, flags)
        assert c["outcome"] != "failed" and c["suspect_target"] != "hub-tools"


# ---- Result shape, error class, output safety -------------------------------------------------


class TestResultShapeAndErrors:
    def test_every_hub_tool_result_key_is_its_output_models(self) -> None:
        """F4: get_code_snippet answers with `snippets`, not `hits`. Table-driven over all tools."""
        from typing import get_origin

        from pydantic import BaseModel

        from pipecat_context_hub.audit.shell import SEARCH_TOOLS
        from pipecat_context_hub.shared import types as T

        models: dict[str, type[BaseModel]] = {
            "search_docs": T.SearchDocsOutput, "search_examples": T.SearchExamplesOutput,
            "search_api": T.SearchApiOutput, "get_code_snippet": T.GetCodeSnippetOutput,
            "get_doc": T.GetDocOutput, "get_example": T.GetExampleOutput,
            "check_deprecation": T.CheckDeprecationOutput, "get_hub_status": T.HubStatusOutput,
        }  # fmt: skip
        assert set(models) == set(audit_extract.HUB_TOOLS) == set(audit_extract.HUB_RESULT_KEY)
        for tool, model in models.items():
            key = audit_extract.HUB_RESULT_KEY[tool]
            assert key in model.model_fields, tool
            is_list = get_origin(model.model_fields[key].annotation) is list
            assert is_list is (key in audit_extract.RESULT_LIST_KEYS), tool
            if tool in SEARCH_TOOLS:  # zero_hits needs a result list to count
                assert is_list, tool
                for via in ("mcp", "cli", "mcp_js"):
                    res = audit_extract.summarize_hub_result(
                        json.dumps({key: [], "evidence": {}}), [], via, tool
                    )
                    assert res["n_hits"] == 0, (tool, via)

    def test_an_empty_code_snippet_result_is_a_zero_hit(self, tmp_path: Path) -> None:
        name = "mcp__pipecat-context-hub__get_code_snippet"
        _jl(
            tmp_path / "in" / "zed" / "s.jsonl",
            [
                _use(1, "2026-11-18T10:00:00Z", name, {"symbol": "Foo"}),
                _result(1, "2026-11-18T10:00:01Z", json.dumps({"snippets": [], "evidence": {}})),
            ],
        )
        assert _extract(tmp_path / "in", tmp_path / "out").exit_code == 0
        (c,) = _rows(tmp_path / "out" / "share" / "calls.jsonl")
        assert c["result"]["n_hits"] == 0 and "zero_hits" in c["flags"]

    @pytest.mark.parametrize(
        ("output", "error"),
        [
            (
                "Traceback (most recent call last):\n"
                + "".join(f'  File "/Users/alice/m{i}.py", line {i}, in f\n    foo()\n' for i in range(3))
                + "ImportError: cannot import name Foo",
                "ImportError",
            ),
            (
                "Traceback (most recent call last):\n  File \"a.py\", line 1\nKeyError: 'k'\n\n"
                "During handling of the above exception, another exception occurred:\n\n"
                "Traceback (most recent call last):\n  File \"b.py\", line 2\n"
                "requests.exceptions.ConnectionError: refused by db.internal",
                "ConnectionError",
            ),
            ("Traceback (most recent call last):\n  File \"a.py\", line 1\nKeyboardInterrupt\n", "KeyboardInterrupt"),
            ("Traceback (most recent call last):\n  File \"a.py\", line 1\n", "Traceback"),
        ],
    )  # fmt: skip
    def test_a_traceback_shares_its_real_exception_class_only(
        self, tmp_path: Path, output: str, error: str
    ) -> None:
        """F5: every real traceback was shared as error class `Traceback`."""
        rows = [
            _use(
                1, "2026-11-18T10:00:00Z", "mcp__pipecat-context-hub__search_docs", {"query": "x"}
            ),
            _result(1, "2026-11-18T10:00:01Z", "{}"),
            _use(2, "2026-11-18T10:00:02Z", "Bash", {"command": "python bot.py"}),
            _result(2, "2026-11-18T10:00:03Z", output),
        ]
        _jl(tmp_path / "in" / "zed" / "s.jsonl", rows)
        assert _extract(tmp_path / "in", tmp_path / "out").exit_code == 0
        (c,) = _rows(tmp_path / "out" / "share" / "calls.jsonl")
        assert [d["error"] for d in c["downstream_errors"]] == [error]

    @pytest.mark.parametrize(
        "plant", ["share/SCHEMA.md", "share/calls.jsonl", "share.tar.gz", "summary.json", "share"]
    )
    def test_extract_refuses_a_planted_symlink_before_writing(
        self, tmp_path: Path, plant: str
    ) -> None:
        """F20: extract wrote through a pre-planted link and packed it as a symlink member."""
        _hub_session(tmp_path / "in" / "zed" / "s.jsonl")
        out = tmp_path / "out"
        victim = tmp_path / "victim"
        victim.mkdir() if plant == "share" else victim.write_text("keep me")
        (out / "share").mkdir(parents=True)
        if plant == "share":
            (out / "share").rmdir()
        (out / plant).symlink_to(victim)
        result = _extract(tmp_path / "in", out)
        assert result.exit_code == 1 and "symlink" in result.output
        if plant == "share":
            assert list(victim.iterdir()) == []
        else:
            assert victim.read_text() == "keep me"

    def test_repack_refuses_a_symlinked_tarball(self, audited: Path, tmp_path: Path) -> None:
        victim = tmp_path / "victim"
        victim.write_text("keep me")
        (audited / "share.tar.gz").unlink()
        (audited / "share.tar.gz").symlink_to(victim)
        assert _invoke("repack", str(audited)).exit_code == 1
        assert victim.read_text() == "keep me"


# ---- C1: the bundle contract has one definition -------------------------------------------------


def _verify_share(share: Path, names: tuple[str, ...]) -> list[str]:
    """The leak check over the named share/ files as they are on disk (egress checks bytes)."""
    return egress.verify_files({n: (share / n).read_bytes() for n in names if (share / n).exists()})


def _merge(paths: list[Path], out: Path) -> dict[str, Any]:
    from pipecat_context_hub.audit.ingest import merge_bundles

    return merge_bundles(paths, out)


def _fake_gh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "https://gist.github.com/x/abc\n", "")

    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _posted_dir(share: Path, argv: list[str], dest: Path) -> Path:
    """What a clone of the gist holds: exactly the files `gh gist create` was given."""
    dest.mkdir()
    for a in argv[argv.index("--") + 1 :]:
        (dest / Path(a).name).write_bytes((share / Path(a).name).read_bytes())
    return dest


def _source(kind: str, root: Path) -> Path:
    """A transcript directory for one kind of fresh extract."""
    src = root / "in"
    if kind == "calls":
        _claude_session(src / "alice" / "s1.jsonl")
    elif kind == "missed":
        _jl(src / "zed" / "s.jsonl", _pipecat_work("s1"))
    elif kind == "nothing":
        _jl(src / "zed" / "s.jsonl", [_use(1, "2026-11-18T10:00:00Z", "Bash", {"command": "ls"})])
    elif kind == "text_scan":
        chat = src / "alice" / "chat.md"
        chat.parent.mkdir(parents=True)
        chat.write_text("ran mcp__pipecat-context-hub__search_docs\nfrom pipecat.pipeline import X")
    return src


def _keep_lines(path: Path, n: int) -> None:
    path.write_text("".join(line + "\n" for line in path.read_text().splitlines()[:n]))


# (fresh extract kind, hand edit, bundle has content after the edit)
VALID_CELLS = {
    "fresh-calls": ("calls", None, True),
    "rows-hand-deleted": ("calls", ("calls.jsonl", 1), True),
    "all-rows-deleted": ("calls", ("calls.jsonl", 0), True),  # the session still used the hub
    "all-rows-and-sessions-deleted": ("calls", ("both", 0), False),
    "zero-sessions-with-calls": ("calls", ("sessions.jsonl", 0), True),
    "zero-calls-pipecat-work": ("missed", None, True),
    "nothing-found": ("nothing", None, False),
    "text-scan-hub-calls-no-rows": ("text_scan", None, True),
}


def _required_key_cells() -> dict[str, tuple[str, Any]]:
    from pipecat_context_hub.audit.bundle import CALL_FIELDS, SESSION_FIELDS

    cells: dict[str, tuple[str, Any]] = {}
    for k in sorted(CALL_FIELDS):
        cells[f"call-missing-{k}"] = (
            "calls.jsonl",
            lambda r, k=k: {x: v for x, v in r.items() if x != k},
        )
    for k in sorted(SESSION_FIELDS):
        cells[f"session-missing-{k}"] = (
            "sessions.jsonl",
            lambda r, k=k: {x: v for x, v in r.items() if x != k},
        )
    return cells


INVALID_CELLS: dict[str, tuple[str, Any]] = {
    **_required_key_cells(),
    "severity-string": ("calls.jsonl", lambda r: {**r, "severity": "hi"}),
    "severity-bool": ("calls.jsonl", lambda r: {**r, "severity": True}),
    "ref-list": ("calls.jsonl", lambda r: {**r, "ref": ["x"]}),
    "via-null": ("calls.jsonl", lambda r: {**r, "via": None}),
    "flags-not-strings": ("calls.jsonl", lambda r: {**r, "flags": [1]}),
    "result-list": ("calls.jsonl", lambda r: {**r, "result": []}),
    "followups-object": ("calls.jsonl", lambda r: {**r, "followups": {}}),
    "row-not-object": ("calls.jsonl", lambda r: [r]),
    "hub-used-string": ("sessions.jsonl", lambda r: {**r, "hub_used": "yes"}),
    "participant-null": ("sessions.jsonl", lambda r: {**r, "participant": None}),
    "duplicate-key": ("calls.jsonl", "dup"),
    "bad-json": ("calls.jsonl", "bad"),
    # Q3: latency_s and input were untyped; a string latency_s crashed repack's report render.
    "latency-string": ("calls.jsonl", lambda r: {**r, "latency_s": "slow"}),
    "latency-negative": ("calls.jsonl", lambda r: {**r, "latency_s": -1}),
    "latency-nan": ("calls.jsonl", lambda r: {**r, "latency_s": float("nan")}),
    "latency-infinite": ("calls.jsonl", lambda r: {**r, "latency_s": float("inf")}),
    "latency-bool": ("calls.jsonl", lambda r: {**r, "latency_s": True}),
    "input-string": ("calls.jsonl", lambda r: {**r, "input": "TTS"}),
    "input-list": ("calls.jsonl", lambda r: {**r, "input": ["TTS"]}),
}  # fmt: skip


class TestBundleContract:
    """Contract C1: one definition of the bundle, so producer and consumer cannot disagree.

    Invariant: a bundle `repack` accepts is one `ingest` accepts; a bundle `gist` posts (with the
    empty JSONL files it leaves out) is one `ingest` accepts; and "nothing to share" is the same
    decision (`bundle.has_content` on the manifest's row counts) in `extract`, `gist` and `ingest`.
    State space: VALID_CELLS x {repack, gist, ingest of the tarball, ingest of the gist} and
    INVALID_CELLS x {repack, gist, ingest}.
    """

    def test_one_definition_of_the_contract(self) -> None:
        """F2: ingest redefined the file list, the schema version and the required call keys."""
        from pipecat_context_hub import cli_audit
        from pipecat_context_hub.audit import bundle, ingest

        for mod in (ingest, audit_extract, cli_audit):
            for name in ("SUPPORTED_SCHEMA", "FILES", "CALL_KEYS", "SESSION_KEYS", "SEVERITY"):
                assert not hasattr(mod, name), (mod.__name__, name)
        assert vars(audit_extract)["BUNDLE_FILES"] is bundle.BUNDLE_FILES
        assert vars(audit_extract)["SCHEMA_VERSION"] is bundle.SCHEMA_VERSION
        assert not hasattr(cli_audit, "_nothing_to_share")  # one predicate: bundle.has_content
        bundle_src = Path(bundle.__file__).read_text()
        assert "from pipecat_context_hub" not in bundle_src  # a leaf: nothing to drift from

    @pytest.mark.parametrize("cell", list(VALID_CELLS))
    def test_every_valid_bundle_round_trips_and_decides_empty_the_same_way(
        self, tmp_path: Path, cell: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F2/F12/F13: repack, gist and ingest each had their own rules for rows, counts and the
        files a gist may leave out; deleting every row or every session made them disagree."""
        kind, edit, content = VALID_CELLS[cell]
        out = tmp_path / "out"
        extracted = _extract(_source(kind, tmp_path), out)
        assert extracted.exit_code == 0, extracted.output
        if edit is None:  # the extract message uses the same rule
            assert ("nothing to share" in extracted.output) is (not content), extracted.output
        else:
            name, n = edit
            for f in ("calls.jsonl", "sessions.jsonl") if name == "both" else (name,):
                _keep_lines(out / "share" / f, n)
        repacked = _invoke("repack", str(out))
        assert repacked.exit_code == 0, repacked.output  # repack accepts...
        share = out / "share"
        counts = json.loads((share / "manifest.json").read_text())["counts"]
        assert counts["call_rows"] == len(_rows(share / "calls.jsonl"))
        assert counts["sessions"] == len(_rows(share / "sessions.jsonl"))

        seen = _fake_gh(monkeypatch)
        strict = _invoke("gist", str(out), "--yes")
        assert (strict.exit_code == 0) is content, strict.output  # gist's empty decision
        assert ("no Context Hub calls and no Pipecat work" in strict.output) is (not content)
        posted = _invoke("gist", str(out), "--yes", "--allow-empty")
        assert posted.exit_code == 0, posted.output
        names = {Path(a).name for a in seen[-1][seen[-1].index("--") + 1 :]}
        empty = {
            n for n in ("calls.jsonl", "sessions.jsonl") if not (share / n).read_text().strip()
        }
        assert names == set(BUNDLE) - empty
        gist = _posted_dir(share, seen[-1], tmp_path / "gist")

        merged = _merge([out / "share.tar.gz", gist], tmp_path / "merged")
        report = merged["report"]
        assert [r["status"] for r in report] == ["ok", "ok"], report  # ...so ingest accepts
        assert [r["has_content"] for r in report] == [content, content]  # same empty decision
        assert report[1]["duplicate_of"] == "share.tar.gz"  # the gist is the same bundle

    @pytest.mark.parametrize("cell", list(INVALID_CELLS))
    def test_a_row_ingest_rejects_is_refused_by_repack_and_gist_too(
        self, audited: Path, tmp_path: Path, cell: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F2: delete `severity` or `via` from a row: repack succeeded, the organizer's ingest
        rejected the bundle. Every row validator is now the shared one."""
        name, change = INVALID_CELLS[cell]
        f = audited / "share" / name
        first, *rest = f.read_text().splitlines()
        if change == "dup":
            bad = first.replace("{", '{"tool": "x", ', 1)
        elif change == "bad":
            bad = first[:-1]
        else:
            bad = json.dumps(change(json.loads(first)))
        f.write_text("".join(line + "\n" for line in [bad, *rest]))
        repacked = _invoke("repack", str(audited))
        assert repacked.exit_code == 1 and isinstance(repacked.exception, SystemExit)
        assert f"{name} line 1" in repacked.output, repacked.output
        seen = _fake_gh(monkeypatch)
        gisted = _invoke("gist", str(audited), "--yes", "--allow-empty")
        assert gisted.exit_code == 1 and not seen, gisted.output
        tar = _tar(
            tmp_path / "bad.tar.gz",
            [(n, (audited / "share" / n).read_bytes(), "f") for n in BUNDLE],
        )
        merged = _merge([audited / "share", tar], tmp_path / "m")
        assert [r["status"] for r in merged["report"]] == ["rejected", "rejected"]
        assert all(f"{name} line 1" in r["reason"] for r in merged["report"]), merged["report"]

    def test_a_hand_edit_without_repack_is_refused_until_repacked(
        self, audited: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F12: after deleting every row, gist read `hub_calls` (still 3) and ingest read
        `call_rows`; now both read the row counts, and stale counts are refused by both."""
        (audited / "share" / "calls.jsonl").write_text("")
        seen = _fake_gh(monkeypatch)
        stale = _invoke("gist", str(audited), "--yes")
        assert stale.exit_code == 1 and "run `audit repack`" in stale.output and not seen
        (rejected,) = _merge([audited / "share"], tmp_path / "m1")["report"]
        assert (
            rejected["status"] == "rejected"
            and "call_rows says 3, rows say 0" in rejected["reason"]
        )
        assert _invoke("repack", str(audited)).exit_code == 0
        assert _invoke("gist", str(audited), "--yes").exit_code == 0  # a session still used it
        (ok,) = _merge([audited / "share"], tmp_path / "m2")["report"]
        assert ok["status"] == "ok" and ok["calls"] == 0

    def test_a_manifest_without_call_rows_is_rejected_with_its_reason(self, tmp_path: Path) -> None:
        """F9: schema v1 accepted a `hub_calls` fallback "for older bundles"; none was ever
        distributed, so v1 has one manifest shape."""
        bundle = tmp_path / "b"
        bundle.mkdir()
        (bundle / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "counts": {"hub_calls": 0}})
        )
        for n in ("SCHEMA.md", "report.md"):
            (bundle / n).write_text("x\n")
        (rejected,) = _merge([bundle], tmp_path / "m")["report"]
        assert rejected["status"] == "rejected" and "call_rows" in rejected["reason"]

    def test_a_duplicate_key_is_refused_everywhere_even_without_a_secret(
        self, audited: Path, tmp_path: Path
    ) -> None:
        """verify_share scans both values of a repeated key; the contract refuses the repeat itself,
        because json.loads (ingest) keeps the last value while a reader of the gist sees both."""
        f = audited / "share" / "calls.jsonl"
        first, *rest = f.read_text().splitlines()
        f.write_text("".join(x + "\n" for x in [first.replace("{", '{"tool": "x", ', 1), *rest]))
        assert _verify_share(audited / "share", ("calls.jsonl",)) == []  # nothing to leak
        for args in (("repack", str(audited)), ("gist", str(audited))):
            refused = _invoke(*args)
            assert refused.exit_code == 1 and "repeats a key" in refused.output, refused.output
        (rejected,) = _merge([audited / "share"], tmp_path / "m")["report"]
        assert "repeats a key" in rejected["reason"]


# ---- C2: ingest reads an untrusted archive lazily and namespaces what it merges -----------------


def _count_tar_headers(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """How many member headers tarfile parsed (TarFile.next calls), for a bounded-work check."""
    calls = [0]
    real = tarfile.TarFile.next

    def counting(self: tarfile.TarFile) -> tarfile.TarInfo | None:
        calls[0] += 1
        return real(self)

    monkeypatch.setattr(tarfile.TarFile, "next", counting)
    return calls


class TestIngestLimits:
    """Contract C2: rejecting a hostile archive costs at most constant work, and one bundle's rows
    can never shadow another's.

    State space: {many members, duplicate names, unknown names, nested name, directory member,
    oversized member, oversized archive, decompression past the cap, truncated gzip} x rejected
    within bounded headers/bytes; {same bundle as tar/dir/gist, two bundles sharing refs and
    sessions} x merged as decided (deduped by content digest / both kept and reported).
    """

    @pytest.mark.parametrize("name", ["SCHEMA.md", "payload.sh"])
    def test_a_flood_of_members_is_rejected_after_a_bounded_number_of_headers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        """F6: 1,000,000 empty members named SCHEMA.md took 662 MB and 10 s, because getmembers()
        parsed every header before any check (scaled down here; the bound is structural)."""
        bad = tmp_path / "flood.tar.gz"
        with tarfile.open(bad, "w:gz") as tf:
            for _ in range(2000):
                tf.addfile(tarfile.TarInfo(name), io.BytesIO(b""))
        headers = _count_tar_headers(monkeypatch)
        (rejected,) = _merge([bad], tmp_path / "m")["report"]
        assert rejected["status"] == "rejected"
        assert ("appears twice" if name == "SCHEMA.md" else "unexpected member") in rejected[
            "reason"
        ]
        assert headers[0] <= len(BUNDLE) + 1, headers[0]

    def test_an_oversized_member_is_rejected_from_its_header_not_its_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F27: a 52 KB gzip holding one 51 MiB calls.jsonl was inflated to ~53 MB before the
        50 MiB limit fired."""
        from pipecat_context_hub.audit import ingest

        bad = tmp_path / "big.tar.gz"
        size = 51 * 1024 * 1024
        with tarfile.open(bad, "w:gz", compresslevel=1) as tf:
            info = tarfile.TarInfo("calls.jsonl")
            info.size = size
            tf.addfile(info, io.BytesIO(bytes(size)))
        readers: list[Any] = []

        class Recording(ingest._CappedReader):
            def __init__(self, *a: Any) -> None:
                super().__init__(*a)
                readers.append(self)

        monkeypatch.setattr(ingest, "_CappedReader", Recording)
        (rejected,) = _merge([bad], tmp_path / "m")["report"]
        assert rejected["status"] == "rejected" and "too large" in rejected["reason"]
        assert readers[0].count < 1024 * 1024, readers[0].count  # the header, not 51 MiB

    def test_decompression_past_the_archive_cap_stops_the_read(
        self, audited: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pipecat_context_hub.audit import bundle

        # round 4 (F8): the limit is the bundle contract's own, read at call time
        monkeypatch.setattr(bundle, "MAX_BUNDLE_BYTES", 2048)
        (rejected,) = _merge([audited / "share.tar.gz"], tmp_path / "m")["report"]
        assert rejected["status"] == "rejected" and "decompresses past 2048" in rejected["reason"]

    def test_an_oversized_archive_is_rejected_before_it_is_read(
        self, audited: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pipecat_context_hub.audit import ingest

        def boom(_p: Path) -> dict[str, bytes]:
            raise AssertionError("must not read an archive over the size limit")

        monkeypatch.setattr(ingest, "MAX_ARCHIVE_BYTES", 100)
        monkeypatch.setattr(ingest, "read_tar", boom)
        (rejected,) = _merge([audited / "share.tar.gz"], tmp_path / "m")["report"]
        assert rejected["status"] == "rejected" and "limit 100" in rejected["reason"]

    @pytest.mark.parametrize(
        ("members", "reason"),
        [
            ([("share/calls.jsonl", b"", "f")], "unexpected member"),
            ([("calls.jsonl", b"", "dir")], "not a regular file"),
            ([("calls.jsonl", b"", "f"), ("calls.jsonl", b"", "f")], "appears twice"),
        ],
        ids=["nested-name", "directory-member", "duplicate-name"],
    )
    def test_a_member_that_breaks_a_rule_is_rejected(
        self, tmp_path: Path, members: list[tuple[str, bytes, str]], reason: str
    ) -> None:
        bad = tmp_path / "bad.tar.gz"
        with tarfile.open(bad, "w:gz") as tf:
            for name, data, kind in members:
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.DIRTYPE if kind == "dir" else tarfile.REGTYPE
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        (rejected,) = _merge([bad], tmp_path / "m")["report"]
        assert rejected["status"] == "rejected" and reason in rejected["reason"]

    def test_the_same_bundle_as_tarball_directory_and_gist_merges_once(
        self, audited: Path, tmp_path: Path
    ) -> None:
        gist = tmp_path / "gist"
        gist.mkdir()
        for n in BUNDLE:
            (gist / n).write_bytes((audited / "share" / n).read_bytes())
        merged = _merge([audited / "share.tar.gz", audited / "share", gist], tmp_path / "m")
        assert merged["calls"] == 3 and merged["ref_collisions"] == 0
        report = merged["report"]
        assert [r["new_calls"] for r in report] == [3, 0, 0]
        assert len({r["bundle"] for r in report}) == 1
        assert [r.get("duplicate_of") for r in report] == [None, "share.tar.gz", "share.tar.gz"]

    def test_a_tampered_copy_listed_first_cannot_shadow_the_genuine_rows(
        self, audited: Path, tmp_path: Path
    ) -> None:
        """F21: merge was first-wins on attacker-chosen `ref` and `participant/session`, so a
        tampered copy of another participant's bundle listed first silently dropped their rows."""
        genuine_rows = _rows(audited / "share" / "calls.jsonl")
        fake = tmp_path / "fake"
        (fake / "share").mkdir(parents=True)
        for n in BUNDLE:
            (fake / "share" / n).write_bytes((audited / "share" / n).read_bytes())
        calls = fake / "share" / "calls.jsonl"
        calls.write_text(calls.read_text().replace('"search_docs"', '"get_hub_status"'))
        assert _invoke("repack", str(fake)).exit_code == 0
        result = _invoke(
            "ingest", str(fake / "share.tar.gz"), str(audited / "share.tar.gz"),
            "--out", str(tmp_path / "m"),
        )  # fmt: skip
        assert result.exit_code == 0 and "WARNING share.tar.gz: 3 call ref(s)" in result.output
        merged = _rows(tmp_path / "m" / "calls.jsonl")
        report = json.loads((tmp_path / "m" / "ingest.json").read_text())
        genuine = report[1]["bundle"]
        assert len(merged) == 6 and report[1]["new_calls"] == 3
        assert (report[1]["ref_collisions"], report[1]["session_collisions"]) == (3, 1)
        kept = [
            {k: v for k, v in r.items() if k != "source_bundle"}
            for r in merged
            if r["source_bundle"] == genuine
        ]
        assert sorted(kept, key=lambda r: r["ref"]) == sorted(genuine_rows, key=lambda r: r["ref"])
        assert len(_rows(tmp_path / "m" / "sessions.jsonl")) == 2  # both sessions kept


# ---- C3: one flag vocabulary, one triage order, hygiene ---------------------------------------


class TestFlagVocabulary:
    """Contract C3: every flag the extractor can emit is declared once (audit/flags.py), and the
    glossary, the severity weight and the outcome class are views of that one row; an undeclared
    flag fails loudly instead of being classed as clean."""

    def test_the_flag_table_is_exactly_what_the_extractor_emits(self, audited: Path) -> None:
        """F1: three tables owned the vocabulary with no parity guard."""
        import re

        from pipecat_context_hub.audit import analysis, flags

        src = Path(audit_extract.__file__).read_text()
        emitted = set(re.findall(r'call_flags\.append\(\s*"(\w+)"', src))
        assert emitted == set(flags.FLAGS), emitted ^ set(flags.FLAGS)
        assert not hasattr(flags, "FLAG_GLOSSARY") and not hasattr(flags, "SEVERITY")  # F10
        assert {f for _, m in analysis._CLASSES for f in m} == set(flags.FLAGS)
        schema = (audited / "share" / "SCHEMA.md").read_text()
        for name, spec in flags.FLAGS.items():
            assert spec.outcome in flags.OUTCOME_ORDER, name
            assert analysis.classify_call({"flags": [name]}) == (spec.outcome, spec.target), name
            assert flags.severity([name]) == spec.weight, name
            assert f"- `{name}` ({spec.weight}): {spec.glossary}" in schema, name

    def test_an_undeclared_flag_fails_loudly(self) -> None:
        """F1: an unmapped flag was classed no_negative_signal ("no flag fired") and weighed 1."""
        from pipecat_context_hub.audit import analysis, flags

        with pytest.raises(flags.UnknownFlagError):
            analysis.classify_call({"flags": ["zero_hits", "made_up_flag"]})
        with pytest.raises(flags.UnknownFlagError):
            flags.severity(["made_up_flag"])

    def test_an_unparsed_shell_result_raises_no_flag(self, tmp_path: Path) -> None:
        """cli_unparsed / js_unparsed are parse values, not flags (the agent's program printed it)."""
        cmd = {"command": "pipecat-context-hub search-docs TTS | head -3"}
        _jl(
            tmp_path / "in" / "zed" / "s.jsonl",
            [
                _use(1, "2026-11-18T10:00:00Z", "Bash", cmd),
                _result(1, "2026-11-18T10:00:01Z", "TTS doc"),
            ],
        )
        assert _extract(tmp_path / "in", tmp_path / "out").exit_code == 0
        (c,) = _rows(tmp_path / "out" / "share" / "calls.jsonl")
        assert (c["result"]["parse"], c["flags"], c["outcome"]) == (
            "cli_unparsed",
            [],
            "no_negative_signal",
        )

    def test_one_triage_order_and_one_latency_formula(self) -> None:
        """F10: "Read first" ranked failed-then-flag-count while "Look first" ranked by severity,
        and median/p90 were computed twice."""
        from pipecat_context_hub.audit import analysis, report

        def call(i: int, flags: list[str], latency: float) -> dict[str, Any]:
            c = {"participant": "p", "session_id": "s", "call_idx": i, "tool": "search_docs",
                 "via": "mcp", "input": {}, "latency_s": latency, "flags": flags,
                 "result": {}, "ref": f"r{i}"}  # fmt: skip
            return c

        calls = [call(0, ["non_json_result"], 1.0), call(1, ["downstream_error", "slow"], 9.0)]
        analysis.annotate_calls(calls)
        a = analysis.analyze([], calls, lambda c: c["ref"])
        md = report.render_report({"analysis": a}, calls)
        look = [
            line.split("|")[1].strip()
            for line in md.splitlines()
            if line.startswith(("| r0 ", "| r1 "))
        ]
        assert a["correctness"]["read_these_first"] == look == ["r1", "r0"]  # severity 4 before 1
        stats = a["efficiency"]["per_tool"]["search_docs"]
        (row,) = report.tool_rows(calls)
        assert row.split("|")[4:6] == [f" {stats['median_s']} ", f" {stats['p90_s']} "]
        assert vars(report)["latency_stats"] is analysis.latency_stats


class TestHygiene:
    def test_extract_does_not_re_export_the_egress(self) -> None:
        """F7: a shim re-exported 14 egress names "because tests call extract.redact"; they did not."""
        for name in ("HOME_RE", "SECRET_RES", "SHARE_PATH_RE", "WIN_HOME_RE", "clean_text",
                     "one_line", "scrub_paths"):  # fmt: skip
            assert not hasattr(audit_extract, name), name
        assert not hasattr(egress, "scrub_paths")  # dead second path-scrub entry point

    def test_the_shell_tokenizer_is_its_own_leaf_module(self) -> None:
        """F8 (limited): the shell tokenizer moved out of extract.py unchanged."""
        from pipecat_context_hub.audit import shell

        assert "from pipecat_context_hub" not in Path(shell.__file__).read_text()
        assert vars(audit_extract)["shell_hub_calls"] is shell.shell_hub_calls
        assert shell.shell_hub_calls('x; uv run pipecat-context-hub search-docs "TTS & STT"') == [
            ["pipecat-context-hub", "search-docs", "TTS & STT"]
        ]

    def test_gist_passes_absolute_paths_after_a_separator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F22: `audit gist ./-pd --yes` built the token `-pd/share/manifest.json`, which gh reads as
        -p (--public) plus -d: a PUBLIC gist."""
        _claude_session(tmp_path / "in" / "alice" / "s1.jsonl")
        assert _extract(tmp_path / "in", tmp_path / "-pd").exit_code == 0
        monkeypatch.chdir(tmp_path)
        seen = _fake_gh(monkeypatch)
        assert _invoke("gist", "./-pd", "--yes").exit_code == 0
        (argv,) = seen
        sep = argv.index("--")
        assert argv[:sep] == [
            "/usr/bin/gh",
            "gist",
            "create",
            "--desc",
            "Context Hub session bundle",
        ]
        paths = argv[sep + 1 :]
        assert paths and all(Path(p).is_absolute() for p in paths)
        assert {Path(p).name for p in paths} == set(BUNDLE)


# ---- Contract G1: report.md is a function of rows the tripwire already accepted -----------------
#
# Closure under sanitize: whatever `sanitize` emits passes `verify_share`. Closure under rendering:
# `render_report` of rows (and manifest values) that pass `verify_share` passes it too. Both are
# checked over generated values below, built from printable text, the characters the rules key on
# and the secret-shape corpus.

_FUZZ_ALPHABET = (
    "abcXYZ0123456789 .,_-/"
    + "|\\\n:@=\"'$()"
    + "\t\r·→…<>`;{}[]#~%"
    + "：=:_-\\\"'"
    + "&?&?"
    + "*_\\`[]\n|#?/@\u00a0\u200b\u2018\u201c"  # round 4 fixer (K1): markdown, unicode
    + "\\ \n|·→#?/@<>\"'=:↦⇒\t"  # round 5 fixer (K1): breaks, cells, arrows, tags, quotes
)  # round 3 (K1): the label model's separators, quotes and escapes weigh more; round 4: `&` `?`
_FUZZ_FRAGMENTS = [
    *(t for t, _ in SECRET_SHAPES), *BENIGN, "password", "TOKEN=", "MONKEY=", "api_key",
    "--api-key ", "--password=", "--password", "Authorization: Basic ", "Bearer ", "C:\\",
    "/tmp/", "/Users/", "Users", "sk-", "0123456789abcdef", "<redacted>", "<user>", "<path>",
    "\\|", " · ", " → ", " | ", "MONKEY=a|b|c|d", "s:\n", "eyJ", "AKIA", "@", "://", "u:p@h",
    '"', "'", "abcdefgh", "-----BEGIN RSA PRIVATE KEY-----",
    # round 3 (K1): every stem, the separators and the escaped-quote forms
    *egress.CREDENTIAL_STEMS, *egress.BENIGN_LABEL_WORDS, "API key", "STRIPE_KEY", "password1",
    "：", "=>", "->", ": ", " = ", '\\"', '\\\\"', '{\\"password\\": \\"', "redis://:",
    "OPENAI_API_KEY_2", "my secret phrase",
    # round 4: context-dependent terminators, plural labels and placeholder forms
    *egress.NON_CREDENTIAL_SEGMENTS, *egress.BENIGN_WHOLE_LABELS, "API_TOKENS=", "SSH_KEYS",
    "?x=1&", "&password=", "abc&def", "ab,cd", "(password: ", "[token: ", '{"secret": ', "${",
    "${AKIA", "$TOKEN", "<token>", "YOUR_API_KEY", "max_tokens", "key: ",
    # round 4 fixer (K1): the keyword forms, the whole credential words and markdown wrappers
    *egress.CREDENTIAL_WORDS, "mysql ", "-p", "login ", "password ", "-u ", "machine ", "--user ",
    "Cookie: ", "**", "`", "|\n  ", ': """', "os.environ[", "].", "#frag", "\u00a0",
    # round 5 fixer (K1): escaped breaks, the new forms' keywords, scheme words and blank runs
    "\\n", "\\n  ", "\n  ", ": |\n  ", ": \\n", " | ", " ↦ ", "IDENTIFIED BY ", "<password>",
    "</password>", '{"name": "API_KEY", ', '"value": ', "name: ", "value: ", "- name: TOKEN\n  ",
    "sshpass -p ", "sshpass ", "Bearer  ", "Basic\t\t", "Authorization: ", " is ", "export ",
    "set ", "setenv ", "PASSWORD '", "→", "⇒", "your-dog-rex", "YOUR_API_KEY", "  ",
]  # fmt: skip


def _fuzz_text(rng: Any) -> str:
    parts = []
    for _ in range(rng.randint(0, 6)):
        if rng.random() < 0.45:
            parts.append(rng.choice(_FUZZ_FRAGMENTS))
        else:
            parts.append("".join(rng.choice(_FUZZ_ALPHABET) for _ in range(rng.randint(0, 12))))
    return "".join(parts)


def _fuzz_value(rng: Any, depth: int = 0) -> Any:
    kind = rng.randrange(8 if depth < 2 else 5)
    if kind in (0, 1):
        return _fuzz_text(rng)
    if kind == 2:
        return rng.choice([0, 7, 2**63, 10 ** rng.randint(0, 45), -(10 ** rng.randint(20, 40))])
    if kind == 3:
        return rng.choice([None, True, False, 0.25, 1e40, float("inf")])
    if kind == 4:
        return rng.random() * 10 ** rng.randint(0, 40)
    if kind in (5, 6):
        return [_fuzz_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {_fuzz_text(rng): _fuzz_value(rng, depth + 1) for _ in range(rng.randint(0, 3))}


def _fuzz_shaped(rng: Any, shape: Any) -> Any:
    """A value with ``shape`` (bundle.ANALYSIS_SHAPE's notation) whose leaves are fuzzed."""
    if shape is None:
        return _fuzz_value(rng)
    if shape is str:
        return _fuzz_text(rng)
    if shape is dict:
        return {_fuzz_text(rng): _fuzz_value(rng) for _ in range(rng.randint(0, 3))}
    if isinstance(shape, list):
        return [_fuzz_shaped(rng, shape[0]) for _ in range(rng.randint(0, 3))]
    return {k: _fuzz_shaped(rng, sub) for k, sub in shape.items()}


def _plain_shaped(shape: Any) -> Any:
    """The smallest value with ``shape``: every leaf 0, every container empty."""
    if shape is None:
        return 0
    if shape is str:
        return ""
    if shape is dict or isinstance(shape, list):
        return [] if isinstance(shape, list) else {}
    return {k: _plain_shaped(sub) for k, sub in shape.items()}


def _fuzz_analysis(rng: Any) -> Any:
    from pipecat_context_hub.audit import bundle

    return _fuzz_shaped(rng, bundle.ANALYSIS_SHAPE)


def _json_clean(v: Any) -> bool:
    return egress._file_leaks("calls.jsonl", json.dumps(v)) == []


def _report_clean(text: str) -> bool:
    return egress._file_leaks("report.md", text) == []


class TestRenderClosure:
    @pytest.mark.parametrize("seed", range(8))
    def test_sanitize_and_render_are_closed_over_generated_values(self, seed: int) -> None:
        """Property: sanitize-closure, render-closure (the answers section from a hand-editable
        manifest `analysis` included), value_text-closure and the cell identity."""
        import random

        from pipecat_context_hub.audit import bundle, flags

        rng = random.Random(seed)
        names = sorted(flags.FLAGS)
        for _ in range(400):
            text = _fuzz_text(rng)
            assert egress.md_decoded(egress.md_cell(text)) == egress.one_line(text)
            query, n_hits = _fuzz_value(rng), _fuzz_value(rng)
            row: dict[str, Any] = {
                "ref": _fuzz_text(rng),
                "tool": _fuzz_text(rng),
                "via": "mcp",
                # round 3 (F13): a hand-edited row's flags are any strings, joined in one cell
                "flags": rng.choice(
                    [rng.sample(names, rng.randint(1, 3)), [_fuzz_text(rng) for _ in range(3)]]
                ),
                "severity": 1,
                "result": {"n_hits": n_hits, "confidence": _fuzz_value(rng)},
                "followups": [],
                "input": {"query": query, "path": _fuzz_value(rng)},
                "hub_invocation": _fuzz_text(rng),
                "latency_s": rng.choice([None, rng.random() * 20]),
            }
            clean = egress.sanitize(row)
            assert _json_clean(clean), clean  # closure under sanitize
            manifest = egress.sanitize(
                {"harnesses": _fuzz_value(rng), "analysis": _fuzz_analysis(rng)}
            )
            assert bundle.shape_error(manifest["analysis"], bundle.ANALYSIS_SHAPE, "a") is None
            candidates = [clean] + ([row] if _json_clean(row) else [])
            for r in candidates:  # closure under rendering
                report = render_report(manifest, [r])
                assert _report_clean(report), (r, egress._file_leaks("report.md", report))
            for v in (query, n_hits, manifest["harnesses"]):
                if _json_clean(v):
                    assert egress.leaks_in(egress.value_text(v)) == [], v

    def test_a_pipe_escape_never_creates_a_match_the_row_lacks(self, tmp_path: Path) -> None:
        """F2: `TOKEN=a|b|c|d` (7 chars after `=`) rendered as `TOKEN=a\\|b\\|c\\|d` matched the
        8+ non-space rule, so verify_share refused report.md and extract deleted the bundle."""
        row = {"ref": "r", "tool": "search_docs", "flags": ["zero_hits"], "result": {},
               "input": {"query": "search MONKEY=a|b|c|d"}}  # fmt: skip
        assert _json_clean(row)
        report = render_report({}, [row])
        assert "search MONKEY=a\\|b\\|c\\|d" in report  # meaning kept: escaped, not substituted
        assert _report_clean(report)
        # the finding's own query, end to end: extract keeps a bundle the tripwire accepts
        _hub_session(tmp_path / "in" / "zed" / "s.jsonl", "search TOKEN=a|b|c|d", confident=False)
        out = tmp_path / "out"
        result = _extract(tmp_path / "in", out)
        assert result.exit_code == 0, result.output
        assert _verify_share(out / "share", tuple(BUNDLE)) == []

    def test_a_non_string_query_renders_as_text_not_repr(self, tmp_path: Path) -> None:
        """F22: `str(["voice options:\\nTTS"])` wrote `\\n` after `s:`, read as a Windows path."""
        empty = json.dumps({"hits": [], "evidence": {"confidence": 0.1, "low_confidence": True}})
        inp = {"query": ["voice options:\nTTS"]}
        _jl(
            tmp_path / "in" / "zed" / "s.jsonl",
            [
                _use(1, "2026-11-18T10:00:00Z", "mcp__pipecat-context-hub__search_docs", inp),
                _result(1, "2026-11-18T10:00:01Z", empty),
            ],
        )
        out = tmp_path / "out"
        result = _extract(tmp_path / "in", out)
        assert result.exit_code == 0, result.output
        report = (out / "share" / "report.md").read_text()
        assert "| voice options: · TTS |" in report and "['" not in report
        assert _verify_share(out / "share", tuple(BUNDLE)) == []
        # headers render dicts the same way, never as a Python dict repr
        assert "Calls by tool: search_docs ↦ 1" in report

    def test_the_number_rule_is_the_same_on_both_sides(self, tmp_path: Path) -> None:
        """Q2: numbers were scanned by neither side, so a 32+ digit integer passed both."""
        big = 12345678901234567890123456789012345678
        out = egress.sanitize({"n": big, "small": 7, "max": 2**63, "f": 1e40, "b": True})
        assert out == {"n": egress.REDACTED, "small": 7, "max": 2**63, "f": 1e40, "b": True}
        share = tmp_path / "share"
        _write_sink(share, "calls.jsonl", json.dumps(out))
        assert _verify_share(share, ("calls.jsonl",)) == []
        _write_sink(share, "calls.jsonl", json.dumps({"input": {"n": big}}))  # a hand edit
        hex_rule = next(n for n, rx in egress._LEAK_RES.items() if rx.search(str(big)))
        assert _verify_share(share, ("calls.jsonl",)) == [f"calls.jsonl: {hex_rule}"]
        assert egress.value_text(big) == str(big) and egress.value_text(1e40) == "1e+40"

    # (representation, how the shape reaches a share row). `argv` goes through a real shell line.
    LABELLED = [
        ('{"password": "hunter2hunter2"}', "hunter2hunter2"),
        ("--password hunter2hunter2", "hunter2hunter2"),
        ("--api-key=cobalt-lantern-private", "cobalt-lantern-private"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
        ("postgres://user:p@ss@host/db", "ss@host"),
    ]
    REPS: dict[str, Any] = {
        "value": lambda t: {"query": t},
        "key": lambda t: {t: "x"},
        "nested": lambda t: {"a": [{"b": t}]},
        "free-text-in-query": lambda t: {"query": f"how do I set {t} for the TTS service"},
        "header": lambda t: {"query": f"curl -H '{t}' https://api.example.com"},
    }

    @pytest.mark.parametrize("rep", list(REPS))
    @pytest.mark.parametrize("shape", LABELLED, ids=[p for _, p in LABELLED])
    def test_a_labelled_credential_is_removed_and_refused_in_every_representation(
        self, tmp_path: Path, shape: tuple[str, str], rep: str
    ) -> None:
        """F21: these carry a key name, yet sanitize left them unchanged and verify_share passed."""
        text, payload = shape
        row = self.REPS[rep](text)
        clean = egress.sanitize(row)
        assert payload not in json.dumps(clean, ensure_ascii=False)  # ground truth, not the rules
        assert _json_clean(clean) and _report_clean(
            render_report(
                {},
                [{"ref": "r", "tool": "t", "flags": ["zero_hits"], "result": {}, "input": clean}],
            )  # fmt: skip
        )
        share = tmp_path / "share"
        _write_sink(share, "calls.jsonl", json.dumps(row))  # a hand edit: refused independently
        assert _verify_share(share, ("calls.jsonl",))
        _write_sink(share, "report.md", f"| q | {text} |")
        assert _verify_share(share, ("report.md",))

    @pytest.mark.parametrize(
        ("flag", "payload"),
        [("--password hunter2hunter2", "hunter2hunter2"),
         ("--api-key=cobalt-lantern-private", "cobalt-lantern-private"),
         ("--token 'two words'", "two words")],
    )  # fmt: skip
    def test_a_credential_flag_in_a_shared_argv_is_redacted_by_structure(
        self, tmp_path: Path, flag: str, payload: str
    ) -> None:
        """F21 (argv flag representation): the invocation is rebuilt from argv, so the value after a
        credential flag is replaced before re-quoting and the result stays one inert command."""
        line = f"pipecat-context-hub search-docs TTS {flag}"
        inv = audit_extract.hub_invocation({"command": line}, "cli")
        assert inv and payload not in inv and egress.sanitize(inv) == inv
        from pipecat_context_hub.audit.shell import shell_hub_calls

        assert shell_hub_calls(inv) == [shlex.split(inv)]  # pasted, it is one command
        cli_json = json.dumps({"hits": [{"doc_id": "d"}], "evidence": {"confidence": 0.9}})
        _jl(
            tmp_path / "in" / "zed" / "s.jsonl",
            [
                _use(1, "2026-11-18T10:00:00Z", "Bash", {"command": line}),
                _result(1, "2026-11-18T10:00:01Z", cli_json),
            ],
        )
        assert _extract(tmp_path / "in", tmp_path / "out").exit_code == 0
        shared = "".join((tmp_path / "out" / "share" / n).read_text() for n in BUNDLE)
        assert payload not in shared


# ---- Contract G2: a shared invocation is shell-inert data; the tokenizer fails closed -----------

F28_LINE = 'printf "%s\\n" "\\$(pipecat-context-hub search-docs Acquisition-Acme-17m)"'
F23_ANSI = "printf $'note\\'; pipecat-context-hub search-docs MY PRIVATE NOTES' \\'"
F23_BRACE = "echo ${GREETING:-hi; pipecat-context-hub search-docs MY PRIVATE NOTES}"
F19_LINE = "diff <(pipecat-context-hub status) x"
F20_LINE = "bash <<EOF\npipecat-context-hub status\nEOF"

# (shell line, the tool counted or None, the invocation shared). A line the tokenizer does not
# fully model is still counted by the regex fallback, but shares "".
G2_CASES = [
    ("pipecat-context-hub search-docs TTS", "search_docs", "pipecat-context-hub search-docs TTS"),
    ("uv run pipecat-context-hub search-docs TTS", "search_docs", "pipecat-context-hub search-docs TTS"),
    ("/opt/x/bin/pipecat-context-hub search-docs TTS", "search_docs", "pipecat-context-hub search-docs TTS"),
    ("pipecat context-hub status", "get_hub_status", "pipecat context-hub status"),
    ('pipecat-context-hub search-docs "TTS & STT; x | y"', "search_docs",
     "pipecat-context-hub search-docs 'TTS & STT; x | y'"),
    ("pipecat-context-hub search-docs TTS # private note", "search_docs", "pipecat-context-hub search-docs TTS"),
    ('echo "pipecat-context-hub search-docs x"', None, ""),
    (F28_LINE, None, ""),  # escaped `\$(` in double quotes is literal text (modelled)
    (F23_ANSI, "search_docs", ""),  # ANSI-C quoting: not modelled, fails closed
    (F23_BRACE, "search_docs", ""),  # `${...}` with operators: fails closed
    ('cd "${HOME}/x" && pipecat-context-hub search-docs a', "search_docs", "pipecat-context-hub search-docs a"),
    (F19_LINE, "get_hub_status", "pipecat-context-hub status"),  # process substitution (modelled)
    ("echo x | tee >(pipecat-context-hub search-docs a)", "search_docs", "pipecat-context-hub search-docs a"),
    ("echo $((1+2)); pipecat-context-hub status", "get_hub_status", ""),  # arithmetic: fails closed
    ("x=`pipecat-context-hub status`", "get_hub_status", "pipecat-context-hub status"),
    ("x=`echo \\`pipecat-context-hub status\\``", "get_hub_status", ""),  # backslash in backticks
    ("cat <<'EOF'\npipecat-context-hub search-docs x\nEOF", None, ""),  # heredoc to cat: data
    ("cat <<EOF\n$(pipecat-context-hub status)\nEOF", "get_hub_status", ""),  # expanded: fails closed
    (F20_LINE, "get_hub_status", "pipecat-context-hub status"),  # heredoc to a shell: a script
    ("bash <<'EOF'\npipecat-context-hub status\nEOF", "get_hub_status", "pipecat-context-hub status"),
    ("python3 <<'EOF'\npipecat-context-hub status\nEOF", None, ""),  # heredoc to python: data
    ('pipecat-context-hub search-docs x <<< "data"', "search_docs", "pipecat-context-hub search-docs x"),
    ('bash <<< "pipecat-context-hub status"', "get_hub_status", ""),  # stdin script: fails closed
    ("echo 'pipecat-context-hub status' | sh", "get_hub_status", ""),  # pipe into a shell
    ("mkdir -p {a,b} && pipecat-context-hub status", "get_hub_status", ""),  # brace expansion
    ('pipecat-context-hub search-docs "$(cat notes.txt)"', "search_docs", ""),  # substituted arg
    ("pipecat-context-hub search-docs 'unterminated", "search_docs", ""),
    ("echo (pipecat-context-hub status)", "get_hub_status", ""),  # `(` mid-command: syntax error
    # words clean one by one, but joined they form `TOKEN= <value>`: cleaning the joined text would
    # rewrite it (an unquoted `<redacted>` is a redirection), so nothing is shared
    ("pipecat-context-hub search-docs TOKEN= abcdefghij", "search_docs", ""),
]  # fmt: skip

# Lines whose ground truth is taken from bash itself, with stand-in executables named like the hub
# that only log their argv. Only harmless commands (echo, printf, cat, diff, true, eval, sh).
BASH_TRUTH = [
    "pipecat-context-hub search-docs TTS",
    "pipecat context-hub status",
    'pipecat-context-hub search-docs "TTS & STT" --limit 3 | cat',
    "pipecat-context-hub search-docs TTS # Acquisition target: Acme",
    "printf '%s' 'pipecat-context-hub search-docs TTS'",
    'echo "pipecat-context-hub search-docs x"',
    F28_LINE,
    F23_ANSI,
    F23_BRACE,
    "cat <(pipecat-context-hub status) > /dev/null",
    "diff <(pipecat-context-hub status) /dev/null",
    "echo $((1+2)); pipecat-context-hub status",
    "x=`pipecat-context-hub status`",
    "cat <<'EOF'\npipecat-context-hub search-docs x\nEOF",
    "cat <<EOF\npipecat-context-hub search-docs x\nEOF",
    "cat <<EOF\n$(pipecat-context-hub status)\nEOF",
    "bash <<'EOF'\npipecat-context-hub status\nEOF",
    F20_LINE,
    "bash -c 'echo hi' <<EOF\npipecat-context-hub status\nEOF",
    "echo 'pipecat-context-hub status' | bash",
    "bash <<< 'pipecat-context-hub status'",
    "cat <<EOF | bash\npipecat-context-hub status\nEOF",
    'pipecat-context-hub search-docs x <<< "data"',
    "bash -c 'pipecat-context-hub status'",
    "eval 'pipecat-context-hub status'",
    'x=$(pipecat-context-hub search-api Foo) && echo "$x"',
    'echo "$(echo "pipecat-context-hub status")"',
    "echo {pipecat-context-hub,status}",
    "( pipecat-context-hub status )",
    "echo \\$(pipecat-context-hub status)",
    "pipecat-context-hub search-docs 'unterminated",
    'true && pipecat-context-hub search-docs "a b" ; echo done',
    'pipecat-context-hub search-docs "a $(pipecat-context-hub status)"',
]

_STUB = """#!/bin/sh
{ printf '%s' "$(basename "$0")"; for a in "$@"; do printf '\\037%s' "$a"; done; printf '\\036'; } >> "$HUB_LOG"
"""


def _bash_runs(line: str, tmp: Path) -> list[list[str]]:
    """The hub argv lists bash actually executes for ``line`` (stand-in binaries log them)."""
    import os
    import shutil

    stubs = tmp / "stubs"
    stubs.mkdir(exist_ok=True)
    for name in ("pipecat-context-hub", "pipecat"):
        (stubs / name).write_text(_STUB)
        (stubs / name).chmod(0o755)
    log = tmp / "hub.log"
    log.write_text("")
    bash = shutil.which("bash")
    assert bash is not None
    env = {"PATH": f"{stubs}{os.pathsep}/usr/bin{os.pathsep}/bin", "HUB_LOG": str(log),
           "HOME": str(tmp)}  # fmt: skip
    subprocess.run(  # noqa: S603 - a fixed list of harmless lines, stand-in binaries only
        [bash, "--noprofile", "--norc", "-c", line],
        cwd=tmp, env=env, capture_output=True, timeout=10, check=False,
    )  # fmt: skip
    runs = []
    for rec in log.read_text().split("\x1e"):
        if rec:
            name, *args = rec.split("\x1f")
            runs.append([name, *args])
    return runs


class TestShellFailClosed:
    @pytest.mark.parametrize(("line", "tool", "invocation"), G2_CASES)
    def test_every_form_is_counted_and_shared_as_specified(
        self, line: str, tool: str | None, invocation: str
    ) -> None:
        found, via, _ = audit_extract.detect_hub("Bash", {"command": line})
        assert (found, via) == ((tool, "cli") if tool else (None, None))
        shared = audit_extract.hub_invocation({"command": line}, "cli")
        assert shared == invocation
        if shared:  # inert data: re-sanitising cannot change it, pasted it is ONE command
            from pipecat_context_hub.audit.shell import shell_hub_calls

            assert egress.sanitize(shared) == shared
            assert shell_hub_calls(shared) == [shlex.split(shared)]

    @pytest.mark.skipif(
        __import__("shutil").which("bash") is None or __import__("os").name == "nt",
        reason="needs a POSIX bash",
    )
    @pytest.mark.parametrize("line", BASH_TRUTH)
    def test_the_tokenizer_agrees_with_bash_on_what_runs(self, tmp_path: Path, line: str) -> None:
        """Ground truth from bash: a line the tokenizer parses must run exactly the hub commands it
        says (same argv when the words are literal); a line it cannot model must share nothing."""
        from pipecat_context_hub.audit.shell import ShellSyntaxError, literal_argv, shell_hub_calls

        ran = _bash_runs(line, tmp_path)
        if ran:  # an executed hub call is always counted (parsed, or by the regex fallback)
            assert audit_extract.detect_hub("Bash", {"command": line})[0] is not None
        try:
            calls = shell_hub_calls(line)
        except ShellSyntaxError:
            assert audit_extract.hub_invocation({"command": line}, "cli") == ""
            return
        heads = sorted(c[: (3 if c[0] == "pipecat" else 2)] for c in calls)
        assert heads == sorted(r[: (3 if r[0] == "pipecat" else 2)] for r in ran), ran
        literal = [w for c in calls if (w := literal_argv(c)) is not None]
        assert all(c in ran for c in literal), (literal, ran)

    def test_an_escaped_substitution_is_not_a_call(self, tmp_path: Path) -> None:
        """F28: the scanner ignored the backslash in `"\\$(...)"` and shared the text as a call."""
        assert audit_extract.detect_hub("Bash", {"command": F28_LINE}) == (None, None, 0)
        rows = [
            _use(1, "2026-11-18T10:00:00Z", "Bash", {"command": F28_LINE}),
            _result(1, "2026-11-18T10:00:01Z", "$(pipecat-context-hub search-docs ...)"),
        ]
        _jl(tmp_path / "in" / "zed" / "s.jsonl", rows)
        assert _extract(tmp_path / "in", tmp_path / "out").exit_code == 0
        shared = "".join((tmp_path / "out" / "share" / n).read_text() for n in BUNDLE)
        assert "Acquisition" not in shared and "Acme" not in shared

    @pytest.mark.parametrize("line", [F23_ANSI, F23_BRACE], ids=["ansi-c", "brace"])
    def test_an_unmodelled_construct_shares_no_text(self, tmp_path: Path, line: str) -> None:
        """F23: `$'...'` and `${...}` were not understood, so string text became a hub_invocation."""
        assert audit_extract.hub_invocation({"command": line}, "cli") == ""
        rows = [
            _use(1, "2026-11-18T10:00:00Z", "Bash", {"command": line}),
            _result(1, "2026-11-18T10:00:01Z", "note"),
        ]
        _jl(tmp_path / "in" / "zed" / "s.jsonl", rows)
        assert _extract(tmp_path / "in", tmp_path / "out").exit_code == 0
        shared = "".join((tmp_path / "out" / "share" / n).read_text() for n in BUNDLE)
        assert "PRIVATE" not in shared and "NOTES" not in shared

    def test_a_process_substitution_runs_its_command(self) -> None:
        """F19: `<(` was read as a redirect, so the hub binary was eaten as its target."""
        assert audit_extract.detect_hub("Bash", {"command": F19_LINE}) == (
            "get_hub_status",
            "cli",
            1,
        )
        assert (
            audit_extract.hub_invocation({"command": F19_LINE}, "cli")
            == "pipecat-context-hub status"
        )

    def test_a_heredoc_fed_to_a_shell_is_its_script(self) -> None:
        """F20: `bash <<EOF` bodies were skipped as data although bash runs them."""
        assert audit_extract.detect_hub("Bash", {"command": F20_LINE}) == (
            "get_hub_status",
            "cli",
            1,
        )

    JS = "const r = await tools.mcp__pipecat_context_hub__search_docs({args});"
    JS_CASES = [
        ('{query: "TTS"}', 'search_docs({"query": "TTS"})'),
        ("{'query': 'TTS', limit: 3, deep: [true, null, -1.5e2],}",
         'search_docs({"query": "TTS", "limit": 3, "deep": [true, null, -150.0]})'),
        ('{"query": "TTS", "api_key": "cobalt-lantern-private"}',
         'search_docs({"query": "TTS", "api_key": "<redacted>"})'),
        ('/* Acquisition: Acme for 17m */ {"query":"TTS","api_key":"cobalt-lantern-private"}', ""),
        ('{query: "TTS" /* note */}', ""),
        ('{query: "TTS" // note\n}', ""),
        ("{query: q}", ""),
        ("{query}", ""),
        ("{query: `TTS ${x}`}", ""),
        ('{query: "a" + secret}', ""),
        ("{...opts}", ""),
        ('{query: "TTS"}, fn()', ""),
    ]  # fmt: skip

    @pytest.mark.parametrize(("args", "shared"), JS_CASES)
    def test_a_js_call_shares_only_literal_arguments(self, args: str, shared: str) -> None:
        """F27: the raw call expression kept comments and credential values."""
        js = self.JS.replace("{args}", args)
        assert audit_extract.detect_hub("exec", js)[:2] == ("search_docs", "mcp_js")
        assert audit_extract.hub_invocation(js, "mcp_js") == shared

    def test_the_js_evidence_never_reaches_the_bundle(self, tmp_path: Path) -> None:
        js = self.JS.replace("{args}", self.JS_CASES[3][0])
        call = {"type": "custom_tool_call", "name": "exec", "input": js}
        _jl(tmp_path / "in" / "dan" / "r.jsonl", TestOtherHarnesses._codex(call, '{"hits": []}'))
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        (c,) = _rows(out / "share" / "calls.jsonl")
        assert c["via"] == "mcp_js" and c["hub_invocation"] == ""
        shared = "".join((out / "share" / n).read_text() for n in BUNDLE)
        assert "Acquisition" not in shared and "cobalt-lantern-private" not in shared

    def test_no_doc_tells_an_organizer_to_execute_bundle_text(self) -> None:
        """F29: SKILL.md (and the bundle's own SCHEMA.md) said to re-run `hub_invocation`."""
        import re

        root = Path(audit_extract.__file__).parents[3]
        skill = (root / ".claude/skills/context-hub-session-audit/SKILL.md").read_text()
        for text in (skill, audit_extract.FIELDS_MD):
            flat = " ".join(text.split())
            assert not re.search(r"re-?run[^.]{0,40}`hub_invocation`", flat), flat
            assert "never paste or execute it" in flat
            assert "rebuild the call by hand" in flat

    def test_one_vocabulary_for_tools_and_subcommands(self) -> None:
        """F6 / F7: the tool and subcommand names were written four times; command_text
        duplicated shell_source with a different list rule."""
        from pipecat_context_hub.audit import shell

        assert audit_extract.HUB_TOOLS == tuple(shell.HUB_TOOL_SUBCOMMANDS)
        assert set(audit_extract.HUB_RESULT_KEY) == set(shell.HUB_TOOL_SUBCOMMANDS)
        assert shell.SEARCH_TOOLS <= set(shell.HUB_TOOL_SUBCOMMANDS)
        assert set(shell.CLI_TO_TOOL.values()) == set(shell.HUB_TOOL_SUBCOMMANDS)
        for sub, tool in shell.CLI_TO_TOOL.items():
            m = audit_extract.CLI_RE.search(f"pipecat-context-hub {sub} x")
            assert m is not None and audit_extract._cli_tool(m.group(1)) == tool
            assert audit_extract.MCP_RE.search(f"mcp__pipecat-context-hub__{tool}")
        src = Path(audit_extract.__file__).read_text()
        assert "search-api|search-docs" not in src and '"status": "get_hub_status"' not in src
        argv = {"command": ["bash", "-lc", "echo 'a b'"]}
        assert (
            audit_extract.command_text(argv)
            == shell.shell_source(argv)
            == shlex.join(argv["command"])
        )


# ---- Contract H1: share/ has one reader, one no-follow writer and one send-side verdict ---------
#
# Invariant. (1) `sharedir.read_share` / `SafeDir.read_bundle` is the only reader of a share/
# directory; gist, repack and ingest all use it, so the same bytes get the same contract verdict
# (`bundle.load_files`). (2) No write goes through a link: every output is a new file created with
# O_EXCL|O_NOFOLLOW in a directory opened with O_NOFOLLOW, then renamed over the target, and repack
# and extract validate everything before writing anything. (3) A failed extract writes nothing,
# so nothing unscrubbed is left that `discard` cannot remove. (4) A malformed manifest, analysis,
# row field or whitespace-only file is a one-line error naming the file, never a traceback, and
# never after a partial rewrite.


def _snapshot(out: Path) -> dict[str, bytes]:
    names = ["share/" + n for n in BUNDLE] + ["share.tar.gz", "share.tar.gz.sha256"]
    return {n: (out / n).read_bytes() for n in names if (out / n).is_file()}


def _clean_exit(result: Result) -> bool:
    """No traceback: the command succeeded or refused with an `Error:` line (exit 1)."""
    return result.exception is None or (
        isinstance(result.exception, SystemExit) and result.exit_code == 1
    )


def _ingest_verdict(path: Path, tmp: Path) -> str:
    (row,) = _merge([path], tmp)["report"]
    return str(row["status"])


def _edit_first_row(path: Path, change: Any) -> None:
    first, *rest = path.read_text().splitlines()
    path.write_text("".join(x + "\n" for x in [json.dumps(change(json.loads(first))), *rest]))


def _edit_manifest(out: Path, change: Any) -> None:
    f = out / "share" / "manifest.json"
    m = json.loads(f.read_text())
    f.write_text(json.dumps(change(m), indent=2))


def _fd_safe() -> bool:
    try:
        from pipecat_context_hub.audit import sharedir
    except ImportError:  # pragma: no cover - only when run against code without the module
        return True
    return sharedir.FD_SAFE


_NOFOLLOW_ONLY = pytest.mark.skipif(
    not _fd_safe(),
    reason="no dir_fd/O_NOFOLLOW (Windows): the path-based fallback has a check-then-use window",
)


def _make_zero_call(tmp_path: Path) -> Path:
    out = tmp_path / "zc"
    assert _extract(_source("missed", tmp_path / "zsrc"), out).exit_code == 0
    return out


# (setup on a fresh bundle, gist verdict, ingest verdict, repack verdict); a verdict is "ok" or
# "rejected". gist and ingest read the same bytes, so they agree on every cell; repack may differ
# only where it is the documented fix (it re-derives stale counts).
def _state_symlinked_share_dir(out: Path, tmp: Path) -> None:
    elsewhere = tmp / "elsewhere"
    (out / "share").rename(elsewhere)
    (out / "share").symlink_to(elsewhere)


def _state_symlinked_file(out: Path, tmp: Path) -> None:
    copy = tmp / "report-copy.md"
    copy.write_bytes((out / "share" / "report.md").read_bytes())
    (out / "share" / "report.md").unlink()
    (out / "share" / "report.md").symlink_to(copy)


def _state_fifo(out: Path, _tmp: Path) -> None:
    import os

    (out / "share" / "calls.jsonl").unlink()
    os.mkfifo(out / "share" / "calls.jsonl")


def _state_whitespace_calls(out: Path, _tmp: Path) -> None:
    (out / "share" / "calls.jsonl").write_text("\n \n")


def _state_oversized(out: Path, _tmp: Path) -> None:
    (out / "share" / "SCHEMA.md").write_text("x" * 200_000)  # the limit is lowered to 100 KB


def _state_analysis(value: Any) -> Any:
    def apply(out: Path, _tmp: Path) -> None:
        _edit_manifest(out, lambda m: {**m, "analysis": value(m["analysis"])})

    return apply


def _state_latency(value: Any) -> Any:
    def apply(out: Path, _tmp: Path) -> None:
        _edit_first_row(out / "share" / "calls.jsonl", lambda r: {**r, "latency_s": value})

    return apply


SHARE_STATES: dict[str, tuple[Any, str, str, str]] = {
    "fresh": (lambda _o, _t: None, "ok", "ok", "ok"),
    "symlinked-share-dir": (_state_symlinked_share_dir, "rejected", "rejected", "rejected"),
    "symlinked-share-file": (_state_symlinked_file, "rejected", "rejected", "rejected"),
    "fifo-at-calls": (_state_fifo, "rejected", "rejected", "rejected"),
    "whitespace-calls-stale-counts": (_state_whitespace_calls, "rejected", "rejected", "ok"),
    "file-over-the-size-limit": (_state_oversized, "rejected", "rejected", "rejected"),
    "analysis-null": (_state_analysis(lambda a: None), "rejected", "rejected", "rejected"),
    "analysis-list": (_state_analysis(lambda a: [a]), "rejected", "rejected", "rejected"),
    "analysis-missing-efficiency": (
        _state_analysis(lambda a: {k: v for k, v in a.items() if k != "efficiency"}),
        "rejected", "rejected", "rejected",
    ),
    "analysis-backlog-object": (
        _state_analysis(lambda a: {**a, "improvement_backlog": {}}),
        "rejected", "rejected", "rejected",
    ),
    "analysis-backlog-item-no-refs": (
        _state_analysis(
            lambda a: {**a, "improvement_backlog": [{"target": "x", "calls": 1, "sessions": 1,
                                                     "participants": 1, "top_signals": {}}]}
        ),
        "rejected", "rejected", "rejected",
    ),
    "analysis-refs-not-strings": (
        _state_analysis(lambda a: {**a, "correctness": {**a["correctness"],
                                                        "read_these_first": [[1]]}}),
        "rejected", "rejected", "rejected",
    ),
    "analysis-signals-list": (
        _state_analysis(lambda a: {**a, "usage": {**a["usage"], "why_not_used": []}}),
        "rejected", "rejected", "rejected",
    ),
    "latency-string": (_state_latency("slow"), "rejected", "rejected", "rejected"),
    "latency-negative": (_state_latency(-2.5), "rejected", "rejected", "rejected"),
    "latency-nan": (_state_latency(float("nan")), "rejected", "rejected", "rejected"),
    "latency-bool": (_state_latency(False), "rejected", "rejected", "rejected"),
}  # fmt: skip


class TestShareDirOneReader:
    """H1 (1) and (4) over the state space SHARE_STATES x {gist, ingest, repack}."""

    @pytest.mark.parametrize("state", list(SHARE_STATES))
    def test_every_reader_gives_the_same_verdict_and_no_traceback(
        self, audited: Path, tmp_path: Path, state: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1/F17/Q3: three readers with three rule sets; a malformed analysis or a string latency
        crashed repack with a traceback after it had rewritten manifest.json."""
        from pipecat_context_hub.audit import bundle

        if state == "fifo-at-calls" and not hasattr(__import__("os"), "mkfifo"):
            pytest.skip("no FIFOs on this platform")
        setup, gist_ok, ingest_ok, repack_ok = SHARE_STATES[state]
        setup(audited, tmp_path)
        # one limit, every reader (raising=False: the regression run against older code)
        monkeypatch.setattr(bundle, "MAX_FILE_BYTES", 100_000, raising=False)
        before = _snapshot(audited) if state != "fifo-at-calls" else {}

        gist = _invoke("gist", str(audited))
        assert _clean_exit(gist), gist.output
        assert ("ok" if gist.exit_code == 0 else "rejected") == gist_ok, gist.output
        assert _ingest_verdict(audited / "share", tmp_path / "m") == ingest_ok

        repack = _invoke("repack", str(audited))
        assert _clean_exit(repack), (repack.output, repack.exception)
        assert ("ok" if repack.exit_code == 0 else "rejected") == repack_ok, repack.output
        if repack_ok == "rejected":
            assert repack.output.startswith("Error: ") and repack.output.count("\n") == 1
            if state != "fifo-at-calls":  # a refused repack rewrote nothing
                assert _snapshot(audited) == before
        if state.startswith(("analysis", "latency")):
            name = "manifest.json" if state.startswith("analysis") else "calls.jsonl"
            for out in (gist.output, repack.output):
                assert name in out, out

    def test_a_zero_call_bundle_reads_the_same_with_a_missing_or_symlinked_calls_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1: ingest skipped a symlinked calls.jsonl (absent, allowed at zero rows) while gist
        refused it, and gist demanded a calls.jsonl that ingest let be absent."""
        out = _make_zero_call(tmp_path)
        calls = out / "share" / "calls.jsonl"
        calls.unlink()  # absent: allowed at zero rows, by all three
        assert _invoke("gist", str(out)).exit_code == 0
        assert _ingest_verdict(out / "share", tmp_path / "m1") == "ok"
        assert _invoke("repack", str(out)).exit_code == 0
        empty = tmp_path / "empty"
        empty.write_text("")
        calls.symlink_to(empty)  # a link: refused by all three
        assert _invoke("gist", str(out)).exit_code == 1
        assert _ingest_verdict(out / "share", tmp_path / "m2") == "rejected"
        assert _invoke("repack", str(out)).exit_code == 1

    def test_gist_repack_and_ingest_all_read_through_the_one_reader(
        self, audited: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1: each command read share/ its own way."""
        from pipecat_context_hub import cli_audit
        from pipecat_context_hub.audit import ingest, sharedir

        reads: list[str] = []
        real = sharedir.SafeDir.read_bundle

        def recording(self: sharedir.SafeDir) -> dict[str, bytes]:
            reads.append(self.path.name)
            return real(self)

        monkeypatch.setattr(sharedir.SafeDir, "read_bundle", recording)
        assert _invoke("gist", str(audited)).exit_code == 0
        assert _invoke("repack", str(audited)).exit_code == 0
        _merge([audited / "share"], tmp_path / "m")
        assert reads == ["share", "share", "share"]
        assert not hasattr(ingest, "read_dir") and not hasattr(audit_extract, "_read_share")
        for mod in (cli_audit, audit_extract, ingest):
            assert "read_bytes()" not in Path(str(mod.__file__)).read_text(), mod.__name__

    def test_the_gist_pre_flight_is_an_audit_function_and_posts_the_checked_bytes(
        self, audited: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F11: gist's pre-flight (leak check, validator, nothing-to-share, omission) lived in the
        click command; gh then re-read share/ after the checks."""
        import inspect

        from pipecat_context_hub import cli_audit
        from pipecat_context_hub.audit import sharedir

        plan = sharedir.gist_plan(audited, allow_empty=False)
        assert list(plan.post) == BUNDLE and plan.omitted == []
        src = inspect.getsource(cli_audit.gist_cmd.callback)  # type: ignore[arg-type]
        for name in ("load_files", "verify_", "has_content", "omitted_when_posting", "read_"):
            assert name not in src, name
        posted: dict[str, bytes] = {}

        def fake_run(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
            for a in argv[argv.index("--") + 1 :]:
                posted[Path(a).name] = Path(a).read_bytes()
            return subprocess.CompletedProcess(argv, 0, "https://gist.github.com/x/abc\n", "")

        real_plan = sharedir.gist_plan

        def plan_then_swap(out_dir: Path, *, allow_empty: bool) -> sharedir.GistPlan:
            p = real_plan(out_dir, allow_empty=allow_empty)
            (out_dir / "share" / "report.md").write_text(f"swapped after the check {SECRET}\n")
            return p

        monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(sharedir, "gist_plan", plan_then_swap)
        assert _invoke("gist", str(audited), "--yes").exit_code == 0
        assert posted == plan.post and SECRET.encode() not in b"".join(posted.values())

    def test_decode_manifest_is_the_bundle_contracts_and_fakes_no_counts(self) -> None:
        """F8: extract.decode_manifest spliced zeroed counts into the manifest to reuse the
        validator."""
        from pipecat_context_hub.audit import bundle

        src = Path(audit_extract.__file__).read_text()
        assert "def decode_manifest" not in src and "ROW_COUNT_KEYS" not in src
        assert vars(audit_extract)["decode_manifest"] is bundle.decode_manifest
        shape = _plain_shaped(bundle.ANALYSIS_SHAPE)
        m = {"schema_version": 1, "counts": {"call_rows": "stale"}, "analysis": shape}
        assert bundle.decode_manifest(json.dumps(m).encode())["counts"] == {"call_rows": "stale"}
        assert bundle.manifest_error(m) is not None  # the count values are load_files's check
        for bad in (b"[]", b'{"schema_version": 2, "counts": {}}', b'{"schema_version": 1}'):
            with pytest.raises(bundle.BundleError):
                bundle.decode_manifest(bad)


class TestShareDirNoFollowWrites:
    """H1 (2): a link planted at any output, before or after the early check, is never written
    through; repack refuses a symlinked share/ before writing anything."""

    def test_repack_refuses_a_symlinked_share_directory_before_writing(
        self, audited: Path, tmp_path: Path
    ) -> None:
        """F26: repack overwrote the link target's manifest.json and report.md, then refused."""
        target = tmp_path / "target"
        (audited / "share").rename(target)
        (audited / "share").symlink_to(target)
        _keep_lines(target / "calls.jsonl", 1)  # a hand edit repack would act on (new counts)
        before = {n: (target / n).read_bytes() for n in BUNDLE}
        result = _invoke("repack", str(audited))
        assert result.exit_code == 1 and "symlink" in result.output, result.output
        assert {n: (target / n).read_bytes() for n in BUNDLE} == before

    @_NOFOLLOW_ONLY
    @pytest.mark.parametrize(
        "plant",
        ["share/report.md", "share/manifest.json", "share.tar.gz", "share.tar.gz.sha256",
         "summary.json", "calls.jsonl"],
    )  # fmt: skip
    def test_a_link_planted_after_the_check_is_replaced_never_followed(
        self, tmp_path: Path, plant: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Q1: refuse_unsafe_outputs then open(..., "w"): a link planted in between was followed.
        The check is disabled here, which is the window; the writer alone must hold."""
        _hub_session(tmp_path / "in" / "zed" / "s.jsonl")
        out = tmp_path / "out"
        (out / "share").mkdir(parents=True)
        victim = tmp_path / "victim"
        victim.write_text("keep me")
        (out / plant).symlink_to(victim)
        monkeypatch.setattr(audit_extract, "refuse_unsafe_outputs", lambda _o: None)
        result = _extract(tmp_path / "in", out)
        assert result.exit_code == 0, result.output
        assert victim.read_text() == "keep me"
        assert (out / plant).is_file() and not (out / plant).is_symlink()

    @_NOFOLLOW_ONLY
    @pytest.mark.parametrize("cmd", ["extract", "repack"])
    def test_a_link_swapped_in_between_validation_and_write_is_not_followed(
        self, audited: Path, tmp_path: Path, cmd: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Q1: the hook plants links right after the checks pass, before the first write."""
        from pipecat_context_hub.audit import sharedir

        victims = {n: tmp_path / f"victim-{n}" for n in ("report.md", "manifest.json")}
        real = sharedir.check_share

        def check_then_plant(files: Any) -> Any:
            verdict = real(files)
            for name, victim in victims.items():
                victim.write_text("keep me")
                (audited / "share" / name).unlink()
                (audited / "share" / name).symlink_to(victim)
            return verdict

        monkeypatch.setattr(audit_extract, "check_share", check_then_plant)
        result = (
            _extract(tmp_path / "in", audited)
            if cmd == "extract"
            else _invoke("repack", str(audited))
        )
        assert result.exit_code == 0, result.output
        assert all(v.read_text() == "keep me" for v in victims.values())
        assert not any((audited / "share" / n).is_symlink() for n in victims)

    @_NOFOLLOW_ONLY
    def test_a_share_directory_swapped_for_a_link_after_the_check_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Q1: share/ itself replaced by a link after the early check: nothing lands in the target."""
        _hub_session(tmp_path / "in" / "zed" / "s.jsonl")
        out = tmp_path / "out"
        out.mkdir()
        target = tmp_path / "target"
        target.mkdir()
        (out / "share").symlink_to(target)
        monkeypatch.setattr(audit_extract, "refuse_unsafe_outputs", lambda _o: None)
        result = _extract(tmp_path / "in", out)
        assert result.exit_code == 1 and "symlink" in result.output, result.output
        assert list(target.iterdir()) == []

    def test_ingest_never_writes_through_a_link_in_its_output(
        self, audited: Path, tmp_path: Path
    ) -> None:
        victim = tmp_path / "victim"
        victim.write_text("keep me")
        out = tmp_path / "merged"
        out.mkdir()
        (out / "ingest.json").symlink_to(victim)
        result = _invoke("ingest", str(audited / "share.tar.gz"), "--out", str(out))
        assert _clean_exit(result) and victim.read_text() == "keep me", result.output


class TestEmptyFileAndFailedBuilds:
    """H1 (3) and the one "empty file" predicate."""

    @pytest.mark.parametrize("data", [b"", b"\n", b" \t\r\n", " \n".encode(), b"{}\n", b"x"])
    def test_one_empty_rule_for_omission_digest_and_rows(self, data: bytes) -> None:
        """F25: gist omitted a whitespace-only file (`data.strip()`), the digest hashed its bytes."""
        from pipecat_context_hub.audit import bundle

        empty = bundle.is_empty(data)
        assert bundle.omitted_when_posting("calls.jsonl", data) is empty
        assert (bundle.content_digest({"calls.jsonl": data}) == bundle.content_digest({})) is empty
        if empty:
            assert bundle.jsonl_rows({"calls.jsonl": data}, "calls.jsonl") == []
        else:
            with pytest.raises(bundle.BundleError):  # a line to parse, and it is not a row
                bundle.jsonl_rows({"calls.jsonl": data}, "calls.jsonl")
        assert not bundle.omitted_when_posting("report.md", data)  # only JSONL may be left out

    @pytest.mark.parametrize("name", ["calls.jsonl", "sessions.jsonl"])
    def test_a_whitespace_only_jsonl_file_has_one_identity_as_tarball_and_gist(
        self, audited: Path, tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F25: delete every row but leave a newline, repack: the gist left the file out, the
        tarball kept the newline, and ingest kept the same session twice as a collision."""
        (audited / "share" / name).write_text("\n")
        assert _invoke("repack", str(audited)).exit_code == 0
        seen = _fake_gh(monkeypatch)
        assert _invoke("gist", str(audited), "--yes", "--allow-empty").exit_code == 0
        posted = {Path(a).name for a in seen[-1][seen[-1].index("--") + 1 :]}
        assert name not in posted
        gist = _posted_dir(audited / "share", seen[-1], tmp_path / "gist")
        report = _merge([audited / "share.tar.gz", gist], tmp_path / "m")["report"]
        assert [r["status"] for r in report] == ["ok", "ok"], report
        assert report[1].get("duplicate_of") == "share.tar.gz", report
        assert report[0]["bundle"] == report[1]["bundle"]

    def test_a_failed_leak_check_leaves_nothing_unscrubbed_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F18: out/sessions.jsonl and calls.jsonl were written before the leak check; on failure
        summary.json was never written, so `discard` refused the directory."""
        src = TestEgress()._canary_transcript(tmp_path / "c")
        out = tmp_path / "out"
        monkeypatch.setattr(audit_extract, "sanitize", lambda v: v)  # a sink that skips the egress
        result = _extract(src, out)
        assert result.exit_code == 1 and "leak check" in result.output
        assert not out.exists() or list(out.rglob("*")) == []

    def test_a_failed_re_run_keeps_the_previous_run_whole_and_discardable(
        self, audited: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F18: a failed re-run deleted share/ (so discard refused the directory) and left the new
        run's unscrubbed local JSONL on disk."""
        before = {p: p.read_bytes() for p in audited.rglob("*") if p.is_file()}
        src = TestEgress()._canary_transcript(tmp_path / "c")
        monkeypatch.setattr(audit_extract, "sanitize", lambda v: v)
        assert _extract(src, audited).exit_code == 1
        monkeypatch.undo()
        assert {p: p.read_bytes() for p in audited.rglob("*") if p.is_file()} == before
        assert _invoke("discard", str(audited), "--yes").exit_code == 0
        assert not audited.exists()


class TestSmallFixes:
    def test_same_named_text_dumps_are_distinct_sessions(self, tmp_path: Path) -> None:
        """F16: root/alice/day1/chat.txt and root/alice/day2/chat.txt were one session ('chat'),
        and ingest kept only the first."""
        for day in ("day1", "day2"):
            f = tmp_path / "in" / "alice" / day / "chat.txt"
            f.parent.mkdir(parents=True)
            f.write_text(
                "ran mcp__pipecat-context-hub__search_docs\nfrom pipecat.pipeline import X"
            )
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        sessions = _rows(out / "share" / "sessions.jsonl")
        assert len({(s["participant"], s["session"]) for s in sessions}) == 2
        assert all("chat" not in s["session"] for s in sessions)  # still opaque
        assert _merge([out / "share.tar.gz"], tmp_path / "m")["sessions"] == 2

    @pytest.mark.parametrize("found", ["gh.bat", "{cwd}/gh.exe"])
    def test_gist_refuses_a_gh_found_in_the_current_directory(
        self, audited: Path, tmp_path: Path, found: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F24: on Windows shutil.which searches the current directory first, so a planted
        gh.bat in an untrusted checkout ran with the bundle."""
        cwd = tmp_path / "checkout"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        def boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("must not run a gh from the current directory")

        monkeypatch.setattr("shutil.which", lambda _n: found.format(cwd=cwd))
        monkeypatch.setattr(subprocess, "run", boom)
        result = _invoke("gist", str(audited), "--yes")
        assert result.exit_code == 1 and "current directory" in result.output, result.output

    def test_skill_flag_table_names_every_flag_and_the_gist_step_the_omission(self) -> None:
        """F12/F13/F14: the table missed non_json_result, named only ' + ', and the gist step said
        it previews five files though an empty JSONL file is left out."""
        from pipecat_context_hub.audit import flags

        root = Path(audit_extract.__file__).parents[3]
        skill = (root / ".claude/skills/context-hub-session-audit/SKILL.md").read_text()
        judge = skill[skill.index("## 2. Judge") : skill.index("## 3. Report")]
        rows = [line for line in judge.splitlines() if line.startswith("| `")]
        for name in flags.FLAGS:
            assert any(f"`{name}`" in r.split("|")[1] for r in rows), name
        (multi,) = [r for r in rows if "`multi_concept_without_delimiter`" in r]
        assert all(f"`{d}`" in multi for d in flags.MULTI_CONCEPT_DELIMITERS)
        gist = " ".join(skill[skill.index("**Gist:**") : skill.index("**Tarball:**")].split())
        assert "left out" in gist and "five files" not in gist


# ---- Contract H2: one description of each analysis and report concept --------------------------


class TestOneDescriptionPerConcept:
    def test_outcome_classes_are_declared_once_with_their_descriptions(self, audited: Path) -> None:
        """F3: outcome classes in flags.OUTCOME_ORDER, their text in analysis.OUTCOME_GLOSSARY."""
        from pipecat_context_hub.audit import analysis, flags

        assert not hasattr(analysis, "OUTCOME_GLOSSARY")
        assert set(flags.OUTCOMES) == {*flags.OUTCOME_ORDER, flags.CLEAN_OUTCOME}
        assert tuple(flags.OUTCOMES)[: len(flags.OUTCOME_ORDER)] == flags.OUTCOME_ORDER
        manifest = json.loads((audited / "share" / "manifest.json").read_text())
        assert manifest["analysis"]["correctness"]["glossary"] == flags.OUTCOMES
        for name in flags.FLAGS:
            assert analysis.classify_call({"flags": [name]})[0] in flags.OUTCOMES

    def test_the_overall_median_is_latency_stats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """F4: analyze recomputed the overall median inline beside latency_stats."""
        from pipecat_context_hub.audit import analysis

        sentinel = 123.4
        monkeypatch.setattr(analysis, "latency_stats", lambda _l: (sentinel, None))
        calls = [{"participant": "p", "session_id": "s", "call_idx": 0, "tool": "get_doc",
                  "via": "mcp", "input": {}, "latency_s": 1.0, "flags": [], "result": {}}]  # fmt: skip
        analysis.annotate_calls(calls)
        assert analysis.analyze([], calls, lambda c: "r")["efficiency"]["median_latency_s"] == 123.4

    @pytest.mark.parametrize("n", range(1, 26))
    def test_p90_is_the_nearest_rank_percentile(self, n: int) -> None:
        """F15: int(0.9 * n) picked the maximum for every n up to 10."""
        from pipecat_context_hub.audit.analysis import latency_stats

        values = [float(v) for v in range(1, n + 1)]
        reference = next(v for i, v in enumerate(values) if 10 * (i + 1) >= 9 * n)
        assert latency_stats(reversed(values))[1] == reference
        assert latency_stats([1, 2, 3, 4, 5, 6, 7, 8, 9, 100]) == (5.5, 9)  # the finding's case

    def test_the_answers_section_is_rendered_by_report_through_its_cell_rules(self) -> None:
        """F5: analysis.render built the answers and backlog rows itself: a `|` broke the table,
        and backticks glued to a 7-character value made the 8+ character secret rule match."""
        from pipecat_context_hub.audit import analysis, bundle

        assert not hasattr(analysis, "render")
        render_report({"analysis": _plain_shaped(bundle.ANALYSIS_SHAPE)}, [])  # reads no other key
        a = _plain_shaped(bundle.ANALYSIS_SHAPE)
        a["usage"]["missed_opportunity"] = 1
        a["usage"]["why_not_used"] = {"MONKEY=abcdefg": 1}
        a["improvement_backlog"] = [{"target": "a|b", "calls": 1, "sessions": 1,
                                     "participants": 1, "top_signals": {"x": 1},
                                     "example_refs": ["r1"]}]  # fmt: skip
        assert _json_clean(a)
        md = render_report({"analysis": a}, [])
        assert "| a\\|b | 1 |" in md
        assert _report_clean(md), egress._file_leaks("report.md", md)

    def test_every_threshold_a_text_names_is_its_constant(self) -> None:
        """F9: SLOW_SECONDS, LARGE_RESULT_BYTES, FOLLOWUP_EVENTS and ERROR_WINDOW_EVENTS lived in
        extract.py and were restated as '8 s', '30 KB', '8 events', '25 events'."""
        import re

        from pipecat_context_hub.audit import flags

        for name in ("SLOW_SECONDS", "LARGE_RESULT_BYTES", "FOLLOWUP_EVENTS",
                     "ERROR_WINDOW_EVENTS", "REFORMULATION_EVENTS", "MULTI_CONCEPT_WORDS"):  # fmt: skip
            assert vars(audit_extract)[name] is getattr(flags, name), name
        g = {k: f.glossary for k, f in flags.FLAGS.items()}
        assert f"{flags.SLOW_SECONDS:g} s" in g["slow"]
        assert f"{flags.LARGE_RESULT_BYTES // 1000} KB" in g["large_result"]
        assert f"within {flags.FOLLOWUP_EVENTS} events" in g["agent_bypassed_after_result"].lower()
        assert f"within {flags.ERROR_WINDOW_EVENTS} events" in g["downstream_error"]
        assert f"within {flags.REFORMULATION_EVENTS} events" in g["reformulation_after_bad_result"]
        assert (
            f"more than {flags.MULTI_CONCEPT_WORDS} words" in g["multi_concept_without_delimiter"]
        )
        assert f"within {flags.ERROR_WINDOW_EVENTS} events" in flags.OUTCOMES["suspect"]
        assert f"next {flags.FOLLOWUP_EVENTS} tool calls" in audit_extract.FIELDS_MD
        assert f"next {flags.ERROR_WINDOW_EVENTS} events" in audit_extract.FIELDS_MD
        allowed = {
            f"{flags.SLOW_SECONDS:g}",
            str(flags.LARGE_RESULT_BYTES // 1000),
            *(
                str(getattr(flags, n))
                for n in (
                    "FOLLOWUP_EVENTS",
                    "ERROR_WINDOW_EVENTS",
                    "REFORMULATION_EVENTS",
                    "MULTI_CONCEPT_WORDS",
                )
            ),
            "0.3",
        }  # fmt: skip  (0.3: the hub's own low-confidence threshold, not ours)
        for text in [*g.values(), *flags.OUTCOMES.values()]:
            assert set(re.findall(r"\d+(?:\.\d+)?", text)) <= allowed, text

    def test_no_parallel_alias_tables(self) -> None:
        """F10: FLAG_GLOSSARY / SEVERITY / CALL_KEYS / SESSION_KEYS had no consumer in src."""
        from pipecat_context_hub.audit import bundle, flags

        for mod, name in ((flags, "FLAG_GLOSSARY"), (flags, "SEVERITY"),
                          (bundle, "CALL_KEYS"), (bundle, "SESSION_KEYS")):  # fmt: skip
            assert not hasattr(mod, name), name


# ---- Round 3, contract K1: one credential-label model, two independent locators -----------------
#
# Invariant. A value written after a credential LABEL is redacted, and the tripwire refuses any
# text that still holds one. The label model is defined once (egress): the recogniser
# (`is_credential_name` over `label_segments`: NON_CREDENTIAL_SEGMENTS blanked anywhere, a
# BENIGN_WHOLE_LABELS label exempt, a BENIGN_LABEL_WORDS phrase dropped only as the TRAILING
# segments, then a CREDENTIAL_STEM contained or a last segment `key`/`keys`), the separators (`:`
# `=` `：` `=>` `->`, or a flag followed by blanks), the quote token (optionally backslash-escaped,
# any depth) and the value extent (`_value_span`: quoted runs to its matching close, an open quote
# to the end; unquoted runs to the end of the segment, through `& , ; ) ] }` unless the context
# before the label makes them terminators: a URL-query key, a bracket still open; no minimum length;
# placeholders exempt by `is_placeholder`, round 4). Sanitize finds labels with a
# regex (locator A, `redact_labelled`); the tripwire with a character walk from each separator
# (locator B, `labelled_value_leaks`). Both share the recogniser and the value rules, so a mutation
# of either locator shows up as a disagreement over the generated space below.

_K1_CANARY = "zq7Canary"
_K1_SEPARATORS = [":", "=", "：", "=>", "->"]
_K1_SPACING = [("", ""), ("", " "), (" ", " ")]
_K1_QUOTES = ["", '"', "'", '\\"', '\\\\"']
_K1_VALUES = {
    "word": _K1_CANARY,
    "short": "zQ",
    "phrase": f"my {_K1_CANARY} phrase",
    "piped": f"north | {_K1_CANARY}",  # quoted only: an unquoted value ends at a segment separator
}


def _k1_labels(stem: str) -> list[str]:
    return [stem, stem.upper(), f"my_{stem}_2", f"{stem.capitalize()}Prod", f"x-{stem}"]


def _k1_cases(stem: str) -> list[str]:
    import itertools

    out = []
    for lab, sep, (a, b), q, (kind, v) in itertools.product(
        _k1_labels(stem), _K1_SEPARATORS, _K1_SPACING, _K1_QUOTES, _K1_VALUES.items()
    ):
        if kind != "piped" or q:
            out.append(f"{q}{lab}{q}{a}{sep}{b}{q}{v}{q}")
    for v in (_K1_CANARY, "zQ"):
        out += [f"--{stem} {v}", f"--{stem}={v}"]
    for q in ('"', "'", '\\"'):
        out += [f"--{stem} {q}my {_K1_CANARY} phrase{q}", f"--{stem}={q}north | {_K1_CANARY}{q}"]
    return out


_K1_BENIGN = [
    "max_tokens: 1000", "max_tokens=1000", "tokenizer: bpe", "password_hint: pet name",
    "PASSWORD_FILE=/run/secrets/db", "secretary: Jane", "token_count=12", "MONKEY=banana",
    "keys: 3", "password: <your-password>", "API_KEY=${API_KEY}", "token: ...", "password: xxxx",
    "api_key: YOUR_API_KEY", 'secret: ""', "x->y: z", "a -> b", "tokens -> 5", "TTS -> STT",
]  # fmt: skip


_R4_CREDENTIAL = [
    "API_TOKENS", "MY_TOKENS", "SECRETS", "PASSWORDS", "secret_key_base", "apiTokens",
    "SSH_KEYS", "key", "password", "x-api-key", "DB_PASSWORD",
    # a benign phrase that is not the TRAILING segments exempts nothing
    "MAX_TOKENS_SECRET", "password_hint_token",
    # a non-credential word blanks only itself: the rest of its segment is still read
    "TOKENIZERSECRET", "secretaryPassword",
]  # fmt: skip
_R4_BENIGN = [
    "tokens", "max_tokens", "token_count", "password_hint", "tokens_used", "keys", "tokenizer",
    "secretary", "tokenizer_name", "MONKEY", "KeyError", "OPENAI_MAX_TOKENS",
]  # fmt: skip
_R4_SEPARATORS = [": ", "=", " = ", "：", " => ", "->"]
# Unquoted values with no blank: redacted through the whole token, whatever punctuation they hold.
_R4_BARE_VALUES = ["abc&def123", "ab,cd1234", "x)y1234", "a]b1234", "a}b;c1234", "ab#cd?e=f"]
_R4_PLACEHOLDERS = [
    "$TOKEN", "${API_KEY}", "<token>", "YOUR_API_KEY", "<your-password>", "your-api-key", "...",
    "…", "xxx", "***", "${OPENAI_API_KEY}",
]  # fmt: skip
_R4_NOT_PLACEHOLDERS = [
    "${AKIAABCDEFGHIJKL}", "$AKIAABCDEFGHIJKL", "<ghp_abcdefghijkl>", "<API_KEY>", "$token",
    "${A_VARIABLE_NAME_LONGER_THAN_32_CH}", "$AB12CD34EF56GH78", "YOUR_abcd1234efgh5678",
    "<sk-proj-abcdefgh>", "${ABCDEF0123456789ABCDEF}", "Your_Key",
    "your-dog-rex", "YOUR_PROJECT_ID",  # round 5 (K1 f): YOUR_ words must name a credential
]  # fmt: skip
# `{0}` is the label, `{1}` the value; the expected output keeps everything around the value.
_R4_CONTEXTS = [
    "({0}: {1}) tail", "[{0}: {1}] tail", '{{"{0}": {1}, "u": 1}}', "{{{0}: {1}}}",
    "https://h/p?x=1&{0}={1}&y=2", "https://h/p?{0}={1}#frag", "a=b&{0}={1}&c=d",
    "f({0}={1}, y=2)", "f({0}={1}; y)", "see ({0}: {1}). Next",
    # a closer pops only the bracket it matches: `]` leaves `(` open, so `)` ends the value
    "[(] {0}: {1}) tail",
]  # fmt: skip
_R4_FORMS = [
    "{s}", "{s}s", "my_{s}", "api_{s}", "my_{s}s", "api_{s}s", "openai_{s}", "x-{s}", "{s}_2",
    "{s}_prod", "{s}_base", "{s}_value", "{s}_id", "{s}_name", "db_{s}_prod", "x_{s}s_y",
    "max_{s}s", "{s}_count", "{s}s_used", "{s}_hint", "{s}_file", "{s}_santa",
]  # fmt: skip
# Documented exceptions to "never less than before": the brief's benign phrases, the existing
# BENIGN fixtures (`tokens`, `keys`), and a label whose `key`/`keys` segment is not its last
# (`KEY_ID`, `KeyError`, `db_key_prod`): `key` is a common word, so only a label ending in it names
# one (`STRIPE_KEY`, `SSH_KEYS`, a bare `key` do).
_R4_OLD_RULE_EXCEPTIONS = {
    "maxtokens", "tokencount", "tokensused", "passwordhint", "passwordfile", "secretsanta",
    "tokens", "keys",
}  # fmt: skip


def _r4_case(label: str, case: str) -> str:
    if case == "upper":
        return label.upper()
    if case == "camel":
        head, *rest = re.split(r"[_\-]", label)
        return head + "".join(w.capitalize() for w in rest)
    return label


def _r4_old_rule_exception(label: str) -> bool:
    segs = egress.label_segments(label)
    if "".join(segs) in _R4_OLD_RULE_EXCEPTIONS:
        return True
    return bool(set(segs) & {"key", "keys"}) and segs[-1] not in ("key", "keys")


class TestLabelModelK1:
    @pytest.mark.parametrize("stem", egress.CREDENTIAL_STEMS)
    def test_every_label_shape_is_redacted_and_refused(self, stem: str) -> None:
        """The whole space: stem x label form x separator x spacing x quote token x value shape,
        each alone, inside prose and inside a serialised share row."""
        for text in _k1_cases(stem):
            secret = "zQ" if "zQ" in text else _K1_CANARY
            clean = egress.sanitize(text)
            assert secret not in clean, (text, clean)
            assert egress.leaks_in(clean) == [], (text, clean)
            assert egress.sanitize(clean) == clean
            assert "credential label" in egress.leaks_in(text), text  # the tripwire is not blind
            assert egress._file_leaks("calls.jsonl", json.dumps({"q": text})), text
            assert secret not in egress.sanitize(f"note {text}"), text
            # the two locators agree (neither borrows the other's matches)
            assert egress.labelled_value_leaks(text) == bool(egress._label_spans(text)), text

    @pytest.mark.parametrize("text", _K1_BENIGN)
    def test_benign_labels_and_placeholders_are_kept(self, text: str) -> None:
        assert egress.sanitize(text) == text
        assert egress.leaks_in(text) == []
        assert egress.labelled_value_leaks(text) is False and egress._label_spans(text) == []

    def test_the_locators_agree_on_generated_text(self) -> None:
        import random

        rng = random.Random(3)
        for _ in range(3000):
            text = _fuzz_text(rng)
            assert egress.labelled_value_leaks(text) == bool(egress._label_spans(text)), text

    @pytest.mark.parametrize(
        ("key", "value"),
        [("password1", "hunter22"), ("api_key_prod", "zzzz1234"), ("OPENAI_API_KEY_2", "abcd"),
         ("secret_key_base", "abcd"), ("STRIPE_KEY", "abcd")],
    )  # fmt: skip
    def test_f4_a_key_containing_a_stem_is_a_credential(
        self, key: str, value: str, tmp_path: Path
    ) -> None:
        """F4: the name rule was suffix-only, so `password1` and `api_key_prod` kept their values."""
        assert egress.sanitize({key: value}) == {key: egress.REDACTED}
        share = tmp_path / "share"
        share.mkdir()
        (share / "calls.jsonl").write_text(json.dumps({"input": {key: value}}) + "\n")
        assert _verify_share(share, ("calls.jsonl",)) != []

    @pytest.mark.parametrize(
        "text", ['{\\"password\\": \\"hunter2xyz\\"}', '{\\"api_key\\":\\"abcd1234efgh\\"}',
                 '{\\\\\\"token\\\\\\": \\\\\\"abcd1234efgh\\\\\\"}'],
    )  # fmt: skip
    def test_f5_escaped_quotes_do_not_hide_a_labelled_value(self, text: str) -> None:
        """F5: a backslash before the closing quote broke the label pattern."""
        clean = egress.sanitize(text)
        assert "hunter2xyz" not in clean and "abcd1234efgh" not in clean, clean
        assert egress.leaks_in(text) and egress._file_leaks("calls.jsonl", json.dumps({"q": text}))

    def test_f16_a_short_first_word_is_still_a_value(self) -> None:
        """F16: the value needed a first word of 4+ chars."""
        assert egress.sanitize("password: my secret phrase") == f"password: {egress.REDACTED}"
        assert egress.leaks_in("password: my secret phrase")

    @pytest.mark.parametrize(
        ("text", "secret"),
        [("API key: abc12xy", "abc12xy"), ("password：hunter2xyz", "hunter2xyz")],
    )
    def test_f17_spaced_labels_and_the_fullwidth_colon(self, text: str, secret: str) -> None:
        """F17: label names excluded spaces, separators excluded U+FF1A."""
        assert secret not in egress.sanitize(text) and egress.leaks_in(text)

    def test_f18_url_userinfo_with_an_empty_user(self, tmp_path: Path) -> None:
        """F18: the userinfo rule needed a non-empty user (the standard Redis URL shape)."""
        text = "redis://:hunter2pass@localhost:6379"
        assert "hunter2pass" not in egress.sanitize(text)
        share = tmp_path / "share"
        share.mkdir()
        (share / "calls.jsonl").write_text(json.dumps({"q": text}) + "\n")
        assert _verify_share(share, ("calls.jsonl",)) != []

    @pytest.mark.parametrize(
        ("query", "words"),
        [('password: "correct horse battery staple"', ("horse", "battery", "staple")),
         ('passphrase: "north | lantern-private-passphrase"', ("lantern-private",))],
    )  # fmt: skip
    def test_f20_a_quoted_value_goes_whole(
        self, query: str, words: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F20: the label rule ate the label before the quoted value was consumed, and a ` | `
        inside quotes ended the value; the suffix reached the bundle and passed every check."""
        clean = egress.sanitize({"query": query})["query"]
        assert not any(w in clean for w in words), clean
        assert egress.leaks_in(query)
        _hub_session(tmp_path / "in" / "zed" / "s.jsonl", query, confident=False)
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        assert _invoke("repack", str(out)).exit_code == 0
        shared = "".join((out / "share" / n).read_text() for n in BUNDLE)
        assert not any(w in shared for w in words)
        seen = _fake_gh(monkeypatch)
        assert _invoke("gist", str(out), "--yes", "--allow-empty").exit_code == 0, seen

    def test_f13_flags_render_with_the_whitespace_bounded_separator(self, audited: Path) -> None:
        """F13: flags joined with ', ' made `password:xyz, a` a labelled value in report.md although
        no single flag string was; repack then refused the report it had just written."""
        row = egress.sanitize(
            {"ref": "r", "tool": "search_docs", "flags": ["password:xyz", "a", "zero_hits"],
             "result": {}, "input": {"query": "q"}}
        )  # fmt: skip
        report = render_report({}, [row])
        assert "| password:<redacted> · a · zero_hits |" in report, report
        assert _report_clean(report)
        # the finding's path end to end: repack regenerates report.md and accepts its own output
        f = audited / "share" / "calls.jsonl"
        first, *rest = f.read_text().splitlines()
        edited = {**json.loads(first), "flags": row["flags"]}
        f.write_text("".join(line + "\n" for line in [json.dumps(edited), *rest]))
        assert _invoke("repack", str(audited)).exit_code == 0

    # ---- Round 4 (K1 continued): segment recogniser, context-aware extent, narrow placeholders.

    @pytest.mark.parametrize("text", ["API_TOKENS=abcd1234efgh", "MY_TOKENS: s3cr3tvalue99"])
    def test_r4_a_plural_credential_label_is_a_credential(self, text: str) -> None:
        """R4-1: `tokens` was blanked anywhere in a label (for max_tokens), so API_TOKENS kept its
        value; the KEY|TOKEN|SECRET|PASSWORD rule before round 3 caught it."""
        label, value = text.split("=" if "=" in text else ": ")
        assert value not in egress.sanitize(text)
        assert egress.leaks_in(text) == ["credential label"]
        assert egress.sanitize({label: value}) == {label: egress.REDACTED}

    @pytest.mark.parametrize(
        ("text", "clean"),
        [
            ("password: abc&def123", "password: <redacted>"),
            ("pwd: ab,cd1234xyz", "pwd: <redacted>"),
        ],
    )
    def test_r4_an_unquoted_value_goes_whole_through_punctuation(
        self, text: str, clean: str
    ) -> None:
        """R4-2: `&` and `,` always ended a value, so the tail after them was shared."""
        assert egress.sanitize(text) == clean
        assert egress.sanitize(f"note {text}") == f"note {clean}"

    def test_r4_a_token_shaped_variable_is_not_a_placeholder(self) -> None:
        """R4-3: any `${NAME}` counted as a placeholder, so `${AKIA...}` was kept."""
        text = "secret: ${AKIAABCDEFGHIJKL}"
        assert egress.sanitize(text) == "secret: <redacted>"
        assert egress.leaks_in(text) == ["credential label"]

    def test_r4_the_whole_space(self) -> None:
        """Label forms x separators x value shapes x contexts x placeholders, each with the
        outcome the stated rules give, both locators and closure checked on every text."""
        import itertools

        r = egress.REDACTED
        cases: list[tuple[str, str]] = []  # (text, expected sanitize output)
        for lab, sep in itertools.product(_R4_CREDENTIAL + _R4_BENIGN, _R4_SEPARATORS):
            cred = lab in _R4_CREDENTIAL
            for v in _R4_BARE_VALUES:
                cases.append((f"{lab}{sep}{v}", f"{lab}{sep}{r if cred else v}"))
            for p in _R4_PLACEHOLDERS:
                cases.append((f"{lab}{sep}{p}", f"{lab}{sep}{p}"))
            for p in _R4_NOT_PLACEHOLDERS:
                cases.append((f"{lab}{sep}{p}", f"{lab}{sep}{r if cred else p}"))
        for lab in _R4_CREDENTIAL + _R4_BENIGN:
            cred = lab in _R4_CREDENTIAL
            for shape in _R4_CONTEXTS:
                cases.append((shape.format(lab, "abc"), shape.format(lab, r if cred else "abc")))
        for text, expected in cases:
            clean = egress.sanitize(text)
            assert clean == expected, (text, clean, expected)
            assert egress.leaks_in(clean) == [], (text, clean)
            assert egress._file_leaks("calls.jsonl", json.dumps({"q": clean})) == [], text
            leaks = clean != text
            assert ("credential label" in egress.leaks_in(text)) is leaks, text
            # A/B: the locators agree on the input and on the output
            assert egress.labelled_value_leaks(text) == bool(egress._label_spans(text)), text
            assert egress.labelled_value_leaks(clean) is False and not egress._label_spans(clean)

    @pytest.mark.parametrize("value", _R4_PLACEHOLDERS)
    def test_r4_placeholders_by_the_rule(self, value: str) -> None:
        assert egress.is_placeholder(value), value

    @pytest.mark.parametrize("value", _R4_NOT_PLACEHOLDERS)
    def test_r4_values_by_the_rule(self, value: str) -> None:
        assert not egress.is_placeholder(value), value

    def test_r4_never_less_than_the_pre_round_3_rule(self) -> None:
        """Every label the 8cabcbe `KEY|TOKEN|SECRET|PASSWORD`-substring rule redacted (with an
        8+ character value) in the generated table is still redacted, except the documented
        exceptions. Substring-only hits (`MONKEY`, `keyboard`, `secretary`, `tokenizer`) are not
        in the table: the old rule over-redacted them, they name no credential."""
        import itertools

        old = re.compile(r"(?i)\b([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*[=:]\s*\S{8,}")
        value = "abcd1234efgh"
        checked, excepted = 0, set()
        for stem, form, case in itertools.product(
            ("key", "token", "secret", "password"), _R4_FORMS, ("lower", "upper", "camel")
        ):
            label = _r4_case(form.format(s=stem), case)
            for sep in ("=", ": "):
                text = f"{label}{sep}{value}"
                if not old.search(text):
                    continue
                checked += 1
                if _r4_old_rule_exception(label):
                    assert value in egress.sanitize(text), text  # the exception is real
                    excepted.add(label)
                else:
                    assert value not in egress.sanitize(text), text
                    assert egress.is_credential_name(label), label
        assert checked > 300 and excepted


# ---- Round 3, contract K2: what extract, repack, gist and ingest accept is total ---------------
#
# Invariant. (1) extract never writes where it reads: --out may not be, contain or sit inside an
# input directory (symlinks resolved), and may hold only files a previous extract wrote. (2) A value
# the producer cannot measure is null, never a value the contract rejects (latency). (3) Every
# validator is total: a hostile number (an integer too big for a float) is a row error, and ingest
# rejects that bundle and goes on. (4) The send-side verdict (`check_share`) re-renders report.md
# from the rows and manifest and refuses a stale one byte for byte. (5) A transcript field of the
# wrong JSON type never aborts extract.


def _transcript(path: Path, rows: list[dict[str, Any]]) -> Path:
    _jl(path, rows)
    return path


class TestExtractTargetK2:
    def _src(self, tmp_path: Path) -> Path:
        src = tmp_path / "transcripts"
        _hub_session(src / "sessions.jsonl")
        return src

    @pytest.mark.parametrize("where", ["same", "inside", "around", "symlink"])
    def test_f19_out_is_never_an_input(self, tmp_path: Path, where: str) -> None:
        """F19: `-i transcripts --out transcripts` skipped every input as output and then wrote an
        empty sessions.jsonl over the transcript of the same name."""
        src = self._src(tmp_path)
        before = (src / "sessions.jsonl").read_bytes()
        if where == "same":
            out = src
        elif where == "inside":
            out = src / "audit"
        elif where == "around":
            out = tmp_path
        else:
            out = tmp_path / "link"
            out.symlink_to(src, target_is_directory=True)
        result = _extract(src, out)
        assert result.exit_code == 1 and "input directory" in result.output, result.output
        assert (src / "sessions.jsonl").read_bytes() == before
        assert not (src / "share").exists() and not (src / "summary.json").exists()

    @pytest.mark.parametrize("where", ["out", "share"])
    def test_f19_out_holding_foreign_files_is_refused(self, tmp_path: Path, where: str) -> None:
        src = self._src(tmp_path)
        out = tmp_path / "out"
        (out / "share").mkdir(parents=True)
        foreign = (out if where == "out" else out / "share") / "notes.txt"
        foreign.write_text("mine")
        result = _extract(src, out)
        assert result.exit_code == 1 and "did not write" in result.output, result.output
        assert foreign.read_text() == "mine" and not (out / "summary.json").exists()

    def test_f19_a_previous_extract_may_be_overwritten(self, tmp_path: Path) -> None:
        src = self._src(tmp_path)
        out = tmp_path / "out"
        assert _extract(src, out).exit_code == 0
        assert _extract(src, out).exit_code == 0


class TestTotalValidatorsK2:
    @pytest.mark.parametrize(
        ("call_ts", "result_ts", "latency"),
        [("2026-11-18T10:00:05Z", "2026-11-18T10:00:01Z", None),
         ("2026-11-18T10:00:05Z", "2026-11-18T10:00:05Z", 0.0),
         ("2026-11-18T10:00:05Z", "2026-11-18T10:00:06.5Z", 1.5),
         ("2026-11-18T10:00:05Z", "", None), ("", "2026-11-18T10:00:05Z", None)],
    )  # fmt: skip
    def test_f2_latency_is_null_when_unmeasurable(
        self, tmp_path: Path, call_ts: str, result_ts: str, latency: float | None
    ) -> None:
        """F2: a result stamped before its call gave latency_s -4.0, which the contract rejects,
        so the whole extract failed with nothing written."""
        body = json.dumps({"hits": [], "evidence": {"confidence": 0.1}})
        _transcript(
            tmp_path / "in" / "a" / "s.jsonl",
            [_use(1, call_ts, "mcp__pipecat-context-hub__search_docs", {"query": "q"}),
             _result(1, result_ts, body)],
        )  # fmt: skip
        out = tmp_path / "out"
        result = _extract(tmp_path / "in", out)
        assert result.exit_code == 0, result.output
        (row,) = _rows(out / "share" / "calls.jsonl")
        assert row["latency_s"] == latency

    def test_f2_the_producer_owns_latency(self) -> None:
        assert audit_extract.latency("2026-11-18T10:00:05Z", "2026-11-18T10:00:01Z") is None
        assert audit_extract.latency(None, "2026-11-18T10:00:01Z") is None
        assert audit_extract.latency("2026-11-18T10:00:01Z", "2026-11-18T10:00:03Z") == 2.0

    @pytest.mark.parametrize("field", ["latency_s", "severity", "confidence", "n_hits"])
    def test_f22_a_huge_integer_is_a_row_error_never_a_crash(
        self, audited: Path, tmp_path: Path, field: str
    ) -> None:
        """F22: latency_s = 10**400 decoded fine, then math.isfinite raised OverflowError, which
        merge_bundles did not catch, so one hostile bundle aborted the whole ingest."""
        from pipecat_context_hub.audit import bundle

        f = audited / "share" / "calls.jsonl"
        first, *rest = f.read_text().splitlines()
        row = json.loads(first)
        if field in ("confidence", "n_hits"):
            row["result"][field] = 10**400
        else:
            row[field] = 10**400
        f.write_text("".join(line + "\n" for line in [json.dumps(row), *rest]))
        files = {n: (audited / "share" / n).read_bytes() for n in BUNDLE}
        try:
            bundle.load_files(files)
        except bundle.BundleError:
            pass  # a row error: the contract names it
        render_report(json.loads(files["manifest.json"]), [row])  # the renderer is total too
        tar = _tar(tmp_path / "bad.tar.gz", [(n, files[n], "f") for n in BUNDLE])
        good = tmp_path / "good"
        _hub_session(good / "in" / "s.jsonl")
        assert _extract(good / "in", good / "out").exit_code == 0
        merged = _merge([tar, good / "out" / "share.tar.gz"], tmp_path / "m")
        assert merged["report"][1]["status"] == "ok", merged["report"]
        if field == "latency_s":
            assert merged["report"][0]["status"] == "rejected"
            assert "latency_s" in merged["report"][0]["reason"]

    @pytest.mark.parametrize("ts", [1700000000, ["2026"], None, {"t": 1}, 1.5])
    def test_f12_a_non_string_timestamp_never_aborts_extract(self, tmp_path: Path, ts: Any) -> None:
        """F12: `--since` sliced the session start as a string; an int timestamp raised TypeError
        outside the per-file guard and the CLI printed a traceback."""
        use = _use(1, "x", "mcp__pipecat-context-hub__search_docs", {"query": "q"})
        use["timestamp"] = ts
        _transcript(tmp_path / "in" / "a" / "s.jsonl", [use])
        result = _extract(tmp_path / "in", tmp_path / "out", "--since", "2026-01-01")
        assert result.exit_code == 0, result.output
        assert audit_extract.parse_ts(ts) is None


class TestStaleReportK2:
    def _edit(self, out: Path, change: str) -> None:
        share = out / "share"
        if change == "query":
            f = share / "calls.jsonl"
            f.write_text(f.read_text().replace("projectbluebird", "XXXXXXXXXXXXXXX"))
        elif change == "flag":
            f = share / "calls.jsonl"
            first, *rest = f.read_text().splitlines()
            row = json.loads(first)
            row["flags"] = [*row["flags"], "zero_hits"] if "zero_hits" not in row["flags"] else []
            f.write_text("".join(line + "\n" for line in [json.dumps(row), *rest]))
        elif change == "report":
            f = share / "report.md"
            f.write_text(f.read_text() + "\nedited\n")

    @pytest.mark.parametrize("change", ["query", "flag", "report"])
    def test_f3_gist_refuses_a_report_the_rows_no_longer_render(
        self, tmp_path: Path, change: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F3: an edited row text without repack posted a report.md that still held the original
        text; only a row count change was noticed."""
        from pipecat_context_hub.audit.sharedir import STALE_REPORT

        _hub_session(tmp_path / "in" / "a" / "s.jsonl", "projectbluebird tts", confident=False)
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        self._edit(out, change)
        seen = _fake_gh(monkeypatch)
        refused = _invoke("gist", str(out), "--yes", "--allow-empty")
        assert refused.exit_code == 1 and not seen, refused.output
        assert "audit repack" in refused.output and STALE_REPORT[:20] in refused.output
        if change != "report":
            assert _invoke("repack", str(out)).exit_code == 0
            assert _invoke("gist", str(out), "--yes", "--allow-empty").exit_code == 0
            assert "projectbluebird" not in (out / "share" / "report.md").read_text() or (
                change == "flag"
            )


class TestJsExecutablePosition:
    C = 'tools.mcp__pipecat_context_hub__search_docs({query: "Acme"})'
    CASES = [  # (batch, counted, shared)
        (f"const r = await {C};", 1, 'search_docs({"query": "Acme"})'),
        (f"// {C}\nconst a = 1;", 0, ""),
        (f"/* {C} */ const a = 1;", 0, ""),
        ("console.log('tools.mcp__pipecat_context_hub__search_docs(x)');", 0, ""),
        ('console.log("tools.mcp__pipecat_context_hub__search_docs(x)");', 0, ""),
        (f"const s = `{C}`;", 0, ""),
        ("const re = /tools.mcp__pipecat_context_hub__search_docs[(]/;", 0, ""),
        (f"const s = `${{await {C}}}`;", 1, ""),
        (f"if (false) {{ await {C}; }}", 1, ""),
        (f"if (false) await {C};", 1, ""),
        (f"const r = x ? await {C} : null;", 1, ""),
        (f"const f = () => {C};", 1, ""),
        (f"x && {C};", 1, ""),
        (f"if (x) y();\nconst r = await {C};", 1, 'search_docs({"query": "Acme"})'),
        (f"const a = 4 / 2; const r = await {C};", 1, 'search_docs({"query": "Acme"})'),
        (f"const s = 'abc;\n{C}", 1, ""),  # unlexable: counted by the regex, nothing shared
    ]

    @pytest.mark.parametrize(("js", "count", "shared"), CASES)
    def test_f21_only_code_counts_and_only_a_sure_call_shares(
        self, js: str, count: int, shared: str
    ) -> None:
        found = audit_extract.detect_hub("exec", js)
        assert found == (("search_docs", "mcp_js", count) if count else (None, None, 0)), js
        assert audit_extract.hub_invocation(js, "mcp_js") == shared

    def test_f21_shell_text_is_never_read_as_javascript(self, tmp_path: Path) -> None:
        """F21: a Bash `printf` of JS text was labelled mcp_js and its quoted query shared."""
        cmd = (
            "printf '%s' 'tools.mcp__pipecat_context_hub__search_docs"
            '({query: "Acquisition-Acme-17m"})\''
        )
        for name in ("Bash", "exec", "shell"):
            assert audit_extract.detect_hub(name, {"command": cmd}) == (None, None, 0)
        rows = [_use(1, "2026-11-18T10:00:00Z", "Bash", {"command": cmd}),
                _result(1, "2026-11-18T10:00:01Z", "tools...")]  # fmt: skip
        _jl(tmp_path / "in" / "a" / "s.jsonl", rows)
        out = tmp_path / "out"
        assert _extract(tmp_path / "in", out).exit_code == 0
        shared = "".join((out / "share" / n).read_text() for n in BUNDLE)
        assert "Acquisition" not in shared and "Acme" not in shared


LAUNCHER_CASES = [  # (line, the hub argv it runs, or None for fail closed)
    ("command -v pipecat-context-hub status", []),
    ("command -V pipecat-context-hub status", []),
    ("command pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("command -p pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("env -u FOO pipecat-context-hub search-docs q",
     [["pipecat-context-hub", "search-docs", "q"]]),
    ("env -uFOO -i pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("env --unset=FOO pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("sudo -u bob pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("sudo -l pipecat-context-hub status", []),
    ("timeout -s TERM 5 pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("timeout 5s pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("nice -n 5 pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("nice -10 pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("time -p pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("uv run --with x pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("uv tool run pipecat-context-hub status", [["pipecat-context-hub", "status"]]),
    ("uvx --from pipecat-ai-context-hub pipecat-context-hub status",
     [["pipecat-context-hub", "status"]]),
    ("uv pip install pipecat-context-hub", []),
    ("env -S 'pipecat-context-hub status'", None),
    ("env --weird pipecat-context-hub status", None),
    ("sudo --weird pipecat-context-hub status", None),
    ("timeout -z 5 pipecat-context-hub status", None),
    ("uv --quiet run pipecat-context-hub status", None),
]  # fmt: skip


class TestLauncherGrammar:
    @pytest.mark.parametrize(("line", "runs"), LAUNCHER_CASES)
    def test_f14_f15_each_launcher_consumes_its_own_options(
        self, line: str, runs: list[list[str]] | None
    ) -> None:
        """F14: `command -v` was read as running its argument. F15: `env -u NAME` stopped the
        launcher walk and the hub call was dropped."""
        from pipecat_context_hub.audit.shell import ShellSyntaxError, shell_hub_calls

        if runs is None:
            with pytest.raises(ShellSyntaxError):
                shell_hub_calls(line)
            assert audit_extract.hub_invocation({"command": line}, "cli") == ""
            assert audit_extract.detect_hub("Bash", {"command": line})[0] is not None
        else:
            assert shell_hub_calls(line) == runs
            found = audit_extract.detect_hub("Bash", {"command": line})
            assert (found[0] is not None) == bool(runs)

    @pytest.mark.skipif(
        __import__("shutil").which("bash") is None or __import__("os").name == "nt",
        reason="needs a POSIX bash",
    )
    @pytest.mark.parametrize(
        "line",
        ["command -v pipecat-context-hub status", "command pipecat-context-hub status",
         "env -u FOO pipecat-context-hub search-docs q", "nice -n 5 pipecat-context-hub status",
         "env -i pipecat-context-hub status"],
    )  # fmt: skip
    def test_launcher_lines_agree_with_bash(self, tmp_path: Path, line: str) -> None:
        from pipecat_context_hub.audit.shell import shell_hub_calls

        ran = _bash_runs(line.replace("env -i ", "env -i PATH=$PATH HUB_LOG=$HUB_LOG "), tmp_path)
        assert shell_hub_calls(line) == [r for r in ran], (line, ran)


class TestRound3Hygiene:
    def test_f6_thresholds_and_outcomes_have_one_home(self) -> None:
        from pipecat_context_hub.audit import analysis, bundle, flags

        eff = bundle.ANALYSIS_SHAPE["efficiency"]
        assert analysis.SEARCH_ONLY_KEY in eff and analysis.STATUS_REPEAT_KEY in eff
        assert set(flags.READ_FIRST_OUTCOMES) <= set(flags.OUTCOMES)
        src = Path(analysis.__file__).read_text()
        assert ">= 3" not in src and '"failed", "suspect", "bypassed"' not in src

    def test_f7_tool_vocabularies_derive_from_one_place(self) -> None:
        from pipecat_context_hub.audit import shell

        # round 4 (F6): the groups live in egress only; extract keeps no alias of them
        standard = {n.lower() for n in egress.STANDARD_TOOLS}
        assert egress.SHELL_TOOLS | egress.READONLY_TOOLS | egress.JS_BATCH_TOOLS <= standard
        for alias in ("SHELLISH", "READONLY"):
            assert not hasattr(audit_extract, alias)
        assert getattr(audit_extract, "JS_BATCH_TOOLS") is egress.JS_BATCH_TOOLS
        tools = set(shell.HUB_TOOL_SUBCOMMANDS)
        assert shell.SEARCH_TOOLS | shell.FETCH_TOOLS | shell.VERSIONED_TOOLS <= tools
        assert shell.STATUS_TOOL in tools and shell.VERSIONED_TOOLS <= shell.SEARCH_TOOLS
        assert all(audit_extract.HUB_TOOLSEARCH_RE.search(t) for t in shell.SEARCH_TOOLS)

    def test_f8_the_output_lifecycle_lives_with_share_on_disk(self, tmp_path: Path) -> None:
        from pipecat_context_hub.audit import sharedir

        for name in ("refuse_unsafe_outputs", "generated_paths", "discard"):
            assert hasattr(sharedir, name) and not hasattr(audit_extract, f"_{name}")
        assert "is_symlink" not in Path(audit_extract.__file__).read_text()
        out = tmp_path / "out"
        out.mkdir()
        (out / "summary.json").write_text("{}")
        repacked = _invoke("repack", str(out))
        gisted = _invoke("gist", str(out))
        assert repacked.exit_code == gisted.exit_code == 1
        assert "share/ does not exist" in repacked.output and "share/ does not exist" in (
            gisted.output
        )

    def test_f1_f9_f10_f11_the_skill_says_what_the_code_does(self) -> None:
        root = Path(audit_extract.__file__).parents[3]
        skill = " ".join(
            (root / ".claude/skills/context-hub-session-audit/SKILL.md").read_text().split()
        )
        fields = " ".join(audit_extract.FIELDS_MD.split())
        for text in (skill, fields):  # F1: the rebuild never runs bundle text
            assert (
                "never text you read" in text and "arguments shown in `hub_invocation`" not in text
            )
        assert "Default transcript locations" not in skill and "no default input" in skill  # F9
        assert "first decodable JSON object" not in skill  # F10
        assert "response shape" in skill
        assert "Re-run the CLI rather than hand-editing" not in skill  # F11
        assert "run `audit repack` before sending" in skill


# ---- Round 4 fixer, contract K1: the label model is one grammar, and the tests come from it ------
#
# Invariant. For every form in `egress.LABEL_FORMS`, every credential label (each stem and whole
# credential word, in several spellings), every value shape the form carries and every place a
# value reaches a bundle (free text, a dict value, a rebuilt argv, a JSON document nested in a
# string), sanitize removes the value, its output passes the tripwire, sanitize is idempotent on it,
# and the tripwire flags the input. Both locators (sanitize's regex locator and the tripwire's char
# walk) agree on every input. A new form is one row in LABEL_FORMS.

_R4F_VALUES = {
    "word": "zq7Canary",
    "one": "Q",
    "punct": "P@ss/w0rd!$%+,;~",
    "spaced": "my zq7 phrase",
    "unicode": "pässwörd‑ßé",
}
_R4F_SPELLINGS = (
    lambda s: s,
    lambda s: s.upper(),
    lambda s: f"DB_{s.upper()}",
    lambda s: f"smtp-{s}",
    lambda s: f"my{s.capitalize()}",
)


def _r4f_labels() -> list[str]:
    stems = (*egress.CREDENTIAL_STEMS, *egress.CREDENTIAL_WORDS)
    return [_R4F_SPELLINGS[i % len(_R4F_SPELLINGS)](s) for i, s in enumerate(stems)] + list(stems)


def _r4f_cases(form: egress.LabelForm) -> list[tuple[str, str, str]]:
    """(text, value, expected free-text output) for every example x label x value shape."""
    cases = []
    for example in form.examples:
        template, expected = example if isinstance(example, tuple) else (example, None)
        labels = _r4f_labels() if "{label}" in template else [""]
        for i, label in enumerate(labels):
            for shape in form.values:
                if shape == "spaced" and "{label}" in template and i % 3:
                    continue  # bounded: every label sees the other shapes, a third see "spaced"
                value = _R4F_VALUES[shape]
                text = template.format(label=label, value=value)
                out = (expected or template).format(
                    label=label, value=egress.REDACTED, redacted=egress.REDACTED
                )
                cases.append((text, value, out))
    return cases


def _r4f_representations(text: str) -> list[Any]:
    return [
        {"input": {"query": text}},
        {"hub_invocation": egress.argv_text(["pipecat-context-hub", "search-docs", text])},
        {"q": json.dumps({"query": text}, ensure_ascii=False)},
    ]


class TestLabelFormsK1:
    def test_every_form_is_documented_and_has_examples(self) -> None:
        names = [f.name for f in egress.LABEL_FORMS]
        assert len(names) == len(set(names)) >= 16
        for form in egress.LABEL_FORMS:
            assert form.rule and form.examples and set(form.values) <= set(_R4F_VALUES)
        docs = (Path(audit_extract.__file__).parents[3] / "docs" / "cli.md").read_text()
        assert "egress.LABEL_FORMS" in docs and "**Limit.**" in docs

    @pytest.mark.parametrize("form", egress.LABEL_FORMS, ids=lambda f: f.name)
    def test_the_whole_space_of_a_form(self, form: egress.LabelForm) -> None:
        cases = _r4f_cases(form)
        assert cases
        for text, value, expected in cases:
            clean = egress.sanitize(text)
            assert clean == expected, (text, clean)
            assert egress.leaks_in(clean) == [], (text, clean)
            assert egress.sanitize(clean) == clean, text
            assert egress.leaks_in(text), text  # the tripwire sees the input
            assert egress.labelled_value_leaks(text) == bool(egress._label_spans(text)), text
            assert egress.labelled_value_leaks(clean) is False, (text, clean)
            for row in _r4f_representations(text):
                out = egress.sanitize(row)
                dumped = json.dumps(out, ensure_ascii=False)
                assert value not in dumped, (form.name, row, out)
                assert _json_clean(out), (row, out)
            # round 5 (K1, F2): the report rendered from the clean row passes the tripwire too
            row = egress.sanitize(_r5_flagged_row(text))
            assert _report_clean(render_report({}, [row])), (form.name, text)

    @pytest.mark.parametrize(
        "form", [f for f in egress.LABEL_FORMS if f.structured], ids=lambda f: f.name
    )
    def test_the_structured_space_of_a_form(self, form: egress.LabelForm) -> None:
        """Round 5 (K1, F13): a form that also has a parsed-object shape is redacted by sanitize's
        structured walk and refused by the tripwire's decoded walk, nested in a row too."""
        for template in form.structured:
            for label in _r4f_labels():
                for shape in form.values:
                    value = _R4F_VALUES[shape]
                    obj = json.loads(template.format(label=label, value=value))
                    for row in (obj, {"input": {"q": obj}}, [obj]):
                        clean = egress.sanitize(row)
                        assert value not in json.dumps(clean, ensure_ascii=False), (row, clean)
                        assert _json_clean(clean) and egress.sanitize(clean) == clean, clean
                        assert not _json_clean(row), row  # the tripwire refuses the hand edit
                        assert _report_clean(render_report({}, [_r5_flagged_row(clean)]))

    def test_benign_fixtures_are_unchanged_by_the_new_forms(self) -> None:
        kept = [
            "http://localhost:8080/x@y", "https://example.com/a@b", "mailto:a@b.c",
            "git@github.com:org/repo", 'os.getenv("API_KEY")', "bypass: yes", "author: me",
            "max_tokens: 4", "Authorization: Bearer $TOKEN", "tokenizer: v", "secretary: v",
            "password_hint: your pet", "--password-file ./pw.txt", 'environ["API_KEY"]',
        ]  # fmt: skip
        for text in kept:
            assert egress.sanitize(text) == text, text
            assert egress.leaks_in(text) == [], text
            assert egress.labelled_value_leaks(text) is False and egress._label_spans(text) == []


def _r5_flagged_row(query: Any) -> dict[str, Any]:
    """A share row that the report lists (flagged), carrying ``query``."""
    return {"ref": "r", "tool": "search_docs", "flags": ["zero_hits"], "result": {"n_hits": 0},
            "input": {"query": query}}  # fmt: skip


def _r4_gone(text: str, *secrets: str) -> None:
    clean = egress.sanitize(text)
    for secret in secrets:
        assert secret not in clean, (text, clean)
    assert egress.leaks_in(clean) == [], (text, clean)
    assert egress.leaks_in(text), text
    assert egress._file_leaks("calls.jsonl", json.dumps({"q": text})), text


class TestRound4FixerFindings:
    @pytest.mark.parametrize(
        "text", ["SMTP_PASS=Tr0ub4dor&3 auth fails", "DB_PASS=hunter2", "DB_AUTH: Tr0ub4dor&3"]
    )
    def test_f1_pass_and_auth_are_credential_words(self, text: str) -> None:
        _r4_gone(text, "Tr0ub4dor" if "Tr0ub4dor" in text else "hunter2")

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ("a=b&password=P@ss#w0rd", "w0rd"),
            ("a&password=correct horse battery", "horse"),
            ("https://h/p?a=1&password=correct horse&b=2", "horse"),
            ("https://h/p?password=P@ss/w0rd!#frag", "w0rd"),
        ],
    )
    def test_f2_a_query_value_runs_to_the_next_ampersand(self, text: str, secret: str) -> None:
        _r4_gone(text, secret)

    def test_f2_a_url_fragment_and_the_next_pair_are_kept(self) -> None:
        assert (
            egress.sanitize("https://h/p?password=x1#frag")
            == "https://h/p?password=<redacted>#frag"
        )
        assert egress.sanitize("?password=x1&b=2") == "?password=<redacted>&b=2"

    @pytest.mark.parametrize(
        "text",
        [
            'os.environ["DAILY_API_KEY"] = "my-real-key-value"',
            "headers['X-Api-Token'] = 'my-real-key-value'",
            "password:\n  my-real-key-value",
            "password:\\n  my-real-key-value",
        ],
    )
    def test_f3_subscript_assignment_and_next_line_values(self, text: str) -> None:
        _r4_gone(text, "my-real-key-value")

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ("connect postgres://admin:Ab/Cd+ef@db:5432/app", "Cd+ef"),
            ("redis://default:ab/cd==@r:6379", "cd=="),
            ("https://user@corp.com:Hunter2pw@host", "Hunter2pw"),
        ],
    )
    def test_f5_url_passwords_with_slash_or_at(self, text: str, secret: str) -> None:
        _r4_gone(text, secret)

    @pytest.mark.parametrize(
        "text",
        ["http://localhost:8080/x@y", "https://example.com/a@b", "ssh://git@github.com:22/x"],
    )
    def test_f5_a_port_or_a_path_at_sign_is_not_a_password(self, text: str) -> None:
        assert egress.sanitize(text) == text and egress.leaks_in(text) == []

    def test_f10_a_run_together_label_ending_in_a_credential_stem(self) -> None:
        _r4_gone("tokenizerkey: abcdefgh12", "abcdefgh12")
        assert egress.is_credential_name("tokenizerkey")
        assert not egress.is_credential_name("tokenizer")

    @pytest.mark.parametrize(
        "text",
        [
            "**password**: Hunter2", "`password`: Hunter2", "‘password’: Hunter2",
            "[password]: Hunter2", "login bob password Hunter2pw", "curl --user alice:Hunter2pw",
            "curl -u alice:Hunter2", "mysql -pHunter2pw", "DB_PASS=Hunter2",
            "password = Hunter2",
        ],
    )  # fmt: skip
    def test_f16_common_labelled_forms(self, text: str) -> None:
        _r4_gone(text, "Hunter2")

    @pytest.mark.parametrize(
        "text",
        [
            "**password**: violet-marmalade-27",
            "`password`: violet-marmalade-27",
            'password = """violet-marmalade-27"""',
            "password: |\n  violet-marmalade-27",
        ],
    )
    def test_f20_f21_config_and_markdown_forms_never_reach_the_bundle(
        self, tmp_path: Path, text: str
    ) -> None:
        _r4_gone(text, "violet-marmalade-27")
        src = tmp_path / "in"
        _hub_session(src / "alice" / "s.jsonl", query=text)
        out = tmp_path / "out"
        assert _extract(src, out).exit_code == 0
        share = b"".join(p.read_bytes() for p in sorted((out / "share").iterdir()))
        assert b"violet-marmalade-27" not in share
        assert b"violet-marmalade-27" not in (out / "share.tar.gz").read_bytes()
        assert _invoke("repack", str(out)).exit_code == 0


class TestJsLexerK2:
    C = 'tools.mcp__pipecat_context_hub__search_docs({query: "q"})'
    SHARED = 'search_docs({"query": "q"})'

    @pytest.mark.parametrize(
        ("js", "count", "shared"),
        [
            # F12: `/` after a postfix `++` divides
            ('let n = 0; n++ / 2; tools.pipecat_context_hub__search_docs({query: "q"}); y = 1 / 3',
             1, SHARED),
            (f"i-- /2/ 3; {C}", 1, SHARED),
            (f"a / b / c; {C}", 1, SHARED),
            (f"x = a.b / 2; {C}", 1, SHARED),
            (f"f(/re/); x = /re/; [a, /re/]; {C}", 1, SHARED),
            # F17: a string after a division is a string, so its text is no call
            ("let y = i++ / 2; let s = \"/ ; tools.mcp__pipecat-context-hub__search_docs("
             "{query: 'my private draft text'}); \"; let t = \"\"; // \"", 0, ""),
            # F22: `/` after an `if (...)` header (or after `}`) is undecidable: nothing shared
            ('if (false) /;tools.mcp__pipecat_context_hub__search_docs('
             '{"query":"confidential merger discussion"})/;', 1, ""),
            (f"if (x) {{}}\n/re/.test(y); {C}", 1, ""),
            (f"x = {{a: 1}} / 2; {C}", 1, ""),
            (f"i\n++ / 2; {C}", 1, ""),
            # F13: a loop body, parentheses and a label do not surely run
            (f"for (let i = 0; i < n; i++) {C}", 1, ""),
            (f"while (x) {C}", 1, ""),
            (f"({C})", 1, ""),
            (f"f({C})", 1, ""),
            (f"label: {C}", 1, ""),
            (f"x ?? {C}", 1, ""),
            (f"const [a, b] = await Promise.all([{C}, {C}]);", 2, SHARED),
            (f"await {C}", 1, SHARED),
        ],
    )  # fmt: skip
    def test_regex_division_and_surely_runs(self, js: str, count: int, shared: str) -> None:
        found = audit_extract.detect_hub("exec", js)
        assert found == (("search_docs", "mcp_js", count) if count else (None, None, 0)), js
        assert audit_extract.hub_invocation(js, "mcp_js") == shared, js

    def test_regex_literal_content_never_reaches_an_invocation(self) -> None:
        secret = "confidential merger discussion"
        for prefix in ("if (a) ", "while (a) ", "for (;;) ", "{} ", "x = {} ", "do {} while (a) "):
            js = f'{prefix}/;tools.mcp__pipecat_context_hub__search_docs({{"query":"{secret}"}})/;'
            assert secret not in audit_extract.hub_invocation(js, "mcp_js"), js


class TestExtractTargetAndLabelK2:
    def test_f15_f18_a_case_flipped_out_inside_an_input_is_refused(self, tmp_path: Path) -> None:
        src = tmp_path / "transcripts"
        _hub_session(src / "sessions.jsonl")
        if not (tmp_path / "TRANSCRIPTS").exists():
            pytest.skip("case-sensitive filesystem")
        before = (src / "sessions.jsonl").read_bytes()
        for out in (tmp_path / "TRANSCRIPTS" / "out", tmp_path / "TRANSCRIPTS"):
            result = _extract(src, out)
            assert result.exit_code == 1 and "input directory" in result.output, result.output
        assert (src / "sessions.jsonl").read_bytes() == before
        assert not (src / "out").exists() and not (src / "share").exists()

    def test_f15_f18_identity_not_spelling_decides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A simulated case-insensitive volume: two spellings, one directory."""
        import os

        from pipecat_context_hub.audit import sharedir

        src = tmp_path / "transcripts"
        src.mkdir()
        real_stat = os.stat

        def folded_stat(p: Any, *a: Any, **k: Any) -> os.stat_result:
            return real_stat(os.fspath(p).replace("TRANSCRIPTS", "transcripts"), *a, **k)

        monkeypatch.setattr(os, "stat", folded_stat)  # sharedir reads os.stat at call time
        for out in (tmp_path / "TRANSCRIPTS" / "out", tmp_path / "TRANSCRIPTS"):
            with pytest.raises(ValueError, match="input directory"):
                sharedir.refuse_extract_target(out, [src])
        sharedir.refuse_extract_target(tmp_path / "elsewhere", [src])  # unrelated: allowed

    def test_f14_codex_date_directories_are_not_participants(self, tmp_path: Path) -> None:
        rel = Path("2026/11/17/rollout-a.jsonl")
        assert audit_extract.participant_label(Path("/x/sessions"), rel) == "sessions"
        assert audit_extract.participant_label(Path("/x/in"), Path("alice/s.jsonl")) == "alice"
        assert audit_extract.participant_label(Path("/x/p"), Path("sid/subagents/a.jsonl")) == "p"
        assert audit_extract.participant_label(Path("/x/p"), Path("s.jsonl")) == "p"
        codex = tmp_path / "sessions"
        call = {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": 'pipecat-context-hub search-docs "TTS"'}),
        }
        for day in ("2025/12/31", "2026/01/01"):
            _jl(codex / day / "rollout-a.jsonl", TestOtherHarnesses._codex(call, '{"hits": []}'))
        claude = tmp_path / "proj"
        _hub_session(claude / "s.jsonl")
        out = tmp_path / "out"
        result = _extract(codex, out, "-i", str(claude))
        assert result.exit_code == 0, result.output
        summary = json.loads((out / "summary.json").read_text())
        assert summary["participants"] == 2
        assert {r["participant"] for r in _rows(out / "sessions.jsonl")} == {"sessions", "proj"}


class TestRound4FixerHygiene:
    def test_f4_f11_the_skill_says_what_is_shared_and_where_the_digest_is(self) -> None:
        root = Path(audit_extract.__file__).parents[3]
        skill = " ".join(
            (root / ".claude/skills/context-hub-session-audit/SKILL.md").read_text().split()
        )
        assert "JS batches (only the one call expression survives)" not in skill
        assert "rebuilt from its parsed argv" in skill and "surely runs" in skill
        assert "share.tar.gz.sha256" in skill.split("**Tarball:**")[1].split("**Discard:**")[0]
        assert "outside that grammar is not detected" in skill

    def test_f7_one_report_recipe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from pipecat_context_hub.audit import sharedir

        assert getattr(audit_extract, "expected_report") is sharedir.expected_report
        assert not hasattr(audit_extract, "_report_bytes")
        assert "render_report(" not in Path(audit_extract.__file__).read_text()
        assert Path(sharedir.__file__).read_text().count("render_report(") == 1
        src = tmp_path / "in"
        _hub_session(src / "alice" / "s.jsonl")
        out = tmp_path / "out"
        assert _extract(src, out).exit_code == 0
        files = {p.name: p.read_bytes() for p in (out / "share").iterdir()}
        assert files["report.md"] == sharedir.expected_report(files)
        monkeypatch.setattr(sharedir, "expected_report", lambda _f: b"other\n")
        with pytest.raises(Exception, match="report.md"):
            sharedir.check_share(files)

    @pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_BUNDLE_BYTES"])
    def test_f8_ingest_reads_the_bundle_limits_at_call_time(
        self, audited: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
    ) -> None:
        from pipecat_context_hub.audit import bundle, ingest

        for alias in ("MAX_MEMBER_BYTES", "MAX_DECOMPRESSED_BYTES"):
            assert not hasattr(ingest, alias)
        monkeypatch.setattr(bundle, limit, 64)
        (rejected,) = _merge([audited / "share.tar.gz"], tmp_path / "m")["report"]
        assert rejected["status"] == "rejected", rejected

    def test_f9_sharedir_names_every_role_and_spells_share_once(self) -> None:
        from pipecat_context_hub.audit import sharedir

        doc = " ".join((sharedir.__doc__ or "").split())
        for role in ("read_share", "SafeDir", "check_share", "expected_report",
                     "refuse_extract_target", "discard", "tar_bytes", "gist_plan", "staged",
                     "SHARE_DIR"):  # fmt: skip
            assert role in doc, role
        for mod in (sharedir, audit_extract):
            assert Path(str(mod.__file__)).read_text().count('"share"') <= (mod is sharedir), mod

    def test_f19_gh_through_a_relative_path_entry_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from pipecat_context_hub import cli_audit

        planted = tmp_path / "checkout" / "node_modules" / ".bin" / "gh"
        planted.parent.mkdir(parents=True)
        planted.write_text("#!/bin/sh\nexit 0\n")
        planted.chmod(0o755)
        monkeypatch.chdir(tmp_path / "checkout")
        monkeypatch.setenv("PATH", os.pathsep.join(["node_modules/.bin", "/usr/bin", "/bin"]))
        with pytest.raises(SystemExit):
            cli_audit._resolve_gh()
        trusted = tmp_path / "bin" / "gh"
        trusted.parent.mkdir()
        trusted.write_text("#!/bin/sh\nexit 0\n")
        trusted.chmod(0o755)
        monkeypatch.setenv("PATH", os.pathsep.join([str(trusted.parent), "/usr/bin"]))
        assert cli_audit._resolve_gh() == str(trusted)


# ---- Round 5 fixer: one regression per finding (K1 grammar and closure, K2 lexer and labels) -----


def _r5_extract(tmp_path: Path, query: str) -> Path:
    src = tmp_path / "in"
    _hub_session(src / "alice" / "s.jsonl", query=query, confident=False)
    out = tmp_path / "out"
    result = _extract(src, out)
    assert result.exit_code == 0, result.output
    return out


def _r5_shared(out: Path) -> bytes:
    return (
        b"".join((out / "share" / n).read_bytes() for n in BUNDLE)
        + (out / "share.tar.gz").read_bytes()
    )


_R5_JS_CALL = 'tools.mcp__pipecat_context_hub__search_docs({query: "secret q"})'


class TestRound5Fixer:
    @pytest.mark.parametrize(
        "query", ["how to set password: \\n", '{"api_key": \\n', "token=\\n", "secret: \\n  "]
    )
    def test_f2_an_empty_next_line_value_never_crosses_a_cell(
        self, tmp_path: Path, query: str
    ) -> None:
        """F2: the escaped-newline value ran past ` | ` into the next cell: a clean row, a report
        the tripwire refused, and the whole extract aborted."""
        row = _r5_flagged_row(query)
        assert _json_clean(row)
        assert _report_clean(render_report({}, [row]))
        assert _verify_share(_r5_extract(tmp_path, query) / "share", tuple(BUNDLE)) == []

    def test_f2_closure_over_labels_breaks_and_separators(self) -> None:
        """Invariant: for every text sanitize accepts, the report rendered from it passes the
        tripwire; label x separator x break x tail."""
        import itertools

        labels = ["password", "api_key", "token", '"secret"', "**pw**", "Authorization", "Cookie"]
        seps = [": ", "=", " => ", " → ", "\uff1a", ": |", ": >-"]
        breaks = ["", "\\n", "\\r", "\\n  ", "\\n\\n", "\n", "\n  ", "\r\n", " ", "  "]
        tails = ["", " | x", " · y", " ↦ z", " |", "|", " …", "\\n | w"]
        for lab, sep, br, tail in itertools.product(labels, seps, breaks, tails):
            text = f"{lab}{sep}{br}{tail}"
            for q in (text, egress.sanitize(text)):
                row = _r5_flagged_row(q)
                if _json_clean(row):
                    report = render_report({}, [row])
                    assert _report_clean(report), (text, egress._file_leaks("report.md", report))

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ("Proxy-Authorization: Basic  dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("Authorization: Bearer  abcSECRET123", "abcSECRET123"),
            ("Cookie: theme=dark;  sessionid=abcSECRET123", "abcSECRET123"),
        ],
    )
    def test_f3_blank_runs_after_a_scheme_or_inside_a_cookie(self, text: str, secret: str) -> None:
        _r4_gone(text, secret)
        assert not _json_clean({"input": {"query": text}})

    def test_f3_the_whole_space_of_blank_runs(self) -> None:
        import itertools

        runs = [c * k for c in (" ", "\t", "\u00a0") for k in range(1, 6)]
        schemes = ("Basic", "Bearer", "Token", "ApiKey", "Digest", "NTLM")
        for label, scheme, run in itertools.product(
            ("Authorization", "Proxy-Authorization", "X-Auth"), schemes, runs
        ):
            _r4_gone(f"{label}: {scheme}{run}zq7Canary", "zq7Canary")
        for run in runs:
            _r4_gone(f"Cookie: a=b;{run}sid=zq7Canary", "zq7Canary")
            _r4_gone(f"Set-Cookie: sid=zq7Canary;{run}Path=/", "zq7Canary")

    def test_f4_one_user_flag_list_drives_both_locators(self) -> None:
        source = Path(egress.__file__).read_text()
        for flag in egress.USER_FLAGS:
            assert source.count(f'"{flag}"') == 1, flag  # spelled once: in USER_FLAGS
            texts = [f"curl {flag} alice:zq7Canary h"]
            texts += [f"curl {flag}alice:zq7Canary h"] if len(flag) == 2 else []
            texts += [f"curl {flag}=alice:zq7Canary"] if len(flag) > 2 else []
            for text in texts:
                assert egress._label_spans(text), text  # locator A
                assert egress.labelled_value_leaks(text), text  # locator B
                assert "zq7Canary" not in egress.sanitize(text)

    def test_f5_no_unreachable_separator_and_fullwidth_still_redacted(self, tmp_path: Path) -> None:
        import unicodedata

        for ch in egress._SEP_CHARS:  # both locators read the NFKC view: a folded char is dead
            assert unicodedata.normalize("NFKC", ch) == ch, ch
        names = {f.name: f for f in egress.LABEL_FORMS}
        assert not any("\uff1a" in str(e) for e in names["separator"].examples)
        assert any("\uff1a" in str(e) for e in names["unicode spelling"].examples)
        for query in ("password\uff1ahunter2xyz", "api_key\uff1dhunter2xyz", "pw\ufe55hunter2xyz"):
            assert b"hunter2xyz" not in _r5_shared(_r5_extract(tmp_path / query[:3], query))

    @pytest.mark.parametrize(
        "prefix",
        ['throw new Error("x");\n', "return;\n", "break;\n", "continue;\n", "process.exit(0);\n",
         "await f(); return 1; "],
    )  # fmt: skip
    def test_f9_an_earlier_top_level_terminator_ends_surely_runs(self, prefix: str) -> None:
        js = f"{prefix}await {_R5_JS_CALL};"
        assert audit_extract.detect_hub("exec", js) == ("search_docs", "mcp_js", 1)
        assert audit_extract.hub_invocation(js, "mcp_js") == "", js
        # a terminator inside a function body is not top level
        ok = f"const f = async () => {{ {prefix} }};\nawait {_R5_JS_CALL};"
        assert "secret q" in audit_extract.hub_invocation(ok, "mcp_js"), ok

    @pytest.mark.parametrize(
        ("root", "rel"),
        [("sessions/2026", "10/03/a.jsonl"), ("sessions/2026", "11/16/b.jsonl"),
         ("sessions/2026/11", "03/a.jsonl"), ("sessions/2026/11/17", "a.jsonl"),
         ("sessions", "2026/11/17/a.jsonl")],
    )  # fmt: skip
    def test_f10_a_codex_date_input_is_one_participant(self, root: str, rel: str) -> None:
        label = audit_extract.participant_label(Path("/x/.codex") / root, Path(rel))
        assert label == "sessions", (root, rel, label)

    def test_f10_relative_date_input_and_other_layouts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "sessions" / "2026").mkdir(parents=True)
        monkeypatch.chdir(tmp_path / "sessions")
        assert audit_extract.participant_label(Path("2026"), Path("11/17/a.jsonl")) == "sessions"
        assert audit_extract.participant_label(Path("/x/2026"), Path("bob/s.jsonl")) == "bob"
        assert audit_extract.participant_label(Path("/x/in"), Path("alice/s.jsonl")) == "alice"
        codex = tmp_path / "sessions" / "2026"
        call = {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": 'pipecat-context-hub search-docs "TTS"'}),
        }
        for day in ("10/03", "11/16"):
            _jl(codex / day / "rollout-a.jsonl", TestOtherHarnesses._codex(call, '{"hits": []}'))
        out = tmp_path / "out"
        assert _extract(codex, out).exit_code == 0
        assert json.loads((out / "summary.json").read_text())["participants"] == 1

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ('{"name":"API_KEY","value":"s3cr3tVALUE"}', "s3cr3tVALUE"),
            ("- name: API_KEY\n  value: hunter2xyz", "hunter2xyz"),
            ("<password>hunter2xyz</password>", "hunter2xyz"),
            ("CREATE USER bob IDENTIFIED BY 'hunter2xyz'", "hunter2xyz"),
            ("export PGPASSWORD hunter2xyz", "hunter2xyz"),
            ("password is hunter2xyz", "hunter2xyz"),
            ("sshpass -p hunter2xyz", "hunter2xyz"),
            ('<add key="ApiKey" value="hunter2xyz"/>', "hunter2xyz"),
            ("api_key → hunter2xyz", "hunter2xyz"),
            ("password→hunter2xyz", "hunter2xyz"),
            ("set password hunter2xyz", "hunter2xyz"),
            ("api_key: your-dog-rex", "your-dog-rex"),
        ],
    )
    def test_f11_structured_and_prose_forms(self, text: str, secret: str) -> None:
        _r4_gone(text, secret)
        assert egress.sanitize("api_key: YOUR_API_KEY") == "api_key: YOUR_API_KEY"

    def test_f12_every_line_of_a_yaml_block_is_redacted(self, tmp_path: Path) -> None:
        query = "api_key: |\n  CedarMoon884!\n  LakeStone992!"
        assert egress.sanitize(query) == "api_key: |\n  <redacted>"
        shared = _r5_shared(_r5_extract(tmp_path, query))
        assert b"CedarMoon884" not in shared and b"LakeStone992" not in shared

    def test_f13_a_name_value_pair_is_redacted_as_an_object(self, tmp_path: Path) -> None:
        assert egress.sanitize({"name": "API_KEY", "value": "CedarMoon884!"}) == {
            "name": "API_KEY", "value": egress.REDACTED,
        }  # fmt: skip
        assert not _json_clean({"name": "API_KEY", "value": "CedarMoon884!"})
        assert egress.sanitize({"name": "voice", "value": "alloy"}) == {
            "name": "voice", "value": "alloy",
        }  # fmt: skip
        query = '{"env":[{"name":"API_KEY","value":"CedarMoon884!"}]}'
        assert b"CedarMoon884" not in _r5_shared(_r5_extract(tmp_path, query))

    def test_f14_schema_md_is_checked_whole(self, audited: Path) -> None:
        from pipecat_context_hub.audit import sharedir

        files = {n: (audited / "share" / n).read_bytes() for n in BUNDLE}
        assert egress.verify_files(files) == []
        files["SCHEMA.md"] += b"\napi_key:\n  CedarMoon884!\n"
        assert egress.verify_files(files) == ["SCHEMA.md: credential label"]
        with pytest.raises(Exception, match="SCHEMA.md"):
            sharedir.check_share(files)

    @pytest.mark.parametrize(
        "js",
        [
            'let n = 1; n /*\n*/ ++ /tools.mcp__pipecat_context_hub__search_docs({query:"PrivateProject"})/.lastIndex;',
            'let n = 1; n /*\r\n*/ ++ /tools.mcp__pipecat_context_hub__search_docs({query:"PrivateProject"})/.lastIndex;',
            'let n = 1; n /*\u2028*/ ++ /tools.mcp__pipecat_context_hub__search_docs({query:"PrivateProject"})/.lastIndex;',
            'let n = 1; n /*\u2029*/ ++ /tools.mcp__pipecat_context_hub__search_docs({query:"PrivateProject"})/.lastIndex;',
            'n // c\r++ /tools.mcp__pipecat_context_hub__search_docs({query:"PrivateProject"})/;',
            'n // c\u2028++ /tools.mcp__pipecat_context_hub__search_docs({query:"PrivateProject"})/;',
        ],
    )  # fmt: skip
    def test_f15_a_line_break_inside_a_comment_is_a_line_break(self, js: str) -> None:
        assert "PrivateProject" not in audit_extract.hub_invocation(js, "mcp_js"), js

    @pytest.mark.parametrize(
        ("js", "shared"),
        [
            # a comment holding `/`, then a call on the next line
            (f"// see a/b/c\nawait {_R5_JS_CALL};", True),
            (f"/* a/b */ await {_R5_JS_CALL};", True),
            # a regex after a comment
            (f"/* c */ x = /re/.test(y);\nawait {_R5_JS_CALL};", True),
            (f"// c\r\nawait {_R5_JS_CALL};", True),
            (f"// c\u2028await {_R5_JS_CALL};", True),
            # a regex literal cannot span a line terminator: unlexable, nothing shared
            (f"x = /a\nb/; await {_R5_JS_CALL};", False),
            (f"x = /a\u2028b/; await {_R5_JS_CALL};", False),
        ],
    )
    def test_f15_comment_and_regex_line_ends(self, js: str, shared: bool) -> None:
        assert audit_extract.detect_hub("exec", js)[0] == "search_docs", js
        assert ("secret q" in audit_extract.hub_invocation(js, "mcp_js")) is shared, js

    def test_fuzz_found_a_cell_boundary_is_not_a_yaml_block_indicator(self) -> None:
        """Round 5 fuzz: a tool cell ending `key: ` before a query cell starting with `\\n  x`
        read as `key: | \\n  x` (a YAML block across the cell boundary)."""
        row = {**_r5_flagged_row("\\n  (@.-x"), "tool": "a1·key: "}
        assert _json_clean(row) and _report_clean(render_report({}, [row]))
        assert egress.report_units("| a | b\\|c |\n- d") == ["", " a ", " b|c ", "", "- d"]

    def test_fuzz_found_a_pair_never_spans_two_rendered_values(self) -> None:
        """Round 5 fuzz: an open-quoted `value` in one list item closed by a quote in the next
        item, whose text then held `name: API_KEY`, made a name/value pair the row lacked."""
        items = ['value: "abc', '" name: API_KEY']
        assert all(_json_clean(i) for i in items)
        assert egress.leaks_in(egress.value_text(items)) == []
        manifest = {"analysis": {}, "harnesses": {"x": items}}
        assert _report_clean(render_report(manifest, []))
        assert egress.leaks_in('value: "abc" name: API_KEY')  # one value: a pair

    def test_fuzz_found_a_line_end_pipe_ends_a_segment_in_the_row_too(self) -> None:
        """Round 5 fuzz: `password X |` then a line break rendered as `password X | · `, a
        separator the row text did not end a segment at, so netrc fired only in the report."""
        key = "password OPENAI_API_KEY_2C:<redacted> |\n  "
        assert egress.leaks_in(key) == ["credential label"]  # the row sees the report's segment
        assert egress.leaks_in(egress.sanitize(key)) == []
        manifest = egress.sanitize({"harnesses": {"<path>": {key: "x"}}})
        assert _report_clean(render_report(manifest, []))


class TestRound5FixerHygiene:
    ROOT = Path(audit_extract.__file__).parents[3]

    def _doc(self, rel: str) -> str:
        return " ".join((self.ROOT / rel).read_text().split())

    def test_f1_the_participant_label_rule_is_documented(self) -> None:
        doc = " ".join((audit_extract.__doc__ or "").split())
        assert "first path component under an input root is the participant" not in doc
        for text in (doc, self._doc("docs/cli.md"), self._doc(
            ".claude/skills/context-hub-session-audit/SKILL.md"
        )):  # fmt: skip
            assert "YYYY/MM/DD" in text and "subagents" in text, text[:80]
            assert "not a date component" in text

    def test_f6_f7_f8_cli_doc_states_the_full_rules(self) -> None:
        cli = self._doc("docs/cli.md")
        assert "relative `PATH` entry" in cli  # F6
        assert "not inside a block, branch or function)" not in cli  # F7
        for part in ("parentheses", "labelled", "top-level `throw`", "Promise.all"):
            assert part in cli, part
        assert "the password running to the last `@`" not in cli  # F8
        assert "scheme and user are not kept" in cli and "zero-width" in cli
