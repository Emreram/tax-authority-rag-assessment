"""
Retrieval grader — Gemini batch-grades retrieved chunks.
Returns: RELEVANT, AMBIGUOUS, or IRRELEVANT.
"""

import json
from app.pipeline.llm import generate
from app.config import get_settings
import structlog

log = structlog.get_logger()

GRADER_SYSTEM = """You are a legal retrieval quality assessor for the Dutch Tax Authority.
Given a tax question and retrieved passages, grade each passage.

For each passage, output a JSON array with objects containing:
- "chunk_id": the passage identifier
- "grade": one of "RELEVANT", "AMBIGUOUS", or "IRRELEVANT"
- "confidence": a float 0.0-1.0
- "reason": one sentence explaining your grade

Grade definitions:
- RELEVANT: passage directly answers or strongly supports answering the question
- AMBIGUOUS: passage is topically related but doesn't directly answer
- IRRELEVANT: passage has no meaningful connection to the question

Respond with ONLY a valid JSON array, no other text."""


async def grade_context(query: str, chunks: list[dict], settings) -> dict:
    """
    Batch-grades all chunks.
    Returns {overall: RELEVANT|AMBIGUOUS|IRRELEVANT, grades: list, relevant_chunks: list}
    """
    if not chunks:
        return {"overall": "IRRELEVANT", "grades": [], "relevant_chunks": []}

    passages = []
    for c in chunks:
        passages.append(f'chunk_id: "{c["chunk_id"]}"\ntext: "{c["chunk_text"][:500]}"')

    user_prompt = f"Question: {query}\n\nPassages:\n" + "\n---\n".join(passages)

    try:
        raw = await generate(
            system_prompt=GRADER_SYSTEM,
            user_prompt=user_prompt,
            temperature=0.0,
        )
        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        grades = json.loads(raw.strip())
    except Exception as e:
        log.warning("grader_parse_error", error=str(e))
        grades = [{"chunk_id": c["chunk_id"], "grade": "AMBIGUOUS", "confidence": 0.5, "reason": "parse error"} for c in chunks]

    relevant_ids = {g["chunk_id"] for g in grades if g.get("grade") == "RELEVANT"}
    relevant_chunks = [c for c in chunks if c["chunk_id"] in relevant_ids]

    relevant_count = len(relevant_ids)
    ambiguous_count = sum(1 for g in grades if g.get("grade") == "AMBIGUOUS")

    if relevant_count >= settings.min_relevant_chunks:
        overall = "RELEVANT"
    elif relevant_count > 0 or ambiguous_count >= 2:
        overall = "AMBIGUOUS"
    else:
        overall = "IRRELEVANT"

    log.info("grading_complete", overall=overall, relevant=relevant_count, ambiguous=ambiguous_count)
    return {"overall": overall, "grades": grades, "relevant_chunks": relevant_chunks}
