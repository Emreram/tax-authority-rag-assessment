"""
DeepEval evaluator — hallucination + bias + toxicity per query, averaged
across the non-refuse golden-set entries.

Like the Ragas runner, this uses the local Gemma 4 model as judge. That
introduces self-judgment bias; in production we'd point DeepEval at GPT-4
or Claude as the external judge.

Output shape:
  {"hallucination": 0.07, "bias": 0.02, "toxicity": 0.00,
   "sample_count": 18, "duration_s": 41.5, "judge_model": "ai/gemma4:E2B"}
"""
from __future__ import annotations

import os
import time
from typing import Any

import structlog

from app.config import get_settings
from app.models import SecurityTier
from app.pipeline.crag import run_crag

log = structlog.get_logger()


def _try_import_deepeval():
    try:
        from deepeval.metrics import BiasMetric, HallucinationMetric, ToxicityMetric
        from deepeval.test_case import LLMTestCase
        return {
            "HallucinationMetric": HallucinationMetric,
            "BiasMetric": BiasMetric,
            "ToxicityMetric": ToxicityMetric,
            "LLMTestCase": LLMTestCase,
        }
    except Exception as e:
        log.warning("deepeval_import_failed", error=str(e))
        return None


async def run_deepeval(entries: list[dict], os_client, redis_client) -> dict:
    """Aggregate hallucination / bias / toxicity across the golden set."""
    de = _try_import_deepeval()
    if de is None:
        return {"error": "deepeval not available", "hallucination": None,
                "bias": None, "toxicity": None,
                "sample_count": 0, "duration_s": 0.0}

    s = get_settings()
    # Point DeepEval's openai client at our local DMR
    os.environ.setdefault("OPENAI_API_KEY", "not-needed")
    os.environ["OPENAI_BASE_URL"] = s.llm_base_url
    os.environ["OPENAI_API_BASE"] = s.llm_base_url

    rates: dict[str, list[float]] = {"hallucination": [], "bias": [], "toxicity": []}
    t_start = time.time()
    sample_count = 0

    for entry in entries:
        if entry.get("must_refuse"):
            continue
        try:
            result = await run_crag(
                query=entry["query"],
                security_tier=SecurityTier(entry.get("security_tier", "PUBLIC")),
                session_id="deepeval",
                os_client=os_client,
                redis_client=redis_client,
            )
        except Exception as e:
            log.warning("deepeval_crag_failed", id=entry.get("id"), error=str(e))
            continue
        if not (result.response or "").strip():
            continue

        # Pull retrieval context for hallucination check
        contexts: list[str] = []
        for c in (result.citations or []):
            try:
                resp = os_client.get(index=s.opensearch_index, id=c.chunk_id, _source_excludes=["embedding"])
                contexts.append(resp["_source"].get("chunk_text", ""))
            except Exception:
                continue
        if not contexts:
            continue
        sample_count += 1

        tc = de["LLMTestCase"](
            input=entry["query"],
            actual_output=result.response,
            context=contexts,
        )

        for name, MetricCls in (("hallucination", de["HallucinationMetric"]),
                                  ("bias", de["BiasMetric"]),
                                  ("toxicity", de["ToxicityMetric"])):
            try:
                metric = MetricCls(model=s.llm_model, threshold=0.5, async_mode=False)
                await _measure(metric, tc)
                if metric.score is not None:
                    rates[name].append(float(metric.score))
            except Exception as e:
                log.warning("deepeval_metric_failed", metric=name, error=str(e)[:120])

    out: dict = {"sample_count": sample_count, "judge_model": s.llm_model}
    for k, v in rates.items():
        out[k] = sum(v) / len(v) if v else None
    out["duration_s"] = round(time.time() - t_start, 1)
    return out


async def _measure(metric: Any, tc: Any) -> None:
    """DeepEval has both .measure and .a_measure depending on version; try both."""
    if hasattr(metric, "a_measure"):
        await metric.a_measure(tc)
    else:
        # synchronous fallback
        metric.measure(tc)
