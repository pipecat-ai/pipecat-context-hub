"""Tests for the repo catalog health and curation scripts (no network).

Both scripts are loaded by path (the ``test_probe_mcp_2x.py`` pattern): they are
not part of the wheel. Every GitHub answer is an injected ``httpx`` response or
``httpx.MockTransport``.

Fixture provenance: the private/archived shapes below were observed on
2026-10-16 (owner ``pipecat-ai``). The 301 redirect SHAPE is real (verified
against ``joyent/node``: ``Location: https://api.github.com/repositories/{id}``,
not a ``/repos/owner/name`` URL), but the second-hop 200 body, the deleted 404,
the ``full_name`` rename beyond case and the case-only difference are
SYNTHETIC.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "catalog-health.yml"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_JUSTFILE = _REPO_ROOT / "justfile"
_CATALOG = _REPO_ROOT / "src" / "pipecat_context_hub" / "data" / "repo_catalog.toml"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check_repo_catalog")
curate = _load("curate_repo_catalog")

_TODAY = date(2026, 10, 17)

_REAL_PRIVATE = [
    "pipecat-ai/nvidia-sgmaker",
    "pipecat-ai/pipecat-websocket-transport",
    "pipecat-ai/hosted-smart-turn",
    "pipecat-ai/pipecat-krisp",
    "pipecat-ai/instant-voice-demo",
    "pipecat-ai/openai-realtime-todo-example",
    "pipecat-ai/pipecat.ai-agents",
    "pipecat-ai/gradient-bang-subagents",
    "pipecat-ai/gradient-bang-synth-forge",
]
_REAL_ARCHIVED = ["pipecat-ai/pipecat-cli", "pipecat-ai/pipecat-client-cxx-daily"]


def _resp(
    status: int, json: object = None, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=json, headers=headers)


def _ok_body(slug: str) -> dict[str, object]:
    return {"full_name": slug, "private": False, "archived": False}


def _result(slug: str, status: str) -> object:
    return check.EntryResult(slug, status)


# --- classifier -------------------------------------------------------------


class TestClassifyResponse:
    def test_ok(self) -> None:
        r = check.classify_response("a/b", _resp(200, _ok_body("a/b")))
        assert r.status == check.STATUS_OK

    @pytest.mark.parametrize("slug", _REAL_PRIVATE)
    def test_real_private_404_is_inaccessible(self, slug: str) -> None:
        # What the job's GITHUB_TOKEN (and an anonymous call) sees.
        r = check.classify_response(slug, _resp(404, {"message": "Not Found"}))
        assert r.status == check.STATUS_INACCESSIBLE

    @pytest.mark.parametrize("slug", _REAL_PRIVATE)
    def test_real_private_200_flag_is_inaccessible(self, slug: str) -> None:
        # What a maintainer's local token sees; the weekly job never does.
        body = {"full_name": slug, "private": True, "archived": False}
        r = check.classify_response(slug, _resp(200, body))
        assert r.status == check.STATUS_INACCESSIBLE

    @pytest.mark.parametrize("slug", _REAL_ARCHIVED)
    def test_real_archived(self, slug: str) -> None:
        body = {"full_name": slug, "private": False, "archived": True}
        assert check.classify_response(slug, _resp(200, body)).status == check.STATUS_ARCHIVED

    def test_synthetic_deleted_404_is_inaccessible(self) -> None:
        r = check.classify_response("pipecat-ai/gone", _resp(404))
        assert r.status == check.STATUS_INACCESSIBLE

    @pytest.mark.parametrize("code", [301, 308])
    def test_redirect_without_resolved_name_is_renamed_with_no_new_name(self, code: int) -> None:
        loc = "https://api.github.com/repositories/211666"  # real shape
        r = check.classify_response("pipecat-ai/old-name", _resp(code, headers={"location": loc}))
        assert r.status == check.STATUS_RENAMED
        assert r.new_name is None

    @pytest.mark.parametrize("code", [301, 308])
    def test_redirect_uses_resolved_name(self, code: int) -> None:
        loc = "https://api.github.com/repositories/211666"
        r = check.classify_response(
            "pipecat-ai/old-name", _resp(code, headers={"location": loc}), "pipecat-ai/new-name"
        )
        assert r.status == check.STATUS_RENAMED
        assert r.new_name == "pipecat-ai/new-name"

    @pytest.mark.parametrize("code", [301, 308])
    def test_redirect_with_repos_location_reports_new_name(self, code: int) -> None:
        loc = "https://api.github.com/repos/pipecat-ai/new-name"
        r = check.classify_response("pipecat-ai/old-name", _resp(code, headers={"location": loc}))
        assert r.status == check.STATUS_RENAMED
        assert r.new_name == "pipecat-ai/new-name"

    def test_synthetic_redirect_falls_back_to_body_url(self) -> None:
        body = {"url": "https://api.github.com/repos/pipecat-ai/body-name"}
        r = check.classify_response("pipecat-ai/old", _resp(301, body))
        assert r.status == check.STATUS_RENAMED
        assert r.new_name == "pipecat-ai/body-name"

    def test_synthetic_full_name_rename_beyond_case(self) -> None:
        body = {"full_name": "pipecat-ai/renamed", "private": False, "archived": False}
        r = check.classify_response("pipecat-ai/original", _resp(200, body))
        assert r.status == check.STATUS_RENAMED
        assert r.new_name == "pipecat-ai/renamed"

    def test_synthetic_case_only_full_name_difference_is_ok(self) -> None:
        body = {"full_name": "Pipecat-AI/Foo", "private": False, "archived": False}
        r = check.classify_response("pipecat-ai/foo", _resp(200, body))
        assert r.status == check.STATUS_OK
        assert r.new_name is None

    @pytest.mark.parametrize("code", [403, 429, 500, 502, 503, 504])
    def test_rate_limit_and_5xx_are_inconclusive_never_inaccessible(self, code: int) -> None:
        r = check.classify_response("a/b", _resp(code))
        assert r.status == check.STATUS_INCONCLUSIVE
        assert r.status != check.STATUS_INACCESSIBLE

    def test_unexpected_status_is_inconclusive(self) -> None:
        assert check.classify_response("a/b", _resp(418)).status == check.STATUS_INCONCLUSIVE

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, content=b"<html>"),
            httpx.Response(200, json=["not", "an", "object"]),
            httpx.Response(200, json={"private": False, "archived": False}),
            httpx.Response(200, json={"full_name": 7, "private": False}),
        ],
    )
    def test_non_json_200_does_not_crash(self, response: httpx.Response) -> None:
        r = check.classify_response("a/b", response)
        assert r.status == check.STATUS_INCONCLUSIVE


class TestCheckSlugErrors:
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ReadTimeout("slow"),
            httpx.ConnectTimeout("slow"),
            httpx.ConnectError("down"),
            httpx.ReadError("reset"),
        ],
    )
    def test_timeout_and_network_errors_are_inconclusive(self, exc: Exception) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc

        with check.make_client(None, httpx.MockTransport(handler)) as client:
            r = check.check_slug(client, "a/b")
        assert r.status == check.STATUS_INCONCLUSIVE
        assert r.status != check.STATUS_INACCESSIBLE

    def test_classify_error_is_inconclusive(self) -> None:
        r = check.classify_error("a/b", httpx.ConnectError("x"))
        assert r.status == check.STATUS_INCONCLUSIVE


# --- client behaviour -------------------------------------------------------


class TestClient:
    def test_authorization_sent_when_token_set(self) -> None:
        assert check.build_headers("tok")["Authorization"] == "Bearer tok"

    @pytest.mark.parametrize("token", [None, ""])
    def test_authorization_omitted_without_token(self, token: str | None) -> None:
        assert "Authorization" not in check.build_headers(token)

    def test_header_reaches_the_wire_only_with_token(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _resp(200, _ok_body("a/b"))

        for token in ("tok", None):
            with check.make_client(token, httpx.MockTransport(handler)) as client:
                check.check_slug(client, "a/b")
        assert seen[0].headers["authorization"] == "Bearer tok"
        assert "authorization" not in seen[1].headers

    def test_301_is_not_followed_automatically(self) -> None:
        # Exactly the explicit second request; the 301 itself is never auto-followed.
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/repos/pipecat-ai/old":
                return _resp(
                    301, headers={"location": "https://api.github.com/repositories/211666"}
                )
            return _resp(200, {"full_name": "pipecat-ai/new"})  # synthetic body

        with check.make_client(None, httpx.MockTransport(handler)) as client:
            r = check.check_slug(client, "pipecat-ai/old")
        assert calls == ["/repos/pipecat-ai/old", "/repositories/211666"]
        assert r.status == check.STATUS_RENAMED
        assert r.new_name == "pipecat-ai/new"

    def test_second_hop_that_redirects_again_is_not_followed(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/repos/o/old":
                return _resp(301, headers={"location": "https://api.github.com/repositories/1"})
            return _resp(301, headers={"location": "https://api.github.com/repositories/2"})

        with check.make_client(None, httpx.MockTransport(handler)) as client:
            r = check.check_slug(client, "o/old")
        assert calls == ["/repos/o/old", "/repositories/1"]
        assert r.status == check.STATUS_RENAMED
        assert r.new_name is None

    @pytest.mark.parametrize(
        "second",
        [
            lambda: _resp(404),
            lambda: _resp(500),
            lambda: httpx.Response(200, content=b"<html>"),
            lambda: _resp(200, ["x"]),
            lambda: _resp(200, {"name": "no-full-name"}),
            lambda: _resp(200, {"full_name": 3}),
            lambda: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        ],
    )
    def test_failed_second_hop_keeps_renamed_without_new_name(self, second: object) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/o/old":
                return _resp(301, headers={"location": "https://api.github.com/repositories/9"})
            return second()  # type: ignore[operator]

        with check.make_client(None, httpx.MockTransport(handler)) as client:
            r = check.check_slug(client, "o/old")
        assert r.status == check.STATUS_RENAMED
        assert r.new_name is None

    def test_off_host_redirect_is_not_requested(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _resp(301, headers={"location": "https://evil.example/repositories/9"})

        with check.make_client("tok", httpx.MockTransport(handler)) as client:
            r = check.check_slug(client, "o/old")
        assert len(calls) == 1
        assert r.status == check.STATUS_RENAMED and r.new_name is None

    def test_client_does_not_follow_redirects(self) -> None:
        with check.make_client(None) as client:
            assert client.follow_redirects is False


# --- verdict, advisory, report ---------------------------------------------


class TestVerdictAndReport:
    def test_advisory_boundary_90_days_ok_91_advisory(self) -> None:
        assert check.is_verified_stale(_TODAY - timedelta(days=90), _TODAY) is False
        assert check.is_verified_stale(_TODAY - timedelta(days=91), _TODAY) is True
        assert check.verified_age_days(_TODAY - timedelta(days=91), _TODAY) == 91

    def test_exit_codes_over_one_mixed_result_set(self) -> None:
        ok = [_result("a/ok", check.STATUS_OK)]
        inconclusive = _result("a/flaky", check.STATUS_INCONCLUSIVE)
        assert check.aggregate_verdict(ok) == 0
        # An old `verified` date is advisory only: it is not an input to the verdict.
        old = _TODAY - timedelta(days=500)
        assert check.aggregate_verdict(ok) == 0
        assert "Advisory" in check.render_report(ok, old, _TODAY)
        assert check.aggregate_verdict([*ok, inconclusive]) == check.EXIT_INCONCLUSIVE != 0
        for stale in (
            check.STATUS_INACCESSIBLE,
            check.STATUS_ARCHIVED,
            check.STATUS_RENAMED,
        ):
            assert check.aggregate_verdict([*ok, _result("a/s", stale)]) == check.EXIT_STALE
            # stale outranks inconclusive
            both = [*ok, inconclusive, _result("a/s", stale)]
            assert check.aggregate_verdict(both) == check.EXIT_STALE

    def test_report_sections(self) -> None:
        results = [
            _result("a/ok", check.STATUS_OK),
            check.EntryResult("a/old", check.STATUS_RENAMED, "redirect", "a/new"),
            check.EntryResult("a/flaky", check.STATUS_INCONCLUSIVE, "HTTP 429"),
        ]
        report = check.render_report(results, _TODAY, _TODAY)
        assert "Stale entries" in report
        assert "a/new" in report
        inc = report.split("## Inconclusive")[1]
        assert "`a/flaky`" in inc and "HTTP 429" in inc
        # Inconclusive entries are not listed under the stale section.
        assert "`a/flaky`" not in report.split("## Inconclusive")[0]
        assert "Advisory" not in report

    def test_report_advisory_only_past_90_days(self) -> None:
        r = [_result("a/ok", check.STATUS_OK)]
        assert "Advisory" not in check.render_report(r, _TODAY - timedelta(days=90), _TODAY)
        assert "Advisory" in check.render_report(r, _TODAY - timedelta(days=91), _TODAY)


# --- main -------------------------------------------------------------------


def _catalog_text(slugs: list[str], verified: str = "2026-10-16") -> str:
    repos = "\n".join(f'  [[groups.g.repos]]\n  slug = "{s}"\n' for s in slugs)
    return (
        f"verified = {verified}\n\n"
        '[groups.g]\nkind = "topic"\ndescription = "d"\naliases = ["g"]\n\n' + repos
    )


class TestMain:
    def _run(
        self,
        tmp_path: Path,
        handler: object,
        slugs: list[str],
        verified: str = "2026-10-16",
        environ: dict[str, str] | None = None,
    ) -> int:
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(slugs, verified), encoding="utf-8")
        return int(
            check.main(
                ["--catalog", str(path)],
                transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
                today=_TODAY,
                environ=environ if environ is not None else {},
            )
        )

    def test_all_ok_exits_zero_even_with_old_verified(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _resp(200, _ok_body(request.url.path.removeprefix("/repos/")))

        assert self._run(tmp_path, handler, ["a/one", "a/two"], verified="2025-01-01") == 0
        assert "Advisory" in capsys.readouterr().out

    def test_mixed_set_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            slug = request.url.path.removeprefix("/repos/")
            if slug == "a/private":
                return _resp(404)
            if slug == "a/arch":
                return _resp(200, {"full_name": slug, "private": False, "archived": True})
            if slug == "a/flaky":
                return _resp(429)
            return _resp(200, _ok_body(slug))

        code = self._run(tmp_path, handler, ["a/fine", "a/private", "a/arch", "a/flaky"])
        out = capsys.readouterr().out
        assert code == 1
        assert "`a/private`: inaccessible" in out
        assert "`a/arch`: archived" in out
        assert "`a/flaky`" in out.split("## Inconclusive")[1]

    def test_inconclusive_only_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("flaky"):
                raise httpx.ReadTimeout("slow")
            return _resp(200, _ok_body(request.url.path.removeprefix("/repos/")))

        assert self._run(tmp_path, handler, ["a/fine", "a/flaky"]) == 2
        assert "## Inconclusive" in capsys.readouterr().out

    def test_token_from_environ_is_sent(self, tmp_path: Path) -> None:
        auth: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            auth.append(request.headers.get("authorization"))
            return _resp(200, _ok_body("a/one"))

        self._run(tmp_path, handler, ["a/one"], environ={"GITHUB_TOKEN": "abc"})
        self._run(tmp_path, handler, ["a/one"], environ={})
        assert auth == ["Bearer abc", None]

    def test_unreadable_catalog_exits_nonzero(self, tmp_path: Path) -> None:
        code = check.main(
            ["--catalog", str(tmp_path / "missing.toml")],
            transport=httpx.MockTransport(lambda r: _resp(200)),
            today=_TODAY,
            environ={},
        )
        assert code == check.EXIT_CATALOG
        assert code not in (check.EXIT_STALE, check.EXIT_INCONCLUSIVE)

    def test_catalog_failure_prints_on_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("not [valid toml", encoding="utf-8")
        for path in (bad, tmp_path / "missing.toml"):
            code = check.main(
                ["--catalog", str(path)],
                transport=httpx.MockTransport(lambda r: _resp(200)),
                today=_TODAY,
                environ={},
            )
            captured = capsys.readouterr()
            assert code == check.EXIT_CATALOG
            assert "cannot load the catalog" in captured.out

    def test_runs_against_the_packaged_catalog_offline(self) -> None:
        # The shipped catalog parses and every slug is probed through the injected
        # transport (so PR CI needs no network).
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return _resp(200, _ok_body(request.url.path.removeprefix("/repos/")))

        code = check.main([], transport=httpx.MockTransport(handler), today=_TODAY, environ={})
        assert code == 0
        assert paths and all(p.startswith("/repos/") for p in paths)


# --- curate -----------------------------------------------------------------


class TestCurate:
    def test_resolve_token_prefers_env_then_gh(self) -> None:
        assert curate.resolve_token({"GITHUB_TOKEN": "env"}, lambda: "gh") == "env"
        assert curate.resolve_token({}, lambda: "gh") == "gh"
        assert curate.resolve_token({}, lambda: None) is None

    def test_no_token_exits_nonzero_with_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request may be made without a token")

        code = curate.main(
            [], transport=httpx.MockTransport(boom), environ={}, gh_token=lambda: None
        )
        assert code != 0
        assert "GITHUB_TOKEN" in capsys.readouterr().err

    def test_report_only_run_leaves_catalog_byte_identical(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")
        before = path.read_bytes()
        shipped_before = _CATALOG.read_bytes()
        seen_auth: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("authorization"))
            assert request.method == "GET"
            p = request.url.path
            if p == "/repos/a/known":
                return _resp(
                    200,
                    {
                        "default_branch": "main",
                        "archived": False,
                        "pushed_at": "2026-10-01T00:00:00Z",
                        "stargazers_count": 7,
                    },
                )
            if p == "/repos/a/known/git/trees/main":
                tree = [
                    {"type": "blob", "path": "x.py"},
                    {"type": "blob", "path": "y.ts"},
                    {"type": "blob", "path": "README.md"},
                    {"type": "tree", "path": "dir.py"},
                ]
                return _resp(200, {"tree": tree})
            if p == "/search/code":
                items = [
                    {"repository": {"full_name": "new/vendor"}},
                    {"repository": {"full_name": "A/Known"}},
                ]
                return _resp(200, {"items": items if request.url.params["page"] == "1" else []})
            return _resp(404)

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        out = capsys.readouterr().out
        assert code == 0
        assert path.read_bytes() == before
        assert _CATALOG.read_bytes() == shipped_before
        assert set(seen_auth) == {"Bearer tok"}
        assert "`a/known`: 2 source files" in out
        assert "new/vendor" in out
        assert "A/Known" not in out  # already catalogued, case-insensitively

    def test_count_source_files(self) -> None:
        tree = {"tree": [{"type": "blob", "path": "a.tsx"}, {"type": "blob", "path": "b.md"}]}
        assert curate.count_source_files(tree) == 1
        assert curate.count_source_files("nope") == 0

    def test_repo_stats_records_errors_instead_of_raising(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _resp(404)

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            assert curate.repo_stats(client, "a/b")["error"] == "HTTP 404"

    def test_catalog_failure_has_its_own_exit_code_and_stdout_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = curate.main(
            ["--catalog", str(tmp_path / "missing.toml")],
            transport=httpx.MockTransport(lambda r: _resp(200)),
            environ={"GITHUB_TOKEN": "tok"},
        )
        captured = capsys.readouterr()
        assert code == curate.EXIT_CATALOG
        assert curate.EXIT_CATALOG not in (curate.EXIT_OK, curate.EXIT_NO_TOKEN)
        assert "cannot load the catalog" in captured.out

    @pytest.mark.parametrize(
        "make",
        [
            lambda: httpx.Response(200, content=b"<html>"),
            lambda: _resp(200, ["x"]),
            lambda: _resp(200, {"items": "nope"}),
        ],
    )
    def test_discover_vendors_records_malformed_response(self, make: object) -> None:
        with curate.make_client("t", httpx.MockTransport(lambda r: make())) as client:  # type: ignore[operator]
            problems: list[str] = []
            found = curate.discover_vendors(client, set(), problems)
        assert not found
        assert len(problems) == 1

    def test_discover_vendors_skips_null_and_non_dict_repository(self) -> None:
        items = [
            {"repository": None},
            {"repository": "str"},
            "junk",
            {"repository": {"full_name": "new/vendor"}},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return _resp(200, {"items": items if request.url.params["page"] == "1" else []})

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            problems: list[str] = []
            found = curate.discover_vendors(client, set(), problems)
        assert list(found) == ["new/vendor"]
        assert problems and "3 item(s)" in problems[0]
        assert "Code-search problems" in curate.render_report([], found, problems)

    def test_repo_stats_non_dict_body_is_recorded(self) -> None:
        with curate.make_client("t", httpx.MockTransport(lambda r: _resp(200, ["x"]))) as client:
            assert "error" in curate.repo_stats(client, "a/b")

    def test_truncated_tree_is_marked_in_report(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/a/b":
                return _resp(200, {"default_branch": "main"})
            return _resp(200, {"truncated": True, "tree": [{"type": "blob", "path": "x.py"}]})

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            stats = curate.repo_stats(client, "a/b")
        assert stats["truncated"] is True
        assert "1 (truncated) source files" in curate.render_report([stats], curate.Counter())

    def test_script_never_opens_the_catalog_for_writing(self) -> None:
        src = (_SCRIPTS / "curate_repo_catalog.py").read_text(encoding="utf-8")
        assert not re.search(r"write_text|write_bytes|open\([^)]*[\"'][wax+]", src)


# --- workflow / justfile / ci wiring (the test_audit_sync.py pattern) -------


class TestWiring:
    def test_workflow_invokes_the_script(self) -> None:
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert "scripts/check_repo_catalog.py" in text
        assert "schedule:" in text and "cron:" in text
        assert "workflow_dispatch" in text
        assert "GITHUB_TOKEN" in text
        assert (_SCRIPTS / "check_repo_catalog.py").is_file()

    def test_justfile_recipes_match_the_workflow_invocation(self) -> None:
        just = _JUSTFILE.read_text(encoding="utf-8")
        health = re.search(r"^catalog-health:\n\s+(.+)$", just, re.MULTILINE)
        curate_recipe = re.search(r"^catalog-curate:\n\s+(.+)$", just, re.MULTILINE)
        assert health and curate_recipe
        assert health.group(1).strip() == "uv run python scripts/check_repo_catalog.py"
        assert curate_recipe.group(1).strip() == "uv run python scripts/curate_repo_catalog.py"
        # The recipe's command is the one the workflow runs.
        assert health.group(1).strip() in _WORKFLOW.read_text(encoding="utf-8")

    def test_pr_ci_never_runs_the_network_health_check(self) -> None:
        ci = _CI_YML.read_text(encoding="utf-8")
        assert "scripts/check_repo_catalog.py" not in ci
        assert "catalog-health" not in ci

    def test_windows_file_list_includes_this_module(self) -> None:
        assert "tests/unit/test_check_repo_catalog.py" in _CI_YML.read_text(encoding="utf-8")

    def test_scripts_have_no_permission_bit_dependence(self) -> None:
        # Windows CI reads files by repo-relative path only; nothing here may rely on
        # POSIX mode bits (the scripts are invoked through `uv run python`).
        for name in ("check_repo_catalog.py", "curate_repo_catalog.py"):
            assert (_SCRIPTS / name).is_file()
