"""Offline chunk-yield smoke guards for repos in the default ingest set.

Like ``test_pipecat_layout.py``, these ALWAYS run on every ``pytest`` call and
must stay offline, deterministic, and fast (mocked ``IndexWriter`` — no
embedding model, no Chroma, no network; only a local ``git init``).

Where ``test_pipecat_layout.py`` asserts taxonomy/example-discovery invariants
against *vendored* fixture trees, these assert the complementary invariant for
repos added to ``SourceConfig.repos``: that the repo's on-disk layout actually
flows through ``SourceIngester`` discovery and emits source chunks. A repo whose
layout falls through the dispatch yields zero source chunks (the failure mode of
the Swift/Kotlin/C++ client SDKs, whose source parser produces nothing — they
get only a few README/config fallback chunks from the GitHub ingester, never
``search_api``-visible API chunks). These guards use synthetic repo trees rather
than vendored snapshots because the layout shape — not specific upstream
content — is what must not regress.
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


async def test_pipecat_ui_pnpm_monorepo_yields_chunks(tmp_path: Path) -> None:
    """``pipecat-ui`` shape: pnpm/turborepo monorepo with a root package.json
    and TypeScript source nested two levels down, under
    ``packages/<name>/src/<category>/``, plus a sibling ``apps/`` tree.

    Mirrors the verified upstream layout (shadcn-registry components under
    ``packages/registry/src/components/``). Distinct from the
    react-native-transports case above: source lives under ``packages/``, not
    ``transports/``, and nests one directory deeper (``src/components/`` vs.
    ``src/``) — a shape the root/immediate-subdir marker check in
    ``_has_ts_markers`` and the recursive ``rglob`` in ``_find_ts_files`` must
    both still traverse into.
    """
    slug = "pipecat-ai/pipecat-ui"
    clone_dir = tmp_path / "repos" / _sanitize_slug(slug)
    files = {
        "package.json": '{"name": "pipecat-ui", "private": true}\n',
        "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n  - 'apps/*'\n",
        "packages/registry/src/components/connect-button.tsx": (
            "export function ConnectButton(): JSX.Element {\n"
            "  return <button>Connect</button>;\n"
            "}\n"
        ),
        # Storybook fixtures are co-located with real components, not under a
        # skippable directory. Typed const export mirrors the real CSF3
        # shape pipecat-ui uses (`export const X: Story = {...}`); _find_ts_files
        # excludes *.stories.ts(x) by filename regardless of content shape.
        "packages/registry/src/components/connect-button.stories.tsx": (
            "import type { Meta, StoryObj } from '@storybook/react';\n"
            "import { ConnectButton } from './connect-button';\n"
            "type Story = StoryObj<typeof ConnectButton>;\n"
            "export const Default: Story = { render: () => <ConnectButton /> };\n"
        ),
        "apps/example/src/App.tsx": (
            "export function App(): JSX.Element {\n  return <div />;\n}\n"
        ),
        # shadcn's registry model vendors components byte-for-byte into
        # consuming apps (apps/example has its own components.json pointing
        # at the @pipecat registry) -- this is the real pipecat-ui shape, not
        # a contrived edge case. Same content as the registry component above,
        # at a different path: exercises the byte-identical-file dedup in
        # SourceIngester.ingest().
        "apps/example/src/components/pipecat/connect-button.tsx": (
            "export function ConnectButton(): JSX.Element {\n"
            "  return <button>Connect</button>;\n"
            "}\n"
        ),
    }
    create_git_repo(clone_dir, files)

    config = make_config(tmp_path)
    writer = make_mock_writer()
    ingester = SourceIngester(config, writer, slug)

    result = await ingester.ingest()

    assert result.errors == []
    assert result.records_upserted > 0, (
        "pnpm/turborepo monorepo yielded zero chunks — discovery dispatch "
        "likely no longer traverses nested packages/<name>/src/ layouts"
    )
    records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
    assert all(rec.content_type == "source" for rec in records)
    assert all(rec.repo == slug for rec in records)
    paths = {rec.path for rec in records}
    assert any("components/connect-button" in p for p in paths), (
        "expected a chunk from the nested packages/registry/src/components/ "
        f"path; got paths: {sorted(paths)}"
    )
    assert not any(p.endswith(".stories.tsx") for p in paths), (
        "Storybook *.stories.tsx fixtures should be excluded from source "
        f"chunking; got paths: {sorted(paths)}"
    )
    connect_button_paths = [
        p for p in paths if "connect-button" in p and not p.endswith(".stories.tsx")
    ]
    assert connect_button_paths == ["packages/registry/src/components/connect-button.tsx"], (
        "the vendored apps/example copy of connect-button.tsx is byte-identical "
        "to the packages/registry original — expected it to be skipped as a "
        f"duplicate file; got paths: {sorted(connect_button_paths)}"
    )
