"""Tests for the repo catalog health script, the helper module both catalog
scripts share, and the scripts' workflow/justfile/CI wiring (no network).
``curate_repo_catalog.py`` has its own ``test_curate_repo_catalog.py``.

The scripts are loaded by path (the ``test_probe_mcp_2x.py`` pattern): they are
not part of the wheel. The shared ``catalog_script_common`` module is loaded
first, so the script's sibling import finds it in ``sys.modules``. Every GitHub
answer is an injected ``httpx`` response or ``httpx.MockTransport``.

Fixture provenance: the private/archived shapes below were observed on
2026-10-16 (owner ``pipecat-ai``). The 301 redirect SHAPE is real (verified
against ``joyent/node``: ``Location: https://api.github.com/repositories/{id}``,
not a ``/repos/owner/name`` URL), but the second-hop 200 body, the deleted 404,
the ``full_name`` rename beyond case and the case-only difference are
SYNTHETIC.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
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


common = _load("catalog_script_common")
check = _load("check_repo_catalog")

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

    def test_partial_200_is_inconclusive_not_ok(self) -> None:
        # Regression: a body with only full_name proves neither public nor
        # unarchived, so it must not reach ok (it did when private/archived were
        # only checked for `is True`).
        r = check.classify_response("a/b", _resp(200, {"full_name": "a/b"}))
        assert r.status == check.STATUS_INCONCLUSIVE

    # Every field the classifier reads, missing and mistyped: each cell of the
    # 200 body state space is inconclusive, never ok or a stale verdict.
    _BAD_200_BODIES = [
        *[
            pytest.param({k: v for k, v in _ok_body("a/b").items() if k != field}, id=f"no-{field}")
            for field in ("full_name", "private", "archived")
        ],
        pytest.param({**_ok_body("a/b"), "full_name": ""}, id="full_name-empty"),
        pytest.param({**_ok_body("a/b"), "full_name": 7}, id="full_name-int"),
        pytest.param({**_ok_body("a/b"), "full_name": None}, id="full_name-null"),
        pytest.param({**_ok_body("a/b"), "private": "false"}, id="private-str"),
        pytest.param({**_ok_body("a/b"), "private": 0}, id="private-int"),
        pytest.param({**_ok_body("a/b"), "private": None}, id="private-null"),
        pytest.param({**_ok_body("a/b"), "archived": "true"}, id="archived-str"),
        pytest.param({**_ok_body("a/b"), "archived": 1}, id="archived-int"),
        pytest.param({**_ok_body("a/b"), "archived": None}, id="archived-null"),
    ]

    @pytest.mark.parametrize("body", _BAD_200_BODIES)
    def test_every_unvalidated_200_cell_is_inconclusive(self, body: dict[str, object]) -> None:
        r = check.classify_response("a/b", _resp(200, body))
        assert r.status == check.STATUS_INCONCLUSIVE
        assert "not a valid repository" in r.detail

    def test_the_classifier_validates_exactly_the_fields_it_reads(self) -> None:
        assert set(check.REPO_FIELDS) == {"full_name", "private", "archived"}

    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            (200, _ok_body("a/b"), "ok"),
            (200, {"full_name": "a/b"}, "inconclusive"),
            (200, None, "inconclusive"),
            (301, None, "renamed"),
            (308, None, "renamed"),
            (404, None, "inaccessible"),
            (403, None, "inconclusive"),
            (429, None, "inconclusive"),
            (500, None, "inconclusive"),
            (503, None, "inconclusive"),
            (418, None, "inconclusive"),
            (204, None, "inconclusive"),
        ],
    )
    def test_status_by_body_table(self, status: int, body: object, expected: str) -> None:
        assert check.classify_response("a/b", _resp(status, body)).status == expected


class TestValidatedBody:
    """The one GitHub-response validator both catalog scripts read through."""

    _FIELDS = {"s": common.non_empty_str, "n": common.plain_int, "b": common.is_bool}
    _GOOD = {"s": "x", "n": 0, "b": False, "extra": [1]}

    def test_valid_body_is_returned(self) -> None:
        body, problem = common.validated_body(_resp(200, self._GOOD), self._FIELDS)
        assert body == self._GOOD and problem == ""

    @pytest.mark.parametrize(
        ("response", "reason"),
        [
            (_resp(404, {"s": "x", "n": 0, "b": False}), "HTTP 404"),
            (_resp(500), "HTTP 500"),
            (httpx.Response(200, content=b"<html>"), "body is not JSON"),
            (_resp(200, ["x"]), "body is not a JSON object"),
            (_resp(200, "a string"), "body is not a JSON object"),
            (_resp(200, {"n": 0, "b": False}), "field 's'"),
            (_resp(200, {"s": "", "n": 0, "b": False}), "field 's'"),
            (_resp(200, {"s": "x", "b": False}), "field 'n'"),
            (_resp(200, {"s": "x", "n": True, "b": False}), "field 'n'"),
            (_resp(200, {"s": "x", "n": -1, "b": False}), "field 'n'"),
            (_resp(200, {"s": "x", "n": "1", "b": False}), "field 'n'"),
            (_resp(200, {"s": "x", "n": 0}), "field 'b'"),
            (_resp(200, {"s": "x", "n": 0, "b": "false"}), "field 'b'"),
            (_resp(200, {"s": "x", "n": 0, "b": 0}), "field 'b'"),
        ],
    )
    def test_every_invalid_cell_returns_no_data(
        self, response: httpx.Response, reason: str
    ) -> None:
        body, problem = common.validated_body(response, self._FIELDS)
        assert body is None
        assert reason in problem

    def test_status_none_accepts_any_status(self) -> None:
        body, _ = common.validated_body(
            _resp(301, {"s": "x"}), {"s": common.non_empty_str}, status=None
        )
        assert body == {"s": "x"}

    def test_both_scripts_share_the_validator_headers_and_catalog_exit(self) -> None:
        for name in ("check_repo_catalog.py", "curate_repo_catalog.py"):
            src = (_SCRIPTS / name).read_text(encoding="utf-8")
            assert "from catalog_script_common import" in src, name
            # No private response parsing left beside the shared validator.
            assert ".json()" not in src, name
            assert "_catalog_failure" not in src, name
            assert '"X-GitHub-Api-Version"' not in src, name


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
            return _resp(200, {"full_name": "pipecat-ai/new", "private": False})  # synthetic

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
            lambda: _resp(200, {"name": "no-full-name", "private": False}),
            lambda: _resp(200, {"full_name": 3, "private": False}),
            # Visibility not confirmed public: the name must not be reported
            # (regression: a private target's name went into the issue body).
            lambda: _resp(200, {"full_name": "secret/private-repo", "private": True}),
            lambda: _resp(200, {"full_name": "secret/private-repo"}),
            lambda: _resp(200, {"full_name": "secret/private-repo", "private": "false"}),
            lambda: _resp(200, {"full_name": "secret/private-repo", "private": 0}),
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

    def test_non_utf8_catalog_exits_catalog_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # UnicodeDecodeError is a ValueError, not an OSError: it must still map to
        # the catalog exit code instead of a traceback.
        bad = tmp_path / "latin1.toml"
        bad.write_bytes(b'verified = "2026-10-16"\n# caf\xe9\n')
        code = check.main(
            ["--catalog", str(bad)],
            transport=httpx.MockTransport(lambda r: _resp(200)),
            today=_TODAY,
            environ={},
        )
        assert code == check.EXIT_CATALOG
        assert "cannot load the catalog" in capsys.readouterr().out

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


# --- workflow / justfile / ci wiring (the test_audit_sync.py pattern) -------


class TestWiring:
    def test_workflow_invokes_the_script(self) -> None:
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert "scripts/check_repo_catalog.py" in text
        assert "schedule:" in text and "cron:" in text
        assert "workflow_dispatch" in text
        assert "GITHUB_TOKEN" in text
        assert (_SCRIPTS / "check_repo_catalog.py").is_file()

    def test_workflow_serialises_runs(self) -> None:
        # Two failing runs must not both see "no open issue" and each create one.
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert re.search(r"^concurrency:\n\s+group: catalog-health\n", text, re.MULTILINE)
        assert "cancel-in-progress: false" in text

    def test_only_the_issue_job_holds_issues_write(self) -> None:
        # The check runs project code and its dependency tree, so it gets a read-only
        # token; the write scope sits on the job that runs `gh` and no project code.
        lines = _WORKFLOW.read_text(encoding="utf-8").splitlines()
        text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        head, sep, report_job = text.partition("\n  report:\n")
        assert sep, "the workflow has no separate `report` job"
        assert "issues: write" not in head
        assert "issues: write" in report_job
        assert "scripts/check_repo_catalog.py" in head
        assert "uv run" not in report_job
        assert "actions/checkout" not in report_job

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
        ci = _CI_YML.read_text(encoding="utf-8")
        assert "tests/unit/test_check_repo_catalog.py" in ci
        assert "tests/unit/test_curate_repo_catalog.py" in ci

    def test_health_job_exposes_the_script_exit_code(self) -> None:
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert "exit-code: ${{ steps.health.outputs.exit-code }}" in text
        assert 'echo "exit-code=${code}" >> "${GITHUB_OUTPUT}"' in text
        assert "EXIT_CODE: ${{ needs.health.outputs.exit-code }}" in text


def _issue_step_script() -> str:
    """The shell body of the report job's issue step, dedented."""
    lines = _WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index("      - name: Open or update tracking issue")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body: list[str] = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="runs the step under bash"
)
class TestIssueStep:
    """The report job's shell, run against a stub ``gh`` that logs its argv."""

    def _run(self, tmp_path: Path, *, exit_code: str, existing: str) -> list[str]:
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        log = tmp_path / "gh.log"
        stub = stub_dir / "gh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'echo "$*" >> "$GH_LOG"\n'
            'if [ "$1 $2" = "issue list" ]; then echo -n "$EXISTING"; fi\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
        (tmp_path / "report.md").write_text("# Repo catalog health\nINCONCLUSIVE-REPORT\n")
        env = {
            "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_LOG": str(log),
            "EXISTING": existing,
            "REPORT_PATH": "report.md",
            "LABEL": "catalog-health",
            "TITLE": "t",
            "RUN_ID": "42",
            "EXIT_CODE": exit_code,
        }
        subprocess.run(  # noqa: S603 - fixed argv, test-only
            ["bash", "-c", _issue_step_script()], cwd=tmp_path, env=env, check=True
        )
        return log.read_text(encoding="utf-8").splitlines()

    def test_inconclusive_run_comments_and_keeps_the_body(self, tmp_path: Path) -> None:
        calls = self._run(tmp_path, exit_code="2", existing="7")
        assert not any(c.startswith("issue edit") for c in calls)
        assert "issue comment 7 --body-file comment.md" in calls
        comment = (tmp_path / "comment.md").read_text(encoding="utf-8")
        assert "INCONCLUSIVE-REPORT" in comment and "run 42" in comment

    def test_stale_run_replaces_the_body(self, tmp_path: Path) -> None:
        calls = self._run(tmp_path, exit_code="1", existing="7")
        assert "issue edit 7 --body-file report.md" in calls

    def test_catalog_failure_replaces_the_body(self, tmp_path: Path) -> None:
        calls = self._run(tmp_path, exit_code="3", existing="7")
        assert "issue edit 7 --body-file report.md" in calls

    def test_inconclusive_run_without_an_issue_opens_one(self, tmp_path: Path) -> None:
        calls = self._run(tmp_path, exit_code="2", existing="")
        assert any(c.startswith("issue create") for c in calls)

    def test_scripts_have_no_permission_bit_dependence(self) -> None:
        # The scripts are committed without the executable bit (and Windows has no
        # POSIX mode bits), so every invocation in the justfile and the workflows
        # must go through the interpreter (`uv run python scripts/<name>`), never
        # run the file directly (`./scripts/<name>` or a bare `scripts/<name>`).
        sources = [_JUSTFILE, *sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml"))]
        texts = {p.name: p.read_text(encoding="utf-8") for p in sources}
        for name in ("check_repo_catalog.py", "curate_repo_catalog.py"):
            assert (_SCRIPTS / name).is_file()
            mentions = 0
            for source, text in texts.items():
                every = re.findall(rf"scripts/{re.escape(name)}", text)
                via_python = re.findall(rf"(?<!\S)python\s+scripts/{re.escape(name)}", text)
                assert len(every) == len(via_python), (source, name)
                mentions += len(every)
            assert mentions, f"{name} is invoked nowhere: the scan proves nothing"
