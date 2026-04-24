"""Unit tests for smoke-test scaffolding: fixture refresh, clone argv, symlink safety.

Covers three narrow behaviours that are hard to exercise end-to-end without
network:

* ``tests.smoke.refresh_fixtures._rebuild_fixture`` — root-layout branch must
  copy every top-level example dir (pipecat-examples style) and skip
  packaged-project dirs (``src``/``tests``/``docs``/…). Regression guard for
  the Codex P2 finding where the old implementation wiped the fixture.
* ``_clone_repo`` / ``_shallow_clone`` — SHA refs use init+fetch+checkout;
  named refs use ``git clone --branch``. Mocks ``subprocess.run`` and asserts
  on the captured argv sequences.
* Symlink handling — ``_copy_filtered`` and ``_scan_topic_tree`` must refuse
  to follow symlinks inside untrusted upstream clones.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipecat_context_hub.services.ingest.taxonomy import TaxonomyBuilder

# ``tests/`` is not a package on sys.path outside pytest, but pytest's rootdir
# handling makes ``tests.smoke`` importable here.
from tests.smoke import refresh_fixtures  # noqa: E402

# The drift script lives under ``scripts/`` and is not a package; add the repo
# root to sys.path so ``scripts.check_pipecat_drift`` resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import check_pipecat_drift  # noqa: E402


# ---------------------------------------------------------------------------
# _rebuild_fixture: root-layout branch
# ---------------------------------------------------------------------------


def _make_root_layout_clone(root: Path) -> None:
    """Build a synthetic ``pipecat-examples``-style clone tree."""
    (root / "pyproject.toml").write_text('[project]\nname = "pipecat-examples"\n')
    (root / "README.md").write_text("# pipecat-examples\n")
    for example in ("simple-chatbot", "storytelling", "voice-bot"):
        ex_dir = root / example
        ex_dir.mkdir()
        (ex_dir / "README.md").write_text(f"# {example}\n")
        (ex_dir / "bot.py").write_text("# synthetic\n")
    # Packaged-project dirs that must be skipped.
    for skip in ("src", "tests", "docs", "scripts", "dashboard"):
        (root / skip).mkdir()
        (root / skip / "placeholder.py").write_text("# should not be vendored\n")
    (root / ".github").mkdir()
    (root / ".github" / "workflow.yml").write_text("---\n")


def test_rebuild_fixture_root_layout_preserves_top_level_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone_root = tmp_path / "clone"
    clone_root.mkdir()
    _make_root_layout_clone(clone_root)

    fixtures_root = tmp_path / "fixtures"
    monkeypatch.setattr(refresh_fixtures, "_FIXTURES_ROOT", fixtures_root)

    refresh_fixtures._rebuild_fixture(
        "pipecat-ai/pipecat-examples", "pipecat-examples", clone_root
    )

    fixture_dir = fixtures_root / "pipecat-examples"
    assert (fixture_dir / "pyproject.toml").is_file()
    assert (fixture_dir / "README.md").is_file()
    for example in ("simple-chatbot", "storytelling", "voice-bot"):
        assert (fixture_dir / example / "bot.py").is_file(), (
            f"root-layout refresh dropped example {example!r}"
        )
        assert (fixture_dir / example / "README.md").is_file()
    # Packaged-project dirs must be excluded.
    for skip in ("src", "tests", "docs", "scripts", "dashboard", ".github"):
        assert not (fixture_dir / skip).exists(), (
            f"root-layout refresh vendored forbidden dir {skip!r}"
        )


def test_rebuild_fixture_topic_layout_uses_examples_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone_root = tmp_path / "clone"
    (clone_root / "examples" / "voice" / "cartesia").mkdir(parents=True)
    (clone_root / "examples" / "voice" / "cartesia" / "bot.py").write_text("# code\n")
    (clone_root / "pyproject.toml").write_text("[project]\nname = 'pipecat'\n")
    # A stray non-examples tree that must be ignored in topic layout.
    (clone_root / "src").mkdir()
    (clone_root / "src" / "mod.py").write_text("# source\n")

    fixtures_root = tmp_path / "fixtures"
    monkeypatch.setattr(refresh_fixtures, "_FIXTURES_ROOT", fixtures_root)

    refresh_fixtures._rebuild_fixture("pipecat-ai/pipecat", "pipecat", clone_root)

    fixture_dir = fixtures_root / "pipecat"
    assert (fixture_dir / "examples" / "voice" / "cartesia" / "bot.py").is_file()
    assert (fixture_dir / "pyproject.toml").is_file()
    # Topic-layout branch must not walk past ``examples/`` into ``src/``.
    assert not (fixture_dir / "src").exists()


# ---------------------------------------------------------------------------
# Clone argv: SHA ref vs named ref
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _capture_subprocess(
    monkeypatch: pytest.MonkeyPatch, module_path: str
) -> list[list[str]]:
    """Replace ``<module_path>.subprocess.run`` with a recorder. Returns the call log."""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], *args: object, **kwargs: object) -> _FakeCompleted:
        calls.append(list(cmd))
        return _FakeCompleted()

    monkeypatch.setattr(f"{module_path}.subprocess.run", _fake_run)
    return calls


def test_clone_repo_named_ref_uses_clone_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_subprocess(monkeypatch, "scripts.check_pipecat_drift")
    dest = tmp_path / "clone"
    check_pipecat_drift._clone_repo("pipecat-ai/pipecat", "main", dest)
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:5] == ["git", "clone", "--depth", "1", "--branch"]
    assert cmd[5] == "main"
    assert cmd[6] == "--"
    assert cmd[7] == "https://github.com/pipecat-ai/pipecat.git"
    assert cmd[8] == str(dest)


def test_clone_repo_sha_ref_uses_init_fetch_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_subprocess(monkeypatch, "scripts.check_pipecat_drift")
    dest = tmp_path / "clone"
    sha = "ef7fa07bf7ce2c18d368a070c095e15ff1a92292"
    check_pipecat_drift._clone_repo("pipecat-ai/pipecat", sha, dest)

    # init, remote add, fetch --depth 1 <sha>, checkout FETCH_HEAD
    assert len(calls) == 4
    assert calls[0][:3] == ["git", "init", "--quiet"]
    assert calls[0][-1] == str(dest)
    assert calls[1][:5] == ["git", "-C", str(dest), "remote", "add"]
    assert calls[1][5:] == [
        "origin",
        "--",
        "https://github.com/pipecat-ai/pipecat.git",
    ]
    assert calls[2][:6] == ["git", "-C", str(dest), "fetch", "--depth", "1"]
    assert calls[2][-2:] == ["origin", sha]
    assert calls[3][:3] == ["git", "-C", str(dest)]
    assert calls[3][3:] == ["checkout", "--quiet", "FETCH_HEAD"]


def test_clone_repo_rejects_ref_starting_with_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_subprocess(monkeypatch, "scripts.check_pipecat_drift")
    with pytest.raises(ValueError, match="Invalid git ref"):
        check_pipecat_drift._clone_repo(
            "pipecat-ai/pipecat", "--upload-pack=whoops", tmp_path / "c"
        )
    assert calls == []


def test_clone_repo_rejects_malformed_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_subprocess(monkeypatch, "scripts.check_pipecat_drift")
    with pytest.raises(ValueError, match="Invalid repo slug"):
        check_pipecat_drift._clone_repo("not-a-valid-slug", "main", tmp_path / "c")
    assert calls == []


def test_shallow_clone_sha_branches_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def _fake_run(
        cmd: list[str], cwd: Path | None = None, timeout: int | None = None
    ) -> str:
        calls.append(list(cmd))
        if cmd[:2] == ["git", "rev-parse"]:
            return "deadbeef" * 5
        return ""

    monkeypatch.setattr(refresh_fixtures, "_run", _fake_run)
    dest = tmp_path / "clone"
    sha = "ef7fa07bf7ce2c18d368a070c095e15ff1a92292"
    result = refresh_fixtures._shallow_clone("pipecat-ai/pipecat", dest, ref=sha)

    assert result == "deadbeef" * 5
    # init, remote add, fetch, checkout, rev-parse HEAD → 5 calls.
    assert len(calls) == 5
    assert calls[0][:3] == ["git", "init", "--quiet"]
    assert calls[2][:6] == ["git", "-C", str(dest), "fetch", "--depth", "1"]
    assert calls[2][-1] == sha
    assert calls[3][3:] == ["checkout", "--quiet", "FETCH_HEAD"]
    assert calls[4][:3] == ["git", "rev-parse", "HEAD"]


# ---------------------------------------------------------------------------
# Symlink handling
# ---------------------------------------------------------------------------


pytestmark_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires elevated privileges on Windows"
)


@pytestmark_symlinks
def test_copy_filtered_refuses_symlinks(tmp_path: Path) -> None:
    src = tmp_path / "clone"
    src.mkdir()
    # A legitimate file that should be copied.
    (src / "bot.py").write_text("# real\n")
    # An attacker-placed symlink pointing outside the clone.
    outside = tmp_path / "secrets.txt"
    outside.write_text("super-secret\n")
    (src / "leak.py").symlink_to(outside)
    # A symlinked directory, also must not be followed.
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (outside_dir / "other.py").write_text("# other\n")
    (src / "sub").symlink_to(outside_dir, target_is_directory=True)

    dst = tmp_path / "vendored"
    refresh_fixtures._copy_filtered(src, dst)

    assert (dst / "bot.py").is_file()
    assert not (dst / "leak.py").exists(), "symlinked file was followed"
    assert not (dst / "sub").exists(), "symlinked directory was followed"


@pytestmark_symlinks
def test_scan_topic_tree_skips_symlinked_example_dirs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    examples = repo / "examples"
    (examples / "voice" / "real-example").mkdir(parents=True)
    (examples / "voice" / "real-example" / "bot.py").write_text("# real\n")

    # A symlinked topic pointing outside the repo — must never produce an entry.
    outside = tmp_path / "attacker-tree" / "malicious"
    outside.mkdir(parents=True)
    (outside / "evil.py").write_text("# evil\n")
    (examples / "hijacked").symlink_to(outside.parent, target_is_directory=True)

    builder = TaxonomyBuilder()
    entries = builder.build_from_topic_dirs(
        examples, repo="pipecat-ai/pipecat", commit_sha="SYNTHETIC"
    )

    paths = {entry.path for entry in entries}
    assert "examples/voice/real-example" in paths
    assert not any("hijacked" in p for p in paths), (
        f"symlinked topic produced a taxonomy entry: {sorted(paths)!r}"
    )


# ---------------------------------------------------------------------------
# Timeout / error propagation in drift script
# ---------------------------------------------------------------------------


def test_check_repo_reports_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git clone", timeout=300)

    monkeypatch.setattr("scripts.check_pipecat_drift.subprocess.run", _boom)
    result = check_pipecat_drift._check_repo(
        "pipecat-ai/pipecat", "main", dry_run=False
    )
    assert not result.ok
    (name, passed, message) = result.checks[0]
    assert name == "git_clone"
    assert not passed
    assert message is not None and "timed out" in message
