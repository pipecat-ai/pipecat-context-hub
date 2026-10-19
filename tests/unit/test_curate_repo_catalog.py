"""Tests for the report-only repo catalog curation script (no network).

The script is loaded by path (the ``test_probe_mcp_2x.py`` pattern), after the
shared ``catalog_script_common`` module so its sibling import resolves. Every
GitHub answer is an ``httpx.MockTransport`` response. The bodies are SYNTHETIC
but carry every field the script validates, as GitHub's real answers do.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
_CATALOG = _REPO_ROOT / "src" / "pipecat_context_hub" / "data" / "repo_catalog.toml"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("catalog_script_common")
curate = _load("curate_repo_catalog")


def _resp(status: int, json: object = None) -> httpx.Response:
    return httpx.Response(status, json=json)


def _meta(**over: object) -> dict[str, object]:
    """A ``/repos/{slug}`` body with every field ``repo_stats`` reads."""
    body: dict[str, object] = {
        "default_branch": "main",
        "archived": False,
        "pushed_at": "2026-10-01T00:00:00Z",
        "stargazers_count": 0,
    }
    return {**body, **over}


def _tree(entries: Sequence[object], *, truncated: bool = False) -> dict[str, object]:
    return {"tree": list(entries), "truncated": truncated}


_COUNT_ITEMS = object()


def _search(
    items: Sequence[object], *, total: object = _COUNT_ITEMS, incomplete: bool = False
) -> dict[str, object]:
    """One search page; ``total_count`` defaults to the items on it."""
    return {
        "total_count": len(items) if total is _COUNT_ITEMS else total,
        "incomplete_results": incomplete,
        "items": list(items),
    }


def _catalog_text(slugs: list[str], verified: str = "2026-10-16") -> str:
    repos = "\n".join(f'  [[groups.g.repos]]\n  slug = "{s}"\n' for s in slugs)
    return (
        f"verified = {verified}\n\n"
        '[groups.g]\nkind = "topic"\ndescription = "d"\naliases = ["g"]\n\n' + repos
    )


def _hit(name: object, private: object = False) -> dict[str, object]:
    """One code-search item; GitHub's carries the repository's ``private`` flag."""
    return {"repository": {"full_name": name, "private": private}}


def _vendor(i: int) -> dict[str, object]:
    return _hit(f"v/r{i}")


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
                return _resp(200, _tree(tree))
            if p == "/search/code":
                items = [
                    _hit("new/vendor"),
                    _hit("A/Known"),
                ]
                return _resp(200, _search(items))
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
            _hit("new/vendor"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return _resp(200, _search(items))

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            problems: list[str] = []
            found = curate.discover_vendors(client, set(), problems)
        assert list(found) == ["new/vendor"]
        assert problems and "3 item(s)" in problems[0]
        assert "Code-search problems" in curate.render_report([], found, problems)

    @pytest.mark.parametrize("status", [403, 422, 429, 500])
    def test_discover_vendors_records_http_failure(self, status: int) -> None:
        # A rate limit or server error ends discovery: it must not read as "no candidates".
        with curate.make_client("t", httpx.MockTransport(lambda r: _resp(status))) as client:
            problems: list[str] = []
            found = curate.discover_vendors(client, set(), problems)
        assert not found
        assert problems == [f"page 1: HTTP {status}"]

    def test_discover_vendors_records_transport_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            problems: list[str] = []
            found = curate.discover_vendors(client, set(), problems)
        assert not found
        assert len(problems) == 1 and "ReadTimeout" in problems[0]

    def test_incomplete_search_exits_nonzero_and_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                return _resp(403, {"message": "rate limited"})
            return _resp(404)

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        out = capsys.readouterr().out
        assert code == curate.EXIT_REPORT_INCOMPLETE
        assert code not in (curate.EXIT_OK, curate.EXIT_NO_TOKEN, curate.EXIT_CATALOG)
        assert "none found (code search incomplete" in out
        assert "page 1: HTTP 403" in out

    def test_default_repos_are_never_candidates(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The catalog parser rejects the shipped defaults, so they are never catalog
        # slugs; they are always indexed and must not top the candidate list.
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                items = [
                    _hit("pipecat-ai/pipecat"),
                    _hit("Pipecat-AI/Pipecat-Examples"),
                    _hit("new/vendor"),
                ]
                return _resp(200, _search(items))
            if request.url.path == "/repos/a/known":
                return _resp(200, _meta())
            return _resp(200, _tree([]))

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        out = capsys.readouterr().out
        assert code == curate.EXIT_OK
        candidates = out.split("## Candidate repos", 1)[1]
        assert "new/vendor" in candidates
        assert "pipecat-ai/pipecat`" not in candidates
        assert "Pipecat-AI/Pipecat-Examples" not in candidates

    def test_scripts_compare_slugs_through_slug_key(self) -> None:
        # repo_slug.slug_key is the one slug identity; a raw casefold would drift
        # from catalog, config and hint behaviour if slug_key ever normalised more.
        for name in ("check_repo_catalog.py", "curate_repo_catalog.py"):
            src = (_SCRIPTS / name).read_text(encoding="utf-8")
            assert ".casefold()" not in src, name
            assert "slug_key(" in src, name

    def test_repo_stats_non_dict_body_is_recorded(self) -> None:
        with curate.make_client("t", httpx.MockTransport(lambda r: _resp(200, ["x"]))) as client:
            assert "error" in curate.repo_stats(client, "a/b")

    def test_truncated_tree_is_marked_in_report(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/a/b":
                return _resp(200, _meta())
            return _resp(200, _tree([{"type": "blob", "path": "x.py"}], truncated=True))

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            stats = curate.repo_stats(client, "a/b")
        assert stats["truncated"] is True
        assert "1 (truncated) source files" in curate.render_report([stats], curate.Counter())
        # A truncated tree undercounts: the report is incomplete, not clean.
        assert curate.stats_incomplete([stats])

    def test_truncated_tree_exits_incomplete(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                return _resp(200, _search([]))
            if request.url.path == "/repos/a/known":
                return _resp(200, _meta())
            return _resp(200, _tree([{"type": "blob", "path": "x.py"}], truncated=True))

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        assert code == curate.EXIT_REPORT_INCOMPLETE
        assert "1 (truncated) source files" in capsys.readouterr().out

    @staticmethod
    def _full_pages(total_count: object) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params["page"]
            items = [_hit(f"v{page}/r{i}") for i in range(curate.SEARCH_PER_PAGE)]
            return _resp(200, _search(items, total=total_count))

        return httpx.MockTransport(handler)

    def test_page_cap_with_unread_results_is_recorded(self) -> None:
        # Every capped page full and GitHub reporting more: the unread results
        # must not read as a complete candidate set.
        with curate.make_client("t", self._full_pages(400)) as client:
            problems: list[str] = []
            found = curate.discover_vendors(client, set(), problems)
        assert len(found) == curate.SEARCH_PAGES * curate.SEARCH_PER_PAGE
        assert len(problems) == 1 and f"{curate.SEARCH_PAGES}-page cap" in problems[0]

    @pytest.mark.parametrize("total_count", [None, "400", True, -1])
    def test_missing_or_mistyped_total_count_is_a_page_problem(self, total_count: object) -> None:
        # Without a usable total_count nothing says whether results were left unread.
        with curate.make_client("t", self._full_pages(total_count)) as client:
            problems: list[str] = []
            found = curate.discover_vendors(client, set(), problems)
        assert not found
        assert problems == ["page 1: field 'total_count' missing or malformed"]

    def test_page_cap_reached_exactly_is_clean(self) -> None:
        total = curate.SEARCH_PAGES * curate.SEARCH_PER_PAGE
        with curate.make_client("t", self._full_pages(total)) as client:
            problems: list[str] = []
            curate.discover_vendors(client, set(), problems)
        assert problems == []

    def test_page_cap_with_unread_results_exits_incomplete(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")
        full = self._full_pages(400)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                return full.handle_request(request)
            if request.url.path == "/repos/a/known":
                return _resp(200, _meta())
            return _resp(200, _tree([]))

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        assert code == curate.EXIT_REPORT_INCOMPLETE
        assert "page cap" in capsys.readouterr().out

    @pytest.mark.parametrize("n", [29, 30, 31, curate.SEARCH_PAGES * curate.SEARCH_PER_PAGE])
    def test_clean_run_lists_every_candidate(
        self, n: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A clean run (exit 0) shows every candidate it read: no top-N cut hides
        # one, below, at or above the old 30-row cap, up to the most discovery
        # can return.
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")
        candidates = [_vendor(i) for i in range(n)]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                page = int(request.url.params["page"])
                per = curate.SEARCH_PER_PAGE
                return _resp(200, _search(candidates[(page - 1) * per : page * per], total=n))
            if request.url.path == "/repos/a/known":
                return _resp(200, _meta())
            return _resp(200, _tree([]))

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        out = capsys.readouterr().out
        assert code == curate.EXIT_OK
        assert re.findall(r"^- `v/r\d+`: 1 matching files$", out, re.MULTILINE) == [
            f"- `v/r{i}`: 1 matching files" for i in range(n)
        ]

    def test_candidates_are_counted_by_slug_key(self) -> None:
        items = [
            _hit("Owner/Repo"),
            _hit("owner/repo"),
            _hit("OWNER/REPO"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return _resp(200, _search(items))

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            found = curate.discover_vendors(client, set(), [])
        assert dict(found) == {"Owner/Repo": 3}  # one row, first spelling seen

    @pytest.mark.parametrize(
        "repository",
        [
            {"private": False},
            {"full_name": None, "private": False},
            {"full_name": 5, "private": False},
            {"full_name": "", "private": False},
        ],
    )
    def test_item_without_a_full_name_is_malformed(self, repository: object) -> None:
        items = [{"repository": repository}]

        def handler(request: httpx.Request) -> httpx.Response:
            return _resp(200, _search(items))

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            problems: list[str] = []
            found = curate.discover_vendors(client, set(), problems)
        assert not found
        assert problems == [
            "page 1: 1 item(s) without a repository full_name and boolean private flag"
        ]

    _MISSING = object()

    @pytest.mark.parametrize(
        ("private", "candidate", "excluded", "malformed"),
        [
            pytest.param(False, True, False, False, id="public"),
            pytest.param(True, False, True, False, id="private"),
            pytest.param(_MISSING, False, False, True, id="missing"),
            pytest.param(None, False, False, True, id="null"),
            pytest.param("false", False, False, True, id="str-false"),
            pytest.param("true", False, False, True, id="str-true"),
            pytest.param(0, False, False, True, id="int-0"),
            pytest.param(1, False, False, True, id="int-1"),
        ],
    )
    def test_only_a_confirmed_public_repository_is_a_candidate(
        self, private: object, candidate: bool, excluded: bool, malformed: bool
    ) -> None:
        # Regression: code search runs with the caller's token and can return
        # private repositories; every item used to become a candidate by name.
        repo: dict[str, object] = {"full_name": "secret/private-repo"}
        if private is not self._MISSING:
            repo["private"] = private

        def handler(request: httpx.Request) -> httpx.Response:
            return _resp(200, _search([{"repository": repo}]))

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            problems: list[str] = []
            notes: list[str] = []
            found = curate.discover_vendors(client, set(), problems, notes)
        assert ("secret/private-repo" in found) is candidate
        assert bool(notes) is excluded
        assert bool(problems) is malformed
        report = curate.render_report([], found, problems, notes)
        assert ("secret/private-repo" in report) is candidate

    def test_private_hits_are_counted_never_named_and_keep_a_clean_exit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")
        items = [
            _hit("secret/private-repo", private=True),
            _hit("Secret/Private-Repo", private=True),
            _hit("other/hidden", private=True),
            _hit("new/vendor"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                return _resp(200, _search(items))
            if request.url.path == "/repos/a/known":
                return _resp(200, _meta())
            return _resp(200, _tree([]))

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        out = capsys.readouterr().out
        assert code == curate.EXIT_OK  # a deliberate exclusion, not a problem
        assert "secret" not in out.casefold() and "hidden" not in out
        assert "3 code-search hit(s) in private repositories excluded" in out
        assert "`new/vendor`: 1 matching files" in out
        assert "Code-search problems" not in out

    def test_a_failing_later_page_keeps_the_earlier_private_count(self) -> None:
        # Page 1 is full and carries private hits; page 2 fails. The failure is a
        # problem, and the private hits already read are still counted (not named).
        page_one = [_hit("secret/private-repo", private=True)] + [
            _vendor(i) for i in range(curate.SEARCH_PER_PAGE - 1)
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params["page"] == "1":
                return _resp(200, _search(page_one, total=500))
            return _resp(500)

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            problems: list[str] = []
            notes: list[str] = []
            found = curate.discover_vendors(client, set(), problems, notes)
        assert problems == ["page 2: HTTP 500"]  # no second "read x of y" problem
        assert len(found) == curate.SEARCH_PER_PAGE - 1
        assert notes == [
            "1 code-search hit(s) in private repositories excluded"
            " (a private repository is never a catalog candidate)"
        ]
        assert "secret" not in curate.render_report([], found, problems, notes)

    def test_non_utf8_catalog_exits_catalog_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "latin1.toml"
        bad.write_bytes(b'verified = "2026-10-16"\n# caf\xe9\n')
        code = curate.main(
            ["--catalog", str(bad)],
            transport=httpx.MockTransport(lambda r: _resp(200)),
            environ={"GITHUB_TOKEN": "tok"},
        )
        assert code == curate.EXIT_CATALOG
        assert "cannot load the catalog" in capsys.readouterr().out

    def test_discover_vendors_records_incomplete_results(self) -> None:
        # GitHub answers 200 with incomplete_results when its search timed out; the
        # hits it did return still count, but "none found" is no longer clean.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params["page"] == "1":
                items = [_hit("new/vendor")]
                return _resp(200, _search(items, incomplete=True))
            return _resp(200, _search([]))

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            problems: list[str] = []
            found = curate.discover_vendors(client, set(), problems)
        assert list(found) == ["new/vendor"]
        assert len(problems) == 1 and "page 1: incomplete_results" in problems[0]

    def test_incomplete_results_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                return _resp(200, _search([], incomplete=True))
            if request.url.path == "/repos/a/known":
                return _resp(200, _meta())
            return _resp(200, _tree([]))

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        out = capsys.readouterr().out
        assert code == curate.EXIT_REPORT_INCOMPLETE
        assert "none found (code search incomplete" in out

    @pytest.mark.parametrize("status", [403, 409, 429])
    def test_tree_failure_is_reported_not_counted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], status: int
    ) -> None:
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                return _resp(200, _search([]))
            if request.url.path == "/repos/a/known":
                return _resp(200, _meta(stargazers_count=3))
            return _resp(status, {"message": "nope"})

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        out = capsys.readouterr().out
        assert code == curate.EXIT_REPORT_INCOMPLETE
        assert f"`a/known`: source files unknown (tree: HTTP {status})" in out
        assert "None source files" not in out
        assert "stars 3" in out  # the metadata survives a failed tree

    @pytest.mark.parametrize(
        "body",
        [
            {k: v for k, v in _meta().items() if k != "default_branch"},
            _meta(default_branch=None),
            _meta(default_branch=""),
        ],
    )
    def test_missing_default_branch_is_a_tree_error_without_a_request(
        self, body: dict[str, object]
    ) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return _resp(200, body)

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            stats = curate.repo_stats(client, "a/b")
        assert paths == ["/repos/a/b"]
        assert stats["tree_error"] == "no default branch"
        assert "source_files" not in stats
        assert curate.stats_incomplete([stats])

    def test_branch_is_quoted_as_one_path_segment(self) -> None:
        raw: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/a/b":
                return _resp(200, _meta(default_branch="rel/1#x?y"))
            raw.append(request.url.raw_path)
            return _resp(200, _tree([{"type": "blob", "path": "x.py"}]))

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            stats = curate.repo_stats(client, "a/b")
        assert raw == [b"/repos/a/b/git/trees/rel%2F1%23x%3Fy?recursive=1"]
        assert stats["source_files"] == 1
        assert not curate.stats_incomplete([stats])

    def test_script_never_opens_the_catalog_for_writing(self) -> None:
        src = (_SCRIPTS / "curate_repo_catalog.py").read_text(encoding="utf-8")
        assert not re.search(r"write_text|write_bytes|open\([^)]*[\"'][wax+]", src)


_ENDPOINTS = {
    "repo": ("/repos/a/b", curate._REPO_FIELDS, _meta()),
    "tree": ("/repos/a/b/git/trees/main", curate._TREE_FIELDS, _tree([])),
    "search": ("/search/code", curate._SEARCH_FIELDS, _search([])),
}

# Written out, not derived from the field maps under test: every field each
# endpoint must validate, with the mistyped values that must be refused.
_REQUIRED: dict[str, dict[str, list[object]]] = {
    "repo": {
        "archived": [None, "false", 0, 1],
        "pushed_at": [None, "", 5],
        "stargazers_count": [None, "7", True, -1, 1.5],
    },
    "tree": {
        "tree": [None, "x", {}, [None], [{"type": "blob"}], [{"path": "x.py"}]],
        "truncated": [None, "false", 0],
    },
    "search": {
        "total_count": [None, "400", True, -1],
        "incomplete_results": [None, "false", 0],
        "items": [None, "nope", {}, 3],
    },
}


def _invalid_cells() -> list[object]:
    """Every non-200 status, body shape, missing field and mistyped field, per endpoint."""
    cells: list[object] = []
    for kind, required in _REQUIRED.items():
        good = _ENDPOINTS[kind][2]
        for status in (403, 404, 409, 422, 429, 500, 502):
            cells.append(pytest.param(kind, status, good, id=f"{kind}-{status}"))
        cells.append(pytest.param(kind, 200, "<html>", id=f"{kind}-not-json"))
        cells.append(pytest.param(kind, 200, ["x"], id=f"{kind}-list"))
        for name, bad_values in required.items():
            missing = {k: v for k, v in good.items() if k != name}
            cells.append(pytest.param(kind, 200, missing, id=f"{kind}-no-{name}"))
            for bad in bad_values:
                cells.append(
                    pytest.param(kind, 200, {**good, name: bad}, id=f"{kind}-{name}-{bad!r}")
                )
    return cells


class TestResponseStateSpace:
    """Every response kind x every status / shape cell, through the one ``_get``."""

    @pytest.mark.parametrize(("kind", "status", "body"), _invalid_cells())
    def test_every_invalid_cell_yields_no_data(self, kind: str, status: int, body: object) -> None:
        path, fields, _ = _ENDPOINTS[kind]

        def handler(request: httpx.Request) -> httpx.Response:
            if body == "<html>":
                return httpx.Response(status, content=b"<html>")
            return _resp(status, body)

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            data, problem = curate._get(client, path, fields)
        assert data is None and problem

    @pytest.mark.parametrize("kind", ["repo", "tree", "search"])
    def test_the_valid_cell_yields_data(self, kind: str) -> None:
        path, fields, good = _ENDPOINTS[kind]
        with curate.make_client("t", httpx.MockTransport(lambda r: _resp(200, good))) as client:
            data, problem = curate._get(client, path, fields)
        assert data == good and problem == ""

    @pytest.mark.parametrize(
        "entry",
        [None, "x.py", {"type": "blob"}, {"path": "x.py"}, {"type": 1, "path": "x.py"}],
    )
    def test_a_malformed_tree_entry_is_a_tree_error(self, entry: object) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/a/b":
                return _resp(200, _meta())
            return _resp(200, _tree([{"type": "blob", "path": "y.py"}, entry]))

        with curate.make_client("t", httpx.MockTransport(handler)) as client:
            stats = curate.repo_stats(client, "a/b")
        assert "source_files" not in stats
        assert curate.stats_incomplete([stats])

    @pytest.mark.parametrize(
        "over",
        [
            pytest.param({"archived": "false"}, id="archived-str"),
            pytest.param({"archived": None}, id="archived-null"),
            pytest.param({"pushed_at": None}, id="pushed_at-null"),
            pytest.param({"stargazers_count": True}, id="stars-bool"),
            pytest.param({"stargazers_count": "7"}, id="stars-str"),
        ],
    )
    def test_malformed_metadata_is_an_error_row_not_a_value(self, over: dict[str, object]) -> None:
        # Regression: archived="false" became True through bool(), and missing
        # pushed_at/stargazers_count became None, both with a clean exit.
        with curate.make_client("t", httpx.MockTransport(lambda r: _resp(200, _meta(**over)))) as c:
            stats = curate.repo_stats(c, "a/b")
        assert "error" in stats and "archived" not in stats
        assert curate.stats_incomplete([stats])

    @pytest.mark.parametrize("field", ["archived", "pushed_at", "stargazers_count"])
    def test_missing_metadata_field_exits_incomplete(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str
    ) -> None:
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")
        meta = {k: v for k, v in _meta().items() if k != field}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                return _resp(200, _search([]))
            if request.url.path == "/repos/a/known":
                return _resp(200, meta)
            return _resp(200, _tree([]))

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        assert code == curate.EXIT_REPORT_INCOMPLETE
        assert f"field {field!r} missing or malformed" in capsys.readouterr().out


class TestPagination:
    """Every way the search can stop, against ``total_count``."""

    @staticmethod
    def _pages(*pages: object) -> httpx.MockTransport:
        """``pages[n]`` answers page n+1: a body dict, or an int status."""

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            answer = pages[page - 1] if page <= len(pages) else pytest.fail(f"page {page}")
            return _resp(answer) if isinstance(answer, int) else _resp(200, answer)

        return httpx.MockTransport(handler)

    _FULL = [_vendor(i) for i in range(100)]

    @pytest.mark.parametrize(
        ("pages", "problem"),
        [
            pytest.param([_search([])], None, id="empty-total-0"),
            pytest.param([_search([_vendor(1)])], None, id="one-short-page-all-read"),
            pytest.param(
                [_search([_vendor(1)], total=10)],
                "page 1 came back short: read 1 of 10",
                id="short-page-unread",
            ),
            pytest.param(
                [_search([_vendor(1)], total=10), _search([], total=10)],
                "page 1 came back short: read 1 of 10",
                id="short-then-empty",
            ),
            pytest.param(
                [_search(_FULL, total=150), _search([], total=150)],
                "page 2 came back short: read 100 of 150",
                id="full-then-empty",
            ),
            pytest.param(
                [_search(_FULL, total=150), _search(_FULL[:50], total=150)],
                None,
                id="full-then-short-all-read",
            ),
            pytest.param([_search(_FULL, total=100)], None, id="one-full-page-all-read"),
            pytest.param([_search(_FULL, total=300)] * 3, None, id="cap-all-read"),
            pytest.param(
                [_search(_FULL, total=400)] * 3,
                "stopped at the 3-page cap: read 300 of 400",
                id="cap-unread",
            ),
            pytest.param(
                [_search(_FULL, total=150), 500], "page 2: HTTP 500", id="error-on-page-2"
            ),
            pytest.param(
                [_search([_vendor(1)], incomplete=True)],
                "page 1: incomplete_results",
                id="incomplete-results",
            ),
        ],
    )
    def test_search_stop_table(self, pages: list[object], problem: str | None) -> None:
        with curate.make_client("t", self._pages(*pages)) as client:
            problems: list[str] = []
            curate.discover_vendors(client, set(), problems)
        if problem is None:
            assert problems == []
        else:
            assert len(problems) == 1 and problems[0].startswith(problem)

    def test_empty_second_page_hiding_results_exits_incomplete(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Regression: total_count=10 with one item then an empty page used to exit 0.
        path = tmp_path / "catalog.toml"
        path.write_text(_catalog_text(["a/known"]), encoding="utf-8")
        search = self._pages(_search([_vendor(1)], total=10), _search([], total=10))

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/code":
                return search.handle_request(request)
            if request.url.path == "/repos/a/known":
                return _resp(200, _meta())
            return _resp(200, _tree([]))

        code = curate.main(
            ["--catalog", str(path)],
            transport=httpx.MockTransport(handler),
            environ={"GITHUB_TOKEN": "tok"},
        )
        assert code == curate.EXIT_REPORT_INCOMPLETE
        assert "read 1 of 10" in capsys.readouterr().out
