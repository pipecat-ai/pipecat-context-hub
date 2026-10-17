"""Tests for ``shared/config_writer.py`` (dev plan PR1 Phase 2c).

One test id per clause of the parent plan's ``[PR1]`` Writer test-plan bullet.
Env-loading names are reached through the module at call time (another test
reloads ``env_loading``); the writer's own ``ConfigNotTrusted`` is used for
``pytest.raises`` because that is the class its seam raises.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import pytest

from pipecat_context_hub.shared import config_writer, env_loading
from pipecat_context_hub.shared.config_writer import (
    EXTRA_REPOS_KEY,
    ConfigWriteRefused,
    update_extra_repos,
)
from tests.unit._config_trust_cases import TRUST_REFUSAL_CASES

_posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")


def _use(monkeypatch: pytest.MonkeyPatch, path: Path) -> Path:
    monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", str(path))
    return path


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str | None) -> Path:
    """A trusted (0700 dir, 0600 file) config at ``tmp_path/cfg/config.toml``."""
    directory = tmp_path / "cfg"
    directory.mkdir(exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    path = directory / "config.toml"
    if text is not None:
        path.write_text(text)
        if os.name == "posix":
            path.chmod(0o600)
    return _use(monkeypatch, path)


def _stray_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != "config.toml")


def _assert_round_trip(path: Path, expected: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Both the raw parse and the real loader see exactly ``expected``."""
    assert env_loading.parse_global_config(path)[EXTRA_REPOS_KEY] == expected
    monkeypatch.delenv(EXTRA_REPOS_KEY, raising=False)
    env_loading.load_global_config()
    assert os.environ[EXTRA_REPOS_KEY] == ",".join(expected)


_RICH = (
    "# header comment\n"
    'PIPECAT_HUB_STALE_AFTER_DAYS = "42"  # inline note\n'
    "\n"
    "# about repos\n"
    'PIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n'
    "\n"
    "# footer comment\n"
    'PIPECAT_HUB_TAINTED_REPOS = ["x/y",  "z/w"]\n'
)


class TestRoundTrip:
    def test_no_file_is_created(self, tmp_path, monkeypatch):
        path = _use(monkeypatch, tmp_path / "new" / "config.toml")
        result = update_extra_repos(add=["a/b"])
        assert result.changed and result.before == [] and result.after == ["a/b"]
        assert path.exists()
        _assert_round_trip(path, ["a/b"], monkeypatch)

    def test_empty_file(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, "")
        update_extra_repos(add=["a/b", "c/d"])
        _assert_round_trip(path, ["a/b", "c/d"], monkeypatch)

    def test_comments_preserved(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, _RICH)
        update_extra_repos(add=["c/d"])
        text = path.read_text()
        for comment in ("# header comment", "# about repos", "# footer comment"):
            assert comment in text
        _assert_round_trip(path, ["a/b", "c/d"], monkeypatch)

    def test_other_keys_byte_identical(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, _RICH)
        update_extra_repos(add=["c/d"])
        old = [ln for ln in _RICH.splitlines() if EXTRA_REPOS_KEY not in ln]
        new = [ln for ln in path.read_text().splitlines() if EXTRA_REPOS_KEY not in ln]
        assert new == old

    def test_other_keys_unchanged_when_key_absent(self, tmp_path, monkeypatch):
        original = '# keep me\nPIPECAT_HUB_STALE_AFTER_DAYS = "7"  # c\n'
        path = _config(tmp_path, monkeypatch, original)
        update_extra_repos(add=["a/b"])
        assert path.read_text().startswith(original)
        _assert_round_trip(path, ["a/b"], monkeypatch)

    def test_csv_value_rewritten_as_array(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, 'PIPECAT_HUB_EXTRA_REPOS = "a/b, c/d"\n')
        result = update_extra_repos(add=["e/f"])
        assert result.before == ["a/b", "c/d"]
        data = tomllib.loads(path.read_text())
        assert data[EXTRA_REPOS_KEY] == ["a/b", "c/d", "e/f"]  # a native array now
        _assert_round_trip(path, ["a/b", "c/d", "e/f"], monkeypatch)

    def test_remove(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, 'PIPECAT_HUB_EXTRA_REPOS = ["a/b", "c/d"]\n')
        update_extra_repos(remove=["a/b"])
        _assert_round_trip(path, ["c/d"], monkeypatch)

    def test_symlinked_config_keeps_link_and_updates_target(self, tmp_path, monkeypatch):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        if os.name == "posix":
            real_dir.chmod(0o700)
        real = real_dir / "config.toml"
        real.write_text('PIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n')
        if os.name == "posix":
            real.chmod(0o600)
        link_dir = tmp_path / "links"
        link_dir.mkdir()
        link = link_dir / "config.toml"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        _use(monkeypatch, link)
        update_extra_repos(add=["c/d"])
        assert link.is_symlink()
        assert tomllib.loads(real.read_text())[EXTRA_REPOS_KEY] == ["a/b", "c/d"]
        _assert_round_trip(link, ["a/b", "c/d"], monkeypatch)

    def test_config_file_env_var_selects_the_path(self, tmp_path, monkeypatch):
        decoy = tmp_path / "decoy.toml"
        target = _config(tmp_path, monkeypatch, "")
        update_extra_repos(add=["a/b"])
        assert not decoy.exists()
        assert tomllib.loads(target.read_text())[EXTRA_REPOS_KEY] == ["a/b"]

    def test_windows_runnable_round_trip(self, tmp_path, monkeypatch):
        """No skip marker: runs on Windows CI, the mirror-the-reader contract."""
        path = _use(monkeypatch, tmp_path / "cfg" / "config.toml")
        update_extra_repos(add=["a/b"])
        update_extra_repos(add=["c/d"])
        _assert_round_trip(path, ["a/b", "c/d"], monkeypatch)
        update_extra_repos(remove=["a/b"])
        _assert_round_trip(path, ["c/d"], monkeypatch)


class TestModes:
    @_posix_only
    def test_created_file_is_0600(self, tmp_path, monkeypatch):
        path = _use(monkeypatch, tmp_path / "cfg" / "config.toml")
        update_extra_repos(add=["a/b"])
        assert path.stat().st_mode & 0o777 == 0o600

    @_posix_only
    def test_created_directory_is_0700(self, tmp_path, monkeypatch):
        path = _use(monkeypatch, tmp_path / "x" / "y" / "config.toml")
        update_extra_repos(add=["a/b"])
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.parent.parent.stat().st_mode & 0o777 == 0o700

    @_posix_only
    def test_existing_mode_is_kept(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, 'PIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n')
        path.chmod(0o640)
        update_extra_repos(add=["c/d"])
        assert path.stat().st_mode & 0o777 == 0o640

    @_posix_only
    def test_existing_directory_mode_is_left_alone(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, "")
        path.parent.chmod(0o750)
        update_extra_repos(add=["a/b"])
        assert path.parent.stat().st_mode & 0o777 == 0o750


class TestAtomicReplace:
    def test_edit_lands_via_os_replace(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, 'PIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n')
        real_replace = os.replace
        calls: list[tuple[Any, Any]] = []

        def spy(src: Any, dst: Any, *a: Any, **kw: Any) -> None:
            calls.append((Path(src), Path(dst)))
            real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", spy)
        update_extra_repos(add=["c/d"])
        assert len(calls) == 1
        src, dst = calls[0]
        assert dst == Path(os.path.realpath(path))
        assert src.parent == dst.parent and src != dst
        assert not src.exists()  # the temp file was consumed
        assert _stray_files(path.parent) == []

    def test_replace_failure_leaves_original_and_no_temp(self, tmp_path, monkeypatch):
        original = 'PIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n'
        path = _config(tmp_path, monkeypatch, original)

        def boom(*a: Any, **kw: Any) -> None:
            raise OSError("disk says no")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            update_extra_repos(add=["c/d"])
        assert path.read_text() == original
        assert _stray_files(path.parent) == []


class TestRefusals:
    @pytest.mark.parametrize(
        "value",
        ["5", "true", "{ a = 1 }", "[1, 2]", '["a/b", 2]', "[[1]]"],
        ids=["int", "bool", "table", "int-array", "mixed-array", "nested-array"],
    )
    def test_non_array_non_string_value_refused(self, value, tmp_path, monkeypatch):
        original = f"PIPECAT_HUB_EXTRA_REPOS = {value}\n"
        path = _config(tmp_path, monkeypatch, original)
        with pytest.raises(ConfigWriteRefused) as excinfo:
            update_extra_repos(add=["a/b"])
        assert excinfo.value.reason
        assert path.read_text() == original
        assert _stray_files(path.parent) == []

    def test_invalid_toml_from_the_seam_refused(self, tmp_path, monkeypatch):
        original = "this is = = not toml\n"
        path = _config(tmp_path, monkeypatch, original)
        with pytest.raises(ConfigWriteRefused):
            update_extra_repos(add=["a/b"])
        assert path.read_text() == original
        assert _stray_files(path.parent) == []

    def test_invalid_toml_from_the_temp_file_reparse_refused(self, tmp_path, monkeypatch):
        original = 'PIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n'
        path = _config(tmp_path, monkeypatch, original)

        def bad_parse(p: Path) -> dict[str, object]:
            raise tomllib.TOMLDecodeError("synthetic", "", 0)

        monkeypatch.setattr(config_writer, "parse_global_config", bad_parse)
        with pytest.raises(ConfigWriteRefused):
            update_extra_repos(add=["c/d"])
        assert path.read_text() == original
        assert _stray_files(path.parent) == []

    def test_failed_reparse_aborts_and_leaves_original_untouched(self, tmp_path, monkeypatch):
        """Mutation target: the edit itself succeeds; only the re-parse disagrees.

        Removing the writer's "re-parse differs" abort lets ``os.replace`` run and
        this test fails (original changed, no ``ConfigWriteRefused``).
        """
        original = _RICH
        path = _config(tmp_path, monkeypatch, original)
        real_parse = env_loading.parse_global_config
        seen: list[Path] = []

        def lying_parse(p: Path) -> dict[str, object]:
            seen.append(p)
            data = dict(real_parse(p))
            data[EXTRA_REPOS_KEY] = ["not", "what", "was", "intended"]
            return data

        monkeypatch.setattr(config_writer, "parse_global_config", lying_parse)
        replaced: list[Any] = []
        real_replace = os.replace

        def spy_replace(*a: Any, **kw: Any) -> None:
            replaced.append(a)
            real_replace(*a, **kw)

        monkeypatch.setattr(os, "replace", spy_replace)
        with pytest.raises(ConfigWriteRefused):
            update_extra_repos(add=["c/d"])
        assert seen, "the temp file must be re-parsed before the rename"
        assert seen[0] != path  # it verified the temp file, not the original
        assert replaced == []
        assert path.read_text() == original
        assert _stray_files(path.parent) == []

    def test_reparse_that_drops_another_key_is_refused(self, tmp_path, monkeypatch):
        original = _RICH
        path = _config(tmp_path, monkeypatch, original)
        real_parse = env_loading.parse_global_config

        def dropping_parse(p: Path) -> dict[str, object]:
            data = dict(real_parse(p))
            data.pop("PIPECAT_HUB_STALE_AFTER_DAYS", None)
            return data

        monkeypatch.setattr(config_writer, "parse_global_config", dropping_parse)
        with pytest.raises(ConfigWriteRefused):
            update_extra_repos(add=["c/d"])
        assert path.read_text() == original

    def test_blank_config_file_override_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIPECAT_HUB_CONFIG_FILE", "")
        with pytest.raises(ConfigWriteRefused) as excinfo:
            update_extra_repos(add=["a/b"])
        assert excinfo.value.reason
        assert list(tmp_path.iterdir()) == []

    def test_unresolvable_path_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_writer, "resolve_global_config_path", lambda: None)
        with pytest.raises(ConfigWriteRefused):
            update_extra_repos(add=["a/b"])

    @pytest.mark.parametrize("build", TRUST_REFUSAL_CASES)
    def test_trust_refusal_propagates_unchanged_and_file_untouched(
        self, build, tmp_path, monkeypatch
    ):
        """Non-regular, world-writable, foreign-owned, untrusted parent dir,
        foreign symlink hop, unreadable, undecodable and embedded-NUL: the
        reader and the writer refuse the same inputs."""
        path = build(tmp_path, monkeypatch)
        # patched directly: an embedded-NUL path cannot live in an env var
        monkeypatch.setattr(config_writer, "resolve_global_config_path", lambda: path)

        def snapshot() -> tuple[object, ...]:
            try:
                st = os.lstat(path)
            except (OSError, ValueError):
                return ("absent",)
            body: bytes | None = None
            if not path.is_dir():
                try:
                    body = path.read_bytes()
                except OSError:
                    body = None
            return (st.st_size, st.st_mtime_ns, st.st_mode, body)

        before = snapshot()
        try:
            siblings = sorted(os.listdir(path.parent))
        except (OSError, ValueError):
            siblings = []
        with pytest.raises(config_writer.ConfigNotTrusted):
            update_extra_repos(add=["a/b"])
        assert snapshot() == before
        try:
            assert sorted(os.listdir(path.parent)) == siblings
        except (OSError, ValueError):
            pass
