"""Tests for Phase 4: Historical Version Indexing (version-pinned indexing).

Tests cover:
- Config: framework_version field + env var + effective_framework_version
- GitHubRepoIngester: _resolve_tag, clone_or_fetch with tag parameter
- CLI: --framework-version flag propagation
- HubStatusOutput: framework_version field
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester
from pipecat_context_hub.shared.config import (
    _FRAMEWORK_VERSION_ENV,
    HubConfig,
    StorageConfig,
)

# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestFrameworkVersionConfig:
    """Tests for the framework_version config field and env var."""

    def test_default_is_none(self):
        config = HubConfig()
        assert config.framework_version is None
        assert config.effective_framework_version is None

    def test_explicit_field_value(self):
        config = HubConfig(framework_version="v0.0.96")
        assert config.framework_version == "v0.0.96"
        assert config.effective_framework_version == "v0.0.96"

    def test_env_var_when_field_is_none(self):
        with patch.dict(os.environ, {_FRAMEWORK_VERSION_ENV: "v0.0.95"}):
            config = HubConfig()
            assert config.framework_version is None
            assert config.effective_framework_version == "v0.0.95"

    def test_field_takes_precedence_over_env_var(self):
        with patch.dict(os.environ, {_FRAMEWORK_VERSION_ENV: "v0.0.95"}):
            config = HubConfig(framework_version="v0.0.96")
            assert config.effective_framework_version == "v0.0.96"

    def test_empty_env_var_returns_none(self):
        with patch.dict(os.environ, {_FRAMEWORK_VERSION_ENV: "  "}):
            config = HubConfig()
            assert config.effective_framework_version is None

    def test_model_copy_propagates_version(self):
        config = HubConfig()
        updated = config.model_copy(update={"framework_version": "v0.0.96"})
        assert updated.effective_framework_version == "v0.0.96"
        assert config.effective_framework_version is None  # original unchanged


# ---------------------------------------------------------------------------
# GitHubRepoIngester._resolve_tag tests
# ---------------------------------------------------------------------------


def _create_tagged_repo(tmp_path: Path, tags: list[str]) -> Path:
    """Create a local git repo with the given tags on HEAD."""
    from git import Repo as GitRepo

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_repo = GitRepo.init(str(repo_dir))
    with git_repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "t@t.com")
    (repo_dir / "README.md").write_text("# Test\n")
    git_repo.index.add(["README.md"])
    git_repo.index.commit("initial")
    for tag in tags:
        # Use update_ref to avoid GPG signing issues in CI/local configs
        git_repo.git.update_ref(f"refs/tags/{tag}", "HEAD")
    return repo_dir


class TestResolveTag:
    """Tests for GitHubRepoIngester._resolve_tag."""

    def test_exact_tag_match(self, tmp_path: Path):
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.96"])
        git_repo = GitRepo(str(repo_dir))
        expected_sha = git_repo.head.commit.hexsha

        sha = GitHubRepoIngester._resolve_tag(git_repo, "v0.0.96")
        assert sha == expected_sha

    def test_auto_prefix_v(self, tmp_path: Path):
        """Passing '0.0.96' resolves to tag 'v0.0.96'."""
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.96"])
        git_repo = GitRepo(str(repo_dir))
        expected_sha = git_repo.head.commit.hexsha

        sha = GitHubRepoIngester._resolve_tag(git_repo, "0.0.96")
        assert sha == expected_sha

    def test_strip_v_prefix(self, tmp_path: Path):
        """Passing 'v1.0.0' resolves to tag '1.0.0' when no v-prefix tag exists."""
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["1.0.0"])
        git_repo = GitRepo(str(repo_dir))
        expected_sha = git_repo.head.commit.hexsha

        sha = GitHubRepoIngester._resolve_tag(git_repo, "v1.0.0")
        assert sha == expected_sha

    def test_missing_tag_raises(self, tmp_path: Path):
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.96"])
        git_repo = GitRepo(str(repo_dir))

        with pytest.raises(ValueError, match="Tag 'v999.0.0' not found"):
            GitHubRepoIngester._resolve_tag(git_repo, "v999.0.0")

    def test_invalid_tag_format_rejected(self, tmp_path: Path):
        """Tags with invalid characters are rejected before git lookup."""
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.96"])
        git_repo = GitRepo(str(repo_dir))

        for bad_tag in ["../evil", "tag\ninjection", "", "a" * 200, "tag with spaces"]:
            with pytest.raises(ValueError, match="Invalid tag format"):
                GitHubRepoIngester._resolve_tag(git_repo, bad_tag)

    def test_annotated_tag(self, tmp_path: Path):
        """Annotated tags are dereferenced to their commit."""
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, [])
        git_repo = GitRepo(str(repo_dir))
        expected_sha = git_repo.head.commit.hexsha
        # Create an annotated tag via git command to avoid GPG issues
        git_repo.git.tag("v0.0.50", "-a", "-m", "Release v0.0.50", "--no-sign")

        sha = GitHubRepoIngester._resolve_tag(git_repo, "v0.0.50")
        assert sha == expected_sha

    def test_missing_tag_hint_is_version_ordered(self, tmp_path: Path):
        """The 'available tags' hint ranks by version, not string order.

        v1.7.0 sorts above v1.10.0 lexicographically, so a name-keyed sort would
        omit the newest release from the hint.
        """
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.99", "v1.7.0", "v1.10.0"])
        git_repo = GitRepo(str(repo_dir))

        with pytest.raises(ValueError, match=r"\['v1.10.0', 'v1.7.0', 'v0.0.99'\]"):
            GitHubRepoIngester._resolve_tag(git_repo, "v999.0.0")


class TestResolveLatest:
    """Tests for the 'latest' sentinel and GitHubRepoIngester._latest_version_tag."""

    def test_picks_highest_version_not_lexicographic(self, tmp_path: Path):
        """v1.10.0 beats v1.7.0 — string ordering would pick the wrong one."""
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.99", "v1.7.0", "v1.10.0"])
        git_repo = GitRepo(str(repo_dir))

        assert GitHubRepoIngester._latest_version_tag(git_repo) == "v1.10.0"

    def test_sentinel_is_case_insensitive(self, tmp_path: Path):
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v1.7.0"])
        git_repo = GitRepo(str(repo_dir))
        expected_sha = git_repo.head.commit.hexsha

        for sentinel in ["latest", "LATEST", " Latest "]:
            assert GitHubRepoIngester._resolve_tag(git_repo, sentinel) == expected_sha

    def test_prerelease_skipped_when_final_exists(self, tmp_path: Path):
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v1.9.0", "v2.0.0-rc1"])
        git_repo = GitRepo(str(repo_dir))

        assert GitHubRepoIngester._latest_version_tag(git_repo) == "v1.9.0"

    def test_prerelease_used_when_only_option(self, tmp_path: Path):
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v2.0.0-rc1", "v2.0.0-rc2"])
        git_repo = GitRepo(str(repo_dir))

        assert GitHubRepoIngester._latest_version_tag(git_repo) == "v2.0.0-rc2"

    def test_unparseable_tags_ignored(self, tmp_path: Path):
        """Junk refs never outrank a real release."""
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["nightly", "v1.2.0", "archive-2024"])
        git_repo = GitRepo(str(repo_dir))

        assert GitHubRepoIngester._latest_version_tag(git_repo) == "v1.2.0"

    def test_no_version_tags_raises(self, tmp_path: Path):
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["nightly"])
        git_repo = GitRepo(str(repo_dir))

        with pytest.raises(ValueError, match="no version-like tags"):
            GitHubRepoIngester._latest_version_tag(git_repo)

    def test_no_tags_at_all_raises(self, tmp_path: Path):
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, [])
        git_repo = GitRepo(str(repo_dir))

        with pytest.raises(ValueError, match="no version-like tags"):
            GitHubRepoIngester._resolve_tag(git_repo, "latest")

    def test_local_version_segment_tag_skipped_not_raised(self, tmp_path: Path):
        """A tag with a PEP 440 local-version segment (e.g. '+cu121') must be
        skipped as a candidate, not crash the whole resolution with
        'Invalid tag format' — a normal release tag should still win.
        """
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v1.9.0", "v1.10.0+cu121"])
        git_repo = GitRepo(str(repo_dir))

        assert GitHubRepoIngester._latest_version_tag(git_repo) == "v1.9.0"

    def test_double_v_prefix_does_not_outrank_single_v(self, tmp_path: Path):
        """Reproduces the previously-reported bug: 'vv1.0.0' must not be
        treated as equal to (or beating) 'v1.0.0' — only one leading 'v' is
        ever stripped, so 'vv1.0.0' fails to parse as a version and is
        excluded from candidacy entirely.
        """
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v1.0.0", "vv1.0.0"])
        git_repo = GitRepo(str(repo_dir))

        assert GitHubRepoIngester._latest_version_tag(git_repo) == "v1.0.0"

    def test_uppercase_v_prefix_tag_can_win_latest(self, tmp_path: Path):
        """An uppercase-``V`` release is a real release.

        The parse used to strip lowercase ``v`` only while rejecting any leading
        ``v``/``V`` that survived, so ``V2.0.0`` was classed unparseable and a
        repo whose newest release used that spelling silently resolved ``latest``
        to an older ``v1.x``. Both ``Version()`` and the tag-input regex accept
        it, so the strip is case-insensitive too.
        """
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v1.9.0", "V2.0.0"])
        git_repo = GitRepo(str(repo_dir))

        assert GitHubRepoIngester._latest_version_tag(git_repo) == "V2.0.0"

    def test_equal_normalized_versions_on_different_commits_are_rejected(self, tmp_path: Path):
        """Aliases such as ``v1.0.0``/``1.0.0`` cannot choose arbitrarily."""
        from git import Repo as GitRepo

        repo_dir = _create_tagged_repo(tmp_path, ["v1.0.0"])
        git_repo = GitRepo(str(repo_dir))
        (repo_dir / "README.md").write_text("# Changed\n")
        git_repo.index.add(["README.md"])
        git_repo.index.commit("second commit")
        git_repo.git.update_ref("refs/tags/1.0.0", "HEAD")

        with pytest.raises(ValueError, match="Ambiguous version aliases"):
            GitHubRepoIngester._latest_version_tag(git_repo)


class TestResolveTagName:
    """Tests for GitHubRepoIngester.resolve_tag_name."""

    def test_concrete_tag_returned_unchanged_without_touching_git(self, tmp_path: Path):
        """A non-'latest' tag is returned as-is; the repo_path need not even
        point at a valid git repo, since resolve_tag_name must never call
        into git for a concrete tag.
        """
        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())

        result = ingester.resolve_tag_name(tmp_path / "not-a-git-repo", "v0.0.96")
        assert result == "v0.0.96"

    def test_latest_resolves_to_same_tag_as_latest_version_tag(self, tmp_path: Path):
        from git import Repo as GitRepo

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.99", "v1.7.0", "v1.10.0"])
        git_repo = GitRepo(str(repo_dir))
        expected = GitHubRepoIngester._latest_version_tag(git_repo)

        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())

        assert ingester.resolve_tag_name(repo_dir, "latest") == expected


# ---------------------------------------------------------------------------
# clone_or_fetch with tag parameter
# ---------------------------------------------------------------------------


def _make_mock_writer():
    """Create a mock IndexWriter."""
    from unittest.mock import AsyncMock

    writer = AsyncMock()
    writer.upsert = AsyncMock(side_effect=lambda records: len(records))
    writer.delete_by_source = AsyncMock(return_value=0)
    return writer


def _create_remote_and_clone_with_tags(
    tmp_path: Path,
    repo_slug: str,
    files: dict[str, str],
    tags: list[str],
) -> tuple[Path, Path]:
    """Create a source repo with tagged commits, a bare origin, and a local clone."""
    from git import Repo as GitRepo

    # Source repo
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    git_repo = GitRepo.init(str(source_dir))
    with git_repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "t@t.com")
    for rel_path, content in files.items():
        fpath = source_dir / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content)
    git_repo.index.add(list(files.keys()))
    git_repo.index.commit("initial")
    for tag in tags:
        git_repo.git.update_ref(f"refs/tags/{tag}", "HEAD")

    # Bare remote
    bare_remote = tmp_path / "origin.git"
    GitRepo.clone_from(str(source_dir), str(bare_remote), bare=True)
    source_repo = GitRepo(str(source_dir))
    source_repo.create_remote("origin", str(bare_remote))
    branch = source_repo.active_branch.name
    source_repo.git.push("origin", branch, "--tags")

    # Local clone (mimicking the data/repos/<safe_name> layout)
    safe_name = repo_slug.replace("/", "_")
    clone_dir = tmp_path / "data" / "repos" / safe_name
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    GitRepo.clone_from(str(bare_remote), str(clone_dir))

    return source_dir, clone_dir


class TestCloneOrFetchWithTag:
    """Tests for clone_or_fetch with the tag parameter."""

    def test_clone_or_fetch_with_tag_resolves_correct_sha(self, tmp_path: Path):
        from git import Repo as GitRepo

        repo_slug = "test-org/test-repo"
        source_dir, clone_dir = _create_remote_and_clone_with_tags(
            tmp_path,
            repo_slug,
            {"main.py": "print('v1')\n"},
            ["v0.0.96"],
        )
        # Add a second commit at HEAD (beyond the tag)
        source_repo = GitRepo(str(source_dir))
        (source_dir / "main.py").write_text("print('v2')\n")
        source_repo.index.add(["main.py"])
        source_repo.index.commit("v2 update")
        branch = source_repo.active_branch.name
        source_repo.git.push("origin", branch)

        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())

        # Fetch with tag — should get the tagged commit, not HEAD
        repo_path, tag_sha = ingester.clone_or_fetch(repo_slug, tag="v0.0.96")
        tagged_repo = GitRepo(str(repo_path))
        assert tagged_repo.head.commit.hexsha == tag_sha
        # The file content should be from the tagged version
        assert (repo_path / "main.py").read_text() == "print('v1')\n"

    def test_clone_or_fetch_without_tag_gets_head(self, tmp_path: Path):
        from git import Repo as GitRepo

        repo_slug = "test-org/test-repo"
        source_dir, clone_dir = _create_remote_and_clone_with_tags(
            tmp_path,
            repo_slug,
            {"main.py": "print('v1')\n"},
            ["v0.0.96"],
        )
        # Add a second commit
        source_repo = GitRepo(str(source_dir))
        (source_dir / "main.py").write_text("print('v2')\n")
        source_repo.index.add(["main.py"])
        source_repo.index.commit("v2 update")
        branch = source_repo.active_branch.name
        source_repo.git.push("origin", branch)

        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())

        # No tag — should get HEAD
        repo_path, head_sha = ingester.clone_or_fetch(repo_slug, tag=None)
        assert (repo_path / "main.py").read_text() == "print('v2')\n"

    def test_clone_or_fetch_with_latest_resolves_newest_release(self, tmp_path: Path):
        """`tag="latest"` lands on the newest release tag, not on HEAD."""
        from git import Repo as GitRepo

        repo_slug = "test-org/test-repo"
        source_dir, clone_dir = _create_remote_and_clone_with_tags(
            tmp_path,
            repo_slug,
            {"main.py": "print('v1.7')\n"},
            ["v1.7.0"],
        )
        # A newer release, then an untagged commit past it.
        source_repo = GitRepo(str(source_dir))
        (source_dir / "main.py").write_text("print('v1.10')\n")
        source_repo.index.add(["main.py"])
        source_repo.index.commit("v1.10 release")
        source_repo.git.update_ref("refs/tags/v1.10.0", "HEAD")
        (source_dir / "main.py").write_text("print('unreleased')\n")
        source_repo.index.add(["main.py"])
        source_repo.index.commit("post-release work")
        branch = source_repo.active_branch.name
        source_repo.git.push("origin", branch, "--tags")

        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())

        repo_path, sha = ingester.clone_or_fetch(repo_slug, tag="latest")
        assert (repo_path / "main.py").read_text() == "print('v1.10')\n"
        assert GitRepo(str(repo_path)).head.commit.hexsha == sha

    def test_latest_drops_a_release_tag_deleted_from_origin(self, tmp_path: Path):
        """A persistent clone must not keep selecting a deleted release tag."""
        from git import Repo as GitRepo

        repo_slug = "test-org/test-repo"
        source_dir, _clone_dir = _create_remote_and_clone_with_tags(
            tmp_path,
            repo_slug,
            {"main.py": "print('v1.9')\n"},
            ["v1.9.0"],
        )
        source_repo = GitRepo(str(source_dir))
        old_sha = source_repo.head.commit.hexsha
        (source_dir / "main.py").write_text("print('v1.10')\n")
        source_repo.index.add(["main.py"])
        source_repo.index.commit("v1.10 release")
        new_sha = source_repo.head.commit.hexsha
        source_repo.git.update_ref("refs/tags/v1.10.0", "HEAD")
        branch = source_repo.active_branch.name
        source_repo.git.push("origin", branch, "--tags")

        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())
        repo_path, first_sha = ingester.clone_or_fetch(repo_slug, tag="latest")
        assert first_sha == new_sha

        source_repo.git.update_ref("-d", "refs/tags/v1.10.0")
        source_repo.git.push("origin", "--delete", "v1.10.0")

        _repo_path, second_sha = ingester.clone_or_fetch(repo_slug, tag="latest")
        assert second_sha == old_sha
        assert "v1.10.0" not in {tag.name for tag in GitRepo(str(repo_path)).tags}

    def test_invalid_tag_raises_on_fetch(self, tmp_path: Path):
        repo_slug = "test-org/test-repo"
        _create_remote_and_clone_with_tags(
            tmp_path,
            repo_slug,
            {"main.py": "x = 1\n"},
            ["v0.0.96"],
        )

        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())

        with pytest.raises(ValueError, match="not found"):
            ingester.clone_or_fetch(repo_slug, tag="v999.0.0")


# ---------------------------------------------------------------------------
# HubStatusOutput framework_version field
# ---------------------------------------------------------------------------


class TestHubStatusFrameworkVersion:
    """Tests for the framework_version field on HubStatusOutput."""

    def test_default_is_none(self):
        from pipecat_context_hub.shared.types import HubStatusOutput

        output = HubStatusOutput(server_version="0.0.16")
        assert output.framework_version is None

    def test_explicit_value(self):
        from pipecat_context_hub.shared.types import HubStatusOutput

        output = HubStatusOutput(server_version="0.0.16", framework_version="v0.0.96")
        assert output.framework_version == "v0.0.96"

    def test_serialization_round_trip(self):
        from pipecat_context_hub.shared.types import HubStatusOutput

        output = HubStatusOutput(
            server_version="0.0.16",
            framework_version="v0.0.96",
            total_records=100,
        )
        json_str = output.model_dump_json()
        restored = HubStatusOutput.model_validate_json(json_str)
        assert restored.framework_version == "v0.0.96"
