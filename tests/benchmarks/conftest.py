"""Benchmark fixtures: seeded index with 100 records for latency testing."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import pytest

from pipecat_context_hub.services.embedding import (
    EmbeddingIndexWriter,
    EmbeddingService,
)
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever
from pipecat_context_hub.shared.config import EmbeddingConfig, StorageConfig
from pipecat_context_hub.shared.types import ChunkedRecord

NOW = datetime.now(tz=timezone.utc)

# Realistic-ish content fragments for docs and code.
_DOC_TOPICS = [
    "Getting started with Pipecat pipelines",
    "Configuring text-to-speech with ElevenLabs",
    "Using Daily as a WebRTC transport",
    "Building a voice assistant with OpenAI LLM",
    "Frame processors and the pipeline graph",
    "Integrating speech-to-text with Deepgram",
    "Running Pipecat on serverless infrastructure",
    "RTVI frontend SDK quickstart",
    "Handling interruptions in voice bots",
    "Deploying to Pipecat Cloud with fly.io",
]

_CODE_SNIPPETS = [
    "from pipecat.pipeline import Pipeline\nasync def main():\n    pipeline = Pipeline()",
    "from pipecat.services.elevenlabs import ElevenLabsTTSService\ntts = ElevenLabsTTSService()",
    "from pipecat.transports.daily import DailyTransport\ntransport = DailyTransport()",
    "from pipecat.services.openai import OpenAILLMService\nllm = OpenAILLMService()",
    "from pipecat.processors.frame_processor import FrameProcessor\nclass MyProcessor(FrameProcessor):",
    "from pipecat.services.deepgram import DeepgramSTTService\nstt = DeepgramSTTService()",
    "import asyncio\nasync def run_bot():\n    await asyncio.gather(pipeline.run())",
    "from pipecat.frames import TextFrame, AudioRawFrame\nframe = TextFrame(text='hello')",
    "from pipecat.services.cartesia import CartesiaTTSService\ntts = CartesiaTTSService()",
    "from pipecat.processors.aggregators import SentenceAggregator\nagg = SentenceAggregator()",
]


def _make_chunk_id(prefix: str, i: int) -> str:
    raw = f"{prefix}-{i:04d}"
    return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def _build_records() -> list[ChunkedRecord]:
    """Generate 50 doc + 50 code records with varied content."""
    records: list[ChunkedRecord] = []

    for i in range(50):
        topic = _DOC_TOPICS[i % len(_DOC_TOPICS)]
        content = f"# {topic}\n\n" + f"Detailed documentation about {topic.lower()}. " * 10
        records.append(
            ChunkedRecord(
                chunk_id=_make_chunk_id("doc", i),
                content=content,
                content_type="doc",
                source_url=f"https://docs.pipecat.ai/page-{i}",
                repo=None,
                path=f"/docs/page-{i}",
                indexed_at=NOW,
                metadata={"title": topic, "section": f"Section {i}"},
            )
        )

    for i in range(50):
        snippet = _CODE_SNIPPETS[i % len(_CODE_SNIPPETS)]
        content = f"# Example {i}: bot.py\n{snippet}\n" + f"# processing step {i}\npass\n" * 5
        records.append(
            ChunkedRecord(
                chunk_id=_make_chunk_id("code", i),
                content=content,
                content_type="code",
                source_url=f"https://github.com/pipecat-ai/pipecat/blob/main/examples/ex-{i}/bot.py",
                repo="pipecat-ai/pipecat",
                path=f"examples/ex-{i}/bot.py",
                commit_sha="bench000",
                indexed_at=NOW,
                metadata={
                    "repo": "pipecat-ai/pipecat",
                    "commit_sha": "bench000",
                    "capability_tags": ["tts", "stt"] if i % 2 == 0 else ["llm", "transport"],
                    "foundational_class": f"ex-{i}",
                    "execution_mode": "local",
                    "key_files": [f"examples/ex-{i}/bot.py"],
                    "language": "python",
                    "line_start": 1,
                    "line_end": 20,
                },
            )
        )

    return records


@pytest.fixture(scope="session")
def bench_embedding_service():
    """Session-scoped embedding service (model loaded once)."""
    return EmbeddingService(EmbeddingConfig())


@pytest.fixture(scope="module")
def bench_seeded_store(bench_embedding_service, tmp_path_factory):
    """Module-scoped IndexStore pre-loaded with 100 embedded records."""
    tmp_dir = tmp_path_factory.mktemp("bench")
    config = StorageConfig(data_dir=tmp_dir / "data")
    store = IndexStore(config)
    writer = EmbeddingIndexWriter(store, bench_embedding_service)

    records = _build_records()
    asyncio.run(writer.upsert(records))

    yield store
    store.close()


@pytest.fixture(scope="module")
def bench_retriever(bench_seeded_store, bench_embedding_service):
    """Module-scoped HybridRetriever wired to seeded index."""
    return HybridRetriever(bench_seeded_store, bench_embedding_service)
