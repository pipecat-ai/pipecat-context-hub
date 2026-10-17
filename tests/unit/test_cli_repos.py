"""Tests for the ``repos list|add|remove`` commands (dev plan PR1 Phase 2d).

One test id per clause of the parent plan's ``[PR1]`` ``repos`` table bullet;
the clause-to-test mapping is in the class docstrings. The standing contract
under test: ``repos add X`` then ``repos list`` shows X, ``effective_repos``
contains X, and ``repos remove X`` undoes it, across every layer combination
(with the ``--config-only`` qualification: under a shadowing layer the slug
lands in ``config.toml`` but not in ``effective_repos``).

Two harnesses:

* ``Hub`` runs the real loaders (real env var, cwd ``.env``, ``config.toml``)
  through ``main`` and recomputes ``effective_repos`` "in a new process".
* The shadow-decision rows patch ``env_loading.load_env_layers`` to return a
  controlled provenance map instead; none of them patches ``os.environ`` for
  the decision.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from pipecat_context_hub.cli import main
from pipecat_context_hub.shared import env_loading, repo_catalog
from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.exit_codes import EXIT_INVALID_INPUT
from pipecat_context_hub.shared.repo_catalog import default_repos, load_catalog, parse_catalog
from tests.unit._config_trust_cases import TRUST_REFUSAL_CASES

KEY = "PIPECAT_HUB_EXTRA_REPOS"
_posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")

# Controlled catalog. Mixed-case slug (Acme/Alpha-Two), a parseable = false
# member (alpha-ios), a member we taint in some tests (alpha-taint), a group of
# only parseable = false members (gamma) and a one-repo group we taint (delta).
CATALOG_TEXT = """\
verified = 2026-10-16

[groups.alpha]
kind = "topic"
description = "Alpha"
aliases = ["alpha"]
  [[groups.alpha.repos]]
  slug = "acme/alpha-one"
  [[groups.alpha.repos]]
  slug = "Acme/Alpha-Two"
  [[groups.alpha.repos]]
  slug = "acme/alpha-ios"
  parseable = false
  [[groups.alpha.repos]]
  slug = "acme/alpha-taint"

[groups.beta]
kind = "vendor"
description = "Beta"
aliases = ["beta"]
  [[groups.beta.repos]]
  slug = "acme/beta-one"
  [[groups.beta.repos]]
  slug = "acme/beta-two"

[groups.gamma]
kind = "topic"
description = "Gamma, nothing parseable"
aliases = ["gamma"]
  [[groups.gamma.repos]]
  slug = "acme/gamma-ios"
  parseable = false

[groups.delta]
kind = "topic"
description = "Delta, will be tainted"
aliases = ["delta"]
  [[groups.delta.repos]]
  slug = "acme/delta-one"
"""

ALPHA_ONE = "acme/alpha-one"
ALPHA_TWO = "Acme/Alpha-Two"  # the catalog's spelling
BETA_ONE = "acme/beta-one"
BETA_TWO = "acme/beta-two"
UNLISTED = "someone/unlisted-repo"


@pytest.fixture(autouse=True)
def _catalog(monkeypatch: pytest.MonkeyPatch):
    """Serve the controlled catalog; the cached loader is cleared around every test."""
    load_catalog.cache_clear()
    monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: CATALOG_TEXT)
    yield
    load_catalog.cache_clear()


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """``repos`` is offline: any socket or subprocess (the ``gh`` path) fails the test."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("repos must make no network call and spawn no subprocess")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)


class Hub:
    """A scratch cwd, a trusted ``config.toml`` location and a CLI runner."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.cwd = tmp_path / "work"
        self.cwd.mkdir()
        monkeypatch.chdir(self.cwd)
        self.cfg_dir = tmp_path / "cfg"
        self.cfg_dir.mkdir()
        if os.name == "posix":
            self.cfg_dir.chmod(0o700)
        self.config = self.cfg_dir / "config.toml"
        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(self.config))
        self._baseline: dict[str, str] | None = None
        self.runner = CliRunner()

    # -- arranging layers ------------------------------------------------
    def write_config(self, text: str) -> None:
        self.config.write_text(text)
        if os.name == "posix":
            self.config.chmod(0o600)

    def config_list(self, *slugs: str) -> None:
        arr = ", ".join(f'"{s}"' for s in slugs)
        self.write_config(f"{KEY} = [{arr}]\n")

    def write_dotenv(self, text: str) -> None:
        (self.cwd / ".env").write_text(text)

    def env(self, **values: str) -> None:
        for k, v in values.items():
            self.monkeypatch.setenv(k, v)
            if self._baseline is not None:
                self._baseline[k] = v

    # -- running ---------------------------------------------------------
    def _fresh_process_env(self) -> None:
        """Reset ``PIPECAT_HUB_*`` to the arranged state (the loaders write ``os.environ``)."""
        if self._baseline is None:
            self._baseline = {k: v for k, v in os.environ.items() if k.startswith("PIPECAT_HUB_")}
        for k in [k for k in os.environ if k.startswith("PIPECAT_HUB_")]:
            del os.environ[k]
        os.environ.update(self._baseline)

    def run(self, *args: str) -> Result:
        self._fresh_process_env()
        return self.runner.invoke(main, ["repos", *args])

    # -- observing -------------------------------------------------------
    def in_toml(self) -> list[str] | None:
        """The ``config.toml`` list via the reader's seam; ``None`` when absent."""
        if not self.config.exists():
            return None
        value = env_loading.parse_global_config(self.config).get(KEY, [])
        assert isinstance(value, list)
        return [str(v) for v in value]

    def effective(self) -> list[str]:
        """``effective_repos`` as a fresh process would resolve it right now."""
        self._fresh_process_env()
        env_loading.load_env_layers()
        result = list(HubConfig().sources.effective_repos)
        self._fresh_process_env()
        return result

    def snapshot(self) -> bytes | None:
        return self.config.read_bytes() if self.config.exists() else None


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Hub:
    return Hub(tmp_path, monkeypatch)


def _lower(slugs: list[str]) -> list[str]:
    return [s.casefold() for s in slugs]


def _tags(stdout: str) -> dict[str, str]:
    """slug -> tag for the ``  slug  [tag]`` rows of ``repos list`` (first section)."""
    rows: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if line.endswith("]") and "  [" in line:
            slug, _, tag = line.rpartition("  [")
            rows.setdefault(slug, tag[:-1])
    return rows


def _patch_provenance(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    monkeypatch.setattr(env_loading, "load_env_layers", lambda: mapping)


# ---------------------------------------------------------------------------
# Contract: add -> list -> effective_repos -> remove, across every layer
# ---------------------------------------------------------------------------


def _arrange(hub: Hub, layer: str) -> None:
    """Five layer combinations; ``BETA_TWO`` is the one the layer itself holds."""
    if layer == "none":
        return
    if layer == "config":
        hub.config_list(BETA_TWO)
    elif layer == "env":
        hub.env(**{KEY: BETA_TWO})
    elif layer == "dotenv":
        hub.write_dotenv(f"{KEY}={BETA_TWO}\n")
    elif layer == "blank-dotenv":
        hub.write_dotenv(f"{KEY}=\n")
        hub.config_list(BETA_TWO)
    else:  # pragma: no cover
        raise AssertionError(layer)


SHADOWED = {"env": "env", "dotenv": ".env"}
UNSHADOWED = ["none", "config", "blank-dotenv"]


@pytest.mark.usefixtures("no_network")
class TestLayerContract:
    """Clause: layers x (add / remove / list / add --config-only), asserting
    ``effective_repos``, the shadow refusal and a golden ``repos list`` per
    layer; each row states whether ``effective_repos`` contains the slug."""

    @pytest.mark.parametrize("layer", UNSHADOWED)
    def test_add_then_list_then_remove_round_trips(self, hub: Hub, layer: str):
        _arrange(hub, layer)
        before = hub.effective()

        added = hub.run("add", ALPHA_ONE)
        assert added.exit_code == 0, added.output
        assert f"Added {ALPHA_ONE}" in added.output
        assert ALPHA_ONE in (hub.in_toml() or [])
        assert ALPHA_ONE in hub.effective()  # effective_repos contains X
        listed = hub.run("list")
        assert _tags(listed.stdout).get(ALPHA_ONE) == "catalog"  # list shows X

        removed = hub.run("remove", ALPHA_ONE)
        assert removed.exit_code == 0, removed.output
        assert f"Removed {ALPHA_ONE}" in removed.output
        assert ALPHA_ONE not in (hub.in_toml() or [])
        assert hub.effective() == before  # remove undid it
        assert ALPHA_ONE not in _tags(hub.run("list").stdout)

    @pytest.mark.parametrize("layer", list(SHADOWED))
    def test_add_under_a_shadowing_layer_is_refused_and_changes_nothing(self, hub: Hub, layer: str):
        _arrange(hub, layer)
        hub.config_list("acme/other")
        before_bytes, before_eff = hub.snapshot(), hub.effective()

        result = hub.run("add", ALPHA_ONE)

        assert result.exit_code == EXIT_INVALID_INPUT
        assert f"layer `{SHADOWED[layer]}`" in result.output
        assert hub.snapshot() == before_bytes
        assert hub.effective() == before_eff

    @pytest.mark.parametrize("layer", list(SHADOWED))
    def test_add_config_only_under_a_shadowing_layer_writes_but_is_not_in_effect(
        self, hub: Hub, layer: str
    ):
        _arrange(hub, layer)
        before_eff = hub.effective()

        result = hub.run("add", "--config-only", ALPHA_ONE)

        assert result.exit_code == 0, result.output
        assert f"Added {ALPHA_ONE}" in result.output
        assert ALPHA_ONE in (hub.in_toml() or [])
        # The qualification: in config.toml, NOT in effective_repos.
        assert ALPHA_ONE not in hub.effective()
        assert hub.effective() == before_eff
        # ...and `repos list` names the layer hiding it.
        listed = hub.run("list").stdout
        assert f"hidden by layer `{SHADOWED[layer]}`" in listed
        assert ALPHA_ONE in listed.split("hidden by", 1)[1]

    @pytest.mark.parametrize("layer", list(SHADOWED))
    def test_remove_config_only_under_a_shadowing_layer_leaves_effective_alone(
        self, hub: Hub, layer: str
    ):
        _arrange(hub, layer)
        hub.run("add", "--config-only", ALPHA_ONE)
        before_eff = hub.effective()

        refused = hub.run("remove", ALPHA_ONE)
        assert refused.exit_code == EXIT_INVALID_INPUT
        assert ALPHA_ONE in (hub.in_toml() or [])

        done = hub.run("remove", "--config-only", ALPHA_ONE)
        assert done.exit_code == 0, done.output
        assert ALPHA_ONE not in (hub.in_toml() or [])
        assert hub.effective() == before_eff

    @pytest.mark.parametrize("layer", ["none", *SHADOWED, "config", "blank-dotenv"])
    def test_list_never_changes_anything(self, hub: Hub, layer: str):
        _arrange(hub, layer)
        before = hub.snapshot()
        result = hub.run("list")
        assert result.exit_code == 0, result.output
        assert hub.snapshot() == before


@pytest.mark.usefixtures("no_network")
class TestListGoldens:
    """Clause: a golden ``repos list`` per layer including the consulted cwd,
    and the ``default`` / ``catalog`` / ``unlisted`` tags. The consulted-cwd
    line is built at runtime from ``Path`` so it holds on every platform."""

    @staticmethod
    def _golden(hub: Hub, extras: list[tuple[str, str]], hidden: str | None = None) -> str:
        defaults = default_repos()
        rows = [(s, "default") for s in defaults] + extras
        lines = [
            f"Consulted .env in cwd: {Path.cwd()}",
            f"Effective repos ({len(rows)}):",
            *[f"  {slug}  [{tag}]" for slug, tag in rows],
        ]
        if hidden is not None:
            lines.append(hidden)
        return "\n".join(lines) + "\n"

    def test_none(self, hub: Hub):
        result = hub.run("list")
        assert result.exit_code == 0
        assert result.stdout == self._golden(hub, [])

    def test_config_only_layer(self, hub: Hub):
        hub.config_list(ALPHA_ONE, UNLISTED)
        result = hub.run("list")
        assert result.stdout == self._golden(hub, [(ALPHA_ONE, "catalog"), (UNLISTED, "unlisted")])

    def test_real_env_layer_names_what_it_hides(self, hub: Hub):
        hub.env(**{KEY: BETA_ONE})
        hub.config_list(ALPHA_ONE)
        result = hub.run("list")
        hidden = (
            "In config.toml but hidden by layer `env` (the real environment):\n"
            f"  {ALPHA_ONE}  [catalog]"
        )
        assert result.stdout == self._golden(hub, [(BETA_ONE, "catalog")], hidden)

    def test_cwd_dotenv_layer_names_what_it_hides(self, hub: Hub):
        hub.write_dotenv(f"{KEY}={BETA_ONE}\n")
        hub.config_list(ALPHA_ONE)
        result = hub.run("list")
        hidden = (
            "In config.toml but hidden by layer `.env` (the `.env` in the current directory):\n"
            f"  {ALPHA_ONE}  [catalog]"
        )
        assert result.stdout == self._golden(hub, [(BETA_ONE, "catalog")], hidden)

    def test_blank_dotenv_does_not_shadow_config(self, hub: Hub):
        hub.write_dotenv(f"{KEY}=\n")
        hub.config_list(ALPHA_ONE)
        result = hub.run("list")
        assert result.stdout == self._golden(hub, [(ALPHA_ONE, "catalog")])
        assert "hidden by" not in result.stdout

    def test_tags_default_catalog_and_unlisted(self, hub: Hub):
        hub.config_list(ALPHA_TWO, UNLISTED)
        tags = _tags(hub.run("list").stdout)
        assert tags[default_repos()[0]] == "default"
        assert tags[ALPHA_TWO] == "catalog"
        assert tags[UNLISTED] == "unlisted"

    def test_tag_matches_the_catalog_case_insensitively(self, hub: Hub):
        hub.config_list("ACME/ALPHA-TWO")
        assert _tags(hub.run("list").stdout)["ACME/ALPHA-TWO"] == "catalog"

    def test_cross_cwd_consults_that_cwds_dotenv(self, hub: Hub, tmp_path: Path):
        """A `serve` launched from another directory reads a different `.env`."""
        other = tmp_path / "other"
        other.mkdir()
        (other / ".env").write_text(f"{KEY}={BETA_TWO}\n")
        hub.write_dotenv(f"{KEY}={BETA_ONE}\n")

        here = hub.run("list")
        hub.monkeypatch.chdir(other)
        there = hub.run("list")

        assert f"Consulted .env in cwd: {hub.cwd}" in here.stdout
        assert f"Consulted .env in cwd: {other}" in there.stdout
        assert BETA_ONE in _tags(here.stdout) and BETA_TWO not in _tags(here.stdout)
        assert BETA_TWO in _tags(there.stdout) and BETA_ONE not in _tags(there.stdout)


# ---------------------------------------------------------------------------
# The check-order table
# ---------------------------------------------------------------------------

_SHADOW_MARKER = "layer `"  # only the check-3 refusal / note names a layer this way


@pytest.mark.usefixtures("no_network")
class TestCheckOrder:
    """Clause: the fixed check order for a positional slug (default/tainted,
    then syntax and catalog membership, then shadow, then no-op, then write).

    Each "two checks at once" row sets a shadowing env var so check 3 WOULD
    fire, and asserts the earlier check's message wins instead."""

    @pytest.mark.parametrize("verb", ["add", "remove"])
    def test_default_repo_is_refused(self, hub: Hub, verb: str):
        default = default_repos()[0]
        result = hub.run(verb, default)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "default repo" in result.output
        assert hub.snapshot() is None  # never created

    def test_default_repo_is_refused_case_insensitively(self, hub: Hub):
        result = hub.run("add", default_repos()[0].upper())
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "default repo" in result.output

    @pytest.mark.parametrize("verb", ["add", "remove"])
    @pytest.mark.parametrize("extra", [[], ["--unlisted"]])
    def test_tainted_repo_is_refused(self, hub: Hub, verb: str, extra: list[str]):
        hub.env(PIPECAT_HUB_TAINTED_REPOS=f"{BETA_ONE},{UNLISTED}")
        for slug in (BETA_ONE, UNLISTED):
            result = hub.run(verb, *extra, slug)
            assert result.exit_code == EXIT_INVALID_INPUT
            assert "tainted" in result.output
        assert hub.snapshot() is None

    def test_default_beats_shadow(self, hub: Hub):
        hub.env(**{KEY: BETA_ONE})
        result = hub.run("add", default_repos()[0])
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "default repo" in result.output
        assert _SHADOW_MARKER not in result.output

    def test_tainted_beats_shadow(self, hub: Hub):
        hub.env(**{KEY: BETA_ONE}, PIPECAT_HUB_TAINTED_REPOS=ALPHA_ONE)
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "tainted" in result.output
        assert _SHADOW_MARKER not in result.output

    def test_default_beats_catalog_membership(self, hub: Hub):
        """Check 1 precedes check 2: a default repo is not in the catalog, yet the
        message is 'default', not 'not in the repo catalog'."""
        result = hub.run("add", default_repos()[0])
        assert "not in the repo catalog" not in result.output

    def test_tainted_beats_catalog_membership(self, hub: Hub):
        hub.env(PIPECAT_HUB_TAINTED_REPOS=UNLISTED)
        result = hub.run("add", UNLISTED)  # no --unlisted: check 2 would also refuse
        assert "tainted" in result.output
        assert "not in the repo catalog" not in result.output

    @pytest.mark.parametrize("bad", ["noslash", "a/b/c", "acme/", "/repo", "ac me/repo", "a/.."])
    def test_malformed_slug_beats_shadow(self, hub: Hub, bad: str):
        hub.env(**{KEY: BETA_ONE})
        result = hub.run("add", "--unlisted", bad)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "not a valid repo slug" in result.output
        assert _SHADOW_MARKER not in result.output

    def test_not_in_catalog_beats_shadow(self, hub: Hub):
        hub.env(**{KEY: BETA_ONE})
        result = hub.run("add", UNLISTED)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "--unlisted" in result.output
        assert _SHADOW_MARKER not in result.output

    def test_unlisted_flag_on_catalog_slug_beats_shadow(self, hub: Hub):
        hub.env(**{KEY: BETA_ONE})
        result = hub.run("add", "--unlisted", ALPHA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "drop --unlisted" in result.output
        assert _SHADOW_MARKER not in result.output

    def test_shadow_beats_noop_for_a_slug_already_in_config(self, hub: Hub):
        """Check 3 precedes check 4: already in config.toml, still refused."""
        hub.env(**{KEY: BETA_ONE})
        hub.config_list(ALPHA_ONE)
        before = hub.snapshot()
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "layer `env`" in result.output
        assert "Already in config.toml" not in result.output
        assert hub.snapshot() == before

    def test_slug_present_only_in_a_shadowing_dotenv_exits_1_not_0(self, hub: Hub):
        hub.write_dotenv(f"{KEY}={ALPHA_ONE}\n")
        assert ALPHA_ONE in hub.effective()
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "layer `.env`" in result.output
        assert hub.snapshot() is None

    def test_noop_names_the_shadowing_layer_with_config_only(self, hub: Hub):
        hub.env(**{KEY: BETA_ONE})
        hub.config_list(ALPHA_ONE)
        before = hub.snapshot()
        result = hub.run("add", "--config-only", ALPHA_ONE)
        assert result.exit_code == 0
        assert f"Already in config.toml: {ALPHA_ONE}" in result.output
        assert "layer `env`" in result.output
        assert hub.snapshot() == before

    @pytest.mark.parametrize("shadow", [None, "env", "dotenv"])
    def test_remove_of_an_unconfigured_slug(self, hub: Hub, shadow: str | None):
        """Three rows: no other layer exits 0; a shadowing layer without
        --config-only exits 1; with --config-only exits 0 and names the layer."""
        if shadow:
            _arrange(hub, shadow)
        without = hub.run("remove", ALPHA_ONE)
        if shadow is None:
            assert without.exit_code == 0
            assert f"Not in config.toml: {ALPHA_ONE}" in without.output
            assert _SHADOW_MARKER not in without.output
            return
        assert without.exit_code == EXIT_INVALID_INPUT
        assert f"layer `{SHADOWED[shadow]}`" in without.output
        with_flag = hub.run("remove", "--config-only", ALPHA_ONE)
        assert with_flag.exit_code == 0
        assert f"Not in config.toml: {ALPHA_ONE}" in with_flag.output
        assert f"layer `{SHADOWED[shadow]}`" in with_flag.output

    def test_write_prints_the_refresh_reminder_and_golden_stdout(self, hub: Hub):
        added = hub.run("add", ALPHA_ONE)
        assert added.stdout.splitlines()[0] == f"Added {ALPHA_ONE}"
        assert "pipecat-context-hub refresh" in added.stdout
        again = hub.run("add", ALPHA_ONE)
        assert again.stdout == f"Already in config.toml: {ALPHA_ONE}\n"
        removed = hub.run("remove", ALPHA_ONE)
        assert removed.stdout.splitlines()[0] == f"Removed {ALPHA_ONE}"
        assert "pipecat-context-hub refresh" in removed.stdout
        gone = hub.run("remove", ALPHA_ONE)
        assert gone.stdout == f"Not in config.toml: {ALPHA_ONE}\n"


@pytest.mark.usefixtures("no_network")
class TestUnlistedRules:
    """Clause: the ``--unlisted`` syntax check, a well-formed non-catalog slug
    without ``--unlisted`` (exit 1), and ``--unlisted`` on a catalog slug (exit 1)."""

    def test_non_catalog_slug_without_the_flag_is_refused(self, hub: Hub):
        result = hub.run("add", UNLISTED)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "--unlisted" in result.output
        assert hub.snapshot() is None

    def test_catalog_slug_with_the_flag_is_refused(self, hub: Hub):
        result = hub.run("add", "--unlisted", ALPHA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "drop --unlisted" in result.output
        assert hub.snapshot() is None

    def test_catalog_slug_with_the_flag_is_refused_case_insensitively(self, hub: Hub):
        result = hub.run("add", "--unlisted", ALPHA_TWO.upper())
        assert result.exit_code == EXIT_INVALID_INPUT
        assert hub.snapshot() is None

    def test_unlisted_slug_is_written_as_typed_and_tagged_unlisted(self, hub: Hub):
        result = hub.run("add", "--unlisted", "Someone/Unlisted-Repo")
        assert result.exit_code == 0, result.output
        assert hub.in_toml() == ["Someone/Unlisted-Repo"]
        assert "Someone/Unlisted-Repo" in hub.effective()
        assert _tags(hub.run("list").stdout)["Someone/Unlisted-Repo"] == "unlisted"

    @pytest.mark.parametrize("bad", ["noslash", "a/b/c", "owner/"])
    def test_malformed_unlisted_slug_is_refused(self, hub: Hub, bad: str):
        result = hub.run("add", "--unlisted", bad)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert hub.snapshot() is None

    def test_catalog_add_writes_the_catalog_spelling(self, hub: Hub):
        result = hub.run("add", ALPHA_TWO.lower())
        assert result.exit_code == 0, result.output
        assert hub.in_toml() == [ALPHA_TWO]
        assert f"Added {ALPHA_TWO}" in result.output

    def test_remove_of_an_unlisted_slug_needs_the_flag_too(self, hub: Hub):
        hub.config_list(UNLISTED)
        without = hub.run("remove", UNLISTED)
        assert without.exit_code == EXIT_INVALID_INPUT
        assert hub.in_toml() == [UNLISTED]
        done = hub.run("remove", "--unlisted", UNLISTED)
        assert done.exit_code == 0, done.output
        assert hub.in_toml() == []

    def test_remove_with_the_flag_on_a_catalog_slug_is_refused(self, hub: Hub):
        hub.config_list(ALPHA_ONE)
        result = hub.run("remove", "--unlisted", ALPHA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert hub.in_toml() == [ALPHA_ONE]

    def test_a_slug_with_neither_slug_nor_group_is_refused(self, hub: Hub):
        for verb in ("add", "remove"):
            assert hub.run(verb).exit_code == EXIT_INVALID_INPUT


@pytest.mark.usefixtures("no_network")
class TestCaseHandling:
    """Clause: a case-variant add (no-op) and remove."""

    def test_case_variant_add_is_a_noop(self, hub: Hub):
        hub.config_list("ACME/ALPHA-ONE")
        before = hub.snapshot()
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == 0
        assert "Already in config.toml" in result.output
        assert "Added" not in result.output
        assert hub.snapshot() == before  # byte-identical: no duplicate written
        assert hub.in_toml() == ["ACME/ALPHA-ONE"]

    def test_case_variant_unlisted_add_is_a_noop(self, hub: Hub):
        hub.config_list("Someone/Unlisted-Repo")
        before = hub.snapshot()
        result = hub.run("add", "--unlisted", UNLISTED)
        assert result.exit_code == 0
        assert "Already in config.toml" in result.output
        assert hub.snapshot() == before

    def test_case_variant_remove_removes_the_configured_spelling(self, hub: Hub):
        hub.config_list("ACME/BETA-ONE", ALPHA_ONE)
        result = hub.run("remove", BETA_ONE)
        assert result.exit_code == 0
        assert "Removed ACME/BETA-ONE" in result.output
        assert hub.in_toml() == [ALPHA_ONE]


@pytest.mark.usefixtures("no_network")
class TestGroup:
    """Clause: ``--group`` (partial membership, ``parseable = false`` and tainted
    skipped and reported, an all-tainted group (exit 0, ``Nothing to add``),
    unknown group, nothing to change, mutual exclusion with a slug and
    ``--unlisted``, shadow with and without ``--config-only``)."""

    def test_partial_membership_skips_configured_and_reports_skipped(self, hub: Hub):
        hub.env(PIPECAT_HUB_TAINTED_REPOS="acme/alpha-taint")
        hub.config_list(ALPHA_ONE)
        result = hub.run("add", "--group", "alpha")
        assert result.exit_code == 0, result.output
        assert f"Added {ALPHA_TWO}" in result.output
        assert f"Already in config.toml: {ALPHA_ONE}" in result.output
        assert "Skipped (parseable = false): acme/alpha-ios" in result.output
        assert "Skipped (tainted): acme/alpha-taint" in result.output
        assert hub.in_toml() == [ALPHA_ONE, ALPHA_TWO]
        assert "pipecat-context-hub refresh" in result.output

    def test_parseable_false_member_is_never_added(self, hub: Hub):
        hub.run("add", "--group", "alpha")
        assert "acme/alpha-ios" not in _lower(hub.in_toml() or [])

    def test_tainted_member_is_never_added(self, hub: Hub):
        hub.env(PIPECAT_HUB_TAINTED_REPOS="acme/alpha-taint")
        hub.run("add", "--group", "alpha")
        assert "acme/alpha-taint" not in _lower(hub.in_toml() or [])

    def test_all_tainted_group_is_nothing_to_add(self, hub: Hub):
        hub.env(PIPECAT_HUB_TAINTED_REPOS="acme/delta-one")
        result = hub.run("add", "--group", "delta")
        assert result.exit_code == 0
        assert "Nothing to add" in result.output
        assert "Skipped (tainted): acme/delta-one" in result.output
        assert hub.snapshot() is None

    def test_group_of_only_parseable_false_members_is_nothing_to_add(self, hub: Hub):
        result = hub.run("add", "--group", "gamma")
        assert result.exit_code == 0
        assert "Nothing to add" in result.output
        assert "Skipped (parseable = false): acme/gamma-ios" in result.output
        assert hub.snapshot() is None

    def test_nothing_to_change_when_every_member_is_configured(self, hub: Hub):
        hub.config_list(BETA_ONE, BETA_TWO)
        before = hub.snapshot()
        result = hub.run("add", "--group", "beta")
        assert result.exit_code == 0
        assert "Nothing to add" in result.output
        assert hub.snapshot() == before

    def test_unknown_group_lists_the_valid_ones(self, hub: Hub):
        for verb in ("add", "remove"):
            result = hub.run(verb, "--group", "nope")
            assert result.exit_code == EXIT_INVALID_INPUT
            for name in ("alpha", "beta", "gamma", "delta"):
                assert name in result.output
        assert hub.snapshot() is None

    def test_mutually_exclusive_with_a_slug(self, hub: Hub):
        for verb in ("add", "remove"):
            result = hub.run(verb, ALPHA_ONE, "--group", "alpha")
            assert result.exit_code == EXIT_INVALID_INPUT
        assert hub.snapshot() is None

    def test_mutually_exclusive_with_unlisted(self, hub: Hub):
        for verb in ("add", "remove"):
            result = hub.run(verb, "--group", "alpha", "--unlisted")
            assert result.exit_code == EXIT_INVALID_INPUT
        assert hub.snapshot() is None

    def test_remove_removes_only_the_groups_configured_repos(self, hub: Hub):
        hub.config_list(ALPHA_ONE, BETA_ONE, ALPHA_TWO, UNLISTED)
        result = hub.run("remove", "--group", "alpha")
        assert result.exit_code == 0, result.output
        assert f"Removed {ALPHA_ONE}" in result.output
        assert f"Removed {ALPHA_TWO}" in result.output
        assert hub.in_toml() == [BETA_ONE, UNLISTED]

    def test_remove_with_nothing_present_is_nothing_to_remove(self, hub: Hub):
        hub.config_list(BETA_ONE)
        before = hub.snapshot()
        result = hub.run("remove", "--group", "alpha")
        assert result.exit_code == 0
        assert "Nothing to remove" in result.output
        assert hub.snapshot() == before

    def test_remove_group_with_no_config_file_is_nothing_to_remove(self, hub: Hub):
        result = hub.run("remove", "--group", "alpha")
        assert result.exit_code == 0
        assert "Nothing to remove" in result.output
        assert hub.snapshot() is None

    @pytest.mark.parametrize("layer", list(SHADOWED))
    def test_shadow_without_config_only_is_refused(self, hub: Hub, layer: str):
        _arrange(hub, layer)
        for verb in ("add", "remove"):
            result = hub.run(verb, "--group", "alpha")
            assert result.exit_code == EXIT_INVALID_INPUT
            assert f"layer `{SHADOWED[layer]}`" in result.output
        assert hub.snapshot() is None

    @pytest.mark.parametrize("layer", list(SHADOWED))
    def test_shadow_with_config_only_writes_but_is_not_in_effect(self, hub: Hub, layer: str):
        _arrange(hub, layer)
        before_eff = hub.effective()
        result = hub.run("add", "--config-only", "--group", "alpha")
        assert result.exit_code == 0, result.output
        assert _lower(hub.in_toml() or []) == [ALPHA_ONE, ALPHA_TWO.lower(), "acme/alpha-taint"]
        assert hub.effective() == before_eff
        removed = hub.run("remove", "--config-only", "--group", "alpha")
        assert removed.exit_code == 0, removed.output
        assert hub.in_toml() == []

    def test_group_name_is_checked_before_shadow(self, hub: Hub):
        hub.env(**{KEY: BETA_ONE})
        result = hub.run("add", "--group", "nope")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "unknown group" in result.output
        assert _SHADOW_MARKER not in result.output


# ---------------------------------------------------------------------------
# Shadow decision: provenance map only
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_network")
class TestShadowDecisionReadsOnlyTheMap:
    """The decision reads ``ctx.obj["env_provenance"]`` and nothing else: the
    map is patched in via ``env_loading.load_env_layers``, ``os.environ`` is
    never set for these rows."""

    def test_blank_real_env_nonblank_dotenv_and_config_toml_resolves_to_config(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ):
        hub.config_list(BETA_ONE)
        _patch_provenance(monkeypatch, {KEY: "config.toml"})
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == 0, result.output
        assert f"Added {ALPHA_ONE}" in result.output
        assert hub.in_toml() == [BETA_ONE, ALPHA_ONE]

    def test_blank_dotenv_alone_key_omitted_no_refusal(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_provenance(monkeypatch, {})
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == 0, result.output
        assert hub.in_toml() == [ALPHA_ONE]

    def test_blank_dotenv_plus_config_toml_no_refusal(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ):
        hub.config_list(BETA_ONE)
        _patch_provenance(monkeypatch, {KEY: "config.toml", "PIPECAT_HUB_OTHER": ".env"})
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == 0, result.output

    def test_whitespace_only_real_env_key_omitted_no_refusal(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_provenance(monkeypatch, {})
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize("layer", ["env", ".env"])
    def test_nonblank_env_or_dotenv_is_refused_naming_the_layer(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch, layer: str
    ):
        _patch_provenance(monkeypatch, {KEY: layer})
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert f"layer `{layer}`" in result.output
        assert hub.snapshot() is None

    @pytest.mark.parametrize("layer", ["env", ".env"])
    def test_config_only_lets_a_shadowed_add_through(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch, layer: str
    ):
        _patch_provenance(monkeypatch, {KEY: layer})
        result = hub.run("add", "--config-only", ALPHA_ONE)
        assert result.exit_code == 0, result.output
        assert hub.in_toml() == [ALPHA_ONE]

    def test_another_keys_layer_is_irrelevant(self, hub: Hub, monkeypatch: pytest.MonkeyPatch):
        _patch_provenance(monkeypatch, {"PIPECAT_HUB_STALE_AFTER_DAYS": "env"})
        assert hub.run("add", ALPHA_ONE).exit_code == 0

    def test_list_hidden_section_follows_the_map(self, hub: Hub, monkeypatch: pytest.MonkeyPatch):
        hub.config_list(ALPHA_ONE)
        _patch_provenance(monkeypatch, {KEY: "env"})
        out = hub.run("list").stdout
        assert "hidden by layer `env`" in out
        _patch_provenance(monkeypatch, {KEY: "config.toml"})
        assert "hidden by" not in hub.run("list").stdout


class TestCarrier:
    """Clause: through ``main`` both loaders run once per ``repos`` invocation,
    and the map from ``load_env_layers`` alone drives the shadow decision."""

    def test_both_loaders_run_once_per_repos_invocation(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[str] = []

        def _cwd() -> dict[str, str]:
            calls.append("cwd")
            return {}

        def _global() -> dict[str, str]:
            calls.append("global")
            return {}

        monkeypatch.setattr(env_loading, "load_cwd_dotenv", _cwd)
        monkeypatch.setattr(env_loading, "load_global_config", _global)
        for args in (["list"], ["add", ALPHA_ONE], ["remove", ALPHA_ONE]):
            calls.clear()
            result = hub.run(*args)
            assert result.exit_code == 0, result.output
            assert calls == ["cwd", "global"], args

    def test_controlled_map_drives_the_decision_and_no_loader_runs(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ):
        def _never(*_a: Any, **_k: Any) -> dict[str, str]:
            raise AssertionError("a layer was re-read")

        monkeypatch.setattr(env_loading, "load_env_layers", lambda: {KEY: ".env"})
        monkeypatch.setattr(env_loading, "load_cwd_dotenv", _never)
        monkeypatch.setattr(env_loading, "load_global_config", _never)

        refused = hub.run("add", ALPHA_ONE)
        assert refused.exit_code == EXIT_INVALID_INPUT
        assert "layer `.env`" in refused.output
        allowed = hub.run("add", "--config-only", ALPHA_ONE)
        assert allowed.exit_code == 0, allowed.output
        assert hub.run("list").exit_code == 0


# ---------------------------------------------------------------------------
# load_catalog() returning None
# ---------------------------------------------------------------------------


def _break_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    load_catalog.cache_clear()
    monkeypatch.setattr(repo_catalog, "_read_catalog_text", lambda: "not [valid toml")


@pytest.mark.usefixtures("no_network")
class TestCatalogFailedToLoad:
    """Clause: ``load_catalog()`` returning ``None``. One row per command."""

    @pytest.mark.parametrize(
        "args",
        [
            ["add", ALPHA_ONE],
            ["add", "--unlisted", UNLISTED],
            ["remove", ALPHA_ONE],
            ["remove", "--unlisted", UNLISTED],
            ["add", "--group", "alpha"],
            ["remove", "--group", "alpha"],
        ],
    )
    def test_add_and_remove_refuse_and_leave_the_file_untouched(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch, args: list[str]
    ):
        hub.config_list(BETA_ONE)
        before = hub.snapshot()
        _break_catalog(monkeypatch)
        result = hub.run(*args)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "catalog failed to load" in result.output
        assert hub.snapshot() == before

    def test_add_with_no_config_file_does_not_create_one(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ):
        _break_catalog(monkeypatch)
        assert hub.run("add", ALPHA_ONE).exit_code == EXIT_INVALID_INPUT
        assert hub.snapshot() is None

    def test_list_prints_effective_tags_non_default_unlisted_and_warns_once(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        hub.config_list(ALPHA_ONE, UNLISTED)
        _break_catalog(monkeypatch)
        with caplog.at_level(logging.WARNING):
            result = hub.run("list")
        assert result.exit_code == 0, result.output
        tags = _tags(result.stdout)
        assert tags[ALPHA_ONE] == "unlisted"  # no catalog to say otherwise
        assert tags[UNLISTED] == "unlisted"
        assert tags[default_repos()[0]] == "default"
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        loader = [r for r in warnings if "catalog failed to load" in r.getMessage()]
        assert len(loader) == 1  # the loader's own log-once
        assert [r for r in warnings if r.name.endswith("cli_repos")] == []  # no second warning
        # A repeat invocation in the same process is served from the cache: still one.
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            hub.run("list")
        assert not [r for r in caplog.records if "catalog failed to load" in r.getMessage()]


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _bad_config(hub: Hub, kind: str) -> None:
    """Arrange a config.toml the writer/reader refuses, by error type."""
    if kind == "write-refused":  # ConfigWriteRefused: a value the loader also ignores
        hub.write_config(f"{KEY} = {{ a = 1 }}\n")
    elif kind == "invalid-toml":  # TOMLDecodeError
        hub.write_config("this is = = not toml\n")
    elif kind == "not-trusted":  # ConfigNotTrusted
        hub.write_config(f'{KEY} = ["{BETA_ONE}"]\n')
        hub.config.chmod(0o666)
    else:  # pragma: no cover
        raise AssertionError(kind)


_ERROR_KINDS = [
    pytest.param("write-refused", id="ConfigWriteRefused"),
    pytest.param("invalid-toml", id="TOMLDecodeError"),
    pytest.param("not-trusted", id="ConfigNotTrusted", marks=_posix_only),
]


@pytest.mark.usefixtures("no_network")
class TestErrorMapping:
    """Clause: ``ConfigWriteRefused``, ``ConfigNotTrusted`` and
    ``TOMLDecodeError`` map to ``EXIT_INVALID_INPUT`` for add/remove (file
    untouched); ``repos list`` catches all three, prints the effective list,
    names the ignored config.toml and the reason, and exits 0; plus the
    'no config.toml location' row. One row each."""

    @pytest.mark.parametrize("kind", _ERROR_KINDS)
    @pytest.mark.parametrize("verb", ["add", "remove"])
    def test_add_and_remove_exit_1_and_leave_the_file_untouched(
        self, hub: Hub, kind: str, verb: str
    ):
        _bad_config(hub, kind)
        before = hub.snapshot()
        result = hub.run(verb, BETA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "config.toml was not changed" in result.output
        assert hub.snapshot() == before

    @pytest.mark.parametrize("verb", ["add", "remove"])
    def test_no_config_location_exits_1(self, hub: Hub, verb: str):
        hub.env(PIPECAT_HUB_CONFIG_FILE="")
        result = hub.run(verb, BETA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "config.toml was not changed" in result.output

    @pytest.mark.parametrize("kind", _ERROR_KINDS)
    def test_list_prints_effective_names_the_ignored_file_and_exits_0(self, hub: Hub, kind: str):
        _bad_config(hub, kind)
        hub.env(**{KEY: ALPHA_ONE})  # a higher layer still gives a real effective list
        result = hub.run("list")
        assert result.exit_code == 0, result.output
        assert _tags(result.stdout)[ALPHA_ONE] == "catalog"
        assert _tags(result.stdout)[default_repos()[0]] == "default"
        problem = [ln for ln in result.stdout.splitlines() if ln.startswith("config.toml:")]
        assert len(problem) == 1
        assert "ignoring" in problem[0]
        assert "config.toml" in problem[0]
        assert {
            "write-refused": "a value the loader ignores",
            "invalid-toml": "not valid TOML",
            "not-trusted": "not trusted",
        }[kind] in problem[0]

    def test_list_reports_no_config_toml_location(self, hub: Hub):
        hub.env(PIPECAT_HUB_CONFIG_FILE="")
        result = hub.run("list")
        assert result.exit_code == 0, result.output
        assert "no config.toml location" in result.stdout
        assert _tags(result.stdout)[default_repos()[0]] == "default"

    @pytest.mark.parametrize("build", TRUST_REFUSAL_CASES)
    def test_every_trust_refusal_is_a_clean_exit_1_for_add(
        self, hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build: Any
    ):
        path = build(tmp_path / "trust", monkeypatch)
        try:
            monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(path))
        except ValueError:
            pytest.skip("an embedded NUL cannot travel through the environment")
        with pytest.raises(env_loading.ConfigNotTrusted):
            env_loading.read_trusted_config(path)
        result = hub.run("add", ALPHA_ONE)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "config.toml was not changed" in result.output

    def test_a_missing_config_is_not_an_error(self, hub: Hub):
        assert not hub.config.exists()
        listed = hub.run("list")
        assert listed.exit_code == 0
        assert "config.toml:" not in listed.stdout
        assert hub.run("remove", ALPHA_ONE).exit_code == 0


# ---------------------------------------------------------------------------
# Offline, and no tomlkit at CLI import
# ---------------------------------------------------------------------------


class TestOfflineAndImportCost:
    """Clause: ``repos add`` makes no network call (every add row above runs
    under the ``no_network`` fixture; this pins the fixture itself)."""

    def test_the_no_network_fixture_trips_on_a_socket_and_a_subprocess(self, no_network):
        with pytest.raises(AssertionError):
            socket.socket()
        with pytest.raises(AssertionError):
            subprocess.run(["true"])

    @pytest.mark.usefixtures("no_network")
    def test_add_of_every_kind_makes_no_network_call(self, hub: Hub):
        assert hub.run("add", ALPHA_ONE).exit_code == 0
        assert hub.run("add", "--unlisted", UNLISTED).exit_code == 0
        assert hub.run("add", "--group", "beta").exit_code == 0
        assert hub.run("add", "--config-only", "--group", "beta").exit_code == 0

    @pytest.mark.skipif(os.name != "posix", reason="python -I subprocess check is POSIX-only")
    def test_importing_the_cli_does_not_import_tomlkit(self, tmp_path: Path):
        code = (
            "import sys\n"
            "import pipecat_context_hub.cli as cli\n"
            "print('repos' in cli.main.commands)\n"
            "print('tomlkit' in sys.modules)\n"
            "print('pipecat_context_hub.shared.config_writer' in sys.modules)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["True", "False", "False"]


class TestRegistration:
    def test_repos_is_registered_with_its_three_commands(self):
        assert "repos" in main.commands
        group = main.commands["repos"]
        assert sorted(group.commands) == ["add", "list", "remove"]  # type: ignore[attr-defined]

    def test_the_shared_test_catalog_is_valid(self):
        catalog = parse_catalog(CATALOG_TEXT)
        assert [g.name for g in catalog.groups] == ["alpha", "beta", "gamma", "delta"]

    def test_config_toml_is_parsed_only_through_the_seam(self, hub: Hub):
        """Writing through the CLI leaves a file the reader's seam parses to the intended list."""
        hub.run("add", ALPHA_ONE)
        hub.run("add", "--unlisted", UNLISTED)
        parsed = tomllib.loads(hub.config.read_text())
        assert parsed[KEY] == [ALPHA_ONE, UNLISTED]
        assert env_loading.parse_global_config(hub.config)[KEY] == [ALPHA_ONE, UNLISTED]


# ---------------------------------------------------------------------------
# Error boundary: no repos command ends in a traceback
# ---------------------------------------------------------------------------


def _clean_exit(result: Result) -> None:
    """Exited through ``SystemExit`` (or normally), never an uncaught exception."""
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output


@pytest.mark.usefixtures("no_network")
class TestErrorBoundary:
    """{list, add, remove} x {cwd removed, config dir unwritable, config unreadable}:
    exit 0 or EXIT_INVALID_INPUT with a home-redacted message, never a traceback."""

    @_posix_only
    @pytest.mark.parametrize(
        "args",
        [("add", ALPHA_ONE), ("remove", ALPHA_ONE), ("add", "--group", "beta")],
        ids=["add", "remove", "add-group"],
    )
    def test_unwritable_config_dir_is_a_clean_exit_1(self, hub: Hub, args: tuple[str, ...]):
        if os.getuid() == 0:
            pytest.skip("root bypasses directory permission bits")
        if args[0] == "remove":
            hub.config_list(ALPHA_ONE)
        else:
            hub.write_config("")
        before = hub.snapshot()
        hub.cfg_dir.chmod(0o500)
        try:
            result = hub.run(*args)
        finally:
            hub.cfg_dir.chmod(0o700)
        _clean_exit(result)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "config.toml was not changed" in result.output
        assert str(Path.home()) not in result.output
        assert hub.snapshot() == before

    def test_an_oserror_from_the_rename_is_a_clean_exit_1(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ):
        def boom(*_a: Any, **_k: Any) -> None:
            raise PermissionError(13, "Permission denied", str(Path.home() / "secret"))

        monkeypatch.setattr(os, "replace", boom)
        result = hub.run("add", ALPHA_ONE)
        _clean_exit(result)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert str(Path.home()) not in result.output
        assert hub.snapshot() is None

    @_posix_only
    @pytest.mark.parametrize("verb", ["list", "add", "remove"])
    def test_unreadable_config_is_clean(self, hub: Hub, verb: str):
        if os.getuid() == 0:
            pytest.skip("root bypasses file permission bits")
        hub.config_list(ALPHA_ONE)
        hub.config.chmod(0o000)
        try:
            result = hub.run(verb) if verb == "list" else hub.run(verb, BETA_ONE)
        finally:
            hub.config.chmod(0o600)
        _clean_exit(result)
        assert result.exit_code == (0 if verb == "list" else EXIT_INVALID_INPUT)

    @_posix_only
    @pytest.mark.parametrize("verb", ["list", "add", "remove"])
    def test_a_removed_cwd_is_clean(self, hub: Hub, tmp_path: Path, verb: str):
        gone = tmp_path / "gone"
        gone.mkdir()
        os.chdir(gone)
        gone.rmdir()
        result = hub.run(verb) if verb == "list" else hub.run(verb, ALPHA_ONE)
        _clean_exit(result)
        assert result.exit_code == 0, result.output
        if verb == "list":
            assert "Consulted .env in cwd: none" in result.stdout
            assert _tags(result.stdout)[default_repos()[0]] == "default"

    def test_any_oserror_in_list_is_a_clean_exit_1(self, hub: Hub, monkeypatch: pytest.MonkeyPatch):
        """The command-level floor: an OSError from anywhere in the body."""
        from pipecat_context_hub import cli_repos

        def boom() -> Any:
            raise OSError(5, "I/O error", str(Path.home() / "x"))

        monkeypatch.setattr(cli_repos, "_configured_in_toml", boom)
        result = hub.run("list")
        _clean_exit(result)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert str(Path.home()) not in result.output

    def test_a_missing_provenance_map_fails_loudly(self, hub: Hub):
        """``main()`` always sets it; without it the shadow check must not fail open."""
        from pipecat_context_hub.cli_repos import repos_group

        result = CliRunner().invoke(repos_group, ["add", ALPHA_ONE], obj={"config": HubConfig()})
        assert isinstance(result.exception, RuntimeError)
        assert "env_provenance" in str(result.exception)
        assert hub.snapshot() is None


@pytest.mark.usefixtures("no_network")
class TestGroupWithNothingActionable:
    @pytest.mark.parametrize("verb", ["add", "remove"])
    def test_does_not_read_config_toml(self, hub: Hub, verb: str):
        """gamma has only ``parseable = false`` members: nothing to write, so a
        broken config.toml is not consulted and the run exits 0."""
        hub.write_config("this is = = not toml\n")
        before = hub.snapshot()
        result = hub.run(verb, "--group", "gamma")
        assert result.exit_code == 0, result.output
        assert ("Nothing to add" if verb == "add" else "Nothing to remove") in result.output
        assert "Skipped (parseable = false): acme/gamma-ios" in result.output
        assert hub.snapshot() == before

    @pytest.mark.parametrize("verb", ["add", "remove"])
    @pytest.mark.parametrize("layer", sorted(SHADOWED))
    @pytest.mark.parametrize(
        ("group", "skipped"),
        [
            ("gamma", "Skipped (parseable = false): acme/gamma-ios"),
            ("delta", "Skipped (tainted): acme/delta-one"),
        ],
    )
    def test_nothing_actionable_under_a_shadow_exits_0(
        self, hub: Hub, verb: str, layer: str, group: str, skipped: str
    ):
        """No write can happen, so the shadow refusal (check 3) does not apply:
        exit 0 with ``Nothing to ...`` and the skipped list, file untouched."""
        hub.env(PIPECAT_HUB_TAINTED_REPOS="acme/delta-one")
        _arrange(hub, layer)
        before = hub.snapshot()
        result = hub.run(verb, "--group", group)
        assert result.exit_code == 0, result.output
        assert ("Nothing to add" if verb == "add" else "Nothing to remove") in result.output
        assert skipped in result.output
        assert hub.snapshot() == before

    @pytest.mark.parametrize("layer", sorted(SHADOWED))
    def test_actionable_group_under_a_shadow_is_still_refused(self, hub: Hub, layer: str):
        _arrange(hub, layer)
        result = hub.run("add", "--group", "beta")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "--config-only" in result.output


@pytest.mark.usefixtures("no_network")
class TestOneTaintRule:
    """Taint matching is casefolded everywhere (``SourceConfig.is_tainted``):
    ``repos`` refusals, ``effective_repos`` and the hidden section agree."""

    def test_config_entry_in_another_case_is_not_effective(self, hub: Hub):
        hub.env(PIPECAT_HUB_TAINTED_REPOS="someone/tainted-one")
        hub.config_list("Someone/Tainted-One", ALPHA_ONE)
        assert "someone/tainted-one" not in _lower(hub.effective())
        assert ALPHA_ONE in hub.effective()
        assert "Someone/Tainted-One" not in _tags(hub.run("list").stdout)

    def test_positional_refusal_agrees_with_effective_repos(self, hub: Hub):
        hub.env(PIPECAT_HUB_TAINTED_REPOS="someone/tainted-one")
        result = hub.run("add", "--unlisted", "Someone/Tainted-One")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "tainted" in result.output
        hub.config_list("Someone/Tainted-One")
        assert "someone/tainted-one" not in _lower(hub.effective())

    def test_hidden_section_omits_a_tainted_entry_in_another_case(self, hub: Hub):
        hub.env(**{KEY: BETA_ONE, "PIPECAT_HUB_TAINTED_REPOS": "someone/tainted-one"})
        hub.config_list("Someone/Tainted-One", ALPHA_ONE)
        out = hub.run("list").stdout
        hidden = out.split("hidden by", 1)[1]
        assert ALPHA_ONE in hidden
        assert "Someone/Tainted-One" not in hidden

    def test_group_skip_uses_the_same_rule(self, hub: Hub):
        hub.env(PIPECAT_HUB_TAINTED_REPOS="ACME/DELTA-ONE")
        result = hub.run("add", "--group", "delta")
        assert result.exit_code == 0, result.output
        assert "Skipped (tainted): acme/delta-one" in result.output
        assert hub.snapshot() is None


@pytest.mark.usefixtures("no_network")
class TestReaderRuleInList:
    """``repos list`` reads config.toml by the loader's own rule."""

    @pytest.mark.parametrize(
        "value,expected",
        [("[1, 2]", ["1", "2"]), ('"a/b, c/d"', ["a/b", "c/d"])],
        ids=["scalar-array", "csv"],
    )
    def test_hidden_section_shows_what_the_loader_reads(
        self, hub: Hub, value: str, expected: list[str]
    ):
        hub.write_config(f"{KEY} = {value}\n")
        hub.env(**{KEY: BETA_ONE})  # shadow, so the config.toml entries are listed as hidden
        result = hub.run("list")
        assert result.exit_code == 0, result.output
        assert "config.toml:" not in result.stdout  # not reported as ignored
        hidden = result.stdout.split("hidden by", 1)[1]
        for slug in expected:
            assert f"  {slug}  [" in hidden


@pytest.mark.usefixtures("no_network")
class TestSlugGrammar:
    @pytest.mark.parametrize("bad", ["owner/repo\n", "owner/.dot", "owner/-x"])
    def test_cli_uses_the_shared_strict_grammar(self, hub: Hub, bad: str):
        result = hub.run("add", "--unlisted", bad)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "not a valid repo slug" in result.output
        assert hub.snapshot() is None


@pytest.mark.usefixtures("no_network")
class TestShadowAwareReminders:
    @pytest.mark.parametrize("layer", list(SHADOWED))
    def test_add_under_a_shadow_does_not_promise_indexing(self, hub: Hub, layer: str):
        _arrange(hub, layer)
        result = hub.run("add", "--config-only", ALPHA_ONE)
        assert result.exit_code == 0, result.output
        assert "to index it" not in result.output
        assert "Not in effect" in result.output
        assert f"layer `{SHADOWED[layer]}`" in result.output

    @pytest.mark.parametrize("layer", list(SHADOWED))
    def test_remove_under_a_shadow_warns_against_prune(self, hub: Hub, layer: str):
        _arrange(hub, layer)
        hub.run("add", "--config-only", ALPHA_ONE)
        result = hub.run("remove", "--config-only", ALPHA_ONE)
        assert result.exit_code == 0, result.output
        assert "Run `pipecat-context-hub refresh --prune`" not in result.output
        assert "Do not run `refresh --prune`" in result.output

    @pytest.mark.parametrize("layer", list(SHADOWED))
    def test_group_remove_under_a_shadow_warns_against_prune(self, hub: Hub, layer: str):
        _arrange(hub, layer)
        hub.run("add", "--config-only", "--group", "beta")
        result = hub.run("remove", "--config-only", "--group", "beta")
        assert result.exit_code == 0, result.output
        assert "Do not run `refresh --prune`" in result.output

    def test_unshadowed_reminders_are_unchanged(self, hub: Hub):
        assert "to index it" in hub.run("add", ALPHA_ONE).output
        assert "refresh --prune` to drop" in hub.run("remove", ALPHA_ONE).output


class TestListGroups:
    def test_groups_flag_names_every_group(self, hub: Hub):
        result = hub.run("list", "--groups")
        assert result.exit_code == 0, result.output
        for name in ("alpha", "beta", "gamma", "delta"):
            assert f"  {name}  [" in result.stdout

    def test_unknown_group_points_at_the_listing(self, hub: Hub):
        result = hub.run("add", "--group", "nope")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "repos list --groups" in result.output
