"""Regression coverage for the portable skills-only plugin packager."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _ROOT / "plugins" / "pipecat-context-hub"


@pytest.fixture
def renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "chatgpt_plugin_setup", _SOURCE / "scripts" / "prepare_local.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_source(renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source" / "plugins" / "pipecat-context-hub"
    shutil.copytree(_SOURCE, source)
    monkeypatch.setattr(renderer, "__file__", str(source / "scripts" / "prepare_local.py"))
    return source


def test_prepare_runs_without_site_packages_or_hub(tmp_path: Path) -> None:
    destination = tmp_path / "prepared"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(_SOURCE / "scripts" / "prepare_local.py"),
            str(destination),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Prepared skills-only plugin" in result.stdout
    assert (destination / "skills" / "setup" / "SKILL.md").is_file()
    assert not (destination / "mcp.json").exists()


def test_render_excludes_evaluation_and_unrelated_files(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _copy_source(renderer, tmp_path, monkeypatch)
    excluded = [
        "mcp.json",
        ".mcp.json",
        "mcp.template.json",
        "hooks/hooks.json",
        ".app.json",
        "evaluation.md",
        "scratch.txt",
        "qualification.json",
        "package-check.json",
        "docs/evaluations/pipecat-context-hub-plugin.md",
        ".conduct/phase3-implementer.report.txt",
        "scripts/__pycache__/prepare_local.pyc",
        "skills/build/qualification.json",
        "skills/build/reports/local-verification.txt",
        "skills/deploy/scratch.txt",
        "skills/deploy/reports/cloud-qualification.json",
        "skills/deploy/__pycache__/cached.pyc",
        "skills/explore/__pycache__/cached.pyc",
        "skills/setup/reports/setup-results.json",
        "skills/private/SKILL.md",
        "assets/scratch.svg",
        "assets/private.env",
    ]
    for relative in excluded:
        artifact = source / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("Repository-only artifact")
    destination = renderer.prepare_local(tmp_path / "prepared")
    copied = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
    assert copied == {
        "plugin.json",
        "README.md",
        "scripts/prepare_local.py",
        "skills/build/SKILL.md",
        "skills/deploy/SKILL.md",
        "skills/explore/SKILL.md",
        "skills/setup/SKILL.md",
        "assets/cat-mark.svg",
        "assets/cat-mark-dark.svg",
        "assets/context-hub-logo.svg",
        "assets/context-hub-logo-dark.svg",
    }
    interface = json.loads((destination / "plugin.json").read_text())["extensions"]["com.openai"][
        "interface"
    ]
    assert interface["logo"] != interface["composerIcon"]
    assert interface["logoDark"] != interface["composerIconDark"]
    for field in ("composerIcon", "composerIconDark", "logo", "logoDark"):
        relative = interface[field]
        assert relative.startswith("./assets/")
        assert (destination / relative).read_bytes() == (source / relative).read_bytes()
    assert not (_SOURCE / "evaluation.md").exists()
    assert (_ROOT / "docs/evaluations/pipecat-context-hub-plugin.md").is_file()


@pytest.mark.parametrize("mode", ["missing", "file_symlink", "directory_symlink"])
def test_render_rejects_missing_or_symlinked_brand_assets(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    source = _copy_source(renderer, tmp_path, monkeypatch)
    asset = source / "assets" / "cat-mark.svg"
    if mode == "directory_symlink":
        external = tmp_path / "external-assets"
        (source / "assets").rename(external)
        (source / "assets").symlink_to(external, target_is_directory=True)
    else:
        asset.unlink()
        if mode == "file_symlink":
            external = tmp_path / "external.svg"
            external.write_text("Untrusted external asset")
            asset.symlink_to(external)
    destination = tmp_path / "prepared"
    with pytest.raises(ValueError, match="Package resource must be a regular file"):
        renderer.prepare_local(destination)
    assert not destination.exists()


@pytest.mark.parametrize("skill", ["explore", "build", "deploy", "setup"])
def test_render_preserves_complete_skill_resource_bytes(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill: str
) -> None:
    source = _copy_source(renderer, tmp_path, monkeypatch)
    relative = Path("skills") / skill / "SKILL.md"
    resource = source / relative
    # Keep the real instructions and make encoding/newline normalisation observable.
    expected = resource.read_bytes() + "\r\nByte-preservation fixture: café\r\n".encode()
    resource.write_bytes(expected)

    destination = renderer.prepare_local(tmp_path / "prepared")

    assert (destination / relative).read_bytes() == expected
    assert resource.read_bytes() == expected


@pytest.mark.parametrize("skill", ["explore", "build", "deploy", "setup"])
def test_render_requires_all_four_skills(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill: str
) -> None:
    source = _copy_source(renderer, tmp_path, monkeypatch)
    (source / "skills" / skill / "SKILL.md").unlink()
    destination = tmp_path / "prepared"
    with pytest.raises(ValueError, match="Package resource must be a regular file"):
        renderer.prepare_local(destination)
    assert not destination.exists()


def test_render_preserves_nonempty_destination(renderer: ModuleType, tmp_path: Path) -> None:
    destination = tmp_path / "prepared"
    destination.mkdir()
    sentinel = destination / "existing.txt"
    sentinel.write_text("Keep this file")
    with pytest.raises(ValueError, match="absent or an empty directory"):
        renderer.prepare_local(destination)
    assert sentinel.read_text() == "Keep this file"
    assert list(destination.iterdir()) == [sentinel]


def test_render_refuses_checkout_destination(renderer: ModuleType) -> None:
    destination = _ROOT / "never-create-test-plugin"
    with pytest.raises(ValueError, match="outside the checkout"):
        renderer.prepare_local(destination)
    assert not destination.exists()


def test_render_rejects_symlink_destination(renderer: ModuleType, tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    destination = tmp_path / "prepared"
    destination.symlink_to(existing, target_is_directory=True)
    with pytest.raises(ValueError, match="Destination must not be a symlink"):
        renderer.prepare_local(destination)
    assert list(existing.iterdir()) == []


def test_skills_only_manifest_and_onboarding(renderer: ModuleType, tmp_path: Path) -> None:
    destination = renderer.prepare_local(tmp_path / "prepared")
    manifest = json.loads((destination / "plugin.json").read_text())
    settings = manifest["extensions"]["com.openai"]
    assert not {"mcpServers", "apps", "hooks"}.intersection(manifest)
    assert not {"apps", "hooks"}.intersection(settings)
    assert (destination / settings["onboardingSkill"]).is_file()
    assert len(settings["interface"]["shortDescription"]) <= 30
