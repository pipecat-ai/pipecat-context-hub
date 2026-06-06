"""Shared synthetic-repo helpers for source-ingestion tests.

These functions build the minimal scaffolding a ``SourceIngester`` run needs —
a mock ``IndexWriter``, a real on-disk git repo, and a mock ``HubConfig`` whose
``storage.data_dir`` points at a temp path. They live in a shared module (not a
per-file copy) so the unit ingester tests and the offline smoke-layout tests
construct identical fixtures from one source of truth.

This is a plain helper module, not a ``conftest`` — the functions are imported
directly rather than injected as fixtures, which keeps call sites explicit
across both ``tests/unit`` and ``tests/smoke``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def make_mock_writer() -> AsyncMock:
    """Create a mock IndexWriter that reports the number of records upserted."""
    writer = AsyncMock()
    writer.upsert = AsyncMock(side_effect=lambda records: len(records))
    writer.delete_by_source = AsyncMock(return_value=0)
    return writer


def make_config(tmp_path: Path) -> MagicMock:
    """Mock HubConfig whose storage data dir points at ``tmp_path``."""
    config = MagicMock()
    config.storage.data_dir = tmp_path
    return config


def create_git_repo(repo_dir: Path, files: dict[str, str]) -> str:
    """Initialise a git repo at ``repo_dir`` with ``files`` and return the SHA."""
    from git import Repo as GitRepo

    repo_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        fpath = repo_dir / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    git_repo = GitRepo.init(str(repo_dir))
    git_repo.index.add([str(repo_dir / p) for p in files])
    git_repo.index.commit("initial commit")
    return git_repo.head.commit.hexsha
