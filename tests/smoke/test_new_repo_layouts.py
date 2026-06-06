"""Offline chunk-yield smoke guards for repos in the default ingest set.

Like ``test_pipecat_layout.py``, these ALWAYS run on every ``pytest`` call and
must stay offline, deterministic, and fast (mocked ``IndexWriter`` — no
embedding model, no Chroma, no network; only a local ``git init``).

Where ``test_pipecat_layout.py`` asserts taxonomy/example-discovery invariants
against *vendored* fixture trees, these assert the complementary invariant for
repos added to ``SourceConfig.repos``: that the repo's on-disk layout actually
flows through ``SourceIngester`` discovery and emits source chunks. A repo whose
layout falls through the dispatch yields zero chunks (the failure mode of the
Swift/Kotlin/C++ client SDKs, which clone but produce nothing). These guards use
synthetic repo trees rather than vendored snapshots because the layout shape —
not specific upstream content — is what must not regress.
"""

from __future__ import annotations

from pathlib import Path

from pipecat_context_hub.services.ingest.source_ingest import (
    SourceIngester,
    _sanitize_slug,
)
from pipecat_context_hub.shared.types import ChunkedRecord

from tests._ingest_helpers import create_git_repo, make_config, make_mock_writer


async def test_react_native_transports_ts_monorepo_yields_chunks(tmp_path: Path) -> None:
    """``pipecat-client-react-native-transports`` shape: root package.json +
    TypeScript transport sources under ``transports/<name>/src/``.

    Mirrors the verified upstream layout (transport.ts / index.tsx living below a
    repo-root package.json, no ``src/`` Python package). The TS repo detector
    keys on package.json/tsconfig at root or an immediate subdir.
    """
    slug = "pipecat-ai/pipecat-client-react-native-transports"
    clone_dir = tmp_path / "repos" / _sanitize_slug(slug)
    files = {
        "package.json": '{"name": "pipecat-react-native-transports"}\n',
        "transports/daily/src/transport.ts": (
            "export class DailyTransport {\n"
            "  private url: string;\n"
            "  constructor(url: string) {\n"
            "    this.url = url;\n"
            "  }\n"
            "  async connect(): Promise<void> {\n"
            "    await fetch(this.url);\n"
            "  }\n"
            "}\n"
        ),
        "transports/daily/src/index.tsx": ("export { DailyTransport } from './transport';\n"),
    }
    create_git_repo(clone_dir, files)

    config = make_config(tmp_path)
    writer = make_mock_writer()
    ingester = SourceIngester(config, writer, slug)

    result = await ingester.ingest()

    assert result.errors == []
    assert result.records_upserted > 0, (
        "TS transports monorepo yielded zero chunks — discovery dispatch "
        "likely no longer recognises the root-package.json layout"
    )
    records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
    assert all(rec.content_type == "source" for rec in records)
    assert all(rec.repo == slug for rec in records)


async def test_pipecat_cli_python_package_yields_chunks(tmp_path: Path) -> None:
    """``pipecat-cli`` shape: ``src/pipecat_cli/`` package with sub-packages.

    Mirrors the verified upstream layout — the AST discovery walker keys on
    ``src/<pkg>/__init__.py``, so a flattened or renamed package root would
    regress here.
    """
    slug = "pipecat-ai/pipecat-cli"
    clone_dir = tmp_path / "repos" / _sanitize_slug(slug)
    files = {
        "src/pipecat_cli/__init__.py": '"""Pipecat CLI."""\n',
        "src/pipecat_cli/commands/__init__.py": "",
        "src/pipecat_cli/commands/serve.py": (
            '"""Serve command."""\n\n\n'
            "class ServeCommand:\n"
            '    """Run the MCP server."""\n\n'
            "    def __init__(self, host: str, port: int):\n"
            "        self.host = host\n"
            "        self.port = port\n\n"
            "    def run(self) -> int:\n"
            '        """Start the server and return an exit code."""\n'
            "        return self._serve(self.host, self.port)\n\n"
            "    def _serve(self, host: str, port: int) -> int:\n"
            "        return 0\n"
        ),
    }
    create_git_repo(clone_dir, files)

    config = make_config(tmp_path)
    writer = make_mock_writer()
    ingester = SourceIngester(config, writer, slug)

    result = await ingester.ingest()

    assert result.errors == []
    assert result.records_upserted > 0, (
        "pipecat-cli Python package yielded zero chunks — discovery walker "
        "likely no longer recognises the src/<pkg>/__init__.py layout"
    )
    records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
    assert all(rec.repo == slug for rec in records)
    chunk_types = {rec.metadata["chunk_type"] for rec in records}
    assert "class_overview" in chunk_types
