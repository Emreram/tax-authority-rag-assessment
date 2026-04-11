# CRAG State Machine — Module 3 Detail Diagram

> This diagram visualizes the Corrective RAG (CRAG) state machine implemented
> in [module3_crag_statemachine.py](../pseudocode/module3_crag_statemachine.py).
> Most candidates solving this assessment will produce a linear chain
> (`retriever | reranker | llm`). This is NOT that. This is a formal
> LangGraph `StateGraph` with 9 states, 2 conditional routers, bounded retry,
> and 5 anti-hallucination gates.
>
> **Why a state machine and not a chain:** A linear chain has no place to
> refuse. If retrieval returns irrelevant chunks, the LLM will generate a
> confident-sounding wrong answer. The state machine inserts a grading gate
> between retrieval and generation, and a citation-validation gate between
> generation and response. Either gate can route to REFUSE. This is the
> architectural embodiment of Assumption A14 (zero-hallucination tolerance)
> and Assumption A16 (prefer false negatives over false positives).

---

## 1. The 9 States

| # | State | Entry point | Exit transitions | Function (module3_crag_statemachine.py) |
|---|---|---|---|---|
| 1 | **RECEIVE_QUERY** | Entry point of the graph | → TRANSFORM_QUERY | `classify_query()` line 162 |
| 2 | **TRANSFORM_QUERY** | After classification | → RETRIEVE | `transform_query()` line 271 |
| 3 | **RETRIEVE** | After transform, or after rewrite_and_retry | → GRADE_CONTEXT | `retrieve()` line 359 |
| 4 | **GRADE_CONTEXT** | After every retrieve | → GENERATE / REWRITE_AND_RETRY / REFUSE (router) | `grade_context()` line 448 |
| 5 | **REWRITE_AND_RETRY** | From GRADE_CONTEXT if AMBIGUOUS and retry<1 | → RETRIEVE (loop back) | `rewrite_and_retry()` line 684 |
| 6 | **GENERATE** | From GRADE_CONTEXT if RELEVANT | → VALIDATE_OUTPUT | `generate()` line 514 |
| 7 | **VALIDATE_OUTPUT** | After every generate | → RESPOND / REFUSE (router) | `validate_output()` line 587 |
| 8 | **RESPOND** | From VALIDATE_OUTPUT if citations valid | → END | `respond()` line 721 |
| 9 | **REFUSE** | From GRADE_CONTEXT (IRRELEVANT or retries exhausted) OR from VALIDATE_OUTPUT (citations invalid) | → END | `refuse()` line 763 |

---

## 2. State Diagram

```
                        ┌──────────────────────────┐
                        │      RECEIVE_QUERY       │
                        │     classify_query()     │
                        │                          │
                        │  Detects query_type:     │
                        │    REFERENCE / SIMPLE /  │
                        │    COMPLEX               │
                        │  Decides should_use_hyde │
                        └────────────┬─────────────┘
                                     │
                                     ▼
                        ┌──────────────────────────┐
                        │     TRANSFORM_QUERY      │
                        │     transform_query()    │
                        │                          │
                        │  If COMPLEX → decompose  │
                        │  If SIMPLE + use_hyde →  │
                        │    generate HyDE passage │
                        │  Else → pass through     │
                        └────────────┬─────────────┘
                                     │
                                     ▼
     ┌──────────────────▶ ┌──────────────────────────┐
     │                    │        RETRIEVE          │
     │                    │       retrieve()         │
     │                    │                          │
     │                    │  REFERENCE →             │
     │                    │    exact_id_retrieve     │
     │                    │  SIMPLE →                │
     │                    │    hybrid_retrieve(40)   │
     │                    │  COMPLEX →               │
     │                    │    hybrid_retrieve per   │
     │                    │    sub-query, merge      │
     │                    │  Then: rerank top-8      │
     │                    └────────────┬─────────────┘
     │                                 │
     │                                 ▼
     │                    ┌──────────────────────────┐
     │                    │     GRADE_CONTEXT        │
     │                    │    grade_context()       │
     │                    │                          │
     │                    │  RetrievalGrader LLM     │
     │                    │  (batch over 8 chunks)   │
     │                    │  Each chunk gets:        │
     │                    │    RELEVANT /            │
     │                    │    AMBIGUOUS /           │
     │                    │    IRRELEVANT + conf     │
     │                    └────────────┬─────────────┘
     │                                 │
     │              route_after_grading(state)
     │                 │               │               │
     │           AMBIGUOUS          RELEVANT      IRRELEVANT
     │         and retry<1             │               │
     │                 │               │               │
     │                 ▼               │               │
     │    ┌──────────────────────┐     │               │
     │    │  REWRITE_AND_RETRY   │     │               │
     │    │  rewrite_and_retry() │     │               │
     │    │                      │     │               │
     │    │  LLM rewrites query  │     │               │
     │    │  retry_count += 1    │     │               │
     │    │  should_use_hyde =   │     │               │
     │    │    False (no HyDE    │     │               │
     │    │    on retry)         │     │               │
     │    └──────────┬───────────┘     │               │
     │               │                 │               │
     └───────────────┘                 │               │
                                       │               │
                                       ▼               │
                          ┌──────────────────────────┐ │
                          │       GENERATE           │ │
                          │      generate()          │ │
                          │                          │ │
                          │  LLM @ T=0.0             │ │
                          │  System prompt enforces  │ │
                          │  [Source: chunk_id |     │ │
                          │   hierarchy_path] on     │ │
                          │  EVERY factual claim     │ │
                          └────────────┬─────────────┘ │
                                       │               │
                                       ▼               │
                          ┌──────────────────────────┐ │
                          │    VALIDATE_OUTPUT       │ │
                          │   validate_output()      │ │
                          │                          │ │
                          │  Extract cited chunk_ids │ │
                          │  Set-membership check:   │ │
                          │  ∀ c ∈ cited:            │ │
                          │    c ∈ graded_chunks?    │ │
                          └────────────┬─────────────┘ │
                                       │               │
                          route_after_validation(state)│
                             │                    │    │
                          VALID              INVALID    │
                             │                    │    │
                             ▼                    │    │
                   ┌──────────────────┐           │    │
                   │     RESPOND      │           │    │
                   │    respond()     │           │    │
                   │                  │           │    │
                   │  Format answer + │           │    │
                   │  source list +   │           │    │
                   │  audit log       │           │    │
                   └────────┬─────────┘           │    │
                            │                     │    │
                            │                     ▼    ▼
                            │               ┌─────────────────┐
                            │               │     REFUSE      │
                            │               │    refuse()     │
                            │               │                 │
                            │               │  Polite Dutch + │
                            │               │  English refusal│
                            │               │  Log refusal    │
                            │               │  reason         │
                            │               └────────┬────────┘
                            │                        │
                            └────────────────────────┘
                                       │
                                       ▼
                                     [END]
```

---

## 3. Conditional Edge Rules (formal)

Two router functions live in module3_crag_statemachine.py. They are the only
places where the state machine branches non-linearly.

### `route_after_grading(state)` — line 840

```python
def route_after_grading(state: CRAGState) -> Literal["generate", "rewrite_and_retry", "refuse"]:
    grading = state["grading_result"]          # GradingResult enum
    retry_count = state["retry_count"]         # int

    if grading == GradingResult.RELEVANT:
        return "generate"

    if grading == GradingResult.AMBIGUOUS and retry_count < MAX_RETRIES:
        return "rewrite_and_retry"

    # IRRELEVANT, OR AMBIGUOUS with retries exhausted
    return "refuse"
```

**Aggregation rule for GradingResult** (from module3_grader.py):
- `RELEVANT` = ≥ 3 chunks graded RELEVANT (majority signal)
- `AMBIGUOUS` = majority of chunks graded AMBIGUOUS, OR < 3 RELEVANT but > 0
- `IRRELEVANT` = 0 chunks graded RELEVANT and majority IRRELEVANT

### `route_after_validation(state)` — line 865

```python
def route_after_validation(state: CRAGState) -> Literal["respond", "refuse"]:
    if state["citations_valid"]:
        return "respond"
    return "refuse"
```

`citations_valid` is set to `True` only if every extracted `[Source: chunk_id | ...]`
token references a chunk_id that exists in the graded context AND at least one
citation is present in the response. Both conditions must hold.

---

## 4. Three Trace Examples

### Trace 1 — Happy Path

Query: **"Wat is de arbeidskorting voor 2024?"**

```
RECEIVE_QUERY
  → query_type = SIMPLE, should_use_hyde = True

TRANSFORM_QUERY
  → HyDE generates: "Op grond van artikel 3.114 Wet IB 2001..."
  → transformed_query set to HyDE text

RETRIEVE
  → hybrid_retrieve() returns top-40
  → rerank_chunks() returns top-8
  → Article 3.114 lid 1 is rank #1

GRADE_CONTEXT
  → Grader: 6 RELEVANT, 2 AMBIGUOUS
  → aggregated → GradingResult.RELEVANT

[route_after_grading] → "generate"

GENERATE
  → LLM produces answer with inline citations
  → e.g., "De arbeidskorting bedraagt 5.532 euro
          [Source: WetIB2001-2024::art3.114::lid1::chunk001 | ...]"

VALIDATE_OUTPUT
  → Extracts 2 cited chunk_ids
  → Both exist in graded context
  → citations_valid = True

[route_after_validation] → "respond"

RESPOND → END
```

Latency: ~1250 ms end-to-end (within 1500 ms budget).

---

### Trace 2 — Ambiguous → Retry → Success

Query: **"Home office deduction?"**  (English, no legal terminology)

```
RECEIVE_QUERY
  → query_type = SIMPLE, should_use_hyde = True

TRANSFORM_QUERY
  → HyDE generates Dutch legal text about werkruimte

RETRIEVE (attempt 1, retry_count = 0)
  → Mixed results: some about werkruimte, some about
    huurwaardeforfait, some unrelated kostenaftrek

GRADE_CONTEXT
  → Grader: 2 RELEVANT, 5 AMBIGUOUS, 1 IRRELEVANT
  → aggregated → GradingResult.AMBIGUOUS

[route_after_grading] → "rewrite_and_retry" (retry<1)

REWRITE_AND_RETRY
  → LLM rewrites: "Aftrekbaarheid werkruimte eigen
     woning artikel 3.17 Wet IB 2001 zelfstandig gedeelte"
  → retry_count = 1
  → should_use_hyde = False (no double-HyDE)

RETRIEVE (attempt 2, retry_count = 1)
  → hybrid_retrieve() with rewritten query
  → Article 3.17 now dominates results

GRADE_CONTEXT
  → Grader: 5 RELEVANT, 2 AMBIGUOUS, 1 IRRELEVANT
  → aggregated → GradingResult.RELEVANT

[route_after_grading] → "generate"

GENERATE → VALIDATE_OUTPUT → RESPOND → END
```

Latency: ~1450 ms (near the budget ceiling — this is why MAX_RETRIES = 1).

---

### Trace 3 — Irrelevant → Refusal

Query: **"Who built the Eiffel Tower?"**  (out of scope)

```
RECEIVE_QUERY
  → query_type = SIMPLE

TRANSFORM_QUERY
  → HyDE generates plausible Dutch legal text about... nothing relevant

RETRIEVE
  → hybrid_retrieve() returns 40 tax-law chunks, none about the Eiffel Tower

GRADE_CONTEXT
  → Grader: 0 RELEVANT, 1 AMBIGUOUS, 7 IRRELEVANT
  → aggregated → GradingResult.IRRELEVANT

[route_after_grading] → "refuse"

REFUSE → END

Response: "I could not find relevant Dutch tax-law
information to answer your question. This system is
scoped to Dutch tax authority documents. Please
rephrase or consult a general information source."
```

Latency: ~600 ms (short — no generation, no retry).

---

## 5. Five Anti-Hallucination Gates

| # | Gate | Where | What it prevents |
|---|---|---|---|
| **G1** | RBAC pre-filter | OpenSearch DLS (before BM25/kNN scoring) | Retrieving documents above the user's tier |
| **G2** | Retrieval grader | `grade_context()` → `route_after_grading()` | Generating from irrelevant context |
| **G3** | Citation format constraint | Generator system prompt (T=0.0) | LLM inventing free-form citations |
| **G4** | Citation set-membership check | `validate_output()` → `route_after_validation()` | LLM fabricating chunk_ids that match the format but don't exist |
| **G5** | Bounded retry (MAX_RETRIES=1) | `route_after_grading()` retry counter | Infinite rewrite loops; budget blow-outs |

Any gate failure routes to `REFUSE`. The system is **fail-closed by construction**:
in every ambiguous or uncertain state, the default action is to refuse, not
to generate.

---

## 6. Why Not a Linear Chain

A common mistake in this assessment would be:

```python
# ANTI-PATTERN — what most candidates build
chain = retriever | reranker | llm
answer = chain.invoke(query)
```

This has zero gates. The LLM will generate an answer from whatever the
retriever returned, even if it was noise. The assessment's "zero-hallucination
tolerance" requirement CANNOT be satisfied by this pattern.

| Aspect | Linear chain | State machine (this design) |
|---|---|---|
| Grading gate between retrieval and generation | No | Yes (`grade_context` → `route_after_grading`) |
| Bounded retry on ambiguous retrieval | No | Yes (MAX_RETRIES=1) |
| Post-generation citation validation | No | Yes (`validate_output`) |
| Explicit REFUSE state | No | Yes |
| Failure mode | Confident hallucination | Polite refusal |
| Acceptable for tax authority | No | Yes |

---

## 7. Latency Annotations (happy path from Trace 1)

Summed against the 1500 ms TTFT budget in [architecture_overview.md §6](architecture_overview.md):

```
  Cache check (miss):                       15 ms
  RECEIVE_QUERY (regex classify):            2 ms
  TRANSFORM_QUERY (HyDE LLM):              180 ms  ← optional, not always paid
  RETRIEVE (embed + hybrid + rerank):      295 ms
  GRADE_CONTEXT (batch grader LLM):        150 ms
  GENERATE (first token):                  800 ms
  VALIDATE_OUTPUT (regex + set check):       3 ms
  RESPOND (format + audit log):              5 ms
                                           ───────
  Total happy-path TTFT:                  1450 ms  ✓ (within 1500 ms)
```

With a cache hit, total = ~15 ms. With an AMBIGUOUS retry, total ≈ 1450 + 580 = 2030 ms
(this is why the retry cap is 1 — 2 retries exceed budget).

---

## 8. Cross-File Anchors

- State names match `CRAGState` TypedDict in [module3_crag_statemachine.py](../pseudocode/module3_crag_statemachine.py) section 3
- Node functions: `classify_query` (l.162), `transform_query` (l.271), `retrieve` (l.359), `grade_context` (l.448), `generate` (l.514), `validate_output` (l.587), `rewrite_and_retry` (l.684), `respond` (l.721), `refuse` (l.763)
- Routers: `route_after_grading` (l.840), `route_after_validation` (l.865)
- `MAX_RETRIES = 1` at line 45; in-code justification at lines 46-54
- `GENERATION_TEMPERATURE = 0.0` at line 56; `QUERY_REWRITE_TEMPERATURE = 0.3` at line 62
- `TOP_K_RETRIEVAL = 40`, `TOP_K_RERANK = 8` at lines 68-69
- Grader implementation: [module3_grader.py](../pseudocode/module3_grader.py) — `RetrievalGrader`, `ChunkGrade`, `GradingResult`
- Generator system prompt: [prompts/generator_system_prompt.txt](../prompts/generator_system_prompt.txt)
- Grader system prompt: [prompts/grader_prompt.txt](../prompts/grader_prompt.txt)
