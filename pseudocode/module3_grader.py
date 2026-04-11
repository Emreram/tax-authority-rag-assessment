"""
Module 3: Retrieval Evaluator (Grader)
======================================

This module answers the assessment question:
  "How do you implement a Retrieval Evaluator (Grader)?"
  "Define the exact fallback actions for Irrelevant, Ambiguous, or Relevant."

Design principles:
  1. Grade EVERY retrieved chunk before generation — no ungraded context reaches the LLM.
  2. Three distinct grades: RELEVANT, AMBIGUOUS, IRRELEVANT (matches assessment wording).
  3. Batch grading (all chunks in one LLM call) for latency — ~150ms vs ~1200ms sequential.
  4. Confidence threshold: even if graded RELEVANT with low confidence → downgrade to AMBIGUOUS.
  5. Temporal awareness: a chunk from a repealed article should not score RELEVANT.
  6. Aggregation logic: ≥3 RELEVANT → proceed. Majority AMBIGUOUS → rewrite. Else → refuse.

This file is imported by module3_crag_statemachine.py (grade_context node).
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser


# =============================================================================
# 1. DATA MODELS
# =============================================================================

class GradingResult(str, Enum):
    """
    The three grading states specified by the assessment.
    Each maps to a specific action in the CRAG state machine:
      RELEVANT   → proceed to generation
      AMBIGUOUS  → rewrite query and retry (if retries remain)
      IRRELEVANT → refuse to answer
    """
    RELEVANT = "RELEVANT"
    AMBIGUOUS = "AMBIGUOUS"
    IRRELEVANT = "IRRELEVANT"


class ChunkGrade(BaseModel):
    """
    Grading result for a single retrieved chunk.

    Fields:
      chunk_id:   Links back to the deterministic ID from chunk_metadata.json.
                  Used by grade_context to filter graded_chunks in CRAGState.
      grade:      RELEVANT | AMBIGUOUS | IRRELEVANT.
      confidence: 0.0 - 1.0. Chunks with confidence below the threshold
                  are downgraded to AMBIGUOUS regardless of stated grade.
      reasoning:  Short explanation from the grading LLM. Logged for
                  observability / debugging but NOT shown to the end user.
    """
    chunk_id: str
    grade: GradingResult
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""


class BatchGradingResponse(BaseModel):
    """
    Structured output model for batch grading.
    The LLM returns one ChunkGrade per retrieved chunk.
    Using Pydantic structured output ensures reliable parsing.
    """
    grades: list[ChunkGrade]


class ContextGradingResult(BaseModel):
    """
    Aggregated result across all chunks — the overall context quality assessment.
    This is what the CRAG state machine reads to decide the next action.
    """
    overall_grade: GradingResult
    chunk_grades: list[ChunkGrade]
    relevant_count: int
    ambiguous_count: int
    irrelevant_count: int
    relevant_chunk_ids: list[str] = Field(
        description="chunk_ids of chunks graded RELEVANT — these become graded_chunks in CRAGState"
    )


# =============================================================================
# 2. CONFIGURATION
# =============================================================================

class GraderConfig(BaseModel):
    """
    All tunable parameters in one place with documented rationale.

    Adjusting these values affects the precision/recall trade-off:
      Higher min_relevant_chunks → fewer false positives, more refusals.
      Lower confidence_threshold → more chunks pass, higher risk of noise in context.
    """
    min_relevant_chunks: int = Field(
        default=3,
        description=(
            "Minimum number of RELEVANT chunks required to proceed to generation. "
            "3 out of 8 means ~37.5% of retrieved context must be directly relevant. "
            "Rationale: a legal answer typically needs ≥2-3 corroborating provisions. "
            "Set higher (4-5) for maximum safety, lower (2) for higher recall."
        ),
    )
    confidence_threshold: float = Field(
        default=0.6,
        description=(
            "Minimum confidence for a RELEVANT grade to stand. Below this, the chunk "
            "is downgraded to AMBIGUOUS even if the LLM said RELEVANT. "
            "Catches cases where the LLM is uncertain but still labels RELEVANT. "
            "0.6 is a moderate threshold — increase to 0.7+ for higher precision."
        ),
    )
    use_batch_grading: bool = Field(
        default=True,
        description=(
            "If True, grade all chunks in a single LLM call (faster: ~150ms). "
            "If False, grade each chunk individually (slower: ~150ms × 8 = ~1200ms). "
            "Batch mode is strongly preferred for meeting the 1.5s TTFT budget."
        ),
    )
    grading_model: str = Field(
        default="mixtral-8x22b",
        description="LLM model for grading. Can be smaller/faster than the generation model.",
    )


# =============================================================================
# 3. GRADING PROMPTS
# =============================================================================

GRADER_SYSTEM_PROMPT = """\
You are a legal retrieval quality assessor for the Dutch National Tax Authority. \
Your job is to evaluate whether retrieved document passages contain information \
that directly helps answer a tax-related question.

Grade each passage as:

RELEVANT — The passage directly addresses the question with at least one of:
  - A specific legal provision (article, paragraph) that applies to the question
  - A court ruling or consideration that directly bears on the legal issue
  - An explicit policy rule or procedure that answers the operational question
  - Concrete numerical values (rates, thresholds, amounts) the question asks about
  Note: The provision must be CURRENTLY EFFECTIVE. A passage about a repealed or \
  superseded article should be graded AMBIGUOUS, not RELEVANT, unless the user \
  explicitly asked about historical law.

AMBIGUOUS — The passage is topically related but lacks direct applicability:
  - Mentions the same area of law but a different specific provision
  - Discusses a related but distinct legal concept
  - Contains general principles without the specific rule being asked about
  - References the right article but from an expired/superseded version
  - Is about the right topic but from a different jurisdiction or tax type

IRRELEVANT — The passage has no meaningful connection to the question:
  - Completely different area of law or policy
  - Different tax type or jurisdiction with no transferable relevance
  - Administrative or procedural content unrelated to the legal question
  - Generic boilerplate text with no substantive content

For each passage, provide:
1. The grade (RELEVANT, AMBIGUOUS, or IRRELEVANT)
2. A confidence score (0.0 to 1.0) indicating how certain you are
3. A brief reasoning (1-2 sentences) explaining your assessment

{format_instructions}
"""

BATCH_GRADING_USER_PROMPT = """\
Question: {query}

Evaluate each of the following {num_chunks} passages:

{passages}

Return a grade for EVERY passage. Do not skip any.
"""

SINGLE_GRADING_USER_PROMPT = """\
Question: {query}

Passage (chunk_id: {chunk_id}):
{chunk_text}

Metadata:
- Document: {title}
- Position: {hierarchy_path}
- Effective date: {effective_date}
- Expiry date: {expiry_date}

Evaluate this single passage.
"""

# ── Few-shot examples embedded in the system prompt ──
FEW_SHOT_EXAMPLES = """
EXAMPLES:

Example 1 — RELEVANT (high confidence):
  Question: "Wat is de arbeidskorting voor 2024?"
  Passage: "Artikel 3.114 lid 1: De arbeidskorting bedraagt 5.532 euro per kalenderjaar."
  Grade: RELEVANT
  Confidence: 0.95
  Reasoning: Passage directly states the arbeidskorting amount for the current year with an exact article reference.

Example 2 — AMBIGUOUS (the version trap):
  Question: "Wat is het tarief Box 1 voor 2024?"
  Passage: "Artikel 2.10 Wet IB 2001 (geldig tot 31-12-2022): Het tarief bedraagt 37,07%."
  Grade: AMBIGUOUS
  Confidence: 0.55
  Reasoning: Correct article and topic, but the provision expired on 31-12-2022. The rate may have changed for 2024. Cannot be cited as current law.

Example 3 — IRRELEVANT:
  Question: "Is thuiskantoorkosten aftrekbaar?"
  Passage: "Artikel 15 AWR: De inspecteur kan een navorderingsaanslag opleggen..."
  Grade: IRRELEVANT
  Confidence: 0.90
  Reasoning: Passage is about tax assessment procedures (navordering), not about deductibility of expenses.
"""


# =============================================================================
# 4. RETRIEVAL GRADER — Core implementation
# =============================================================================

class RetrievalGrader:
    """
    Grades retrieved chunks for relevance to the query.

    Two modes:
      Batch (preferred): All chunks graded in one LLM call → ~150ms
      Individual:        Each chunk graded separately → ~150ms × N

    Used by: module3_crag_statemachine.py → grade_context() node.
    """

    def __init__(self, config: GraderConfig = GraderConfig()):
        self.config = config
        self._parser = PydanticOutputParser(pydantic_object=BatchGradingResponse)
        self._llm = self._init_llm()

    def _init_llm(self):
        """Initialize the grading LLM (self-hosted, no data leaves the network)."""
        from langchain_community.chat_models import ChatOpenAI
        return ChatOpenAI(
            model=self.config.grading_model,
            temperature=0.0,  # Deterministic grading
            base_url="https://llm.tax-authority.internal/v1",
            api_key="internal",  # Auth via mTLS
        )

    def grade_context(
        self, query: str, chunks: list[dict]
    ) -> ContextGradingResult:
        """
        Grade all chunks and return aggregated result.

        This is the main entry point called by the CRAG state machine.

        Args:
            query: The original user query (not the HyDE/rewritten version).
            chunks: List of chunk dicts with full metadata from retrieval.

        Returns:
            ContextGradingResult with overall grade and individual chunk grades.
        """
        if not chunks:
            return ContextGradingResult(
                overall_grade=GradingResult.IRRELEVANT,
                chunk_grades=[],
                relevant_count=0,
                ambiguous_count=0,
                irrelevant_count=0,
                relevant_chunk_ids=[],
            )

        # Grade chunks
        if self.config.use_batch_grading:
            chunk_grades = self._batch_grade(query, chunks)
        else:
            chunk_grades = self._individual_grade(query, chunks)

        # Apply confidence threshold: downgrade low-confidence RELEVANT to AMBIGUOUS
        chunk_grades = self._apply_confidence_threshold(chunk_grades)

        # Aggregate into overall result
        return self._aggregate(chunk_grades)

    def _batch_grade(
        self, query: str, chunks: list[dict]
    ) -> list[ChunkGrade]:
        """
        Grade all chunks in a single LLM call.

        Latency: ~150ms (one call regardless of chunk count).
        Preferred mode for meeting the 1.5s TTFT budget.

        The LLM receives all chunks with metadata and returns structured
        output (BatchGradingResponse) via Pydantic output parser.
        """
        # Build passages block with metadata for each chunk
        passages_parts = []
        for i, chunk in enumerate(chunks, 1):
            passages_parts.append(
                f"--- Passage {i} (chunk_id: {chunk['chunk_id']}) ---\n"
                f"Document: {chunk.get('title', 'N/A')}\n"
                f"Position: {chunk.get('hierarchy_path', 'N/A')}\n"
                f"Effective: {chunk.get('effective_date', 'N/A')}\n"
                f"Expired: {chunk.get('expiry_date', 'N/A (currently active)')}\n"
                f"Text:\n{chunk.get('chunk_text', chunk.get('text', ''))}\n"
            )
        passages_block = "\n".join(passages_parts)

        # Build prompt
        system_prompt = GRADER_SYSTEM_PROMPT.format(
            format_instructions=self._parser.get_format_instructions()
        ) + "\n" + FEW_SHOT_EXAMPLES

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", BATCH_GRADING_USER_PROMPT),
        ])

        messages = prompt.format_messages(
            query=query,
            num_chunks=len(chunks),
            passages=passages_block,
        )

        # Invoke LLM and parse structured output
        response = self._llm.invoke(messages)
        parsed: BatchGradingResponse = self._parser.parse(response.content)

        # Verify we got grades for all chunks
        graded_ids = {g.chunk_id for g in parsed.grades}
        for chunk in chunks:
            if chunk["chunk_id"] not in graded_ids:
                # LLM missed a chunk — assign AMBIGUOUS with low confidence as safe default
                parsed.grades.append(ChunkGrade(
                    chunk_id=chunk["chunk_id"],
                    grade=GradingResult.AMBIGUOUS,
                    confidence=0.3,
                    reasoning="Chunk was not graded by the LLM — assigned AMBIGUOUS as safe default.",
                ))

        return parsed.grades

    def _individual_grade(
        self, query: str, chunks: list[dict]
    ) -> list[ChunkGrade]:
        """
        Grade each chunk individually in separate LLM calls.

        Latency: ~150ms × N chunks (~1200ms for 8 chunks).
        Use only as fallback if batch mode fails or for debugging.
        """
        single_parser = PydanticOutputParser(pydantic_object=ChunkGrade)

        system_prompt = GRADER_SYSTEM_PROMPT.format(
            format_instructions=single_parser.get_format_instructions()
        ) + "\n" + FEW_SHOT_EXAMPLES

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", SINGLE_GRADING_USER_PROMPT),
        ])

        grades: list[ChunkGrade] = []
        for chunk in chunks:
            messages = prompt.format_messages(
                query=query,
                chunk_id=chunk["chunk_id"],
                chunk_text=chunk.get("chunk_text", chunk.get("text", "")),
                title=chunk.get("title", "N/A"),
                hierarchy_path=chunk.get("hierarchy_path", "N/A"),
                effective_date=chunk.get("effective_date", "N/A"),
                expiry_date=chunk.get("expiry_date", "N/A (currently active)"),
            )
            response = self._llm.invoke(messages)
            grade = single_parser.parse(response.content)
            grades.append(grade)

        return grades

    def _apply_confidence_threshold(
        self, grades: list[ChunkGrade]
    ) -> list[ChunkGrade]:
        """
        Safety net: downgrade low-confidence RELEVANT grades to AMBIGUOUS.

        Why this matters:
          The grading LLM might say "RELEVANT" for a chunk that superficially
          matches (same topic, same article family) but is actually about a
          different specific provision. The confidence score captures this
          uncertainty. If confidence < threshold, we treat it as AMBIGUOUS.

        Example:
          Question: "Box 1 rate 2024"
          Chunk: "Box 1 rate 2022 was 37.07%" (expired version)
          LLM grade: RELEVANT (it IS about Box 1 rates) with confidence 0.5
          After threshold: AMBIGUOUS (confidence 0.5 < 0.6 threshold)
          → The chunk will NOT be used for generation.
        """
        adjusted = []
        for grade in grades:
            if (
                grade.grade == GradingResult.RELEVANT
                and grade.confidence < self.config.confidence_threshold
            ):
                adjusted.append(ChunkGrade(
                    chunk_id=grade.chunk_id,
                    grade=GradingResult.AMBIGUOUS,
                    confidence=grade.confidence,
                    reasoning=(
                        f"Downgraded from RELEVANT to AMBIGUOUS: confidence "
                        f"{grade.confidence:.2f} < threshold {self.config.confidence_threshold}. "
                        f"Original reasoning: {grade.reasoning}"
                    ),
                ))
            else:
                adjusted.append(grade)
        return adjusted

    def _aggregate(self, grades: list[ChunkGrade]) -> ContextGradingResult:
        """
        Aggregate individual chunk grades into an overall context assessment.

        Decision logic:
          1. Count RELEVANT, AMBIGUOUS, IRRELEVANT grades.
          2. If RELEVANT >= min_relevant_chunks (default 3) → overall RELEVANT.
             Rationale: 3+ relevant chunks provide enough context for a grounded answer.
          3. Else if RELEVANT < min_relevant_chunks AND AMBIGUOUS is majority → overall AMBIGUOUS.
             Rationale: some topical relevance exists — a query rewrite might improve retrieval.
          4. Else → overall IRRELEVANT.
             Rationale: no meaningful context found — refuse rather than hallucinate.

        The relevant_chunk_ids list is used by the CRAG state machine to filter
        graded_chunks — only RELEVANT chunks are passed to the generation node.
        """
        relevant_count = sum(1 for g in grades if g.grade == GradingResult.RELEVANT)
        ambiguous_count = sum(1 for g in grades if g.grade == GradingResult.AMBIGUOUS)
        irrelevant_count = sum(1 for g in grades if g.grade == GradingResult.IRRELEVANT)

        relevant_ids = [g.chunk_id for g in grades if g.grade == GradingResult.RELEVANT]

        # Decision logic
        if relevant_count >= self.config.min_relevant_chunks:
            overall = GradingResult.RELEVANT
        elif ambiguous_count > irrelevant_count and relevant_count > 0:
            # Some relevance + mostly ambiguous = worth retrying with better query
            overall = GradingResult.AMBIGUOUS
        elif ambiguous_count > irrelevant_count:
            # All ambiguous, no relevant = still worth a retry
            overall = GradingResult.AMBIGUOUS
        else:
            overall = GradingResult.IRRELEVANT

        return ContextGradingResult(
            overall_grade=overall,
            chunk_grades=grades,
            relevant_count=relevant_count,
            ambiguous_count=ambiguous_count,
            irrelevant_count=irrelevant_count,
            relevant_chunk_ids=relevant_ids,
        )


# =============================================================================
# 5. WORKED EXAMPLES
# =============================================================================

"""
WORKED EXAMPLE 1 — Overall RELEVANT (proceed to generation)

Query: "Is thuiskantoorkosten aftrekbaar?"
8 retrieved chunks after reranking:

  Chunk 1: WET-IB-2001-v12::art3.17::par1::chunk000
    "Art 3.17 lid 1: Kosten die verband houden met een werkruimte in de eigen woning..."
    Grade: RELEVANT (confidence 0.92)
    → Directly addresses deductibility of home office with specific article.

  Chunk 2: WET-IB-2001-v12::art3.17::par1::suba::chunk000
    "Sub a: de werkruimte moet een zelfstandig gedeelte van de woning vormen..."
    Grade: RELEVANT (confidence 0.88)
    → Specifies the conditions under Art 3.17 lid 1.

  Chunk 3: POLICY-IH-2024-007::ch5::sec2::chunk000
    "Beleid thuiswerken: de werknemer die structureel meer dan 40% thuiswerkt..."
    Grade: RELEVANT (confidence 0.78)
    → Internal policy on home office deduction criteria.

  Chunk 4: WET-IB-2001-v12::art3.16::par1::chunk000
    "Art 3.16: Aftrekbare kosten in dienstbetrekking..."
    Grade: AMBIGUOUS (confidence 0.62)
    → Related article about deductible costs, but not specifically home office.

  Chunk 5: WET-IB-2001-v12::art3.18::par1::chunk000
    "Art 3.18: Reiskosten woon-werkverkeer..."
    Grade: AMBIGUOUS (confidence 0.45)
    → Different type of deduction (commuting), not home office.

  Chunk 6: ECLI-NL-HR-2019-456::consideration3::chunk000
    "De Hoge Raad oordeelt dat de werkruimte niet kwalificeert als..."
    Grade: RELEVANT (confidence 0.85)
    → Case law about home office qualification — directly relevant.

  Chunk 7: ELEARN-MOD-022::lesson5::chunk000
    "Module 5: Overzicht aftrekposten particulieren..."
    Grade: AMBIGUOUS (confidence 0.40)
    → General overview, not specific enough for citation.

  Chunk 8: AWR-2024-v3::art67::par1::chunk000
    "Artikel 67 AWR: Verzuimboete..."
    Grade: IRRELEVANT (confidence 0.93)
    → About penalty provisions, not deductions.

Aggregation:
  RELEVANT:   4 (chunks 1, 2, 3, 6)
  AMBIGUOUS:  3 (chunks 4, 5, 7)
  IRRELEVANT: 1 (chunk 8)
  → 4 >= min_relevant_chunks (3) → overall RELEVANT
  → graded_chunks = [chunk 1, 2, 3, 6] → passed to generate()


WORKED EXAMPLE 2 — Overall AMBIGUOUS (retry once)

Query: "digital services tax Netherlands"
8 retrieved chunks: 0 RELEVANT, 6 AMBIGUOUS, 2 IRRELEVANT
→ Majority AMBIGUOUS → overall AMBIGUOUS → rewrite_and_retry
→ Rewrite: "digitale dienstenbelasting Nederland EU-richtlijn"
→ After retry: 4 RELEVANT → proceed to generate


WORKED EXAMPLE 3 — Overall IRRELEVANT (refuse)

Query: "parking regulations Amsterdam city center"
8 retrieved chunks: 0 RELEVANT, 1 AMBIGUOUS, 7 IRRELEVANT
→ Majority IRRELEVANT → overall IRRELEVANT → refuse
→ "I could not find legal provisions that address your question..."


WORKED EXAMPLE 4 — Confidence threshold catches near-miss

Query: "Box 1 tarief 2024"
Chunk: "Art 2.10 Wet IB 2001 (geldig tot 31-12-2022): tarief 37,07%"
  LLM grade: RELEVANT (topic match) with confidence 0.52
  After threshold (0.6): DOWNGRADED to AMBIGUOUS
  Reasoning: "Expired provision. Confidence 0.52 < 0.6 threshold."
→ This chunk is NOT passed to generation.
→ Prevents the LLM from citing an expired tax rate as current law.
"""
