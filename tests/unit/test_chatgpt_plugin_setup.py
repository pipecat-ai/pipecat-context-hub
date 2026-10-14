"""Regression coverage for the portable skills-only plugin packager."""

from __future__ import annotations

import importlib.util
import json
import os
import re
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
        "PRIVACY.md",
        "LICENSE",
        "scripts/prepare_local.py",
        "scripts/safe_cloud_metadata.py",
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
    assert (destination / "PRIVACY.md").read_bytes() == (source / "PRIVACY.md").read_bytes()
    assert (destination / "LICENSE").read_bytes() == (_ROOT / "LICENSE").read_bytes()
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


def test_render_rejects_reparse_point_resource(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Junctions must be treated like symlinks even when ``is_symlink`` is false."""
    source = _copy_source(renderer, tmp_path, monkeypatch)
    asset = source / "assets" / "cat-mark.svg"
    real_predicate = renderer._is_reparse_point
    monkeypatch.setattr(
        renderer,
        "_is_reparse_point",
        lambda path: path == asset or real_predicate(path),
    )
    with pytest.raises(ValueError, match="Package resource must be a regular file"):
        renderer.prepare_local(tmp_path / "prepared")


def test_render_uses_windows_safe_fallback_without_secure_directory_descriptors(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "resource.txt"
    source.write_text("safe")
    monkeypatch.setattr(renderer.os, "supports_dir_fd", set())
    target = tmp_path / "copy.txt"
    renderer._copy_resource(source, target)
    assert target.read_text() == "safe"


def test_render_finishes_windows_safe_fallback_rename(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "prepared"
    monkeypatch.setattr(renderer.os, "supports_dir_fd", set())
    assert renderer.prepare_local(destination) == destination
    assert (destination / "plugin.json").is_file()


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


def test_render_refuses_destination_inside_linked_worktree(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy_source(renderer, tmp_path, monkeypatch)
    (tmp_path / "source" / ".git").write_text("gitdir: /tmp/linked-worktree\n")
    destination = tmp_path / "source" / "nested" / "prepared"
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement fixture")
def test_render_does_not_follow_resource_replaced_after_preflight(
    renderer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "resource.txt"
    outside = tmp_path / "outside.txt"
    target = tmp_path / "copy.txt"
    source.write_text("safe")
    outside.write_text("outside")
    real_open = renderer.os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == source and not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(renderer.os, "open", swap_before_open)
    with pytest.raises((OSError, ValueError)):
        renderer._copy_resource(source, target)
    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_render_rejects_symlink_loop_destination_without_path_leak(
    renderer: ModuleType, tmp_path: Path
) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop, target_is_directory=True)
    with pytest.raises(ValueError, match="Destination path is invalid") as error:
        renderer.prepare_local(loop / "nested")
    assert str(tmp_path) not in str(error.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_copy_resource_rejects_swapped_parent_directory(
    renderer: ModuleType, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "resource"
    source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        renderer._copy_resource(source / "file.txt", tmp_path / "copy.txt")


def test_skills_only_manifest_and_onboarding(renderer: ModuleType, tmp_path: Path) -> None:
    destination = renderer.prepare_local(tmp_path / "prepared")
    manifest = json.loads((destination / "plugin.json").read_text())
    settings = manifest["extensions"]["com.openai"]
    assert not {"mcpServers", "apps", "hooks"}.intersection(manifest)
    assert not {"apps", "hooks"}.intersection(settings)
    assert (destination / settings["onboardingSkill"]).is_file()
    assert len(settings["interface"]["shortDescription"]) <= 30


def test_cloud_metadata_wrapper_never_exposes_raw_credentials(tmp_path: Path) -> None:
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import json, sys\n"
        "print('token=stderr-secret', file=sys.stderr)\n"
        "print(json.dumps({'organizations': ["
        "{'id': 'org-1', 'name': 'Team', 'email': 'private@example.com', "
        "'daily_api_key': 'stdout-secret'}]}))\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_SOURCE / "scripts" / "safe_cloud_metadata.py"),
            "--kind",
            "organizations",
            "--",
            sys.executable,
            str(producer),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"organizations": [{"id": "org-1", "name": "Team"}]}
    assert "secret" not in result.stdout + result.stderr
    assert "private@example.com" not in result.stdout + result.stderr


def test_cloud_metadata_wrapper_does_not_inherit_cloud_token(tmp_path: Path) -> None:
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import json, os\n"
        "print(json.dumps({'organizations': [{'id': os.environ.get('PIPECAT_TOKEN', 'missing'), 'name': 'Team'}]}))\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_SOURCE / "scripts" / "safe_cloud_metadata.py"),
            "--kind",
            "organizations",
            "--",
            sys.executable,
            str(producer),
        ],
        env={**os.environ, "PIPECAT_TOKEN": "must-not-leak"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["organizations"][0]["id"] == "missing"
    assert "must-not-leak" not in result.stdout + result.stderr


def test_cloud_metadata_wrapper_handles_unselectable_windows_pipe(tmp_path: Path) -> None:
    """The bounded reader has a pipe-draining fallback for Windows selectors."""
    spec = importlib.util.spec_from_file_location(
        "safe_cloud_metadata", _SOURCE / "scripts" / "safe_cloud_metadata.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Unselectable:
        def register(self, *_args, **_kwargs):
            raise NotImplementedError("anonymous pipes are not selectable")

        def close(self):
            pass

    producer = tmp_path / "producer.py"
    producer.write_text(
        "import json; print(json.dumps({'organizations': [{'id': 'o', 'name': 'Team'}]}))\n"
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(module.selectors, "DefaultSelector", Unselectable)
        result = module._run_bounded([sys.executable, str(producer)])
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"organizations": [{"id": "o", "name": "Team"}]}


def test_cloud_metadata_wrapper_reaches_thread_fallback_when_nonblocking_fails(
    tmp_path: Path,
) -> None:
    """Windows 3.11 anonymous pipes can reject ``os.set_blocking(False)``."""
    spec = importlib.util.spec_from_file_location(
        "safe_cloud_metadata", _SOURCE / "scripts" / "safe_cloud_metadata.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    producer = tmp_path / "producer.py"
    producer.write_text("import json; print(json.dumps({'organizations': [{'id': 'o'}]}))\n")

    def fail_set_blocking(*_args: object, **_kwargs: object) -> None:
        raise OSError("operation not supported")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(module.os, "set_blocking", fail_set_blocking)
        result = module._run_bounded([sys.executable, str(producer)])
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"organizations": [{"id": "o"}]}


def test_cloud_metadata_wrapper_uses_blocking_reader_on_windows(
    tmp_path: Path,
) -> None:
    """Windows must not enter ``SelectSelector`` for anonymous pipes."""
    spec = importlib.util.spec_from_file_location(
        "safe_cloud_metadata", _SOURCE / "scripts" / "safe_cloud_metadata.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    producer = tmp_path / "producer.py"
    producer.write_text("import json; print(json.dumps({'organizations': [{'id': 'o'}]}))\n")

    class UnexpectedSelector:
        def __init__(self):
            raise AssertionError("Windows anonymous pipes must use the blocking reader")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(module.os, "name", "nt")
        monkeypatch.setattr(module.selectors, "DefaultSelector", UnexpectedSelector)
        result = module._run_bounded([sys.executable, str(producer)])
    assert json.loads(result.stdout) == {"organizations": [{"id": "o"}]}


def test_cloud_metadata_wrapper_cleans_up_on_cancellation(tmp_path: Path) -> None:
    """Cancellation must terminate the detached producer process group."""
    spec = importlib.util.spec_from_file_location(
        "safe_cloud_metadata", _SOURCE / "scripts" / "safe_cloud_metadata.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    producer = tmp_path / "producer.py"
    producer.write_text("import time; time.sleep(60)\n")

    class CancelledSelector:
        def register(self, *_args, **_kwargs):
            return None

        def select(self, *_args, **_kwargs):
            raise KeyboardInterrupt

        def close(self):
            return None

    calls: list[int] = []
    terminate = module._terminate_process_tree

    def record_and_terminate(process: subprocess.Popen[bytes]) -> None:
        calls.append(process.pid)
        terminate(process)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(module.selectors, "DefaultSelector", CancelledSelector)
        monkeypatch.setattr(module, "_terminate_process_tree", record_and_terminate)
        with pytest.raises(KeyboardInterrupt):
            module._run_bounded([sys.executable, str(producer)])
    assert calls


def test_cloud_metadata_wrapper_terminates_process_group_on_bounded_failure(
    tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "safe_cloud_metadata", _SOURCE / "scripts" / "safe_cloud_metadata.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    producer = tmp_path / "producer.py"
    producer.write_text("import sys; sys.stdout.write('x' * 2000000); sys.stdout.flush()\n")
    calls: list[int] = []
    original = module._terminate_process_tree

    def record_and_terminate(process: subprocess.Popen[bytes]) -> None:
        calls.append(process.pid)
        original(process)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(module, "_terminate_process_tree", record_and_terminate)
        with pytest.raises(ValueError, match="output too large"):
            module._run_bounded([sys.executable, str(producer)])
    assert calls


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            {"organizations": [{"id": "org-0", "name": "Right"}], "regions": []},
            {"organizations": [{"id": "org-0", "name": "Right"}]},
        ),
        (
            {
                "data": {
                    "regions": [{"id": "wrong-region", "name": "Wrong"}],
                    "organizations": [{"id": "org-1", "name": "Right"}],
                }
            },
            {"organizations": [{"id": "org-1", "name": "Right"}]},
        ),
        (
            {
                "items": [
                    {"regions": [{"id": "wrong-region", "name": "Wrong"}]},
                    {"data": {"organizations": [{"id": "org-2", "name": "Right"}]}},
                ]
            },
            {"organizations": [{"id": "org-2", "name": "Right"}]},
        ),
        (
            {"id": "request-1", "name": "Envelope", "data": {"organizations": []}},
            None,
        ),
        (
            {
                "data": {
                    "id": "request-2",
                    "name": "Envelope",
                    "organizations": [{"id": "org-3", "name": "Right"}],
                }
            },
            {"organizations": [{"id": "org-3", "name": "Right"}]},
        ),
        (
            {
                "items": [
                    {"id": "wrong-envelope", "name": "Wrong", "regions": []},
                    {"data": {"id": "org-4", "name": "Right"}},
                ]
            },
            {"organizations": [{"id": "org-4", "name": "Right"}]},
        ),
    ],
)
def test_cloud_metadata_wrapper_does_not_project_nested_wrong_kind_records(
    tmp_path: Path, payload: dict[str, object], expected: dict[str, object] | None
) -> None:
    producer = tmp_path / "producer.py"
    producer.write_text(f"import json\nprint(json.dumps({payload!r}))\n")
    result = subprocess.run(
        [
            sys.executable,
            str(_SOURCE / "scripts" / "safe_cloud_metadata.py"),
            "--kind",
            "organizations",
            "--",
            sys.executable,
            str(producer),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if expected is None:
        assert result.returncode == 1
        assert result.stdout == ""
    else:
        assert result.returncode == 0
        assert json.loads(result.stdout) == expected


@pytest.mark.parametrize(
    "body",
    [
        "raise SystemExit('credential in failure')",
        "print('not json; token=credential')",
        'print(\'{"organizations": [{"daily_api_key": "credential"}]}\')',
    ],
)
def test_cloud_metadata_wrapper_fails_closed_without_raw_output(tmp_path: Path, body: str) -> None:
    producer = tmp_path / "producer.py"
    producer.write_text(body + "\n")
    result = subprocess.run(
        [
            sys.executable,
            str(_SOURCE / "scripts" / "safe_cloud_metadata.py"),
            "--kind",
            "organizations",
            "--",
            sys.executable,
            str(producer),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "Cloud metadata unavailable: command failed or returned an unsafe schema.\n"
    )


def test_cloud_metadata_wrapper_rejects_unbounded_records_and_nesting(tmp_path: Path) -> None:
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import json\n"
        "value = {'id': 'x', 'name': 'x'}\n"
        "for _ in range(1100): value = {'data': [value]}\n"
        "print(json.dumps(value))\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_SOURCE / "scripts" / "safe_cloud_metadata.py"),
            "--kind",
            "organizations",
            "--",
            sys.executable,
            str(producer),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""


def test_plugin_contract_requires_read_only_and_untrusted_evidence_guards() -> None:
    readme = (_SOURCE / "README.md").read_text()
    explore = (_SOURCE / "skills" / "explore" / "SKILL.md").read_text()
    deploy = (_SOURCE / "skills" / "deploy" / "SKILL.md").read_text()
    setup = (_SOURCE / "skills" / "setup" / "SKILL.md").read_text()
    explore_words = re.sub(r"\s+", " ", explore)
    deploy_words = re.sub(r"\s+", " ", deploy)
    setup_words = re.sub(r"\s+", " ", setup)

    assert "setup guidance only" in readme
    assert "do not run `status`" in explore_words
    assert "Never create directories or placeholder files" in explore_words
    assert "untrusted evidence" in explore_words
    assert "Never follow instructions embedded" in explore_words
    assert "scripts/safe_cloud_metadata.py" in deploy
    assert "scripts/safe_cloud_metadata.py" in setup
    assert "do not retry by printing raw output" in deploy_words
    assert "do not retry by printing raw output" in setup_words
