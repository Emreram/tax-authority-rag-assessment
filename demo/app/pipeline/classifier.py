"""
Query classifier — determines query type: REFERENCE, SIMPLE, or COMPLEX.
REFERENCE: detected by regex (ECLI/Article patterns), no LLM needed.
SIMPLE vs COMPLEX: uses the local Gemma LLM.
"""

import re
import structlog

from app.pipeline.llm import generate, generate_json

log = structlog.get_logger()

ECLI_PATTERN = re.compile(r"ECLI:[A-Z]{2}:[A-Z]{1,10}:\d{4}:[A-Z0-9]+", re.IGNORECASE)
ARTICLE_PATTERN = re.compile(r"\b[Aa]rt(?:ikel)?\s*\.?\s*(\d+[\.\:]?\d*[a-z]?)\b")

CLASSIFICATION_SYSTEM = """You are a query classifier for a Dutch tax law information system.
Classify the user query as exactly one of:
- SIMPLE: a single, focused question about one tax concept, rate, or rule
- COMPLEX: a multi-part question involving multiple tax concepts, scenarios, or requiring several provisions

Respond with only the word SIMPLE or COMPLEX, nothing else."""


DECOMPOSE_SYSTEM = """Je bent een query-decomposer voor een Nederlandse fiscale RAG-pipeline.
Splits een complexe vraag in 2 of 3 onafhankelijke sub-vragen die elk los te
beantwoorden zijn met retrieval. Sub-vragen moeten zelfstandig leesbaar zijn
(geen referenties zoals "die" of "het"). Geef terug als JSON:

{"sub_queries": ["sub-vraag 1", "sub-vraag 2"]}

Voorbeeld input: "Ik ben ZZP'er met thuiskantoor — wat aftrekken en hoe BTW?"
Output: {"sub_queries":["welke kosten mag een ZZP'er aftrekken voor een thuiskantoor","is een ZZP'er BTW-plichtig over zijn diensten"]}

Geef MAX 3 sub-vragen. Als de input al simpel/atomair is, geef terug:
{"sub_queries":[]}
"""


async def classify_query(query: str) -> str:
    """Returns: REFERENCE, SIMPLE, or COMPLEX. Stable signature — used by crag.run_crag."""
    if ECLI_PATTERN.search(query):
        return "REFERENCE"
    if ARTICLE_PATTERN.search(query) and len(query.split()) < 10:
        return "REFERENCE"

    result = await generate(
        system_prompt=CLASSIFICATION_SYSTEM,
        user_prompt=query,
        temperature=0.0,
    )
    classification = result.strip().upper()
    if classification not in ("SIMPLE", "COMPLEX"):
        return "SIMPLE"
    return classification


async def decompose_complex(query: str) -> list[str]:
    """For COMPLEX queries: produce 2-3 independent sub-queries.
    Returns empty list if the LLM signals atomic, fails to parse, or any error.
    Caller is responsible for only invoking this on COMPLEX classifications.
    """
    try:
        d = await generate_json(DECOMPOSE_SYSTEM, query, temperature=0.0, max_tokens=300)
    except Exception as e:
        log.warning("decompose_failed", error=str(e))
        return []
    sub = d.get("sub_queries") if isinstance(d, dict) else None
    if not isinstance(sub, list):
        return []
    cleaned = [s.strip() for s in sub if isinstance(s, str) and s.strip()]
    return cleaned[:3]
