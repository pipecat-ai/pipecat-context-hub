"""``repos`` driven through the real console script, not ``CliRunner``.

The unit tests (``tests/unit/test_cli_repos.py``) run ``main`` in-process. This
one spawns the installed ``pipecat-context-hub`` executable in a clean process,
so entry-point wiring, real environment inheritance, the loader's log output
and the on-disk file mode are all exercised for real. POSIX-only (it asserts
the 0600 mode and a 0700 config directory) and deliberately absent from the
Windows CI list.

Everything lives under ``tmp_path``: the config file is a scratch
``PIPECAT_HUB_CONFIG_FILE`` and ``HOME`` is a scratch directory, so the
developer's real ``~/.config/pipecat-context-hub/config.toml`` is never read.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from pipecat_context_hub.shared.config import SourceConfig
from pipecat_context_hub.shared.repo_catalog import load_catalog

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and flock semantics")

_SCRIPT = Path(sys.executable).parent / "pipecat-context-hub"


def _catalog_picks() -> tuple[str, str, str]:
    """``(parseable slug, parseable=false slug, that slug's group)`` from the packaged catalog."""
    catalog = load_catalog()
    assert catalog is not None
    defaults = SourceConfig()
    unparseable_group = None
    unparseable = None
    for group in catalog.groups:
        for repo in group.repos:
            if not defaults.is_default(repo.slug) and not repo.parseable:
                unparseable, unparseable_group = repo.slug, group
                break
        if unparseable:
            break
    assert unparseable and unparseable_group, "catalog needs a parseable = false repo"
    # `remove --group` removes every non-tainted member, so the parseable pick must sit
    # outside that group or the later `remove <parseable>` would find nothing to remove.
    in_group = {repo.slug for repo in unparseable_group.repos}
    parseable = next(
        (
            repo.slug
            for group in catalog.groups
            for repo in group.repos
            if repo.parseable and repo.slug not in in_group and not defaults.is_default(repo.slug)
        ),
        None,
    )
    assert parseable, "catalog needs a parseable repo outside the parseable = false repo's group"
    return parseable, unparseable, unparseable_group.name


def test_repos_round_trip_through_the_console_script(tmp_path: Path) -> None:
    # Not a skip: a missing script is the very entry-point wiring failure this test detects.
    assert _SCRIPT.exists(), (
        f"console script {_SCRIPT} is missing; run `uv sync --extra dev` so the "
        "`pipecat-context-hub` entry point is installed next to python"
    )
    parseable, unparseable, group = _catalog_picks()

    home = tmp_path / "home"
    cwd = tmp_path / "cwd"  # no .env here
    cfg_dir = tmp_path / "cfg"
    for d in (home, cwd, cfg_dir):
        d.mkdir()
    cfg_dir.chmod(0o700)
    config = cfg_dir / "config.toml"

    env = {k: v for k, v in os.environ.items() if not k.startswith("PIPECAT_HUB_")}
    env.update(HOME=str(home), PIPECAT_HUB_CONFIG_FILE=str(config))

    def run(
        *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, scratch paths only
            [str(_SCRIPT), "repos", *args],
            cwd=cwd,
            env={**env, **(extra_env or {})},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    # list on a fresh machine: nothing configured, still exit 0.
    listed = run("list")
    assert listed.returncode == 0, listed.stderr
    assert "Effective repos" in listed.stdout
    assert parseable not in listed.stdout

    # add a parseable repo: written 0600, listed as catalog, no loader chatter.
    added = run("add", parseable)
    assert added.returncode == 0, added.stderr
    assert f"Added {parseable}" in added.stdout
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert f"{parseable}  [catalog]" in run("list").stdout

    # add a parseable=false repo: succeeds with a stderr warning.
    warned = run("add", unparseable)
    assert warned.returncode == 0, warned.stderr
    assert f"Added {unparseable}" in warned.stdout
    assert "parseable = false" in warned.stderr
    # config.toml now exists: the loader's INFO line must not reach the terminal.
    assert "Loaded" not in warned.stderr

    # the shadow refusal: a real env var outranks config.toml.
    before = config.read_bytes()
    refused = run("add", "--unlisted", "someone/else", extra_env={"PIPECAT_HUB_EXTRA_REPOS": "x/y"})
    assert refused.returncode == 1
    assert "layer `env`" in refused.stderr
    assert config.read_bytes() == before

    # remove the parseable=false repo by group, the other one by slug.
    by_group = run("remove", "--group", group)
    assert by_group.returncode == 0, by_group.stderr
    assert f"Removed {unparseable}" in by_group.stdout
    by_slug = run("remove", parseable)
    assert by_slug.returncode == 0, by_slug.stderr
    assert f"Removed {parseable}" in by_slug.stdout
    assert parseable not in run("list").stdout
    assert unparseable not in run("list").stdout
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
