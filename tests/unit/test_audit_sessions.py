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

from pipecat_context_hub.audit import egress
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
        sessions.write_text(sessions.read_text() + json.dumps({"note": SECRET}) + "\n")
        args = ("gist", str(audited), "--yes") if cmd == "gist" else ("repack", str(audited))
        refused = _invoke(*args)
        assert refused.exit_code == 1 and "sessions.jsonl" in refused.output
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
