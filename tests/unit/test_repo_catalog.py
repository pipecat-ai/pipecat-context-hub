"""Tests for the repo catalog: loader contract, renderers and generated sections.

The catalog (`data/repo_catalog.toml`) is the single source for the generated
block in `.env.example`, the generated section in `docs/configuration.md` and
(later) corpus hints. A test here fails when they drift.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
import sys
from datetime import date
from importlib import resources
from pathlib import Path

import pytest

from pipecat_context_hub.shared import repo_catalog, repo_catalog_render
from pipecat_context_hub.shared.config import SourceConfig
from pipecat_context_hub.shared.repo_catalog import (
    REPO_SLUG_RE,
    Catalog,
    CatalogError,
    is_valid_repo_slug,
    load_catalog,
    parse_catalog,
)
from pipecat_context_hub.shared.repo_catalog_render import (
    DOCS_BEGIN,
    DOCS_END,
    ENV_BEGIN,
    ENV_END,
    SpliceError,
    bump_verified,
    render_docs,
    render_docs_block,
    render_env_block,
    render_env_example,
    splice,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_DOCS = _REPO_ROOT / "docs" / "configuration.md"
_JUSTFILE = _REPO_ROOT / "justfile"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_CATALOG_TOML = _REPO_ROOT / "src" / "pipecat_context_hub" / "data" / "repo_catalog.toml"

_DEFAULT_REPOS: list[str] = list(SourceConfig.model_fields["repos"].default)

_VALID = """\
verified = 2026-10-16

[groups.acme]
kind = "vendor"
description = "Acme voice"
aliases = ["acme"]

  [[groups.acme.repos]]
  slug = "acme/voice-demo"
  note = "demo"

  [[groups.acme.repos]]
  slug = "acme/swift-sdk"
  parseable = false

[groups.widgets]
kind = "topic"
description = "Widget topics"
aliases = ["widget", "gizmo"]

  [[groups.widgets.repos]]
  slug = "widgets/one"
  maintainer = true
"""


def _with(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


@pytest.fixture(autouse=True)
def _fresh_cache() -> object:
    load_catalog.cache_clear()
    yield
    load_catalog.cache_clear()


@pytest.fixture(scope="module")
def shipped_text() -> str:
    return _CATALOG_TOML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shipped_catalog(shipped_text: str) -> Catalog:
    return parse_catalog(shipped_text)


# --------------------------------------------------------------------------
# parse_catalog: schema and static invariants
# --------------------------------------------------------------------------


class TestParseCatalog:
    def test_valid_catalog_parses(self) -> None:
        cat = parse_catalog(_VALID)
        assert cat.verified == date(2026, 10, 16)
        assert [g.name for g in cat.groups] == ["acme", "widgets"]
        acme = cat.groups[0]
        assert acme.kind == "vendor"
        assert acme.aliases == ("acme",)
        assert [r.slug for r in acme.repos] == ["acme/voice-demo", "acme/swift-sdk"]
        assert acme.repos[0].parseable is True
        assert acme.repos[1].parseable is False
        assert cat.groups[1].repos[0].maintainer is True

    def test_shipped_catalog_loads(self, shipped_catalog: Catalog) -> None:
        assert shipped_catalog.groups

    def test_invalid_toml_is_a_catalog_error(self) -> None:
        with pytest.raises(CatalogError):
            parse_catalog("verified = [")

    @pytest.mark.parametrize("key", ["private", "archived", "status"])
    def test_visibility_keys_rejected_on_repo(self, key: str) -> None:
        text = _with(
            _VALID, '  slug = "acme/voice-demo"\n', f'  slug = "acme/voice-demo"\n  {key} = true\n'
        )
        with pytest.raises(CatalogError, match=key):
            parse_catalog(text)

    @pytest.mark.parametrize("key", ["private", "archived", "status"])
    def test_visibility_keys_rejected_on_group(self, key: str) -> None:
        text = _with(_VALID, 'kind = "vendor"\n', f'kind = "vendor"\n{key} = "x"\n')
        with pytest.raises(CatalogError, match=key):
            parse_catalog(text)

    def test_unknown_key_rejected(self) -> None:
        text = _with(_VALID, 'kind = "vendor"\n', 'kind = "vendor"\nwat = 1\n')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    def test_slug_in_two_groups_rejected(self) -> None:
        text = _with(_VALID, 'slug = "widgets/one"', 'slug = "acme/voice-demo"')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    def test_slug_collision_is_casefolded(self) -> None:
        text = _with(_VALID, 'slug = "widgets/one"', 'slug = "ACME/Voice-Demo"')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    def test_duplicate_slug_within_group_rejected(self) -> None:
        text = _with(_VALID, 'slug = "acme/swift-sdk"', 'slug = "acme/voice-demo"')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    def test_alias_in_two_groups_rejected(self) -> None:
        text = _with(_VALID, 'aliases = ["widget", "gizmo"]', 'aliases = ["widget", "acme"]')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    def test_alias_collision_is_casefolded(self) -> None:
        text = _with(_VALID, 'aliases = ["widget", "gizmo"]', 'aliases = ["widget", "ACME"]')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    def test_default_repo_rejected(self) -> None:
        text = _with(_VALID, 'slug = "widgets/one"', f'slug = "{_DEFAULT_REPOS[0]}"')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    def test_default_repo_rejected_casefolded(self) -> None:
        text = _with(_VALID, 'slug = "widgets/one"', f'slug = "{_DEFAULT_REPOS[0].upper()}"')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    @pytest.mark.parametrize("kind", ["other", "Vendor", ""])
    def test_bad_kind_rejected(self, kind: str) -> None:
        text = _with(_VALID, 'kind = "topic"', f'kind = "{kind}"')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    @pytest.mark.parametrize("value", ['"yes"', "1", '"true"'])
    def test_bad_parseable_rejected(self, value: str) -> None:
        text = _with(_VALID, "parseable = false", f"parseable = {value}")
        with pytest.raises(CatalogError):
            parse_catalog(text)

    @pytest.mark.parametrize(
        "value",
        ['"2026-10-16"', "2026-10-16T10:00:00", "20261016", "not-a-date"],
    )
    def test_bad_verified_rejected(self, value: str) -> None:
        text = _with(_VALID, "verified = 2026-10-16", f"verified = {value}")
        with pytest.raises(CatalogError):
            parse_catalog(text)

    def test_missing_verified_rejected(self) -> None:
        with pytest.raises(CatalogError):
            parse_catalog(_with(_VALID, "verified = 2026-10-16\n", ""))

    def test_malformed_slug_rejected(self) -> None:
        text = _with(_VALID, 'slug = "widgets/one"', 'slug = "no-slash"')
        with pytest.raises(CatalogError):
            parse_catalog(text)

    def test_no_groups_rejected(self) -> None:
        with pytest.raises(CatalogError):
            parse_catalog("verified = 2026-10-16\n")


# --------------------------------------------------------------------------
# load_catalog: cached None on failure, logs once, tainted env does not break
# --------------------------------------------------------------------------


class TestSlugGrammar:
    """One owner/repo grammar for the catalog parser, ``repos`` and the writer.

    It is the ingester's grammar, so every slug accepted here can be cloned.
    """

    @pytest.mark.parametrize(
        "slug", ["a/b", "NVIDIA-AI-Blueprints/nemotron-voice-agent", "aaronsu11/Dum-E", "x/a.b_c-d"]
    )
    def test_accepts(self, slug: str) -> None:
        assert is_valid_repo_slug(slug)

    @pytest.mark.parametrize(
        "slug",
        ["noslash", "a/b/c", "a/", "/b", "a b/c", "./b", "a/..", "-a/b", "a/.b", "_a/b", "a/b\n"],
    )
    def test_rejects(self, slug: str) -> None:
        assert not is_valid_repo_slug(slug)

    @pytest.mark.parametrize("slug", ["-lead/repo", "owner/.dot", "owner/b\n"])
    def test_catalog_parser_uses_the_same_grammar(self, slug: str) -> None:
        """Before the grammars were unified the catalog accepted these, so a
        catalog slug could be impossible to ``repos add``."""
        with pytest.raises(CatalogError):
            parse_catalog(_with(_VALID, 'slug = "widgets/one"', f'slug = "{slug}"'))

    def test_every_shipped_slug_passes(self, shipped_catalog: Catalog) -> None:
        for group in shipped_catalog.groups:
            for repo in group.repos:
                assert is_valid_repo_slug(repo.slug), repo.slug

    def test_ingester_uses_the_same_pattern(self) -> None:
        from pipecat_context_hub.services.ingest import github_ingest

        assert github_ingest._REPO_SLUG_RE is REPO_SLUG_RE


class TestCatalogLookups:
    def test_find_repo_is_casefolded_and_returns_the_catalog_spelling(
        self, shipped_catalog: Catalog
    ) -> None:
        repo = shipped_catalog.find_repo("AARONSU11/dum-e")
        assert repo is not None and repo.slug == "aaronsu11/Dum-E"
        assert shipped_catalog.find_repo("nobody/nothing") is None

    def test_find_group_is_casefolded(self, shipped_catalog: Catalog) -> None:
        group = shipped_catalog.find_group("AWS")
        assert group is not None and group.name == "aws"
        assert shipped_catalog.find_group("nope") is None

    def test_slugs_is_every_slug_casefolded(self, shipped_catalog: Catalog) -> None:
        expected = {r.slug.casefold() for g in shipped_catalog.groups for r in g.repos}
        assert shipped_catalog.slugs() == expected


class TestLoadCatalog:
    def test_loads_shipped_catalog_and_caches(self) -> None:
        first = load_catalog()
        assert first is not None
        assert load_catalog() is first

    def test_failure_returns_none_and_logs_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: "verified = [")
        load_catalog.cache_clear()
        with caplog.at_level(logging.DEBUG, logger="pipecat_context_hub"):
            results = [load_catalog() for _ in range(3)]
        assert results == [None, None, None]
        records = [r for r in caplog.records if r.name.startswith("pipecat_context_hub")]
        assert len(records) == 1

    def test_read_failure_returns_none_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> str:
            raise OSError("no such resource")

        monkeypatch.setattr(repo_catalog, "_read_catalog_text", boom)
        load_catalog.cache_clear()
        assert load_catalog() is None

    def test_schema_violation_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = _with(_VALID, 'kind = "vendor"\n', 'kind = "vendor"\nprivate = true\n')
        monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: bad)
        load_catalog.cache_clear()
        assert load_catalog() is None

    def test_patched_text_is_what_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: _VALID)
        load_catalog.cache_clear()
        cat = load_catalog()
        assert cat is not None
        assert [g.name for g in cat.groups] == ["acme", "widgets"]

    def test_tainted_env_slug_does_not_break_loading(
        self, monkeypatch: pytest.MonkeyPatch, shipped_catalog: Catalog
    ) -> None:
        slug = shipped_catalog.groups[0].repos[0].slug
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REPOS", slug)
        load_catalog.cache_clear()
        cat = load_catalog()
        assert cat is not None
        assert slug in {r.slug for g in cat.groups for r in g.repos}


# --------------------------------------------------------------------------
# Import boundary and packaged resource
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="python -I subprocess boundary test is POSIX-only")
def test_repo_catalog_imports_nothing_from_cli_server_services() -> None:
    code = (
        "import sys\n"
        "import pipecat_context_hub.shared.repo_catalog\n"
        "bad = sorted(k for k in sys.modules if k.startswith("
        "('pipecat_context_hub.cli', 'pipecat_context_hub.server', "
        "'pipecat_context_hub.services')))\n"
        "print(repr(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_repo_catalog_module_has_no_forbidden_imports_statically() -> None:
    tree = ast.parse(Path(repo_catalog.__file__).read_text(encoding="utf-8"))
    forbidden = ("cli", "server", "services")
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for name in names:
            parts = name.split(".")
            if parts[0] == "pipecat_context_hub" and len(parts) > 1:
                assert parts[1] not in forbidden, name


def test_catalog_resource_path_is_a_file() -> None:
    resource = resources.files("pipecat_context_hub").joinpath("data/repo_catalog.toml")
    assert resource.is_file()


# --------------------------------------------------------------------------
# Shipped data: vendor-per-group rule
# --------------------------------------------------------------------------

_VENDOR_SLUG_HINTS = {
    "aws": "aws",
    "tavus": "tavus",
    "murf": "murf",
    "supermemory": "supermemory",
}


class TestVendorRule:
    def test_each_named_vendor_has_its_own_group(self, shipped_catalog: Catalog) -> None:
        by_name = {g.name: g for g in shipped_catalog.groups}
        for vendor in _VENDOR_SLUG_HINTS:
            assert vendor in by_name, f"missing a dedicated {vendor!r} group"
            assert by_name[vendor].kind == "vendor"
            assert vendor in {a.casefold() for a in by_name[vendor].aliases}

    def test_no_catch_all_group_names(self, shipped_catalog: Catalog) -> None:
        pattern = re.compile(r"(^|-)(other|others|misc|miscellaneous|various|providers)(-|$)")
        for group in shipped_catalog.groups:
            assert not pattern.search(group.name), group.name

    def test_vendor_groups_do_not_mix_vendors(self, shipped_catalog: Catalog) -> None:
        # Murf, Supermemory and Tavus slugs must not share a group with each other.
        wanted = {
            "murf": "murf-ai/pipecat-murf-tts",
            "supermemory": "supermemoryai/pipecat-memory",
            "tavus": "aws-samples/sample-voice-ai-tavus-avatar-demo",
        }
        located: dict[str, str] = {}
        for group in shipped_catalog.groups:
            for repo in group.repos:
                for vendor, slug in wanted.items():
                    if repo.slug.casefold() == slug.casefold():
                        located[vendor] = group.name
        assert located == {"murf": "murf", "supermemory": "supermemory", "tavus": "tavus"}

    def test_every_vendor_group_has_a_unique_name_alias(self, shipped_catalog: Catalog) -> None:
        for group in shipped_catalog.groups:
            if group.kind == "vendor":
                assert group.name.casefold() in {a.casefold() for a in group.aliases}


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


class TestRendererPurity:
    def test_extra_repos_env_does_not_change_output(
        self, monkeypatch: pytest.MonkeyPatch, shipped_catalog: Catalog
    ) -> None:
        before_env = render_env_block(shipped_catalog)
        before_docs = render_docs_block(shipped_catalog)
        monkeypatch.setenv("PIPECAT_HUB_EXTRA_REPOS", "someone/else,another/one")
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REPOS", "x/y")
        assert render_env_block(shipped_catalog) == before_env
        assert render_docs_block(shipped_catalog) == before_docs
        assert "someone/else" not in before_env
        assert "someone/else" not in before_docs

    def test_renders_without_load_env_layers(
        self, monkeypatch: pytest.MonkeyPatch, shipped_catalog: Catalog
    ) -> None:
        from pipecat_context_hub.shared import env_loading

        def boom(*_a: object, **_k: object) -> None:
            raise AssertionError("renderer must not load env layers")

        monkeypatch.setattr(env_loading, "load_env_layers", boom)
        assert render_env_block(shipped_catalog)
        assert render_docs_block(shipped_catalog)

    def test_renderer_code_uses_defaults_not_env_layers(self) -> None:
        for mod in (repo_catalog_render, repo_catalog):
            assert mod.__file__ is not None
            tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    assert node.attr not in {"effective_repos", "load_env_layers"}
                if isinstance(node, ast.Name):
                    assert node.id != "load_env_layers"
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "pipecat_context_hub.shared.env_loading"
                    assert not (node.module or "").startswith("tomlkit")
                if isinstance(node, ast.Import):
                    assert not any(a.name.startswith("tomlkit") for a in node.names)

    def test_default_repos_are_not_rendered(self, shipped_catalog: Catalog) -> None:
        block = render_env_block(shipped_catalog)
        docs = render_docs_block(shipped_catalog)
        listed = {
            slug.casefold()
            for value in re.findall(r'PIPECAT_HUB_EXTRA_REPOS="([^"]*)"', block)
            for slug in value.split(",")
        }
        listed |= {m.casefold() for m in re.findall(r"`([^`\s]+/[^`\s]+)`", docs)}
        assert listed
        assert listed.isdisjoint(d.casefold() for d in _DEFAULT_REPOS)

    def test_block_lists_every_catalog_slug(self, shipped_catalog: Catalog) -> None:
        block = render_env_block(shipped_catalog)
        docs = render_docs_block(shipped_catalog)
        defaults = {d.casefold() for d in _DEFAULT_REPOS}
        for group in shipped_catalog.groups:
            for repo in group.repos:
                if repo.slug.casefold() in defaults:
                    continue
                assert repo.slug in block
                assert repo.slug in docs

    def test_rendering_is_deterministic(self, shipped_catalog: Catalog) -> None:
        assert render_env_block(shipped_catalog) == render_env_block(shipped_catalog)
        assert render_docs_block(shipped_catalog) == render_docs_block(shipped_catalog)


class TestGeneratedFilesMatchCatalog:
    """The drift guard: checked-in `.env.example` and docs equal the catalog."""

    def test_env_example_matches_catalog(self, shipped_catalog: Catalog) -> None:
        current = _ENV_EXAMPLE.read_text(encoding="utf-8")
        assert render_env_example(current, shipped_catalog) == current, (
            "`.env.example` generated block is stale; run `just catalog-regen`"
        )

    def test_docs_section_matches_catalog(self, shipped_catalog: Catalog) -> None:
        current = _DOCS.read_text(encoding="utf-8")
        assert render_docs(current, shipped_catalog) == current, (
            "docs/configuration.md catalog section is stale; run `just catalog-regen`"
        )

    def test_every_non_default_catalog_slug_is_in_env_example(
        self, shipped_catalog: Catalog
    ) -> None:
        text = _ENV_EXAMPLE.read_text(encoding="utf-8")
        defaults = {d.casefold() for d in _DEFAULT_REPOS}
        expected = {
            r.slug
            for g in shipped_catalog.groups
            for r in g.repos
            if r.slug.casefold() not in defaults
        }
        assert expected
        for slug in expected:
            assert slug in text

    def test_markers_present_exactly_once(self) -> None:
        env = _ENV_EXAMPLE.read_text(encoding="utf-8")
        docs = _DOCS.read_text(encoding="utf-8")
        assert env.count(ENV_BEGIN) == 1 and env.count(ENV_END) == 1
        assert docs.count(DOCS_BEGIN) == 1 and docs.count(DOCS_END) == 1

    def test_env_example_outside_markers_has_no_hand_listed_catalog_slugs(
        self, shipped_catalog: Catalog
    ) -> None:
        text = _ENV_EXAMPLE.read_text(encoding="utf-8")
        outside = text[: text.index(ENV_BEGIN)] + text[text.index(ENV_END) :]
        defaults = {d.casefold() for d in _DEFAULT_REPOS}
        for group in shipped_catalog.groups:
            for repo in group.repos:
                if repo.slug.casefold() in defaults:
                    continue
                assert repo.slug not in outside, f"{repo.slug} listed outside the generated block"


# --------------------------------------------------------------------------
# splice
# --------------------------------------------------------------------------


class TestSplice:
    BEGIN = "# BEGIN"
    END = "# END"

    def _doc(self, body: str) -> str:
        return f"head line\r\n  indented\n{self.BEGIN}\n{body}\n{self.END}\ntail\tline \n\n"

    def test_changes_only_between_markers(self) -> None:
        original = self._doc("old body")
        out = splice(original, self.BEGIN, self.END, "new body\nline 2")
        assert out == self._doc("new body\nline 2")
        pre = original[: original.index(self.BEGIN)]
        post = original[original.index(self.END) :]
        assert out.startswith(pre)
        assert out.endswith(post)

    def test_idempotent(self) -> None:
        once = splice(self._doc("a"), self.BEGIN, self.END, "b")
        assert splice(once, self.BEGIN, self.END, "b") == once

    def test_missing_begin_raises(self) -> None:
        with pytest.raises(SpliceError):
            splice(f"x\n{self.END}\n", self.BEGIN, self.END, "b")

    def test_missing_end_raises(self) -> None:
        with pytest.raises(SpliceError):
            splice(f"x\n{self.BEGIN}\n", self.BEGIN, self.END, "b")

    def test_duplicated_markers_raise(self) -> None:
        dup = f"{self.BEGIN}\na\n{self.END}\n{self.BEGIN}\nb\n{self.END}\n"
        with pytest.raises(SpliceError):
            splice(dup, self.BEGIN, self.END, "c")

    def test_reversed_markers_raise(self) -> None:
        with pytest.raises(SpliceError):
            splice(f"{self.END}\nbody\n{self.BEGIN}\n", self.BEGIN, self.END, "c")

    def test_env_splice_with_changed_catalog_leaves_outside_identical(self) -> None:
        cat_a = parse_catalog(_VALID)
        cat_b = parse_catalog(_with(_VALID, 'slug = "widgets/one"', 'slug = "widgets/two"'))
        original = f"prose\n  keep me  \n{ENV_BEGIN}\nstale\n{ENV_END}\nPIPECAT_HUB_X=1\n"
        out = render_env_example(original, cat_b)
        assert out != render_env_example(original, cat_a)
        assert out.startswith(f"prose\n  keep me  \n{ENV_BEGIN}\n")
        assert out.endswith(f"\n{ENV_END}\nPIPECAT_HUB_X=1\n")
        assert "widgets/two" in out and "widgets/one" not in out

    def test_docs_splice_with_changed_catalog_leaves_outside_identical(self) -> None:
        cat = parse_catalog(_VALID)
        original = f"# Title\n\n| a | b |\n|--|--|\n\n{DOCS_BEGIN}\nstale\n{DOCS_END}\n\nafter\n"
        out = render_docs(original, cat)
        assert out.startswith(f"# Title\n\n| a | b |\n|--|--|\n\n{DOCS_BEGIN}\n")
        assert out.endswith(f"\n{DOCS_END}\n\nafter\n")
        assert "acme/voice-demo" in out

    def test_render_without_markers_raises(self) -> None:
        cat = parse_catalog(_VALID)
        with pytest.raises(SpliceError):
            render_env_example("no markers here\n", cat)
        with pytest.raises(SpliceError):
            render_docs("no markers here\n", cat)


# --------------------------------------------------------------------------
# bump_verified
# --------------------------------------------------------------------------


class TestBumpVerified:
    def test_changes_only_top_level_verified_line(self, shipped_text: str) -> None:
        today = date(2030, 1, 2)
        out = bump_verified(shipped_text, today)
        old_lines = shipped_text.splitlines()
        new_lines = out.splitlines()
        assert len(old_lines) == len(new_lines)
        diff = [(a, b) for a, b in zip(old_lines, new_lines, strict=True) if a != b]
        assert len(diff) == 1
        assert diff[0][0].startswith("verified")
        assert diff[0][1].startswith("verified")
        assert "2030-01-02" in diff[0][1]
        assert parse_catalog(out).verified == today

    def test_indented_verified_inside_group_untouched(self) -> None:
        # An indented `verified` is an unknown key, so use a raw text (not parsed
        # until after the bump would fail); the bump re-validates, so keep the
        # group-level line inside a comment-free but schema-valid place: a comment.
        text = _with(
            _VALID,
            'description = "Acme voice"\n',
            'description = "Acme voice"\n  # verified = 2020-01-01\n',
        )
        out = bump_verified(text, date(2031, 5, 6))
        assert "  # verified = 2020-01-01" in out
        assert out.splitlines()[0] == "verified = 2031-05-06"
        assert parse_catalog(out).verified == date(2031, 5, 6)

    def test_keeps_trailing_comment(self) -> None:
        text = _with(_VALID, "verified = 2026-10-16", "verified = 2026-10-16  # audit")
        out = bump_verified(text, date(2027, 1, 1))
        assert out.splitlines()[0] == "verified = 2027-01-01  # audit"

    def test_rewritten_text_is_revalidated(self) -> None:
        # A catalog that is invalid for another reason must not be written out.
        broken = _with(_VALID, 'kind = "vendor"', 'kind = "bogus"')
        with pytest.raises(CatalogError):
            bump_verified(broken, date(2027, 1, 1))

    def test_does_not_use_cached_load_catalog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: "verified = [")
        load_catalog.cache_clear()
        assert load_catalog() is None
        out = bump_verified(_VALID, date(2027, 1, 1))
        assert parse_catalog(out).verified == date(2027, 1, 1)

    def test_no_top_level_verified_line_raises(self) -> None:
        with pytest.raises(ValueError):
            bump_verified(_with(_VALID, "verified = 2026-10-16\n", ""), date(2027, 1, 1))

    def test_two_top_level_verified_lines_raise(self) -> None:
        with pytest.raises(ValueError):
            bump_verified("verified = 2026-01-01\n" + _VALID, date(2027, 1, 1))


class TestMainCheck:
    def _stage(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_text: str) -> Path:
        root = tmp_path / "a" / "b" / "c"
        (root / "src" / "pipecat_context_hub" / "data").mkdir(parents=True)
        (root / "docs").mkdir()
        (root / "src" / "pipecat_context_hub" / "data" / "repo_catalog.toml").write_text(
            _CATALOG_TOML.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (root / ".env.example").write_text(env_text, encoding="utf-8")
        (root / "docs" / "configuration.md").write_text(
            _DOCS.read_text(encoding="utf-8"), encoding="utf-8"
        )
        monkeypatch.setattr(repo_catalog_render, "_repo_root", lambda: root)
        return root

    def test_check_passes_when_in_sync_and_exits_1_when_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shipped_catalog: Catalog
    ) -> None:
        env_text = _ENV_EXAMPLE.read_text(encoding="utf-8")
        root = self._stage(tmp_path, monkeypatch, env_text)
        assert repo_catalog_render.main(["--check"]) == 0

        stale = env_text.replace("\n" + render_env_block(shipped_catalog), "\n# stale\n", 1)
        if stale == env_text:  # block layout differs; corrupt between the markers
            head, rest = env_text.split(ENV_BEGIN, 1)
            _, tail = rest.split(ENV_END, 1)
            stale = f"{head}{ENV_BEGIN}\n# stale\n{ENV_END}{tail}"
        (root / ".env.example").write_text(stale, encoding="utf-8")
        assert repo_catalog_render.main(["--check"]) == 1
        # --check never writes
        assert (root / ".env.example").read_text(encoding="utf-8") == stale
        # a plain run regenerates it
        assert repo_catalog_render.main([]) == 0
        assert repo_catalog_render.main(["--check"]) == 0

    def test_bump_verified_writes_nothing_when_a_render_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The catalog is not bumped on disk before the renders succeed."""
        root = self._stage(tmp_path, monkeypatch, "no markers here\n")
        catalog_file = root / "src" / "pipecat_context_hub" / "data" / "repo_catalog.toml"
        before = catalog_file.read_text(encoding="utf-8")
        docs_before = (root / "docs" / "configuration.md").read_text(encoding="utf-8")
        with pytest.raises(SpliceError):
            repo_catalog_render.main(["--bump-verified"])
        assert catalog_file.read_text(encoding="utf-8") == before
        assert (root / "docs" / "configuration.md").read_text(encoding="utf-8") == docs_before


# --------------------------------------------------------------------------
# justfile recipe and CI wiring
# --------------------------------------------------------------------------


def _recipe_body(name: str) -> str | None:
    lines = _JUSTFILE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line and not line[0].isspace() and not line.startswith("#"):
            head = line.split(":", 1)[0].split()
            if head and head[0] == name:
                body: list[str] = [line]
                for nxt in lines[i + 1 :]:
                    if nxt and not nxt[0].isspace():
                        break
                    body.append(nxt)
                return "\n".join(body)
    return None


class TestJustRecipe:
    def test_catalog_regen_recipe_exists(self) -> None:
        assert _recipe_body("catalog-regen") is not None

    def test_recipe_runs_renderer_module_and_passes_args(self) -> None:
        body = _recipe_body("catalog-regen")
        assert body is not None
        assert "pipecat_context_hub.shared.repo_catalog_render" in body
        assert "*args" in body.splitlines()[0]
        assert "{{args}}" in body

    def test_recipe_body_does_not_bump_verified(self) -> None:
        body = _recipe_body("catalog-regen")
        assert body is not None
        assert "--bump-verified" not in body


def test_windows_ci_file_list_includes_this_module() -> None:
    assert "tests/unit/test_repo_catalog.py" in _CI_YML.read_text(encoding="utf-8")
