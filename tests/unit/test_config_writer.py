"""Tests for ``shared/config_writer.py`` (dev plan PR1 Phase 2c).

One test id per clause of the parent plan's ``[PR1]`` Writer test-plan bullet.
``ConfigNotTrusted`` has one import path, ``env_loading``.
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from pipecat_context_hub.shared import config_writer, env_loading
from pipecat_context_hub.shared.config_writer import (
    ConfigWriteRefused,
    update_extra_repos,
)
from pipecat_context_hub.shared.env_keys import EXTRA_REPOS_ENV
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
    assert env_loading.parse_global_config(path)[EXTRA_REPOS_ENV] == expected
    monkeypatch.delenv(EXTRA_REPOS_ENV, raising=False)
    env_loading.load_global_config()
    assert os.environ[EXTRA_REPOS_ENV] == ",".join(expected)


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
        old = [ln for ln in _RICH.splitlines() if EXTRA_REPOS_ENV not in ln]
        new = [ln for ln in path.read_text().splitlines() if EXTRA_REPOS_ENV not in ln]
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
        assert data[EXTRA_REPOS_ENV] == ["a/b", "c/d", "e/f"]  # a native array now
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
        assert tomllib.loads(real.read_text())[EXTRA_REPOS_ENV] == ["a/b", "c/d"]
        _assert_round_trip(link, ["a/b", "c/d"], monkeypatch)

    def test_config_file_env_var_selects_the_path(self, tmp_path, monkeypatch):
        decoy = tmp_path / "decoy.toml"
        target = _config(tmp_path, monkeypatch, "")
        update_extra_repos(add=["a/b"])
        assert not decoy.exists()
        assert tomllib.loads(target.read_text())[EXTRA_REPOS_ENV] == ["a/b"]

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
        ["5", "true", "{ a = 1 }", "2026-01-01", '["a/b", 2]', "[[1]]", '["a/b,c/d"]'],
        ids=["int", "bool", "table", "date", "mixed-array", "nested-array", "comma-element"],
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
            data[EXTRA_REPOS_ENV] = ["not", "what", "was", "intended"]
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
        with pytest.raises(env_loading.ConfigNotTrusted):
            update_extra_repos(add=["a/b"])
        assert snapshot() == before
        try:
            assert sorted(os.listdir(path.parent)) == siblings
        except (OSError, ValueError):
            pass


_COMMENTED = (
    "# top\n"
    "PIPECAT_HUB_EXTRA_REPOS = [\n"
    '  "a/b",  # keep a\n'
    "  # own line\n"
    '  "c/d", # keep c\n'
    "]\n"
    "PIPECAT_HUB_STALE_AFTER_DAYS = 3\n"
)


class TestArrayEditedInPlace:
    """An existing array of strings is edited in place: element comments,
    own-line comments, the multiline layout and CRLF line endings survive."""

    def test_add_keeps_element_and_own_line_comments(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, _COMMENTED)
        update_extra_repos(add=["e/f"])
        text = path.read_text()
        for comment in ("# keep a", "# own line", "# keep c", "# top"):
            assert comment in text
        _assert_round_trip(path, ["a/b", "c/d", "e/f"], monkeypatch)

    def test_add_keeps_the_array_multiline(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, _COMMENTED)
        update_extra_repos(add=["e/f"])
        lines = path.read_text().splitlines()
        start = lines.index("PIPECAT_HUB_EXTRA_REPOS = [")
        end = lines.index("]", start)
        body = [ln.strip() for ln in lines[start + 1 : end]]
        assert body == ['"a/b",  # keep a', "# own line", '"c/d", # keep c', '"e/f",']

    def test_remove_of_a_commented_entry_drops_only_that_entry(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, _COMMENTED)
        update_extra_repos(remove=["C/D"])  # casefolded like every other match
        text = path.read_text()
        assert "# keep c" not in text  # the removed element's own comment goes with it
        assert "# keep a" in text and "# own line" in text
        _assert_round_trip(path, ["a/b"], monkeypatch)

    def test_add_to_an_array_with_a_trailing_comment(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, 'PIPECAT_HUB_EXTRA_REPOS = ["a/b"]  # mine\n')
        update_extra_repos(add=["c/d"])
        assert "# mine" in path.read_text()
        _assert_round_trip(path, ["a/b", "c/d"], monkeypatch)

    def test_crlf_file_stays_crlf(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, None)
        path.write_bytes(_COMMENTED.replace("\n", "\r\n").encode())
        if os.name == "posix":
            path.chmod(0o600)
        update_extra_repos(add=["e/f"], remove=["a/b"])
        raw = path.read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n")
        assert b"# keep c" in raw and b"# own line" in raw
        _assert_round_trip(path, ["c/d", "e/f"], monkeypatch)

    def test_crlf_file_stays_crlf_when_the_key_is_new(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, None)
        path.write_bytes(b"# top\r\nPIPECAT_HUB_STALE_AFTER_DAYS = 3\r\n")
        if os.name == "posix":
            path.chmod(0o600)
        update_extra_repos(add=["a/b"])
        raw = path.read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n")
        _assert_round_trip(path, ["a/b"], monkeypatch)


class TestReaderRuleParity:
    """The writer reads and verifies the list by the loader's own rule
    (``env_loading.config_list_value``), so reader, writer and ``repos list``
    agree on every value."""

    def test_scalar_array_the_loader_reads_is_editable(self, tmp_path, monkeypatch):
        """``load_global_config`` reads ``[1, 2]`` as ``"1,2"``; the writer agrees
        instead of refusing it (it used to say "must be an array of strings")."""
        path = _config(tmp_path, monkeypatch, "PIPECAT_HUB_EXTRA_REPOS = [1, 2]\n")
        monkeypatch.delenv(EXTRA_REPOS_ENV, raising=False)
        env_loading.load_global_config()
        assert os.environ[EXTRA_REPOS_ENV] == "1,2"
        result = update_extra_repos(add=["a/b"])
        assert result.before == ["1", "2"]
        _assert_round_trip(path, ["1", "2", "a/b"], monkeypatch)

    def test_csv_entries_are_split_and_stripped_like_the_reader(self, tmp_path, monkeypatch):
        path = _config(tmp_path, monkeypatch, 'PIPECAT_HUB_EXTRA_REPOS = " a/b ,, c/d "\n')
        assert update_extra_repos(add=["e/f"]).before == ["a/b", "c/d"]
        _assert_round_trip(path, ["a/b", "c/d", "e/f"], monkeypatch)

    @pytest.mark.parametrize("slug", ["a/b,c/d", " a/b", "a/b\n", "noslash", "a/.."])
    def test_a_slug_that_cannot_round_trip_is_refused(self, slug, tmp_path, monkeypatch):
        """A comma would split on read-back; the writer refuses before touching anything."""
        original = 'PIPECAT_HUB_EXTRA_REPOS = ["x/y"]\n'
        path = _config(tmp_path, monkeypatch, original)
        with pytest.raises(ConfigWriteRefused):
            update_extra_repos(add=[slug])
        assert path.read_text() == original
        assert _stray_files(path.parent) == []

    def test_reparse_is_checked_through_the_reader_rule(self, tmp_path, monkeypatch):
        """A re-parsed list the loader would read differently (a comma inside an
        element) aborts, even though the raw TOML value is "a list of strings"."""
        original = 'PIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n'
        path = _config(tmp_path, monkeypatch, original)
        real_parse = env_loading.parse_global_config

        def comma_parse(p: Path) -> dict[str, object]:
            data = dict(real_parse(p))
            data[EXTRA_REPOS_ENV] = ["a/b,c/d"]
            return data

        monkeypatch.setattr(config_writer, "parse_global_config", comma_parse)
        with pytest.raises(ConfigWriteRefused):
            update_extra_repos(add=["c/d"])
        assert path.read_text() == original

    def test_reparse_accepts_what_the_reader_reads_as_the_intended_list(
        self, tmp_path, monkeypatch
    ):
        """The accept side: a re-parsed CSV string ``"a/b,c/d"`` is, to the
        reader, exactly ``["a/b", "c/d"]``, so the edit succeeds. A writer that
        compared the raw TOML value (a str against a list) would refuse it."""
        path = _config(tmp_path, monkeypatch, 'PIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n')
        real_parse = env_loading.parse_global_config

        def csv_parse(p: Path) -> dict[str, object]:
            data = dict(real_parse(p))
            data[EXTRA_REPOS_ENV] = "a/b,c/d"
            return data

        monkeypatch.setattr(config_writer, "parse_global_config", csv_parse)
        result = update_extra_repos(add=["c/d"])
        assert result.changed and result.after == ["a/b", "c/d"]
        assert env_loading.parse_global_config(path)[EXTRA_REPOS_ENV] == ["a/b", "c/d"]

    def test_nan_in_another_key_does_not_block_the_edit(self, tmp_path, monkeypatch):
        """NaN != NaN: a plain ``==`` re-parse check refused every edit of such a file."""
        original = 'PIPECAT_HUB_WARMUP = nan\nPIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n'
        path = _config(tmp_path, monkeypatch, original)
        result = update_extra_repos(add=["c/d"])
        assert result.changed
        assert "PIPECAT_HUB_WARMUP = nan" in path.read_text()
        assert env_loading.parse_global_config(path)[EXTRA_REPOS_ENV] == ["a/b", "c/d"]


class TestWriteErrors:
    @_posix_only
    def test_unwritable_directory_raises_oserror_and_leaves_no_file(self, tmp_path, monkeypatch):
        """The write phase's ``OSError`` propagates (``cli_repos`` maps it to exit 1)."""
        if os.getuid() == 0:
            pytest.skip("root bypasses directory permission bits")
        original = 'PIPECAT_HUB_EXTRA_REPOS = ["a/b"]\n'
        path = _config(tmp_path, monkeypatch, original)
        path.parent.chmod(0o500)
        try:
            with pytest.raises(OSError):
                update_extra_repos(add=["c/d"])
            assert path.read_text() == original
        finally:
            path.parent.chmod(0o700)
        assert _stray_files(path.parent) == []


# ---------------------------------------------------------------------------
# A config.toml that does not exist yet: the writer must not create a file the
# reader would refuse (the reader's seam runs no path check for a missing file).
# ---------------------------------------------------------------------------


def _foreign_symlinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every symlink ``lstat`` reports is owned by another (non-root) user."""
    real_lstat = os.lstat
    foreign = os.getuid() + 12345

    class _Foreign:
        def __init__(self, st: os.stat_result) -> None:
            self._st = st

        def __getattr__(self, name: str) -> object:
            return getattr(self._st, name)

        st_uid = foreign

    def _lstat(p: Any, *a: Any, **kw: Any) -> Any:
        st = real_lstat(p, *a, **kw)
        return _Foreign(st) if stat.S_ISLNK(st.st_mode) else st

    monkeypatch.setattr(os, "lstat", _lstat)


def _foreign_dir(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """``os.stat(directory)`` reports another (non-root) owner."""
    real_stat = os.stat
    foreign = os.getuid() + 12345
    marker = str(directory)

    class _Foreign:
        def __init__(self, st: os.stat_result) -> None:
            self._st = st

        def __getattr__(self, name: str) -> object:
            return getattr(self._st, name)

        st_uid = foreign

    def _stat(p: Any, *a: Any, **kw: Any) -> Any:
        st = real_stat(p, *a, **kw)
        return _Foreign(st) if str(p) == marker else st

    monkeypatch.setattr(os, "stat", _stat)


def _racing_mkdir(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """``directory.mkdir`` loses a race: the directory appears, then FileExistsError."""
    real_mkdir = Path.mkdir

    def _mkdir(self: Path, *a: Any, **kw: Any) -> None:
        real_mkdir(self, *a, **kw)
        if self == directory:
            raise FileExistsError(str(self))

    monkeypatch.setattr(Path, "mkdir", _mkdir)


def _dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def _link(link: Path, target: Path) -> Path:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    return link


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@_posix_only
class TestNewFileTrust:
    """State space for a config path with no file yet. Accepted rows end with a
    file the reader reads; refused rows raise ``ConfigNotTrusted`` and create
    nothing (no directory, no file, no temp file)."""

    # -- accepted ---------------------------------------------------------
    def test_missing_file_and_missing_directories(self, tmp_path, monkeypatch):
        path = _use(monkeypatch, _dir(tmp_path / "home") / "a" / "b" / "config.toml")
        update_extra_repos(add=["a/b"])
        assert env_loading.read_trusted_config(path) is not None

    def test_dangling_link_owned_by_self_into_a_trusted_dir(self, tmp_path, monkeypatch):
        real_dir = _dir(tmp_path / "dotfiles")
        link = _link(_dir(tmp_path / "cfg") / "config.toml", real_dir / "sub" / "config.toml")
        _use(monkeypatch, link)
        update_extra_repos(add=["a/b"])
        assert link.is_symlink()
        assert (real_dir / "sub" / "config.toml").is_file()
        assert env_loading.parse_global_config(link)[EXTRA_REPOS_ENV] == ["a/b"]

    def test_missing_file_in_a_sticky_world_writable_dir(self, tmp_path, monkeypatch):
        sticky = _dir(tmp_path / "tmpish", 0o777 | stat.S_ISVTX)
        path = _use(monkeypatch, sticky / "config.toml")
        update_extra_repos(add=["a/b"])
        assert env_loading.read_trusted_config(path) is not None

    def test_own_directory_appearing_mid_way_is_accepted(self, tmp_path, monkeypatch):
        """A benign race (our own concurrent mkdir) still writes."""
        appeared = tmp_path / "home" / "appeared"
        _dir(tmp_path / "home")
        path = _use(monkeypatch, appeared / "config.toml")
        _racing_mkdir(monkeypatch, appeared)
        update_extra_repos(add=["a/b"])
        assert env_loading.parse_global_config(path)[EXTRA_REPOS_ENV] == ["a/b"]

    # -- refused ----------------------------------------------------------
    def _refused(self, root: Path) -> None:
        before = _tree(root)
        with pytest.raises(env_loading.ConfigNotTrusted) as excinfo:
            update_extra_repos(add=["a/b"])
        assert excinfo.value.reason  # names the predicate that refused, not a generic text
        assert _tree(root) == before

    def test_dangling_link_owned_by_another_user_in_a_sticky_dir(self, tmp_path, monkeypatch):
        """The reproduced attack: a planted dangling link in a 1777 directory
        made the writer create the victim directory and file at its target."""
        sticky = _dir(tmp_path / "shared", 0o777 | stat.S_ISVTX)
        victim = _dir(tmp_path / "victim")
        link = _link(sticky / "config.toml", victim / "made" / "config.toml")
        _use(monkeypatch, link)
        _foreign_symlinks(monkeypatch)
        self._refused(tmp_path)
        assert "symlink owned by uid" in env_loading_reason(link)

    def test_dangling_link_into_a_world_writable_non_sticky_dir(self, tmp_path, monkeypatch):
        loose = _dir(tmp_path / "loose", 0o777)
        link = _link(_dir(tmp_path / "cfg") / "config.toml", loose / "config.toml")
        _use(monkeypatch, link)
        self._refused(tmp_path)

    def test_missing_file_in_a_world_writable_non_sticky_dir(self, tmp_path, monkeypatch):
        _use(monkeypatch, _dir(tmp_path / "loose", 0o777) / "config.toml")
        self._refused(tmp_path)

    def test_missing_dirs_under_a_world_writable_non_sticky_ancestor(self, tmp_path, monkeypatch):
        _use(monkeypatch, _dir(tmp_path / "loose", 0o777) / "x" / "y" / "config.toml")
        self._refused(tmp_path)

    def test_foreign_directory_appearing_mid_way_is_refused_before_any_write(
        self, tmp_path, monkeypatch
    ):
        """Another account creates a missing directory between the pre-check and
        mkdir (FileExistsError). The writer re-validates with the reader's own
        predicates and refuses before creating even its temp file there."""
        home = _dir(tmp_path / "home")
        appeared = home / "appeared"
        path = _use(monkeypatch, appeared / "config.toml")
        _racing_mkdir(monkeypatch, appeared)
        _foreign_dir(monkeypatch, appeared)
        temps: list[str] = []
        real_mkstemp = config_writer.tempfile.mkstemp

        def spy(*a: Any, **kw: Any) -> Any:
            temps.append(str(kw.get("dir")))
            return real_mkstemp(*a, **kw)

        monkeypatch.setattr(config_writer.tempfile, "mkstemp", spy)
        with pytest.raises(env_loading.ConfigNotTrusted) as excinfo:
            update_extra_repos(add=["a/b"])
        assert "owned by uid" in excinfo.value.reason
        assert temps == []
        assert not path.exists()
        assert sorted(os.listdir(appeared)) == []

    def test_dangling_link_into_a_foreign_owned_dir(self, tmp_path, monkeypatch):
        other = _dir(tmp_path / "other-user")
        link = _link(_dir(tmp_path / "cfg") / "config.toml", other / "new" / "config.toml")
        _use(monkeypatch, link)
        _foreign_dir(monkeypatch, other)
        self._refused(tmp_path)


@_posix_only
class TestWriterTargetsTheFileTheReaderOpens:
    """A ``..`` after a component that is not an existing directory: the kernel
    fails the lookup while ``realpath`` collapses it lexically, so the writer
    would create a file the reader never sees. Refused, nothing created."""

    @pytest.mark.parametrize("make", ["missing", "file", "dangling-link"])
    def test_dotdot_after_a_non_directory_is_refused(self, make, tmp_path, monkeypatch):
        home = _dir(tmp_path / "home")
        hop = home / "hop"
        if make == "file":
            hop.write_text("")
        elif make == "dangling-link":
            _link(hop, home / "nowhere" / "dir")
        _use(monkeypatch, Path(f"{hop}/../config.toml"))
        before = _tree(tmp_path)
        with pytest.raises(ConfigWriteRefused, match=r"\.\."):
            update_extra_repos(add=["a/b"])
        assert _tree(tmp_path) == before
        assert not (home / "config.toml").exists()

    def test_dotdot_after_an_existing_directory_is_written_where_the_reader_reads(
        self, tmp_path, monkeypatch
    ):
        home = _dir(tmp_path / "home")
        _dir(home / "sub")
        path = _use(monkeypatch, Path(f"{home}/sub/../config.toml"))
        update_extra_repos(add=["a/b"])
        assert env_loading.parse_global_config(path)[EXTRA_REPOS_ENV] == ["a/b"]
        assert (home / "config.toml").is_file()


def env_loading_reason(path: Path) -> str:
    """The reason ``check_config_write_target`` gives for ``path``."""
    with pytest.raises(env_loading.ConfigNotTrusted) as excinfo:
        env_loading.check_config_write_target(path)
    return excinfo.value.reason
