"""Tests for ``pipecat-context-hub audit`` (extract, share bundle, repack, gist, discard, ingest).

Transcripts are synthetic. The leak tests freeze the secret, token and home-path shapes the share
bundle must never carry; the hostile-tarball cases freeze what ``ingest`` must reject.
"""

from __future__ import annotations

import hashlib
import io
import json
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

    def test_output_inside_the_input_is_not_read_back(self, tmp_path: Path) -> None:
        src = tmp_path / "in"
        _hub_session(src / "alice" / "s1.jsonl")
        first = json.loads(_extract(src, src / "audit").stdout)
        second = json.loads(_extract(src, src / "audit").stdout)
        assert first["sessions"] == second["sessions"] == 1

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
            assert leaks_in(blob) == [], text
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
    "a token: is",
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
        assert egress.verify_share(share, (sink,)) == []
        assert payload not in _decoded_text(share / sink)
        for encoded in (json.dumps(row), _escape_all(row)):  # a hand edit, plain or escaped
            _write_sink(share, sink, encoded)
            assert egress.verify_share(share, (sink,)), (rep, sink, encoded[:40])

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
        assert egress.verify_share(share, ("report.md",)) == []
        assert payload not in report
        _write_sink(share, "report.md", f"| q | {text} |")  # a hand edit to report.md
        assert egress.verify_share(share, ("report.md",))

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
        assert egress.verify_share(share, (sink,)) == []
        if rep != "credential-key":
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
        assert egress.verify_share(out / "share", tuple(BUNDLE)) == []

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
        assert egress.verify_share(share, ("calls.jsonl",)) == ["calls.jsonl: credential field"]

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
        assert egress.verify_share(share, ("calls.jsonl",))  # json.loads keeps only the last
        _write_sink(share, "sessions.jsonl", "not json")
        assert egress.verify_share(share, ("sessions.jsonl",)) == [
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
        assert egress.verify_share(tmp_path / "share", ("report.md",))

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

        from pipecat_context_hub.audit.analysis import SEARCH_TOOLS
        from pydantic import BaseModel

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
    from pipecat_context_hub.audit.bundle import CALL_KEYS, SESSION_KEYS

    cells: dict[str, tuple[str, Any]] = {}
    for k in sorted(CALL_KEYS):
        cells[f"call-missing-{k}"] = (
            "calls.jsonl",
            lambda r, k=k: {x: v for x, v in r.items() if x != k},
        )
    for k in sorted(SESSION_KEYS):
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
        assert egress.verify_share(audited / "share", ("calls.jsonl",)) == []  # nothing to leak
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
        from pipecat_context_hub.audit import ingest

        monkeypatch.setattr(ingest, "MAX_DECOMPRESSED_BYTES", 2048)
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
        assert set(flags.FLAG_GLOSSARY) == set(flags.SEVERITY) == set(flags.FLAGS)
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

_FUZZ_ALPHABET = "abcXYZ019 .,_-/" + "|\\\n:@=\"'$()" + "\t\r·→…<>`;{}[]#~%"
_FUZZ_FRAGMENTS = [
    *(t for t, _ in SECRET_SHAPES), *BENIGN, "password", "TOKEN=", "MONKEY=", "api_key",
    "--api-key ", "--password=", "--password", "Authorization: Basic ", "Bearer ", "C:\\",
    "/tmp/", "/Users/", "Users", "sk-", "0123456789abcdef", "<redacted>", "<user>", "<path>",
    "\\|", " · ", " → ", " | ", "MONKEY=a|b|c|d", "s:\n", "eyJ", "AKIA", "@", "://", "u:p@h",
    '"', "'", "abcdefgh", "-----BEGIN RSA PRIVATE KEY-----",
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


def _json_clean(v: Any) -> bool:
    return egress._file_leaks("calls.jsonl", json.dumps(v)) == []


def _report_clean(text: str) -> bool:
    return egress._file_leaks("report.md", text) == []


class TestRenderClosure:
    @pytest.mark.parametrize("seed", range(4))
    def test_sanitize_and_render_are_closed_over_generated_values(self, seed: int) -> None:
        """Property: sanitize-closure, render-closure, value_text-closure and the cell identity."""
        import random

        from pipecat_context_hub.audit import flags

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
                "flags": rng.sample(names, rng.randint(1, 3)),
                "severity": 1,
                "result": {"n_hits": n_hits, "confidence": _fuzz_value(rng)},
                "followups": [],
                "input": {"query": query, "path": _fuzz_value(rng)},
                "hub_invocation": _fuzz_text(rng),
                "latency_s": rng.choice([None, rng.random() * 20]),
            }
            clean = egress.sanitize(row)
            assert _json_clean(clean), clean  # closure under sanitize
            manifest = egress.sanitize({"harnesses": _fuzz_value(rng)})
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
        assert egress.verify_share(out / "share", tuple(BUNDLE)) == []

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
        assert egress.verify_share(out / "share", tuple(BUNDLE)) == []
        # headers render dicts the same way, never as a Python dict repr
        assert "Calls by tool: search_docs → 1" in report

    def test_the_number_rule_is_the_same_on_both_sides(self, tmp_path: Path) -> None:
        """Q2: numbers were scanned by neither side, so a 32+ digit integer passed both."""
        big = 12345678901234567890123456789012345678
        out = egress.sanitize({"n": big, "small": 7, "max": 2**63, "f": 1e40, "b": True})
        assert out == {"n": egress.REDACTED, "small": 7, "max": 2**63, "f": 1e40, "b": True}
        share = tmp_path / "share"
        _write_sink(share, "calls.jsonl", json.dumps(out))
        assert egress.verify_share(share, ("calls.jsonl",)) == []
        _write_sink(share, "calls.jsonl", json.dumps({"input": {"n": big}}))  # a hand edit
        assert egress.verify_share(share, ("calls.jsonl",)) == ["calls.jsonl: secret pattern 7"]
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
        assert egress.verify_share(share, ("calls.jsonl",))
        _write_sink(share, "report.md", f"| q | {text} |")
        assert egress.verify_share(share, ("report.md",))

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
        from pipecat_context_hub.audit import analysis, shell

        assert audit_extract.HUB_TOOLS == tuple(shell.HUB_TOOL_SUBCOMMANDS)
        assert set(audit_extract.HUB_RESULT_KEY) == set(shell.HUB_TOOL_SUBCOMMANDS)
        assert analysis.SEARCH_TOOLS <= set(shell.HUB_TOOL_SUBCOMMANDS)
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
