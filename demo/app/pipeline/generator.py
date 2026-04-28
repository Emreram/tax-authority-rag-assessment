"""
Response generator — uses the local LLM (Docker Model Runner) to produce a grounded
answer with citations, then post-processes the output for human readability:
  - Inline `[Source: chunk_id | path]` markers are collapsed to `[N]` refs.
  - Any 'Bronnen:' footer the model adds is stripped (UI shows citation pills).
  - Markdown formatting (bullets, tables, **bold**) is preserved verbatim.
"""

from app.pipeline.citation_format import compact_citations
from app.pipeline.llm import generate
import structlog

log = structlog.get_logger()

GENERATOR_SYSTEM = """Je bent de KennisAssistent van de Belastingdienst.

INHOUDSREGELS:
1. Gebruik UITSLUITEND informatie uit de meegegeven contextpassages. Geen externe kennis.
2. Plaats na elke feitelijke claim één citatie in exact dit formaat: [Source: <chunk_id> | <hierarchy_path>]
3. Als de context onvoldoende is, zeg dat eerlijk in één korte zin.
4. Antwoord in dezelfde taal als de vraag (Nederlands of Engels).
5. Wees precies met bedragen, percentages en data — dit zijn juridische feiten.

OPMAAKREGELS (Markdown — voor leesbaarheid):
- Korte vraag (ja/nee, één feit) → 1–2 zinnen, geen heading, geen lijst.
- Numerieke schijven, tarieven of voorwaardenlijsten → gebruik een Markdown-tabel.
- Meerdere losse voorwaarden of stappen → gebruik bullet points (`- `).
- Bedragen, percentages en data: zet ze **vet** (`**€ 5.532**`, `**8,231%**`).
- GEEN 'Bronnen:' sectie aan het einde — de inline citaten zijn voldoende, de UI toont de bronnen apart.
- Geen H1. Optioneel één H2 (`## Onderwerp`) als de vraag dat verdient (>3 punten).
- Houd de response compact — geen herhaling, geen disclaimer-tekst.
"""


async def generate_response(query: str, relevant_chunks: list[dict]) -> tuple[str, list[str]]:
    """
    Generates an answer using the configured LLM, then post-processes for readability.
    Returns (response_text_with_compact_refs, ordered_cited_chunk_ids).
    The order of cited_ids matches the [N] indices in the response text.
    """
    context = []
    for chunk in relevant_chunks[:6]:  # max 6 chunks in context
        context.append(
            f"[chunk_id: {chunk['chunk_id']}]\n"
            f"[path: {chunk['hierarchy_path']}]\n"
            f"{chunk['chunk_text']}"
        )

    user_prompt = f"Vraag: {query}\n\nContext:\n\n" + "\n\n---\n\n".join(context)

    raw_response = await generate(
        system_prompt=GENERATOR_SYSTEM,
        user_prompt=user_prompt,
        temperature=0.0,
        max_tokens=900,
    )

    # Extract chunk_ids from the raw [Source: ...] markers BEFORE compacting,
    # so validation and the ordered list are derived from the same source-of-truth.
    import re
    raw_cited = re.findall(r"\[Source:\s*([^\|\]]+)\s*[\|\]]", raw_response)
    raw_cited = [cid.strip() for cid in raw_cited]

    known_ids = {c["chunk_id"] for c in relevant_chunks}
    cleaned_text, ordered_cids = compact_citations(raw_response, known_ids)

    # Build the final cited_ids list. Prefer the post-processed order (it matches [N]),
    # then fall back through the same chain as before for small-model robustness.
    cited_ids = list(ordered_cids)

    if not cited_ids:
        # Fallback 1: small models often skip the [Source:...] markup. Look for any
        # known chunk_id appearing verbatim in the response.
        cited_ids = [cid for cid in known_ids if cid in raw_response]

    if not cited_ids and relevant_chunks:
        # Fallback 2: still nothing → attribute the top 2 retrieved chunks as the
        # implicit sources used to ground the answer.
        cited_ids = [c["chunk_id"] for c in relevant_chunks[:2]]
        log.info("citation_fallback_implicit", count=len(cited_ids))

    log.info("generation_complete", citations=len(cited_ids), refs_in_text=len(ordered_cids))
    return cleaned_text, cited_ids


async def rewrite_query(query: str) -> str:
    """Rewrites an ambiguous query with more specific Dutch legal terminology."""
    system = """You are a Dutch tax law query specialist.
Rewrite the given query to be more specific and use precise Dutch legal terminology.
Add relevant article numbers, law names, or technical terms if appropriate.
Return only the rewritten query, nothing else."""

    rewritten = await generate(system_prompt=system, user_prompt=query, temperature=0.3)
    return rewritten.strip()
