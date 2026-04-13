# Module 3 — Agentic RAG & Self-Healing (CRAG State Machine)

> **Assessment sub-questions answered in this module:**
> 1. How do we handle complex, multi-part tax questions using Query Decomposition or HyDE?
> 2. Design a state-machine (control loop) using a framework like LangGraph.
> 3. How do we implement a Retrieval Evaluator (Grader)?
> 4. Define the **exact fallback actions** if context is classified `Irrelevant`, `Ambiguous`, or `Relevant`.

---

## 3.1 Why a linear RAG chain fails the zero-hallucination requirement

The assessment specifies **zero tolerance for hallucination** (Assumption A14) and exact
citations for every factual claim (A12). The most common mistake in this kind of
assessment is to submit a linear chain:

```python
# ANTI-PATTERN — what a weak submission looks like
chain = retriever | reranker | llm
answer = chain.invoke(query)
```

This pipeline has no place to refuse. If the retriever returns irrelevant chunks
because the query was ambiguous, the embedding space mis-mapped the concept, or the
user asked an out-of-scope question, the LLM will **still generate a confident-sounding
wrong answer**. In a tax authority context a fabricated article number or a confused
tax year can cause an incorrect assessment, which is exactly the failure mode A14
forbids.

**Our alternative**: a formal LangGraph `StateGraph` with 9 states, 2 conditional
routers, a bounded retry, and 5 explicit anti-hallucination gates. The architecture
is **fail-closed by construction** — in any ambiguous or uncertain state, the
default action is to refuse, not to generate. The state machine is visualized in
[diagrams/crag_state_machine.md](../diagrams/crag_state_machine.md) and implemented
in [pseudocode/module3_crag_statemachine.py](../pseudocode/module3_crag_statemachine.py).

---

## 3.2 Query classification — the decision that drives everything

Before any transformation happens, the query is classified into one of three types
by [classify_query()](../pseudocode/module3_crag_statemachine.py#L162). The class
determines the retrieval path, the transformation strategy, and whether HyDE applies.

| Type | Detection | Example | Transformation | Retrieval path |
|---|---|---|---|---|
| `REFERENCE` | Regex: ECLI pattern (`ECLI:NL:HR:2023:1234`) or Article pattern (`artikel 3.114`) | "Wat zegt artikel 3.114 Wet IB 2001 over arbeidskorting?" | None (pass-through) | `exact_id_retrieve()` shortcut |
| `SIMPLE` | LLM classifier; no reference found; single-fact question | "Wat is de arbeidskorting voor 2024?" | Optional HyDE (if conceptual) | `hybrid_retrieve()` single pass |
| `COMPLEX` | LLM classifier; multi-part, requires >1 legal provision | "I'm a freelancer with a home office — what can I deduct and do I owe BTW?" | Decomposition into ≤3 sub-queries | `hybrid_retrieve()` per sub-query, merge + dedupe |

Regex is used for `REFERENCE` because it is deterministic and costs ~0 ms. The
`SIMPLE`/`COMPLEX` distinction uses a small LLM call because the boundary is fuzzy
(a single-sentence question can still require multiple legal provisions).

**The HyDE decision gate**: HyDE is applied **only** when all of the following hold:
1. The query is `SIMPLE` (not `REFERENCE`, not `COMPLEX`).
2. No legal references were detected.
3. The first 20 characters contain no digits (a heuristic for "conceptual, not numeric").

This gate matters because HyDE adds ~300–500 ms of latency. Applying it to every
query would blow the 1500 ms TTFT budget. Applying it to reference queries would
degrade retrieval (the query was already precise). The gate is enforced in
[classify_query() line 216-223](../pseudocode/module3_crag_statemachine.py#L216).

---

## 3.3 Query transformation — HyDE (Hypothetical Document Embeddings)

**Problem HyDE solves**: a user asks "Can I deduct my home office?" in casual English.
The relevant answer lives in a Dutch legal passage: "Op grond van artikel 3.17 Wet IB
2001 zijn kosten voor een werkruimte in de eigen woning aftrekbaar indien…". The
embedding of the casual English question is **far** from the embedding of the formal
Dutch legal text in vector space. A naive kNN search will miss it.

**How HyDE bridges the gap**: the transformation LLM generates a short hypothetical
passage (3–5 sentences) that **would** answer the question if it were itself a legal
source. The hypothetical text uses the right Dutch legal terminology (`aftrekbaar`,
`werkruimte`, `eigen woning`, `Wet IB 2001`). The embedding of this hypothetical text
lands much closer to the real legal passages in E5 vector space, so the kNN retriever
now hits the correct article.

Inline excerpt of the HyDE prompt from
[module3_crag_statemachine.py line 245](../pseudocode/module3_crag_statemachine.py#L245):

```python
HYDE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a senior Dutch tax law expert. Given a tax question, write a short "
        "paragraph (3-5 sentences) that would appear in an official legal text or "
        "policy document answering this question. Include specific article numbers, "
        "legal terms, and provisions if you can infer them. Write in Dutch if the "
        "question is in Dutch.\n\n"
        "IMPORTANT: This is a hypothetical answer used for retrieval — accuracy of "
        "specific numbers is less important than using the right legal terminology "
        "and structure."
    )),
    ("human", "{query}"),
])
```

**Crucial detail**: the hypothetical text replaces the query **only for embedding
and retrieval**. The reranker still scores against the original question (see
[retrieve() line 427](../pseudocode/module3_crag_statemachine.py#L427)), and the
grader grades against the original question
([grade_context() line 463](../pseudocode/module3_crag_statemachine.py#L463)). The
hypothetical content never leaks into the final answer.

**When HyDE is not used** (already covered in §3.2): reference queries, complex
queries, numeric queries, and — critically — **retry attempts**. After a query
rewrite, `should_use_hyde` is forced to `False`
([rewrite_and_retry() line 706](../pseudocode/module3_crag_statemachine.py#L706)).
Re-applying HyDE on top of a rewrite compounds the transformation drift and usually
makes retrieval worse, not better.

---

## 3.4 Query transformation — Decomposition (for COMPLEX queries)

Complex queries require information from multiple legal provisions. Example:

> "I'm a freelancer with a home office, I earned €65,000 this year — what can I
> deduct for the workspace, do I qualify for the self-employed deduction, and do I
> need to charge BTW?"

A single retrieval pass cannot surface all three answers because the top-8 context
gets crowded out by whichever topic the embedding space happens to prefer. The
decomposition strategy splits the question into independent sub-queries:

1. "Wat zijn de voorwaarden voor aftrek van een werkruimte in de eigen woning?"
2. "Wanneer heeft een ondernemer recht op de zelfstandigenaftrek?"
3. "Is een zzp'er met omzet onder €20.000 btw-plichtig? (KOR-regeling)"

Each sub-query runs through `hybrid_retrieve()` independently, results are merged,
and duplicates are removed by `chunk_id`
([retrieve() lines 397–415](../pseudocode/module3_crag_statemachine.py#L397)). The
merged set is then reranked against the **original** question to pick the 8 most
relevant chunks across all three topics.

Inline excerpt of the decomposition prompt
([module3_crag_statemachine.py line 257](../pseudocode/module3_crag_statemachine.py#L257)):

```python
DECOMPOSITION_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a legal research assistant. Break this complex tax question into "
        "independent sub-questions that can each be answered by a single legal "
        "passage.\n\n"
        "Rules:\n"
        "- Each sub-question should be self-contained\n"
        "- Maximum 3 sub-questions (more adds too much latency)\n"
        "- Each sub-question should target a specific legal provision or concept\n"
        "- Return ONLY the sub-questions, one per line, numbered 1-3\n"
    )),
    ("human", "{query}"),
])
```

**Why the hard cap at 3 sub-queries**: each sub-query costs one full
`hybrid_retrieve()` call (~80 ms parallel BM25+kNN). Three parallel sub-query
retrievals + a single rerank of the merged pool stays within the latency budget.
Four or more sub-queries push reranker cost up (more candidates to score) and risk
blowing TTFT.

**HyDE vs decomposition — when each applies**:

| Situation | Apply |
|---|---|
| Query is conceptual, in a non-legal vocabulary | HyDE |
| Query asks multiple independent things | Decomposition |
| Query contains exact legal references | Neither (pass-through) |
| Retry attempt after AMBIGUOUS grading | Neither — use query rewrite instead (§3.8) |

The two transformations are **never stacked**. You do not decompose into
sub-queries and then HyDE each sub-query; that would cost 3 × 500 ms of
transformation latency alone.

---

## 3.5 The 9-state machine — formal definition

The state machine is a LangGraph `StateGraph` with 9 nodes, 2 conditional routers,
and 2 terminal edges. The graph is **compiled** at startup
([build_crag_graph() line 884](../pseudocode/module3_crag_statemachine.py#L884)),
not interpreted at request time — this is a LangGraph property that matters for
latency and correctness.

| # | State | Function | Role |
|---|---|---|---|
| 1 | `RECEIVE_QUERY` | `classify_query()` | Regex + LLM classification → REFERENCE / SIMPLE / COMPLEX |
| 2 | `TRANSFORM_QUERY` | `transform_query()` | HyDE / decomposition / pass-through |
| 3 | `RETRIEVE` | `retrieve()` | Three-path hybrid retrieval + reranking → top-8 |
| 4 | `GRADE_CONTEXT` | `grade_context()` | RetrievalGrader LLM batch-scores all 8 chunks |
| 5 | `REWRITE_AND_RETRY` | `rewrite_and_retry()` | Query reformulation, bumps `retry_count` (max 1) |
| 6 | `GENERATE` | `generate()` | LLM @ T=0.0, system prompt forces inline citations |
| 7 | `VALIDATE_OUTPUT` | `validate_output()` | Set-membership check: cited chunk_ids must exist in graded context |
| 8 | `RESPOND` | `respond()` | Format answer + source list + audit log → return to user |
| 9 | `REFUSE` | `refuse()` | Polite refusal + partial leads + audit log |

Inline excerpt of the graph wiring
([build_crag_graph() line 927](../pseudocode/module3_crag_statemachine.py#L927)):

```python
graph = StateGraph(CRAGState)

graph.add_node("classify_query", classify_query)
graph.add_node("transform_query", transform_query)
graph.add_node("retrieve", retrieve)
graph.add_node("grade_context", grade_context)
graph.add_node("generate", generate)
graph.add_node("validate_output", validate_output)
graph.add_node("respond", respond)
graph.add_node("rewrite_and_retry", rewrite_and_retry)
graph.add_node("refuse", refuse)

graph.set_entry_point("classify_query")

# Unconditional edges (always follow this path)
graph.add_edge("classify_query", "transform_query")
graph.add_edge("transform_query", "retrieve")
graph.add_edge("retrieve", "grade_context")
graph.add_edge("generate", "validate_output")
graph.add_edge("rewrite_and_retry", "retrieve")   # retry loop

# Conditional edges (branching based on state)
graph.add_conditional_edges("grade_context", route_after_grading, {
    "generate": "generate",
    "rewrite_and_retry": "rewrite_and_retry",
    "refuse": "refuse",
})
graph.add_conditional_edges("validate_output", route_after_validation, {
    "respond": "respond",
    "refuse": "refuse",
})

graph.add_edge("respond", END)
graph.add_edge("refuse", END)

compiled = graph.compile()
```

The shared state object `CRAGState` is a `TypedDict` with 20 fields
([line 111](../pseudocode/module3_crag_statemachine.py#L111)) — every node reads
from and writes to it. LangGraph enforces immutability at the node boundary,
which makes the whole pipeline debuggable: any state snapshot can be replayed
through the graph for postmortem analysis.

The full visual state diagram, including all conditional edges and retry loop, is
in [diagrams/crag_state_machine.md §2](../diagrams/crag_state_machine.md).

---

## 3.6 The Retrieval Evaluator (Grader) — implementation

The grader is the gate between retrieval and generation. Without it, any retrieved
context — no matter how irrelevant — reaches the LLM, which will generate from
whatever is in front of it. The grader is implemented in
[pseudocode/module3_grader.py](../pseudocode/module3_grader.py) and called from
the `grade_context` node.

**Design properties:**

1. **Batched grading**. All 8 reranked chunks are graded in a **single** LLM call
   via structured output. Sequential grading would cost 8 × 150 ms = 1200 ms; batched
   costs ~150 ms total ([GraderConfig.use_batch_grading line 118](../pseudocode/module3_grader.py#L118)).

2. **Three grades**, matching the assessment wording exactly:
   - `RELEVANT` — directly addresses the question with a specific provision, ruling,
     policy rule, or numeric value
   - `AMBIGUOUS` — topically related but not directly applicable (wrong paragraph,
     related concept, superseded version, different tax type)
   - `IRRELEVANT` — no meaningful connection

3. **Confidence threshold**. Even a chunk graded `RELEVANT` with confidence < 0.6
   is **downgraded to AMBIGUOUS** by `GraderConfig.confidence_threshold`
   ([line 109](../pseudocode/module3_grader.py#L109)). This catches the failure mode
   where the grading LLM is uncertain but still labels `RELEVANT` under social
   pressure from the system prompt.

4. **Temporal awareness**. The grader is explicitly instructed that a passage from
   a **repealed or superseded** article should be graded `AMBIGUOUS`, not
   `RELEVANT`, unless the user asked about historical law
   ([GRADER_SYSTEM_PROMPT lines 148–150](../pseudocode/module3_grader.py#L148)).
   This closes the failure mode where a 2022 Box 1 rate is served in answer to a
   2024 question.

5. **Aggregation rule**. Individual chunk grades are aggregated into one overall
   grade using these rules
   (see [ContextGradingResult](../pseudocode/module3_grader.py#L73) aggregation):

   | Overall grade | Rule |
   |---|---|
   | `RELEVANT` | ≥ 3 chunks graded `RELEVANT` AND confidence ≥ 0.6 |
   | `AMBIGUOUS` | < 3 `RELEVANT` but ≥ 1 `RELEVANT` or `AMBIGUOUS` |
   | `IRRELEVANT` | 0 `RELEVANT` AND majority `IRRELEVANT` |

   The `min_relevant_chunks = 3` threshold
   ([GraderConfig line 100](../pseudocode/module3_grader.py#L100)) encodes the rule
   of thumb that a legal answer typically needs at least two or three corroborating
   provisions to be defensible. Lower values (2) give higher recall but more noise;
   higher values (4–5) give maximum safety at the cost of more refusals.

Inline excerpt of the grader system prompt (first 20 lines,
full at [module3_grader.py line 136](../pseudocode/module3_grader.py#L136)):

```
You are a legal retrieval quality assessor for the Dutch National Tax Authority.
Your job is to evaluate whether retrieved document passages contain information
that directly helps answer a tax-related question.

Grade each passage as:

RELEVANT — The passage directly addresses the question with at least one of:
  - A specific legal provision (article, paragraph) that applies to the question
  - A court ruling or consideration that directly bears on the legal issue
  - An explicit policy rule or procedure that answers the operational question
  - Concrete numerical values (rates, thresholds, amounts) the question asks about
  Note: The provision must be CURRENTLY EFFECTIVE. A passage about a repealed or
  superseded article should be graded AMBIGUOUS, not RELEVANT, unless the user
  explicitly asked about historical law.

AMBIGUOUS — The passage is topically related but lacks direct applicability...
IRRELEVANT — The passage has no meaningful connection to the question...
```

The full prompt, including three few-shot examples, is available as a standalone
reference at [prompts/grader_prompt.txt](../prompts/grader_prompt.txt).

---

## 3.7 Fallback actions — the exact decision table

This section answers the assessment's most precise sub-question:
**"Define the exact fallback actions if context is classified `Irrelevant`,
`Ambiguous`, or `Relevant`."** The answer is encoded in the
[route_after_grading() router at line 840](../pseudocode/module3_crag_statemachine.py#L840):

| Grading result | Retry count | Action | Next state | Rationale |
|---|---|---|---|---|
| `RELEVANT` | any | Proceed to LLM generation with the graded context | `GENERATE` | ≥ 3 chunks directly address the question; generation is safe |
| `AMBIGUOUS` | `retry_count < 1` | Rewrite the query with more specific Dutch legal terminology and retry retrieval | `REWRITE_AND_RETRY` → `RETRIEVE` | The topic is right but the phrasing missed the specific provision; one rewrite is worth trying |
| `AMBIGUOUS` | `retry_count ≥ 1` | Refuse with partial leads (titles of topically related documents) | `REFUSE` | Budget exhausted; further retries would blow the 1500 ms TTFT cap (see §3.11) |
| `IRRELEVANT` | any | Refuse immediately with out-of-scope message | `REFUSE` | No retry will help — the corpus does not contain the answer |

Inline excerpt of the router
([route_after_grading() line 840](../pseudocode/module3_crag_statemachine.py#L840)):

```python
def route_after_grading(state: CRAGState) -> Literal["generate", "rewrite_and_retry", "refuse"]:
    grading = state.get("grading_result", "")
    retries = state.get("retry_count", 0)

    if grading == GradingResult.RELEVANT.value:
        return "generate"
    elif grading == GradingResult.AMBIGUOUS.value and retries < MAX_RETRIES:
        return "rewrite_and_retry"
    else:
        # IRRELEVANT, or AMBIGUOUS with retries exhausted
        return "refuse"
```

The second router,
[route_after_validation() at line 865](../pseudocode/module3_crag_statemachine.py#L865),
governs the post-generation gate:

| Validation result | Action | Next state |
|---|---|---|
| `citations_valid == True` | Return the generated answer with verified citations | `RESPOND` |
| `citations_valid == False` | Refuse — fabricated citations are worse than no answer | `REFUSE` |

Any uncertainty on either router defaults to `REFUSE`. This is the
**fail-closed design principle** in action: ambiguity is resolved against generation,
not in favor of it.

---

## 3.8 Query rewrite — how the retry actually works

When the grader returns `AMBIGUOUS` and `retry_count < 1`, the state machine
transitions to
[rewrite_and_retry() at line 684](../pseudocode/module3_crag_statemachine.py#L684).
This node does three things:

1. Calls a small LLM with `REWRITE_PROMPT` at temperature 0.3 (some creativity is
   wanted; deterministic rewrites are too rigid).
2. Forces `should_use_hyde = False` — HyDE on a retry compounds transformation
   drift.
3. Increments `retry_count` by 1.

Inline excerpt of the rewrite prompt
([line 668](../pseudocode/module3_crag_statemachine.py#L668)):

```python
REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a legal search specialist. The user's tax question did not "
        "retrieve sufficiently relevant documents. Rephrase the question using "
        "more specific Dutch legal terminology, statute names, or article "
        "references that might improve retrieval.\n\n"
        "Rules:\n"
        "- Keep the semantic meaning identical\n"
        "- Add specific legal terms if you can infer them\n"
        "- If the question is in English, also provide the Dutch legal equivalent\n"
        "- Return ONLY the rewritten question, nothing else\n"
    )),
    ("human", "Original question: {query}\n\nPrevious retrieval returned ambiguous results. Rewrite:"),
])
```

**Example rewrite:**

| Original | Rewritten |
|---|---|
| "Can I deduct my home office expenses?" | "Aftrekbaarheid werkruimte eigen woning artikel 3.17 Wet IB 2001 zelfstandig gedeelte" |
| "What's the Box 1 rate this year?" | "Tarief Box 1 inkomstenbelasting 2024 schijf tabel" |

The rewritten query loops back to `RETRIEVE` via an unconditional edge
([build_crag_graph line 948](../pseudocode/module3_crag_statemachine.py#L948)).
After the second retrieval, grading happens again. If the grader still says
`AMBIGUOUS` — now with `retry_count == 1` — the router falls through to `REFUSE`.

---

## 3.9 Generation with mandatory citations

Once the grader says `RELEVANT`, the state machine routes to
[generate() at line 514](../pseudocode/module3_crag_statemachine.py#L514). Two
decisions here are deliberate and non-obvious:

**Temperature = 0.0**. Defined as
[GENERATION_TEMPERATURE at line 56](../pseudocode/module3_crag_statemachine.py#L56).
Zero temperature is the only safe choice for a tax authority setting — we want
deterministic output for the same graded context. Non-zero temperature would give
slightly different answers to the same user on repeated queries, which an auditor
would rightly flag. The tradeoff (zero creativity) is acceptable because we do not
want the model to be creative about tax law.

**Forced citation format in the system prompt**.
[GENERATION_SYSTEM_PROMPT at line 492](../pseudocode/module3_crag_statemachine.py#L492)
requires:

```
1. ONLY use information from the provided context. Do not use prior knowledge.
2. For EVERY factual claim, include an inline citation in this exact format:
   [Source: {chunk_id} | {hierarchy_path}]
3. If the context does not contain enough information to fully answer the question,
   explicitly state what you CAN answer and what you CANNOT.
4. Never guess, infer, or extrapolate beyond what the passages state.
5. Use precise legal language appropriate for tax authority professionals.
6. When citing amounts, percentages, or dates, always include the source.
7. If multiple provisions apply, present them in logical order with clear structure.
```

Rule 2 is the critical one. It forces the LLM to emit citations in a **machine-parseable
format** that downstream validation can verify. The exact pattern
`[Source: chunk_id | hierarchy_path]` is chosen because:
- `chunk_id` is deterministic and unique (see Module 1 §1.3)
- `hierarchy_path` is human-readable (`Wet IB 2001 > Hoofdstuk 3 > Artikel 3.114 > Lid 1`)
- The `|` delimiter is rare in legal text, so extraction regex is robust

The context block passed to the LLM
([generate() lines 531–544](../pseudocode/module3_crag_statemachine.py#L531))
includes `chunk_id`, `hierarchy_path`, `title`, `article_num`, `paragraph_num`, and
`effective_date` for each graded chunk. The LLM has the raw material to construct
proper citations; it does not need to guess.

The generation system prompt as a standalone reference is at
[prompts/generator_system_prompt.txt](../prompts/generator_system_prompt.txt).

---

## 3.10 Post-generation citation validation — catching the remaining hallucinations

Even with a forced citation format and temperature 0, LLMs **still** occasionally
fabricate citations. GPT-4 and Claude will both invent plausible-looking `chunk_id`
strings (e.g. `WetIB2001-2024::art3.115::lid2::chunk001`) that match the format but
don't exist in the graded context. This is the single most dangerous failure mode:
the response *looks* sourced but the citation is fiction.

[validate_output() at line 587](../pseudocode/module3_crag_statemachine.py#L587)
defends against this with a **set-membership check**. The logic is:

```python
valid_chunk_ids = {chunk["chunk_id"] for chunk in graded_chunks}

# Check 1: Response must contain at least one citation
if not citations:
    citations_valid = False  # Route to REFUSE

# Check 2: Every cited chunk_id must exist in the graded context
for citation in citations:
    if citation["chunk_id"] not in valid_chunk_ids:
        citations_valid = False  # Route to REFUSE
```

Both conditions must hold. An uncited answer is rejected (rule 2 of the system
prompt was ignored). A cited answer with even **one** fabricated `chunk_id` is
rejected entirely — we don't partially trust the response. Failure routes to
`REFUSE` via [route_after_validation() at line 865](../pseudocode/module3_crag_statemachine.py#L865).

This is a cheap check (~3 ms: regex extract + Python set lookup) and it catches
the highest-impact failure mode in the pipeline.

---

## 3.11 The 5 anti-hallucination gates (summary)

Pulling together everything above, the system has **five** explicit gates, any one
of which can block generation or route to refusal:

| # | Gate | Location | What it prevents |
|---|---|---|---|
| **G1** | RBAC pre-filter (DLS) | OpenSearch, before BM25/kNN scoring | Retrieving documents above the user's security tier (Module 4) |
| **G2** | Retrieval grader | `grade_context()` → `route_after_grading()` | Generating from irrelevant or off-topic context |
| **G3** | Forced citation format | Generator system prompt, T=0.0 | LLM inventing free-form or unparseable citations |
| **G4** | Citation set-membership check | `validate_output()` → `route_after_validation()` | LLM fabricating `chunk_id` strings that match the format but don't exist |
| **G5** | Bounded retry (`MAX_RETRIES = 1`) | `route_after_grading()` retry counter | Infinite rewrite loops; TTFT budget blow-outs |

Any gate failure routes to `REFUSE`. This is the architectural embodiment of
assumptions A14 (zero-hallucination tolerance) and A16 (prefer false negatives over
false positives). The system **can** say "I don't know" — and it will, whenever any
gate fails. A fabricated legal citation could drive an incorrect tax assessment;
a refusal cannot.

---

## 3.12 Why `MAX_RETRIES = 1` — the TTFT math

The retry cap is set to **1** at
[module3_crag_statemachine.py line 45](../pseudocode/module3_crag_statemachine.py#L45),
with the in-code justification spanning lines 46–54. The math:

| Stage (happy path, no retry) | Budget |
|---|---:|
| Cache check | 15 ms |
| Query embedding | 30 ms |
| Hybrid retrieval (BM25 ∥ kNN) | 80 ms |
| Cross-encoder rerank (40 pairs) | 200 ms |
| Retrieval grading (batch LLM) | 150 ms |
| LLM first token (generate) | 800 ms |
| Buffer (network / jitter) | 225 ms |
| **Total happy-path TTFT** | **1500 ms** |

Each retry adds a full sub-pipeline:

| Stage (one retry) | Cost |
|---|---:|
| Query rewrite LLM call | 150 ms |
| Second retrieval (BM25 ∥ kNN) | 80 ms |
| Second rerank | 200 ms |
| Second grading | 150 ms |
| **Added cost per retry** | **580 ms** |

With `MAX_RETRIES = 1`: worst case ≈ 1500 + 580 = 2080 ms. The buffer absorbs some
of this, and the retry usually happens on a miss-then-hit path where the first
retrieval + rerank + grading took less than the budget. In practice worst-case
single-retry TTFT sits around 1900–2050 ms — over budget but tolerable as a p99
tail event.

With `MAX_RETRIES = 2`: worst case ≈ 1500 + 2 × 580 = 2660 ms. This is >75% over
budget and would regularly page on-call. Not acceptable.

**Alternative considered**: allow more retries but shortcut them (skip rerank on
retries). Rejected because skipping rerank on a retry is exactly the path where
rerank is most valuable — the first retrieval was ambiguous, so the merged candidate
set needs precision filtering. You cannot cut the most important component out of
the recovery path.

The system therefore holds the line at exactly one retry, and refuses if the retry
also fails. This is the point where **the latency budget forces a safety decision**.

---

## 3.13 Three worked traces through the state machine

Three traces from [diagrams/crag_state_machine.md §4](../diagrams/crag_state_machine.md),
quoted here to illustrate the three outcomes the state machine can produce.

**Trace 1 — Happy path** (matches the arbeidskorting example in Module 2):

```
Query: "Wat is de arbeidskorting voor 2024?"

RECEIVE_QUERY     → query_type=SIMPLE, should_use_hyde=True
TRANSFORM_QUERY   → HyDE generates Dutch legal passage about arbeidskorting
RETRIEVE          → hybrid_retrieve returns 40, rerank → top-8
                    Article 3.114 lid 1 is rank #1
GRADE_CONTEXT     → grader: 6 RELEVANT, 2 AMBIGUOUS → overall RELEVANT
route_after_grading → "generate"
GENERATE          → LLM produces answer with inline citations
                    "De arbeidskorting bedraagt [...] [Source: WetIB2001-2024::art3.114::lid1::chunk001 | ...]"
VALIDATE_OUTPUT   → 2 citations extracted, both exist in graded context → valid=True
route_after_validation → "respond"
RESPOND           → format + audit log → END

Latency: ~1250 ms (within 1500 ms budget)
```

**Trace 2 — Ambiguous → retry → success**:

```
Query: "Home office deduction?"   (English, no legal terminology)

RECEIVE_QUERY     → SIMPLE, should_use_hyde=True
TRANSFORM_QUERY   → HyDE generates Dutch passage about werkruimte
RETRIEVE          (attempt 1, retry_count=0) → mixed results
GRADE_CONTEXT     → grader: 2 RELEVANT, 5 AMBIGUOUS, 1 IRRELEVANT → overall AMBIGUOUS
route_after_grading → "rewrite_and_retry" (retry_count < 1)

REWRITE_AND_RETRY → LLM rewrites to:
                    "Aftrekbaarheid werkruimte eigen woning artikel 3.17 Wet IB 2001"
                    retry_count = 1, should_use_hyde = False

RETRIEVE          (attempt 2, retry_count=1) → article 3.17 now dominates
GRADE_CONTEXT     → grader: 5 RELEVANT, 2 AMBIGUOUS, 1 IRRELEVANT → overall RELEVANT
route_after_grading → "generate"
GENERATE → VALIDATE_OUTPUT → RESPOND → END

Latency: ~1450 ms (near the 1500 ms budget ceiling)
```

**Trace 3 — Irrelevant → refusal**:

```
Query: "Who built the Eiffel Tower?"   (out of scope)

RECEIVE_QUERY     → SIMPLE
TRANSFORM_QUERY   → HyDE generates Dutch legal text about... nothing relevant
RETRIEVE          → 40 tax-law chunks, none about the Eiffel Tower
GRADE_CONTEXT     → grader: 0 RELEVANT, 1 AMBIGUOUS, 7 IRRELEVANT → overall IRRELEVANT
route_after_grading → "refuse"

REFUSE → "I could not find relevant Dutch tax-law information to answer your
          question. This system is scoped to Dutch tax authority documents.
          Please rephrase or consult a general information source." → END

Latency: ~600 ms (no generation, no retry)
```

The refusal path is intentionally fast — fail-closed systems should fail quickly
so users can reformulate or escalate. Long refusals make the UX worse than
accurate errors.

---

## 3.14 Supporting artifacts

| Artifact | Purpose |
|---|---|
| [pseudocode/module3_crag_statemachine.py](../pseudocode/module3_crag_statemachine.py) | Full LangGraph state machine: 9 nodes, 2 routers, graph wiring, `invoke_crag()` entry point |
| [pseudocode/module3_grader.py](../pseudocode/module3_grader.py) | `RetrievalGrader`, `ChunkGrade`, `GradingResult`, `ContextGradingResult`, aggregation rules |
| [prompts/grader_prompt.txt](../prompts/grader_prompt.txt) | Standalone grader system prompt + few-shot examples |
| [prompts/generator_system_prompt.txt](../prompts/generator_system_prompt.txt) | Standalone generator system prompt with citation format rules |
| [prompts/hyde_prompt.txt](../prompts/hyde_prompt.txt) | Standalone HyDE prompt with usage notes |
| [prompts/decomposition_prompt.txt](../prompts/decomposition_prompt.txt) | Standalone decomposition + rewrite prompts |
| [diagrams/crag_state_machine.md](../diagrams/crag_state_machine.md) | Visual state diagram with all 9 states, conditional edges, and three worked traces |
| [reference/assumptions.md](../reference/assumptions.md) | A12 (exact citations), A13 (TTFT<1500ms), A14 (zero hallucination), A16 (prefer refusal) |

**Ends Module 3.** Module 4 takes the state machine and the retrieval service and
wraps them in production concerns: the semantic cache that sits *before* the state
machine, the RBAC model that is enforced *inside* OpenSearch, and the evaluation
pipeline that gates every deploy.
