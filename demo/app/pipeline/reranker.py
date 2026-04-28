"""
LLM-as-reranker.

After RRF fusion we have ~20 candidate chunks. We ask Ollama to rank them against
the query and return a JSON array of {chunk_id, score} pairs. We sort by score desc
and take the top N.

Why not bge-reranker-base: a dedicated cross-encoder is ~1.2 GB RAM and adds a
second model to the stack. On a 16 GB laptop we keep the Ollama instance as the
single source of inference.
"""

from __future__ import annotations

from typing import Iterable

import structlog

from app.pipeline.llm import generate_json

log = structlog.get_logger()


SYSTEM_PROMPT = """Je beoordeelt relevantie voor een Nederlandse belastingvraag.
Voor elk fragment geef je een score tussen 0.0 (niet relevant) en 1.0 (zeer relevant).

Geef antwoord in dit JSON-formaat:
{"scores": [{"chunk_id": "<id>", "score": <float>}, ...]}
"""


async def rerank(query: str, chunks: Iterable[dict], top_k: int = 8) -> list[dict]:
    chunks = list(chunks)
    if len(chunks) <= 1:
        return chunks

    passages = []
    for c in chunks:
        passages.append(f'chunk_id: "{c["chunk_id"]}"\ntext: "{(c.get("chunk_text", ""))[:400]}"')
    user_prompt = f"Vraag: {query}\n\nFragmenten:\n" + "\n---\n".join(passages)

    try:
        raw = await generate_json(SYSTEM_PROMPT, user_prompt, temperature=0.0, max_tokens=512)
        scores_list = raw.get("scores", []) if isinstance(raw, dict) else []
    except Exception as e:
        log.warning("rerank_failed", error=str(e))
        return chunks[:top_k]

    score_by_id: dict[str, float] = {}
    for entry in scores_list:
        try:
            score_by_id[str(entry["chunk_id"])] = float(entry.get("score", 0.0))
        except (KeyError, TypeError, ValueError):
            continue

    # Fallback: chunks the LLM missed get a low baseline score.
    def _key(c: dict) -> float:
        return score_by_id.get(c["chunk_id"], 0.1)

    ranked = sorted(chunks, key=_key, reverse=True)
    for c in ranked:
        c["_rerank_score"] = _key(c)
    return ranked[:top_k]
