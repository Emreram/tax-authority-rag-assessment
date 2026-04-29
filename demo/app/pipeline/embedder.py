"""
In-process embedder using sentence-transformers with intfloat/multilingual-e5-small.

e5 models require a task prefix: 'query:' for retrieval queries, 'passage:' for indexed docs.
Output dim = 384. Runs on CPU.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Iterable

import numpy as np
import structlog
from sentence_transformers import SentenceTransformer

from app.config import get_settings

log = structlog.get_logger()

_model: SentenceTransformer | None = None
_load_lock = asyncio.Lock()
# Dedicated pool — keeps embedding work off the default executor so the main
# event loop isn't starved under concurrent ingest/query load.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="embed")


def _load() -> SentenceTransformer:
    global _model
    if _model is None:
        settings = get_settings()
        log.info("embedder_loading", model=settings.embedding_model)
        _model = SentenceTransformer(settings.embedding_model, device="cpu")
        log.info("embedder_loaded", dim=_model.get_sentence_embedding_dimension())
    return _model


async def _aload() -> SentenceTransformer:
    if _model is None:
        async with _load_lock:
            if _model is None:
                _load()
    return _model


def _prefix(text: str, kind: str) -> str:
    if kind == "query":
        return f"query: {text}"
    return f"passage: {text}"


async def embed_query(text: str) -> list[float]:
    return (await embed_batch([text], kind="query"))[0]


async def embed_document(text: str) -> list[float]:
    return (await embed_batch([text], kind="passage"))[0]


async def embed_batch(texts: Iterable[str], kind: str = "passage") -> list[list[float]]:
    model = await _aload()
    prefixed = [_prefix(t, kind) for t in texts]
    loop = asyncio.get_event_loop()
    vectors = await loop.run_in_executor(
        _executor,
        lambda: model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False),
    )
    return [v.tolist() for v in vectors]


def cosine(a: list[float], b: list[float]) -> float:
    av = np.asarray(a, dtype=np.float32)
    bv = np.asarray(b, dtype=np.float32)
    denom = (np.linalg.norm(av) * np.linalg.norm(bv)) or 1.0
    return float(np.dot(av, bv) / denom)


@lru_cache(maxsize=1)
def preload() -> None:
    """Force-load the embedder at startup (outside the request path)."""
    _load()
