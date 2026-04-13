"""
Query classifier — determines query type: REFERENCE, SIMPLE, or COMPLEX.
REFERENCE: detected by regex (ECLI/Article patterns), no LLM needed.
SIMPLE vs COMPLEX: uses Gemini LLM.
"""

import re
from app.pipeline.llm import generate

ECLI_PATTERN = re.compile(r"ECLI:[A-Z]{2}:[A-Z]{1,10}:\d{4}:[A-Z0-9]+", re.IGNORECASE)
ARTICLE_PATTERN = re.compile(r"\b[Aa]rt(?:ikel)?\s*\.?\s*(\d+[\.\:]?\d*[a-z]?)\b")

CLASSIFICATION_SYSTEM = """You are a query classifier for a Dutch tax law information system.
Classify the user query as exactly one of:
- SIMPLE: a single, focused question about one tax concept, rate, or rule
- COMPLEX: a multi-part question involving multiple tax concepts, scenarios, or requiring several provisions

Respond with only the word SIMPLE or COMPLEX, nothing else."""


async def classify_query(query: str) -> str:
    """Returns: REFERENCE, SIMPLE, or COMPLEX"""
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
