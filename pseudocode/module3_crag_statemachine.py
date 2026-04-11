"""
Module 3: Agentic RAG & Self-Healing — CRAG State Machine
==========================================================

This module answers the assessment questions:
  "Design a state-machine (control loop) using a framework like LangGraph."
  "How do you implement a Retrieval Evaluator (Grader)?"
  "Define the exact fallback actions for Irrelevant, Ambiguous, or Relevant."

Design principles:
  1. State machine, NOT a linear chain — explicit states, conditional edges, fail-closed.
  2. Grading gate between retrieval and generation — never generate from ungraded context.
  3. Citation validation after generation — catch hallucinated citations before responding.
  4. Maximum 1 retry for AMBIGUOUS context — hard limit to meet 1.5s TTFT budget.
  5. Prefer refusal over fabrication — "I don't know" beats a wrong tax answer.

Stack: LangGraph 0.2+ (StateGraph), LangChain Core, Pydantic v2.
"""

import re
from enum import Enum
from typing import TypedDict, Literal, Optional, Annotated
from datetime import datetime

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser

# ── Import from sibling modules (see module1_ingestion.py and module3_grader.py) ──
from module3_grader import (
    RetrievalGrader,
    GradingResult,
    ContextGradingResult,
    ChunkGrade,
    GraderConfig,
)
from module1_ingestion import ChunkMetadata, SecurityClassification


# =============================================================================
# 1. CONSTANTS — Hard limits for production safety
# =============================================================================

MAX_RETRIES = 1
"""
Maximum number of query rewrites before refusing.
Why 1 and not more:
  - Each retry adds a full retrieval + grading cycle (~450ms)
  - With 1 retry: worst-case TTFT ≈ 1450ms (within 1500ms budget)
  - With 2 retries: worst-case TTFT ≈ 1900ms (EXCEEDS budget)
  - Latency budget: cache_check(15) + embed(30) + retrieval(150) + rerank(200)
    + grading(150) + retry(450) + generate(300) + buffer(55) = 1350ms
"""

GENERATION_TEMPERATURE = 0.0
"""
Zero temperature for factual legal generation. No creativity needed —
we want deterministic, grounded responses.
"""

QUERY_REWRITE_TEMPERATURE = 0.3
"""
Low but non-zero temperature for query reformulation. Allows the LLM
to find alternative legal phrasings without hallucinating.
"""

TOP_K_RETRIEVAL = 40   # Initial hybrid retrieval (20 BM25 + 20 kNN, fused via RRF)
TOP_K_RERANK = 8       # After cross-encoder reranking — passed to grader and LLM


# =============================================================================
# 2. QUERY CLASSIFICATION
# =============================================================================

class QueryType(str, Enum):
    """
    Three query categories that determine the transformation strategy.

    REFERENCE:  Contains an exact legal identifier (ECLI, Article number).
                → Skip HyDE, use exact-ID retrieval shortcut.
    SIMPLE:     Single-fact lookup, no exact identifier.
                → Optionally apply HyDE if conceptual.
    COMPLEX:    Multi-part question requiring decomposition.
                → Decompose into sub-queries, retrieve for each, merge.
    """
    REFERENCE = "REFERENCE"
    SIMPLE = "SIMPLE"
    COMPLEX = "COMPLEX"


# Patterns for detecting exact legal references in queries
ECLI_QUERY_PATTERN = re.compile(r"ECLI:[A-Z]{2}:[A-Z]+:\d{4}:[A-Za-z0-9]+")
ARTICLE_QUERY_PATTERN = re.compile(
    r"(?:artikel|article|art\.?)\s+(\d+(?:\.\d+)*[a-z]?)", re.IGNORECASE
)


# =============================================================================
# 3. CRAG STATE — The typed state object carried through every node
# =============================================================================

class Citation(BaseModel):
    """A single citation extracted from the generated response."""
    chunk_id: str
    hierarchy_path: str
    article_ref: Optional[str] = None     # "Article 3.114, Paragraph 2"
    source_title: Optional[str] = None    # "Algemene wet inzake rijksbelastingen"


class CRAGState(TypedDict):
    """
    State object carried through the LangGraph state machine.

    Every node reads from and writes to this state.
    LangGraph manages state transitions automatically.
    """
    # ── Input ──
    query: str                              # Original user query
    user_security_tier: str                 # From auth: PUBLIC|INTERNAL|RESTRICTED|CLASSIFIED_FIOD
    session_id: str                         # For tracing / observability

    # ── Query understanding ──
    query_type: str                         # QueryType enum value
    transformed_query: str                  # After HyDE or rewrite (may equal original)
    sub_queries: list[str]                  # For COMPLEX: decomposed sub-questions
    detected_references: list[str]          # Exact ECLI/Article refs found in query

    # ── Retrieval ──
    retrieved_chunks: list[dict]            # Raw retrieval results (full metadata dicts)
    reranked_chunks: list[dict]             # After cross-encoder reranking (top-k=8)

    # ── Grading ──
    grading_result: str                     # GradingResult enum value: RELEVANT|AMBIGUOUS|IRRELEVANT
    graded_chunks: list[dict]              # Only chunks that passed grading (RELEVANT)
    chunk_grades: list[dict]                # Individual ChunkGrade results (for observability)

    # ── Generation ──
    generated_response: str                 # LLM-generated answer
    citations: list[dict]                   # Extracted Citation objects
    citations_valid: bool                   # Post-validation: all citations verified?

    # ── Control flow ──
    retry_count: int                        # Number of query rewrites performed (max: MAX_RETRIES)
    should_use_hyde: bool                   # Decision gate: apply HyDE on this query?
    error_message: str                      # Populated when refusing (explains why)

    # ── Output ──
    final_response: str                     # The response returned to the user
    final_citations: list[dict]             # Verified citations returned with the response
    pipeline_trace: list[dict]              # Trace of every node visited (for observability)


# =============================================================================
# 4. NODE FUNCTIONS — Each function is a node in the state machine
# =============================================================================

# ────────────────────────────────────────
# NODE: classify_query
# ────────────────────────────────────────

def classify_query(state: CRAGState) -> CRAGState:
    """
    Classify the incoming query into REFERENCE, SIMPLE, or COMPLEX.

    Uses regex for REFERENCE detection (fast, deterministic).
    Uses LLM for SIMPLE vs COMPLEX distinction.

    This classification determines:
      - REFERENCE → exact-ID retrieval shortcut (skip vector search)
      - SIMPLE + conceptual → optionally apply HyDE
      - COMPLEX → decompose into sub-queries
    """
    query = state["query"]
    detected_refs: list[str] = []

    # ── Check for exact legal references (regex — fast and deterministic) ──
    ecli_matches = ECLI_QUERY_PATTERN.findall(query)
    article_matches = ARTICLE_QUERY_PATTERN.findall(query)
    detected_refs.extend(ecli_matches)
    detected_refs.extend([f"art{m}" for m in article_matches])

    if detected_refs:
        # Query contains explicit legal references — use exact-ID retrieval
        return {
            **state,
            "query_type": QueryType.REFERENCE.value,
            "detected_references": detected_refs,
            "should_use_hyde": False,  # Never HyDE on reference queries
            "transformed_query": query,
            "sub_queries": [],
            "pipeline_trace": state.get("pipeline_trace", []) + [
                {"node": "classify_query", "result": "REFERENCE",
                 "refs_found": detected_refs, "timestamp": datetime.utcnow().isoformat()}
            ],
        }

    # ── Use LLM to classify SIMPLE vs COMPLEX ──
    classification_prompt = ChatPromptTemplate.from_messages([
        ("system", (
            "You classify tax questions as SIMPLE or COMPLEX.\n"
            "SIMPLE: A single fact lookup (e.g., 'What is the Box 1 rate for 2024?').\n"
            "COMPLEX: A multi-part question that requires information from multiple "
            "articles or multiple legal sources (e.g., 'How does the interaction between "
            "transfer pricing rules and the arm's length principle affect a Dutch subsidiary?').\n"
            "Respond with exactly one word: SIMPLE or COMPLEX."
        )),
        ("human", "{query}"),
    ])

    # llm is injected via the LangGraph configuration (see graph wiring section)
    llm = get_classification_llm()
    response = llm.invoke(classification_prompt.format_messages(query=query))
    is_complex = "COMPLEX" in response.content.upper()

    # ── HyDE decision gate ──
    # Only apply HyDE for conceptual SIMPLE queries (no legal references)
    # HyDE adds ~500ms latency — only worth it for vague conceptual queries
    should_hyde = (
        not is_complex
        and not detected_refs
        and not any(char.isdigit() for char in query[:20])  # No numbers = likely conceptual
    )

    query_type = QueryType.COMPLEX.value if is_complex else QueryType.SIMPLE.value

    return {
        **state,
        "query_type": query_type,
        "detected_references": detected_refs,
        "should_use_hyde": should_hyde,
        "transformed_query": query,
        "sub_queries": [],
        "pipeline_trace": state.get("pipeline_trace", []) + [
            {"node": "classify_query", "result": query_type,
             "should_hyde": should_hyde, "timestamp": datetime.utcnow().isoformat()}
        ],
    }


# ────────────────────────────────────────
# NODE: transform_query
# ────────────────────────────────────────

HYDE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a senior Dutch tax law expert. Given a tax question, write a short "
        "paragraph (3-5 sentences) that would appear in an official legal text or policy "
        "document answering this question. Include specific article numbers, legal terms, "
        "and provisions if you can infer them. Write in Dutch if the question is in Dutch.\n\n"
        "IMPORTANT: This is a hypothetical answer used for retrieval — accuracy of specific "
        "numbers is less important than using the right legal terminology and structure."
    )),
    ("human", "{query}"),
])

DECOMPOSITION_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a legal research assistant. Break this complex tax question into "
        "independent sub-questions that can each be answered by a single legal passage.\n\n"
        "Rules:\n"
        "- Each sub-question should be self-contained\n"
        "- Maximum 3 sub-questions (more adds too much latency)\n"
        "- Each sub-question should target a specific legal provision or concept\n"
        "- Return ONLY the sub-questions, one per line, numbered 1-3\n"
    )),
    ("human", "{query}"),
])


def transform_query(state: CRAGState) -> CRAGState:
    """
    Transform the query based on its classification.

    REFERENCE → Pass through unchanged (already precise).
    SIMPLE + should_hyde → Apply HyDE: generate hypothetical legal passage, use its
                           embedding for retrieval instead of the raw question embedding.
    COMPLEX → Decompose into 2-3 sub-queries. Retrieve for each, merge results.

    HyDE rationale for legal domain:
      User asks: "Can I deduct my home office expenses?"
      HyDE generates: "Op grond van artikel 3.17 Wet IB 2001 zijn kosten voor een
      werkruimte in de eigen woning aftrekbaar indien..."
      The hypothetical text contains legal terms ("aftrekbaar", "werkruimte") and
      article references that improve embedding similarity with real legal passages.
    """
    query_type = state["query_type"]
    query = state["query"]

    if query_type == QueryType.REFERENCE.value:
        # Reference queries are already precise — no transformation needed
        return {
            **state,
            "transformed_query": query,
            "sub_queries": [],
            "pipeline_trace": state.get("pipeline_trace", []) + [
                {"node": "transform_query", "action": "passthrough_reference",
                 "timestamp": datetime.utcnow().isoformat()}
            ],
        }

    if query_type == QueryType.COMPLEX.value:
        # Decompose into sub-queries
        llm = get_transformation_llm(temperature=QUERY_REWRITE_TEMPERATURE)
        response = llm.invoke(DECOMPOSITION_PROMPT.format_messages(query=query))

        # Parse numbered sub-questions
        sub_queries = []
        for line in response.content.strip().split("\n"):
            cleaned = re.sub(r"^\d+[\.\)]\s*", "", line.strip())
            if cleaned:
                sub_queries.append(cleaned)
        sub_queries = sub_queries[:3]  # Hard cap at 3

        return {
            **state,
            "transformed_query": query,  # Keep original for context
            "sub_queries": sub_queries,
            "pipeline_trace": state.get("pipeline_trace", []) + [
                {"node": "transform_query", "action": "decomposition",
                 "sub_queries": sub_queries, "timestamp": datetime.utcnow().isoformat()}
            ],
        }

    if state.get("should_use_hyde", False):
        # Apply HyDE: generate hypothetical legal passage
        llm = get_transformation_llm(temperature=QUERY_REWRITE_TEMPERATURE)
        response = llm.invoke(HYDE_PROMPT.format_messages(query=query))
        hyde_text = response.content.strip()

        # The hypothetical text becomes the retrieval query
        # (its embedding will be closer to real legal passages)
        return {
            **state,
            "transformed_query": hyde_text,
            "sub_queries": [],
            "pipeline_trace": state.get("pipeline_trace", []) + [
                {"node": "transform_query", "action": "hyde",
                 "hyde_text": hyde_text[:200], "timestamp": datetime.utcnow().isoformat()}
            ],
        }

    # SIMPLE without HyDE — pass through
    return {
        **state,
        "transformed_query": query,
        "sub_queries": [],
        "pipeline_trace": state.get("pipeline_trace", []) + [
            {"node": "transform_query", "action": "passthrough_simple",
             "timestamp": datetime.utcnow().isoformat()}
        ],
    }


# ────────────────────────────────────────
# NODE: retrieve
# ────────────────────────────────────────

def retrieve(state: CRAGState) -> CRAGState:
    """
    Execute hybrid retrieval against OpenSearch.

    Three retrieval paths (detailed in module2_retrieval.py):
      1. REFERENCE queries → exact-ID keyword filter (bypasses vector search)
      2. BM25 sparse retrieval → top-20 by keyword relevance
      3. kNN dense retrieval → top-20 by semantic similarity
    Paths 2+3 are fused via RRF (k=60) → top-40 → cross-encoder rerank → top-8.

    CRITICAL: user_security_tier is passed to OpenSearch, which applies the DLS
    filter from rbac_roles.json BEFORE any scoring occurs. A helpdesk user
    (INTERNAL tier) never sees RESTRICTED or CLASSIFIED_FIOD documents.

    For COMPLEX queries with sub_queries:
      - Retrieve for each sub-query independently
      - Merge results, deduplicate by chunk_id
      - Rerank the merged set → top-8
    """
    user_tier = state["user_security_tier"]
    query_type = state["query_type"]
    sub_queries = state.get("sub_queries", [])

    # ── Import retrieval function (detailed in module2_retrieval.py) ──
    from module2_retrieval import hybrid_retrieve, exact_id_retrieve, rerank_chunks

    all_retrieved: list[dict] = []

    if query_type == QueryType.REFERENCE.value:
        # Exact-ID shortcut: bypass vector search, use keyword filter
        for ref in state.get("detected_references", []):
            results = exact_id_retrieve(
                reference=ref,
                user_security_tier=user_tier,
                top_k=TOP_K_RERANK,  # Already precise, no need for wide retrieval
            )
            all_retrieved.extend(results)

    elif sub_queries:
        # COMPLEX: retrieve for each sub-query, merge
        for sub_q in sub_queries:
            results = hybrid_retrieve(
                query=sub_q,
                user_security_tier=user_tier,
                top_k=TOP_K_RETRIEVAL,
            )
            all_retrieved.extend(results)

        # Deduplicate by chunk_id (same chunk may be relevant to multiple sub-queries)
        seen_ids = set()
        deduped = []
        for chunk in all_retrieved:
            cid = chunk["chunk_id"]
            if cid not in seen_ids:
                seen_ids.add(cid)
                deduped.append(chunk)
        all_retrieved = deduped

    else:
        # SIMPLE: single retrieval pass
        all_retrieved = hybrid_retrieve(
            query=state["transformed_query"],
            user_security_tier=user_tier,
            top_k=TOP_K_RETRIEVAL,
        )

    # ── Rerank with cross-encoder → top-8 ──
    reranked = rerank_chunks(
        query=state["query"],  # Rerank against ORIGINAL query (not HyDE text)
        chunks=all_retrieved,
        top_k=TOP_K_RERANK,
    )

    return {
        **state,
        "retrieved_chunks": all_retrieved,
        "reranked_chunks": reranked,
        "pipeline_trace": state.get("pipeline_trace", []) + [
            {"node": "retrieve", "total_retrieved": len(all_retrieved),
             "after_rerank": len(reranked), "query_type": query_type,
             "timestamp": datetime.utcnow().isoformat()}
        ],
    }


# ────────────────────────────────────────
# NODE: grade_context
# ────────────────────────────────────────

def grade_context(state: CRAGState) -> CRAGState:
    """
    Evaluate retrieval quality using the RetrievalGrader (see module3_grader.py).

    Grades each of the top-8 reranked chunks as RELEVANT / AMBIGUOUS / IRRELEVANT.
    Aggregates into an overall grading result:
      - ≥3 chunks RELEVANT → overall RELEVANT → proceed to generation
      - Majority AMBIGUOUS → overall AMBIGUOUS → rewrite query and retry (if retries left)
      - Majority IRRELEVANT → overall IRRELEVANT → refuse to answer

    This is the CRITICAL GATE that prevents hallucination. Without this step,
    the LLM would generate from any retrieved context, no matter how irrelevant.
    """
    grader = RetrievalGrader(config=GraderConfig())
    chunks = state["reranked_chunks"]
    query = state["query"]  # Grade against original query

    # Grade all chunks (batch mode for latency — single LLM call)
    context_result: ContextGradingResult = grader.grade_context(
        query=query, chunks=chunks
    )

    return {
        **state,
        "grading_result": context_result.overall_grade.value,
        "graded_chunks": [c for c in chunks if any(
            g.chunk_id == c["chunk_id"] and g.grade == GradingResult.RELEVANT
            for g in context_result.chunk_grades
        )],
        "chunk_grades": [g.model_dump() for g in context_result.chunk_grades],
        "pipeline_trace": state.get("pipeline_trace", []) + [
            {"node": "grade_context", "overall": context_result.overall_grade.value,
             "relevant_count": context_result.relevant_count,
             "ambiguous_count": context_result.ambiguous_count,
             "irrelevant_count": context_result.irrelevant_count,
             "timestamp": datetime.utcnow().isoformat()}
        ],
    }


# ────────────────────────────────────────
# NODE: generate
# ────────────────────────────────────────

GENERATION_SYSTEM_PROMPT = """\
You are the National Tax Authority's AI legal assistant. Answer the user's tax question \
using ONLY the provided legal context passages. Follow these rules strictly:

1. ONLY use information from the provided context. Do not use prior knowledge.
2. For EVERY factual claim, include an inline citation in this exact format:
   [Source: {{chunk_id}} | {{hierarchy_path}}]
3. If the context does not contain enough information to fully answer the question, \
   explicitly state what you CAN answer and what you CANNOT.
4. Never guess, infer, or extrapolate beyond what the passages state.
5. Use precise legal language appropriate for tax authority professionals.
6. When citing amounts, percentages, or dates, always include the source.
7. If multiple provisions apply, present them in logical order with clear structure.

Context passages (each with metadata for citation):
{context}

Remember: Every claim needs a [Source: chunk_id | hierarchy_path] citation.
Incorrect citations are worse than no answer at all.
"""


def generate(state: CRAGState) -> CRAGState:
    """
    Generate the answer using only graded-as-RELEVANT chunks.

    The system prompt enforces:
      1. Only use provided context (no prior knowledge)
      2. Cite every claim with [Source: chunk_id | hierarchy_path]
      3. Admit gaps rather than guessing

    Temperature = 0.0 for deterministic, factual output.

    The context block includes chunk metadata (hierarchy_path, article_num, etc.)
    so the LLM has the raw material to construct proper citations.
    """
    graded_chunks = state["graded_chunks"]
    query = state["query"]

    # Build context block with metadata for citation
    context_parts = []
    for i, chunk in enumerate(graded_chunks, 1):
        context_parts.append(
            f"--- Passage {i} ---\n"
            f"chunk_id: {chunk['chunk_id']}\n"
            f"hierarchy_path: {chunk.get('hierarchy_path', 'N/A')}\n"
            f"title: {chunk.get('title', 'N/A')}\n"
            f"article: {chunk.get('article_num', 'N/A')}\n"
            f"paragraph: {chunk.get('paragraph_num', 'N/A')}\n"
            f"effective_date: {chunk.get('effective_date', 'N/A')}\n"
            f"text:\n{chunk.get('chunk_text', chunk.get('text', ''))}\n"
        )
    context_block = "\n".join(context_parts)

    # Format the generation prompt
    prompt = ChatPromptTemplate.from_messages([
        ("system", GENERATION_SYSTEM_PROMPT),
        ("human", "{query}"),
    ])

    llm = get_generation_llm(temperature=GENERATION_TEMPERATURE)
    response = llm.invoke(
        prompt.format_messages(context=context_block, query=query)
    )
    generated_text = response.content

    # ── Extract citations from the generated text ──
    citation_pattern = re.compile(
        r"\[Source:\s*([^\|]+?)\s*\|\s*([^\]]+?)\s*\]"
    )
    citations = []
    for match in citation_pattern.finditer(generated_text):
        chunk_id = match.group(1).strip()
        hierarchy_path = match.group(2).strip()
        citations.append(Citation(
            chunk_id=chunk_id,
            hierarchy_path=hierarchy_path,
        ).model_dump())

    return {
        **state,
        "generated_response": generated_text,
        "citations": citations,
        "pipeline_trace": state.get("pipeline_trace", []) + [
            {"node": "generate", "citation_count": len(citations),
             "response_length": len(generated_text),
             "timestamp": datetime.utcnow().isoformat()}
        ],
    }


# ────────────────────────────────────────
# NODE: validate_output
# ────────────────────────────────────────

def validate_output(state: CRAGState) -> CRAGState:
    """
    Post-generation validation: verify that every citation references a chunk
    that was actually retrieved and graded as RELEVANT.

    This catches LLM-hallucinated citations. Even GPT-4 and Claude fabricate
    plausible-looking article numbers and ECLI references. We use deterministic
    chunk_id matching (from our schema) to verify.

    Validation logic:
      For each citation in the response:
        - Check that citation.chunk_id exists in graded_chunks
        - If ANY citation references a chunk_id NOT in graded_chunks → FAIL
        - If response has NO citations at all → FAIL (every claim must be cited)

    On failure: route to refuse() node instead of respond().
    """
    citations = state.get("citations", [])
    graded_chunks = state.get("graded_chunks", [])
    generated_response = state.get("generated_response", "")

    # Build set of valid chunk_ids from graded context
    valid_chunk_ids = {chunk["chunk_id"] for chunk in graded_chunks}

    # Check 1: Response must contain at least one citation
    if not citations:
        return {
            **state,
            "citations_valid": False,
            "error_message": (
                "Generated response contains no citations. "
                "Every factual claim must be grounded in a specific source."
            ),
            "pipeline_trace": state.get("pipeline_trace", []) + [
                {"node": "validate_output", "result": "FAILED",
                 "reason": "no_citations", "timestamp": datetime.utcnow().isoformat()}
            ],
        }

    # Check 2: Every cited chunk_id must exist in the graded context
    invalid_citations = []
    valid_citations = []
    for citation in citations:
        cid = citation.get("chunk_id", "")
        if cid in valid_chunk_ids:
            valid_citations.append(citation)
        else:
            invalid_citations.append(cid)

    if invalid_citations:
        return {
            **state,
            "citations_valid": False,
            "error_message": (
                f"Response contains {len(invalid_citations)} citation(s) referencing "
                f"chunks not in the retrieved context: {invalid_citations}. "
                "This indicates potential hallucination."
            ),
            "pipeline_trace": state.get("pipeline_trace", []) + [
                {"node": "validate_output", "result": "FAILED",
                 "reason": "invalid_chunk_ids", "invalid": invalid_citations,
                 "timestamp": datetime.utcnow().isoformat()}
            ],
        }

    # All citations verified
    return {
        **state,
        "citations_valid": True,
        "pipeline_trace": state.get("pipeline_trace", []) + [
            {"node": "validate_output", "result": "PASSED",
             "verified_citations": len(valid_citations),
             "timestamp": datetime.utcnow().isoformat()}
        ],
    }


# ────────────────────────────────────────
# NODE: rewrite_and_retry
# ────────────────────────────────────────

REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a legal search specialist. The user's tax question did not retrieve "
        "sufficiently relevant documents. Rephrase the question using more specific "
        "Dutch legal terminology, statute names, or article references that might "
        "improve retrieval.\n\n"
        "Rules:\n"
        "- Keep the semantic meaning identical\n"
        "- Add specific legal terms if you can infer them\n"
        "- If the question is in English, also provide the Dutch legal equivalent\n"
        "- Return ONLY the rewritten question, nothing else\n"
    )),
    ("human", "Original question: {query}\n\nPrevious retrieval returned ambiguous results. Rewrite:"),
])


def rewrite_and_retry(state: CRAGState) -> CRAGState:
    """
    Query reformulation when grading result is AMBIGUOUS.

    Uses LLM to rephrase with more specific legal terminology.
    Increments retry_count (hard limit: MAX_RETRIES = 1).

    Example:
      Original: "Can I deduct my home office?"
      Rewrite:  "Aftrekbaarheid werkruimte eigen woning artikel 3.17 Wet IB 2001"

    The rewritten query goes back through retrieve → grade_context.
    If still AMBIGUOUS/IRRELEVANT after retry → refuse.
    """
    llm = get_transformation_llm(temperature=QUERY_REWRITE_TEMPERATURE)
    response = llm.invoke(REWRITE_PROMPT.format_messages(query=state["query"]))
    rewritten = response.content.strip()

    return {
        **state,
        "transformed_query": rewritten,
        "sub_queries": [],          # Reset sub-queries
        "should_use_hyde": False,   # Don't double-apply HyDE on retry
        "retry_count": state.get("retry_count", 0) + 1,
        "pipeline_trace": state.get("pipeline_trace", []) + [
            {"node": "rewrite_and_retry",
             "original": state["query"], "rewritten": rewritten,
             "retry_count": state.get("retry_count", 0) + 1,
             "timestamp": datetime.utcnow().isoformat()}
        ],
    }


# ────────────────────────────────────────
# NODE: respond
# ────────────────────────────────────────

def respond(state: CRAGState) -> CRAGState:
    """
    Format and return the verified response to the user.

    Includes:
      - The generated answer with inline citations
      - A source list at the bottom with document titles and hierarchy paths
      - Confidence indicator based on grading results
    """
    graded_chunks = state.get("graded_chunks", [])
    citations = state.get("citations", [])

    # Build source list for the footer
    source_list = []
    seen_docs = set()
    for chunk in graded_chunks:
        doc_key = chunk.get("doc_id", "")
        if doc_key not in seen_docs:
            seen_docs.add(doc_key)
            source_list.append({
                "doc_id": doc_key,
                "title": chunk.get("title", "Unknown"),
                "hierarchy_path": chunk.get("hierarchy_path", ""),
                "effective_date": chunk.get("effective_date", ""),
                "source_url": chunk.get("source_url"),
            })

    return {
        **state,
        "final_response": state["generated_response"],
        "final_citations": citations,
        "pipeline_trace": state.get("pipeline_trace", []) + [
            {"node": "respond", "sources": len(source_list),
             "timestamp": datetime.utcnow().isoformat()}
        ],
    }


# ────────────────────────────────────────
# NODE: refuse
# ────────────────────────────────────────

def refuse(state: CRAGState) -> CRAGState:
    """
    Generate a polite, informative refusal when the system cannot produce
    a verified answer.

    Triggered when:
      - Grading result is IRRELEVANT (no relevant context found)
      - Grading result is AMBIGUOUS after max retries exhausted
      - Citation validation fails (hallucinated citations detected)

    The refusal includes:
      - Clear explanation of why the system cannot answer
      - Titles of any partially relevant documents found (so the user has a lead)
      - Suggestion to rephrase or consult a human expert

    Philosophy: "I don't know" is better than a wrong tax answer.
    A fabricated legal citation could lead to incorrect tax assessments.
    """
    error_message = state.get("error_message", "")
    reranked_chunks = state.get("reranked_chunks", [])

    # Collect any document titles that were at least retrieved (even if not relevant)
    partial_leads = []
    seen_titles = set()
    for chunk in reranked_chunks[:5]:  # Show up to 5 potentially related docs
        title = chunk.get("title", "")
        if title and title not in seen_titles:
            seen_titles.add(title)
            partial_leads.append(title)

    # Build refusal message
    if error_message:
        reason = error_message
    elif state.get("grading_result") == GradingResult.IRRELEVANT.value:
        reason = (
            "I could not find legal provisions or documents that directly address "
            "your question in the available corpus."
        )
    elif state.get("grading_result") == GradingResult.AMBIGUOUS.value:
        reason = (
            "I found documents that are topically related to your question, but none "
            "contain sufficiently specific information to provide a verified answer."
        )
    else:
        reason = (
            "I was unable to verify the accuracy of all citations in the generated "
            "response. To avoid providing potentially incorrect legal information, "
            "I am withholding the answer."
        )

    refusal_text = f"{reason}\n\n"
    if partial_leads:
        refusal_text += "You may find relevant information in the following documents:\n"
        for title in partial_leads:
            refusal_text += f"  - {title}\n"
        refusal_text += "\n"
    refusal_text += (
        "Please try rephrasing your question with more specific legal references, "
        "or consult a tax law specialist for assistance."
    )

    return {
        **state,
        "final_response": refusal_text,
        "final_citations": [],  # No citations on refusal
        "pipeline_trace": state.get("pipeline_trace", []) + [
            {"node": "refuse", "reason": reason[:200],
             "partial_leads": partial_leads,
             "timestamp": datetime.utcnow().isoformat()}
        ],
    }


# =============================================================================
# 5. CONDITIONAL ROUTING FUNCTIONS
# =============================================================================

def route_after_grading(state: CRAGState) -> Literal["generate", "rewrite_and_retry", "refuse"]:
    """
    Conditional edge after grade_context.

    Decision tree:
      RELEVANT                         → generate (proceed with answer)
      AMBIGUOUS + retries < MAX_RETRIES → rewrite_and_retry (one more chance)
      AMBIGUOUS + retries >= MAX_RETRIES → refuse (no more attempts)
      IRRELEVANT                        → refuse (immediately, no retry)

    The retry limit (MAX_RETRIES=1) is enforced HERE, not in the rewrite node.
    This is the architectural decision point that prevents infinite loops.
    """
    grading = state.get("grading_result", "")
    retries = state.get("retry_count", 0)

    if grading == GradingResult.RELEVANT.value:
        return "generate"
    elif grading == GradingResult.AMBIGUOUS.value and retries < MAX_RETRIES:
        return "rewrite_and_retry"
    else:
        # IRRELEVANT or AMBIGUOUS with retries exhausted
        return "refuse"


def route_after_validation(state: CRAGState) -> Literal["respond", "refuse"]:
    """
    Conditional edge after validate_output.

    If all citations are verified → respond (return to user).
    If any citation is invalid  → refuse (don't return unverified legal advice).

    This is the last safety gate before the response reaches the user.
    """
    if state.get("citations_valid", False):
        return "respond"
    else:
        return "refuse"


# =============================================================================
# 6. GRAPH WIRING — Assemble the state machine
# =============================================================================

def build_crag_graph() -> StateGraph:
    """
    Build and compile the CRAG state machine.

    State diagram:

    ┌─────────────────┐
    │  classify_query  │
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │ transform_query  │
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │    retrieve      │
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │  grade_context   │──── RELEVANT ────→┌──────────┐
    └────────┬────────┘                    │ generate  │
             │                             └─────┬────┘
             ├── AMBIGUOUS                       │
             │   (retries < 1)            ┌──────▼──────┐
             │        │                   │validate_output│
             │   ┌────▼──────────┐        └──────┬──────┘
             │   │rewrite_and_retry│             │
             │   └────┬──────────┘      ┌───────┴───────┐
             │        │                 │               │
             │        └──→ retrieve     │ VALID     INVALID
             │           (loop back)    │               │
             │                    ┌─────▼───┐    ┌──────▼──┐
             ├── IRRELEVANT       │ respond  │    │ refuse   │
             │        │           └─────────┘    └─────────┘
             └────────▼
                  ┌────────┐
                  │ refuse  │
                  └────────┘

    Compile options:
      - checkpointer: Optional MemorySaver for debugging (replay any state)
      - interrupt_before: Can pause before generate() for human-in-the-loop review
    """
    graph = StateGraph(CRAGState)

    # ── Add nodes ──
    graph.add_node("classify_query", classify_query)
    graph.add_node("transform_query", transform_query)
    graph.add_node("retrieve", retrieve)
    graph.add_node("grade_context", grade_context)
    graph.add_node("generate", generate)
    graph.add_node("validate_output", validate_output)
    graph.add_node("respond", respond)
    graph.add_node("rewrite_and_retry", rewrite_and_retry)
    graph.add_node("refuse", refuse)

    # ── Set entry point ──
    graph.set_entry_point("classify_query")

    # ── Unconditional edges (always follow this path) ──
    graph.add_edge("classify_query", "transform_query")
    graph.add_edge("transform_query", "retrieve")
    graph.add_edge("retrieve", "grade_context")
    graph.add_edge("generate", "validate_output")
    graph.add_edge("rewrite_and_retry", "retrieve")  # Loop back for retry

    # ── Conditional edges (branching based on state) ──
    graph.add_conditional_edges(
        "grade_context",
        route_after_grading,
        {
            "generate": "generate",
            "rewrite_and_retry": "rewrite_and_retry",
            "refuse": "refuse",
        },
    )
    graph.add_conditional_edges(
        "validate_output",
        route_after_validation,
        {
            "respond": "respond",
            "refuse": "refuse",
        },
    )

    # ── Terminal edges ──
    graph.add_edge("respond", END)
    graph.add_edge("refuse", END)

    # ── Compile ──
    # Optional: add checkpointer for debugging/observability
    # from langgraph.checkpoint.memory import MemorySaver
    # compiled = graph.compile(checkpointer=MemorySaver())
    compiled = graph.compile()

    return compiled


# =============================================================================
# 7. EXECUTION — How to invoke the CRAG pipeline
# =============================================================================

def invoke_crag(
    query: str,
    user_security_tier: str,
    session_id: str,
) -> dict:
    """
    Invoke the CRAG state machine for a user query.

    Args:
        query: The user's tax question.
        user_security_tier: From JWT auth — PUBLIC|INTERNAL|RESTRICTED|CLASSIFIED_FIOD.
                           Passed to OpenSearch for DLS filtering (pre-retrieval enforcement).
        session_id: For tracing / observability correlation.

    Returns:
        dict with 'final_response', 'final_citations', and 'pipeline_trace'.
    """
    graph = build_crag_graph()

    initial_state: CRAGState = {
        "query": query,
        "user_security_tier": user_security_tier,
        "session_id": session_id,
        # ── Defaults (populated by nodes) ──
        "query_type": "",
        "transformed_query": "",
        "sub_queries": [],
        "detected_references": [],
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "grading_result": "",
        "graded_chunks": [],
        "chunk_grades": [],
        "generated_response": "",
        "citations": [],
        "citations_valid": False,
        "retry_count": 0,
        "should_use_hyde": False,
        "error_message": "",
        "final_response": "",
        "final_citations": [],
        "pipeline_trace": [],
    }

    # Run the state machine
    result = graph.invoke(initial_state)

    return {
        "response": result["final_response"],
        "citations": result["final_citations"],
        "trace": result["pipeline_trace"],
        "grading_result": result["grading_result"],
    }


# =============================================================================
# 8. LLM FACTORY FUNCTIONS — Injected based on deployment configuration
# =============================================================================

def get_classification_llm():
    """
    Returns a lightweight LLM for query classification.
    Can be a smaller model (e.g., Mixtral 8x7B) since classification is simple.
    """
    from langchain_community.chat_models import ChatOpenAI
    return ChatOpenAI(
        model="mixtral-8x7b",  # Or the self-hosted model endpoint
        temperature=0.0,
        base_url="https://llm.tax-authority.internal/v1",
        api_key="internal",    # Auth handled by mTLS, not API key
    )


def get_transformation_llm(temperature: float = 0.3):
    """
    Returns the LLM for HyDE generation and query decomposition.
    Uses slightly higher temperature for creative rephrasing.
    """
    from langchain_community.chat_models import ChatOpenAI
    return ChatOpenAI(
        model="mixtral-8x22b",
        temperature=temperature,
        base_url="https://llm.tax-authority.internal/v1",
        api_key="internal",
    )


def get_generation_llm(temperature: float = 0.0):
    """
    Returns the primary LLM for answer generation.
    Must be the most capable model available — this generates the user-facing answer.
    Temperature=0.0 for deterministic, factual output.
    """
    from langchain_community.chat_models import ChatOpenAI
    return ChatOpenAI(
        model="mixtral-8x22b",  # Or GPT-4 via Azure Gov Cloud
        temperature=temperature,
        max_tokens=2048,
        base_url="https://llm.tax-authority.internal/v1",
        api_key="internal",
    )


# =============================================================================
# 9. USAGE EXAMPLE
# =============================================================================

"""
USAGE EXAMPLE — Trace of a successful query:

>>> result = invoke_crag(
...     query="Wat is de arbeidskorting voor 2024 volgens artikel 3.114 AWR?",
...     user_security_tier="INTERNAL",
...     session_id="sess-12345",
... )

Pipeline trace:
  1. classify_query  → REFERENCE (detected "artikel 3.114")
  2. transform_query → passthrough_reference (no transformation needed)
  3. retrieve        → exact-ID filter on article_num="3.114", DLS tier=INTERNAL
                       → 12 chunks found, reranked to top 8
  4. grade_context   → 5 RELEVANT, 2 AMBIGUOUS, 1 IRRELEVANT → overall RELEVANT
  5. generate        → LLM generates answer with 3 citations
  6. validate_output → All 3 chunk_ids verified in graded_chunks → PASSED
  7. respond         → Return answer with citations and source list

Result:
  response: "De arbeidskorting voor 2024 bedraagt 5.532 euro per kalenderjaar
  [Source: AWR-2024-v3::art3.114::par1::chunk000 | Algemene > Hoofdstuk 3 > Art 3.114 > Lid 1].
  Indien het arbeidsinkomen meer bedraagt dan 39.958 euro, wordt de arbeidskorting
  verminderd met 6,51% van het meerdere
  [Source: AWR-2024-v3::art3.114::par2::chunk000 | Algemene > Hoofdstuk 3 > Art 3.114 > Lid 2]."

  citations: [
    {chunk_id: "AWR-2024-v3::art3.114::par1::chunk000", hierarchy_path: "..."},
    {chunk_id: "AWR-2024-v3::art3.114::par2::chunk000", hierarchy_path: "..."},
  ]


USAGE EXAMPLE — Trace of an AMBIGUOUS → retry → success:

>>> result = invoke_crag(
...     query="Can I deduct my home office expenses?",
...     user_security_tier="INTERNAL",
...     session_id="sess-12346",
... )

Pipeline trace:
  1. classify_query    → SIMPLE (no references, conceptual)
  2. transform_query   → HyDE applied (generated hypothetical about "aftrekpost werkruimte")
  3. retrieve          → hybrid search with HyDE embedding → top 40 → rerank → top 8
  4. grade_context     → 1 RELEVANT, 5 AMBIGUOUS, 2 IRRELEVANT → overall AMBIGUOUS
  5. rewrite_and_retry → Rewrite: "Aftrekbaarheid werkruimte eigen woning artikel 3.17 Wet IB"
                         retry_count: 0 → 1
  6. retrieve          → hybrid search with rewritten query → top 40 → rerank → top 8
  7. grade_context     → 4 RELEVANT, 3 AMBIGUOUS, 1 IRRELEVANT → overall RELEVANT
  8. generate          → answer with citations
  9. validate_output   → PASSED
  10. respond          → return verified answer


USAGE EXAMPLE — Trace of an IRRELEVANT → refuse:

>>> result = invoke_crag(
...     query="What is the weather forecast for Amsterdam?",
...     user_security_tier="INTERNAL",
...     session_id="sess-12347",
... )

Pipeline trace:
  1. classify_query  → SIMPLE
  2. transform_query → passthrough (no HyDE — has no legal character)
  3. retrieve        → hybrid search → top 8 (all about tax, none about weather)
  4. grade_context   → 0 RELEVANT, 0 AMBIGUOUS, 8 IRRELEVANT → overall IRRELEVANT
  5. refuse          → "I could not find legal provisions that address your question..."
"""
