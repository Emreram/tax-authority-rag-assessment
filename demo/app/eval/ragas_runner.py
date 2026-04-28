"""
Ragas evaluator — runs context_recall, faithfulness, answer_relevancy
on the golden set using the same Gemma 4 model that powers the demo
generator. Production would use GPT-4 (or a stronger external judge)
to remove the self-judgment bias.

Output shape:
  {"context_recall": 0.84, "faithfulness": 0.91, "answer_relevancy": 0.88,
   "sample_count": 18, "duration_s": 73.2, "judge_model": "ai/gemma4:E2B"}

Notes on Ragas adapter classes:
  Ragas expects LangChain-style LLM/Embeddings objects. We avoid the
  langchain dependency by implementing the minimal Ragas BaseRagasLLM /
  BaseRagasEmbeddings interface directly.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import structlog
from openai import AsyncOpenAI

from app.config import get_settings
from app.models import SecurityTier
from app.pipeline.crag import run_crag
from app.pipeline.embedder import embed_query

log = structlog.get_logger()


def _try_import_ragas():
    """Defer import so a missing/incompatible ragas does not break startup."""
    try:
        from ragas.dataset_schema import SingleTurnSample
        from ragas.embeddings.base import BaseRagasEmbeddings
        from ragas.llms.base import BaseRagasLLM
        from ragas.metrics import (
            answer_relevancy,
            context_recall,
            faithfulness,
        )
        return {
            "SingleTurnSample": SingleTurnSample,
            "BaseRagasEmbeddings": BaseRagasEmbeddings,
            "BaseRagasLLM": BaseRagasLLM,
            "context_recall": context_recall,
            "faithfulness": faithfulness,
            "answer_relevancy": answer_relevancy,
        }
    except Exception as e:
        log.warning("ragas_import_failed", error=str(e))
        return None


def _make_adapters(ragas_mod: dict[str, Any]):
    """Build Ragas LLM + Embeddings adapters that delegate to our local
    Docker Model Runner client and our in-process e5-small embedder."""

    class _LocalRagasLLM(ragas_mod["BaseRagasLLM"]):
        def __init__(self, client: AsyncOpenAI, model: str):
            self.client = client
            self.model = model

        async def agenerate_text(self, prompt, n: int = 1, temperature: Optional[float] = 0.0,
                                 stop=None, callbacks=None, **kwargs) -> Any:
            text = prompt if isinstance(prompt, str) else getattr(prompt, "to_string", lambda: str(prompt))()
            try:
                r = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": text}],
                    temperature=temperature or 0.0,
                    max_tokens=512,
                )
                content = r.choices[0].message.content or ""
            except Exception as e:
                log.warning("ragas_llm_call_failed", error=str(e))
                content = ""
            # Ragas may expect a LLMResult-like object; provide a duck-typed shim
            return _RagasResult(content)

        def generate_text(self, prompt, n: int = 1, temperature: Optional[float] = 0.0,
                          stop=None, callbacks=None, **kwargs) -> Any:
            return asyncio.run(self.agenerate_text(prompt, n, temperature, stop, callbacks, **kwargs))

        async def is_finished(self, response) -> bool:
            return True

    class _LocalRagasEmbeddings(ragas_mod["BaseRagasEmbeddings"]):
        async def aembed_query(self, text: str) -> list[float]:
            return await embed_query(text)

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            return [await embed_query(t) for t in texts]

        def embed_query(self, text: str) -> list[float]:
            return asyncio.run(self.aembed_query(text))

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return asyncio.run(self.aembed_documents(texts))

    return _LocalRagasLLM, _LocalRagasEmbeddings


class _RagasResult:
    """Minimal duck-typed shim for Ragas' expected LLMResult shape."""
    def __init__(self, text: str):
        self._text = text
        self.generations = [[_RagasGen(text)]]


class _RagasGen:
    def __init__(self, text: str):
        self.text = text
        self.message = _RagasMsg(text)


class _RagasMsg:
    def __init__(self, text: str):
        self.content = text


async def run_ragas(entries: list[dict], os_client, redis_client) -> dict:
    """Returns aggregated metrics across the golden set."""
    ragas_mod = _try_import_ragas()
    if ragas_mod is None:
        return {"error": "ragas not available", "context_recall": None,
                "faithfulness": None, "answer_relevancy": None,
                "sample_count": 0, "duration_s": 0.0}

    s = get_settings()
    client = AsyncOpenAI(base_url=s.llm_base_url, api_key="not-needed", timeout=300)
    LLMCls, EmbCls = _make_adapters(ragas_mod)
    llm = LLMCls(client, s.llm_model)
    emb = EmbCls()

    samples = []
    t_start = time.time()
    for entry in entries:
        if entry.get("must_refuse"):
            continue  # Ragas not designed to evaluate refusal-correctness
        try:
            result = await run_crag(
                query=entry["query"],
                security_tier=SecurityTier(entry.get("security_tier", "PUBLIC")),
                session_id="ragas",
                os_client=os_client,
                redis_client=redis_client,
            )
        except Exception as e:
            log.warning("ragas_crag_failed", id=entry.get("id"), error=str(e))
            continue
        ground_truth = " ".join(entry.get("expected_answer_contains", [])) or entry["query"]
        retrieved_contexts = []
        for c in (result.citations or []):
            # Citation only carries chunk_id; pull the actual chunk text from OS
            try:
                resp = os_client.get(index=s.opensearch_index, id=c.chunk_id, _source_excludes=["embedding"])
                retrieved_contexts.append(resp["_source"].get("chunk_text", ""))
            except Exception:
                continue
        if not retrieved_contexts:
            continue
        samples.append(ragas_mod["SingleTurnSample"](
            user_input=entry["query"],
            response=result.response or "",
            retrieved_contexts=retrieved_contexts,
            reference=ground_truth,
        ))

    metrics: dict = {"sample_count": len(samples), "judge_model": s.llm_model}
    metric_pairs = [
        (ragas_mod["context_recall"], "context_recall"),
        (ragas_mod["faithfulness"], "faithfulness"),
        (ragas_mod["answer_relevancy"], "answer_relevancy"),
    ]
    for metric_obj, key in metric_pairs:
        scores = []
        # ragas metrics expose .llm and .embeddings attrs that need to be set
        try:
            metric_obj.llm = llm
            metric_obj.embeddings = emb
        except Exception:
            pass
        for sample in samples:
            try:
                score = await metric_obj.single_turn_ascore(sample)
                if score is not None:
                    scores.append(float(score))
            except Exception as e:
                log.warning("ragas_metric_failed", metric=key, error=str(e)[:120])
        metrics[key] = sum(scores) / len(scores) if scores else None

    metrics["duration_s"] = round(time.time() - t_start, 1)
    return metrics
