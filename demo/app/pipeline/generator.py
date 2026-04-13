"""
Response generator — uses Gemini to generate a grounded answer with citations.
Citation format: [Source: chunk_id | hierarchy_path]
"""

from app.pipeline.llm import generate
import structlog

log = structlog.get_logger()

GENERATOR_SYSTEM = """You are a Dutch Tax Authority legal information assistant.

CRITICAL RULES:
1. ONLY use information from the provided context passages. Do NOT use prior knowledge.
2. Every factual claim MUST be followed by a citation in this exact format:
   [Source: <chunk_id> | <hierarchy_path>]
3. If the context does not contain enough information, say so clearly.
4. Answer in the same language as the question (Dutch or English).
5. Be precise with numbers, percentages, and dates — these are legal facts.
6. End your response with a "Bronnen:" (Sources) section listing all cited chunk_ids.

Format example:
De arbeidskorting bedraagt maximaal € 5.532 in 2024. [Source: WetIB2001-2024::art3.114::lid2::chunk002 | Wet IB 2001 > Hoofdstuk 3 > Art. 3.114 > Lid 2]

Bronnen:
- WetIB2001-2024::art3.114::lid2::chunk002"""


async def generate_response(query: str, relevant_chunks: list[dict]) -> tuple[str, list[str]]:
    """
    Generates an answer using Gemini.
    Returns (response_text, list_of_cited_chunk_ids)
    """
    context = []
    for chunk in relevant_chunks[:6]:  # max 6 chunks in context
        context.append(
            f"[chunk_id: {chunk['chunk_id']}]\n"
            f"[path: {chunk['hierarchy_path']}]\n"
            f"{chunk['chunk_text']}"
        )

    user_prompt = f"Question: {query}\n\nContext passages:\n\n" + "\n\n---\n\n".join(context)

    response_text = await generate(
        system_prompt=GENERATOR_SYSTEM,
        user_prompt=user_prompt,
        temperature=0.0,
    )

    # Extract cited chunk_ids from response
    import re
    cited_ids = re.findall(r"\[Source:\s*([^\|]+)\s*\|", response_text)
    cited_ids = [cid.strip() for cid in cited_ids]

    log.info("generation_complete", citations=len(cited_ids))
    return response_text, cited_ids


async def rewrite_query(query: str) -> str:
    """Rewrites an ambiguous query with more specific Dutch legal terminology."""
    system = """You are a Dutch tax law query specialist.
Rewrite the given query to be more specific and use precise Dutch legal terminology.
Add relevant article numbers, law names, or technical terms if appropriate.
Return only the rewritten query, nothing else."""

    rewritten = await generate(system_prompt=system, user_prompt=query, temperature=0.3)
    return rewritten.strip()
