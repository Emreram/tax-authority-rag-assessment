# Module 4 — Production Ops, Security & Evaluation

> **Assessment sub-questions answered in this module:**
> 1. Design a Semantic Cache. **What cosine similarity threshold is safe for financial/tax data?**
> 2. Exactly how do you implement RBAC?
> 3. **At what stage of the pipeline must filtering occur to prevent data leaks MATHEMATICALLY?**
> 4. How do you automatically evaluate the system before deploying a new embedding model or LLM?
> 5. Exact metrics (via DeepEval or Ragas) for **Faithfulness and Context Precision**.

---

## 4.1 The semantic cache — purpose and placement

The semantic cache sits **before** the CRAG state machine, not inside it. A cache
hit skips every downstream stage — no retrieval, no rerank, no grading, no
generation. This is where the "TTFT ≈ 15 ms for repeat queries" claim originates.

```
User query + JWT
      │
      ▼
 ┌──────────────────────┐
 │ API Gateway (JWT)    │
 └──────────┬───────────┘
            │
            ▼
 ┌──────────────────────┐  HIT → return cached response (TTFT ≈ 15ms)
 │ SemanticCache.check  │────────────────────────────────┐
 │ (RediSearch KNN)     │                                 │
 └──────────┬───────────┘                                 │
            │ MISS                                        │
            ▼                                             │
 ┌──────────────────────┐                                 │
 │ CRAG State Machine   │                                 │
 │ (Module 3)           │                                 │
 └──────────┬───────────┘                                 │
            │                                             │
            ▼                                             │
 ┌──────────────────────┐                                 │
 │ SemanticCache.store  │                                 │
 │ (on success only)    │                                 │
 └──────────┬───────────┘                                 │
            │                                             │
            ▼                                             ▼
        Response                                      Response
```

The wrapper is [handle_query() at module4_cache.py line 562](../pseudocode/module4_cache.py#L562).
Cache check and cache store are both tagged by the user's security tier — we never
serve a higher-tier cached entry to a lower-tier user (§4.5).

**Why cache is not a LangGraph node**: a LangGraph node in the state machine would
still pay graph-traversal and state-serialization overhead on a miss. Keeping the
cache outside the graph lets cache hits return in ~15 ms without touching the
CRAG machinery at all. The CRAG pipeline is logic; caching is infrastructure.

---

## 4.2 The 0.97 cosine similarity threshold — the exact safe value for fiscal data

**The answer to the assessment's sub-question: cosine similarity ≥ 0.97.** Any
lower threshold is unsafe in a financial/tax domain. The justification is
concrete and demonstrable with a single example.

**The year-confusion failure mode.** Consider these two queries:

| Query A | Query B |
|---|---|
| "Wat is het Box 1 tarief voor 2024?" | "Wat is het Box 1 tarief voor 2023?" |

Same structure, one word different. Embed both with `multilingual-e5-large` and
measure their cosine similarity: **≈ 0.94**. The year token is one of ~1024
dimensions in the E5 space, so a one-word difference moves the vectors by a small
angular amount but not by much.

Now consider the industry default threshold of 0.90 used by generic semantic
caching libraries:

- Threshold 0.90: query B hits a cache entry created by query A → serves the
  **2023** rate as the answer to a **2024** question. This is a critical fiscal
  error. An incorrect tax rate in an official tax authority response could drive
  an incorrect assessment, an incorrect refund, or incorrect penalty calculation.
- Threshold 0.95: query B still hits the cache. Unsafe.
- **Threshold 0.97**: query B **misses** the cache (0.94 < 0.97), triggers a fresh
  CRAG pipeline, and returns the correct 2024 rate. Safe.

The threshold is defined at
[module4_cache.py line 49](../pseudocode/module4_cache.py#L49), with the
year-confusion example inline in the docstring. The tradeoff is explicit: **we
accept a lower cache hit rate (more cache misses) in exchange for guaranteed
accuracy.** In a fiscal domain this is the correct framing — near-miss hits are
catastrophic, not merely suboptimal.

This choice aligns with Assumption A16 (prefer false negatives over false
positives) and Assumption A14 (zero-hallucination tolerance) from
[notes/assumptions.md](../notes/assumptions.md).

**Why we do not use a score-dependent threshold**: a dynamic threshold (e.g., "0.97
for rate queries, 0.90 for procedural queries") would require pre-classifying
every query before caching. That pre-classification is itself an LLM call and would
cost more latency than it saves. The uniform 0.97 threshold is a simpler and
safer default.

---

## 4.3 TTL strategy — different lifetimes for different query classes

A single TTL for all cache entries is wrong because different query classes have
different staleness profiles. The cache uses three tiers
([determine_ttl() at module4_cache.py line 211](../pseudocode/module4_cache.py#L211)):

| Query type | TTL | Rationale |
|---|---|---|
| **Case law** (ECLI pattern, `jurisprudentie`, `uitspraak`, `arrest`) | **0 seconds (no cache)** | Higher courts can overturn rulings. Caching "what does ECLI:NL:HR:2023:1234 say" is dangerous if the Hoge Raad later clarifies or reverses it. Always retrieve fresh. |
| **Procedural** (`procedure`, `aanvraag`, `formulier`, `hoe kan ik`) | **7 days** | Filing procedures, form requirements, and submission workflows change infrequently. Helpdesk users ask these repeatedly — high cache value, low staleness risk. |
| **Everything else** (default) | **24 hours** | Tax rates, thresholds, legal provisions. Changes are typically annual (tax plan) but occasionally mid-year (emergency legislation). 24h catches most updates within one news cycle. |

TTL is a **maximum** lifetime. Document-aware invalidation (§4.4) can evict
entries earlier if their source documents are re-indexed. TTL and invalidation
are complementary: TTL covers "silent" staleness (something in the law changed
but we didn't re-index yet), invalidation covers "known" staleness (we just
re-indexed a document).

---

## 4.4 Cache invalidation on document re-index

When a legal document is re-indexed — because the legislation was amended, a new
paragraph was added, or a correction was published — every cache entry that used
chunks from that document becomes stale. The cache must evict them before the next
query hits.

[SemanticCache.invalidate_by_doc_ids() at module4_cache.py line 478](../pseudocode/module4_cache.py#L478)
handles this. Each cache entry stores the `doc_ids` of the chunks that contributed
to its response as a RediSearch `TAG` field. On re-index, the ingestion pipeline
calls:

```python
semantic_cache.invalidate_by_doc_ids(["AWR-2024-v3", "WetIB2001-2024-amendment-5"])
```

RediSearch runs a TAG query for each doc_id and deletes matching cache entries.
The callback is wired into the offline ingestion pipeline so the cache and the
index stay consistent: no query can ever be served from cached context built on
now-outdated chunks. The integration point is described in
[diagrams/architecture_overview.md §4](../diagrams/architecture_overview.md) as
"Cache Invalidation Callback".

There is also an emergency `invalidate_by_tier()` method for the case where a
document was temporarily misclassified — e.g., a RESTRICTED document was briefly
accessible at INTERNAL — which allows wiping all cache entries for the affected
tier in one call.

---

## 4.5 Cache tier partitioning — preventing cross-tier contamination

The cache is partitioned by security tier using a RediSearch `TAG` field. Key
format (from [rbac_roles.json cache_partitioning](../schemas/rbac_roles.json)):

```
cache:{security_tier}:{hash(query_embedding)}
```

Each cache entry is tagged with the `security_tier` of the user who triggered
its creation. Lookup rule:

> A user at tier T can read cache entries with tier ∈ {tiers ≤ T in the hierarchy}.

The hierarchy is `PUBLIC < INTERNAL < RESTRICTED < CLASSIFIED_FIOD`
([TIER_HIERARCHY at module4_cache.py line 101](../pseudocode/module4_cache.py#L101)).
Accessible tiers are resolved by
[get_accessible_tiers() at line 120](../pseudocode/module4_cache.py#L120):

| User tier | Accessible cache tiers |
|---|---|
| `PUBLIC` | `PUBLIC` |
| `INTERNAL` (helpdesk) | `PUBLIC`, `INTERNAL` |
| `RESTRICTED` (inspector, legal counsel) | `PUBLIC`, `INTERNAL`, `RESTRICTED` |
| `CLASSIFIED_FIOD` (FIOD investigator) | all four tiers |

**The tag filter runs BEFORE the KNN similarity search** in the RediSearch query
([check_cache() at module4_cache.py line 357](../pseudocode/module4_cache.py#L357)):

```python
query = (
    Query(f"(@security_tier:{{{tier_filter}}})=>[KNN 1 @embedding $vec AS score]")
    .sort_by("score")
    ...
)
```

The `(@security_tier:{...})=>` syntax applies the TAG filter first; the KNN search
operates only over the filtered subset. A helpdesk user can never receive a
CLASSIFIED_FIOD cache entry **even if the cosine similarity is 0.99**, because
the FIOD entry is never a candidate in the similarity search.

**The failure this prevents — the cross-tier poisoning attack.** Without
partitioning: a FIOD investigator asks "transfer pricing investigation
procedures", gets an answer sourced from classified documents, the answer is
cached with no tier label. A helpdesk user later asks a semantically similar
query, the cache returns the FIOD-generated response. The helpdesk user just
received classified content through a cache side-channel, bypassing every
DLS protection in OpenSearch. Tier partitioning closes this hole by
construction.

---

## 4.6 RBAC — the 4-tier / 6-role model

The RBAC model has **4 security tiers** assigned to documents and **6 OpenSearch
roles** assigned to users. The full configuration is in
[schemas/rbac_roles.json](../schemas/rbac_roles.json); the role-tier matrix is:

| Role | `PUBLIC` | `INTERNAL` | `RESTRICTED` | `CLASSIFIED_FIOD` |
|---|---|---|---|---|
| `role_public_user` | ✓ | — | — | — |
| `role_helpdesk` | ✓ | ✓ | — | — |
| `role_tax_inspector` | ✓ | ✓ | ✓ | — |
| `role_legal_counsel` | ✓ | ✓ | ✓ | — |
| `role_fiod_investigator` | ✓ | ✓ | ✓ | ✓ |
| `role_ingestion_service` | write-only (no search access) | | | |

This matches the assessment's specific FIOD example: "A helpdesk employee must
not be able to retrieve answers based on classified FIOD documents." The
`role_helpdesk` DLS query from
[rbac_roles.json line 72](../schemas/rbac_roles.json) implements this directly:

```json
{
  "bool": {
    "must_not": [
      {"terms": {"security_classification": ["RESTRICTED", "CLASSIFIED_FIOD"]}}
    ]
  }
}
```

This DLS query is attached to the role inside OpenSearch's Security Plugin. Every
search that the helpdesk user's JWT-impersonated session runs has this filter
applied **by the search engine**, not by application code. The documents never
appear in any result set the application sees — which is what makes the
enforcement mathematical rather than procedural (§4.7).

**Identity flow** (end-to-end):

```
  User logs in via organizational AD/ADFS (OIDC)
                  │
                  ▼
          IdP issues JWT with idp_groups claim
                  │  (e.g., idp_groups=["TAX_HELPDESK"])
                  ▼
   FastAPI gateway validates JWT, extracts idp_groups
                  │
                  ▼
   Role mapping: idp_group → OpenSearch role
                  │  (TAX_HELPDESK → role_helpdesk)
                  ▼
   Gateway injects impersonation header on OpenSearch calls
                  │
                  ▼
   OpenSearch Security Plugin resolves role_helpdesk → DLS query
                  │
                  ▼
   DLS applied BEFORE BM25 + kNN scoring
                  │  (S_user = S_total \ {RESTRICTED, CLASSIFIED_FIOD})
                  ▼
   Search executes on restricted index view only
```

The AD-group-to-OpenSearch-role mapping is in
[rbac_roles.json role_mapping](../schemas/rbac_roles.json). It uses the
organization's existing IdP (Assumption A4) — no new authentication system.

---

## 4.7 Pre-retrieval vs post-retrieval filtering — the mathematical proof

This section answers the assessment's most specific question:
**"At what stage of the pipeline must filtering occur to prevent data leaks
MATHEMATICALLY?"** The answer is **pre-retrieval, inside the search engine,
before any scoring occurs**. Post-retrieval filtering is mathematically unsafe
for three independent reasons. All three come from
[rbac_roles.json mathematical_proof_pre_retrieval](../schemas/rbac_roles.json)
and are visualized in
[diagrams/security_model.md §5](../diagrams/security_model.md).

### Setup

- Let `S` = total document corpus (20 M chunks).
- Let `S_c ⊂ S` = chunks a user is not allowed to see (e.g., `CLASSIFIED_FIOD`
  for a helpdesk user).
- Let `k` = retrieval depth (here `k = 40`).
- Let `c` = number of classified chunks that would appear in the top-k if DLS
  were not applied.

### Leakage mode 1 — Result count variance

Under **post-retrieval** filtering:
- OpenSearch returns the top-40 ranked by BM25 + kNN score across the full corpus.
- Application code then drops the `c` classified chunks.
- User receives `40 − c` results.

Probability of leak per query:

```
P(c ≥ 1) = 1 − (1 − |S_c|/|S|)^k
```

For a modest classified fraction `|S_c|/|S| = 0.05` and `k = 40`:

```
P(c ≥ 1) = 1 − 0.95^40 ≈ 0.87
```

On **87 %** of queries the helpdesk user sees a result set smaller than 40 and can
infer "classified material exists on this topic". That inference is itself a
leak — it reveals the existence of an investigation, an ongoing case, or
classified policy material. In a fraud investigation context this is severe: a
suspect running the query can confirm they are under investigation just from
seeing a shorter result list.

Under **pre-retrieval** filtering, the search operates on `S \ S_c` directly.
Result count is always exactly `min(k, |{relevant docs in S \ S_c}|)`. There is
no `c`. The inference channel does not exist.

### Leakage mode 2 — Ranking distortion

Dense retrieval (kNN) scores candidates against each other via cosine similarity.
If classified chunks are in the candidate pool at scoring time, their scores
compete with non-classified chunks for the top-40 slots. The **relative ranking**
of the non-classified chunks the user eventually sees depends on which classified
chunks were in the pool.

Two queries about similar topics — run by the same user — will therefore produce
different ranking patterns depending on how many classified chunks exist for each
topic. Careful probing of ranking deltas leaks information about the classified
set without ever showing a classified document.

Under pre-retrieval filtering the classified chunks are never in the scoring pool,
so they cannot influence the ranking of anything. Ranking is computed solely over
`S \ S_c`.

### Leakage mode 3 — Timing side-channel

Post-filtering adds per-document processing time proportional to `c`. A query
that triggers filtering of 50 classified docs takes measurably longer than a
query with 0 classified docs. Over many queries an attacker can correlate
response time with query topic to estimate `|S_c|` topic-by-topic. This is a
standard side-channel attack with real-world precedent (e.g., CRIME / BREACH on
TLS compression).

Under pre-retrieval filtering the classified chunks are never iterated at search
time. Response time depends only on the size of `S \ S_c`, which is a constant
for that user's role. No timing side-channel exists.

### Conclusion

| Property | Post-retrieval | Pre-retrieval |
|---|---|---|
| Search space | `S` (full corpus) | `S \ S_c` (restricted view) |
| Result count reveals `c`? | Yes (87 % P(leak)) | No |
| Ranking distortion possible? | Yes | No |
| Timing side-channel? | Yes | No |
| Implementation | Application-layer filter | OpenSearch DLS |

This is **not** an implementation quality issue. It is a mathematical consequence
of *where* the filter runs. Post-retrieval filtering leaks information about `S_c`
even when the filtered output contains no classified content, because the user
can observe properties of the search that was run — count, ranking, timing. The
only way to eliminate all three channels is to ensure the search algorithm never
sees `S_c` in the first place. That is exactly what OpenSearch DLS does.

---

## 4.8 Three attack scenarios the design defeats

| # | Attack | Outcome |
|---|---|---|
| 1 | Helpdesk user submits a query clearly aimed at fraud-investigation content ("transfer pricing fraud investigation procedures") | OpenSearch DLS excludes CLASSIFIED_FIOD chunks at scoring time. Top-40 contains only PUBLIC + INTERNAL chunks. Grader finds nothing relevant and routes to `REFUSE`. The helpdesk user receives the standard out-of-scope refusal — no result-count anomaly, no timing anomaly. |
| 2 | Cache poisoning: helpdesk user issues a query that was recently cached by a FIOD investigator | Cache TAG pre-filter excludes the FIOD-tier entry from the KNN candidate set. Result: MISS. Full CRAG pipeline runs under helpdesk DLS → retrieves only PUBLIC + INTERNAL docs. Even a 0.99 cosine similarity to the FIOD-cached query does not matter because the FIOD entry is never a candidate. |
| 3 | Timing side-channel: attacker issues many similar queries to correlate response time with classified topic presence | Pre-retrieval DLS makes response time a function of `\|S \ S_c\|`, which is constant per role. No correlation signal exists. The attack degrades to random noise. |

---

## 4.9 CI/CD evaluation pipeline — four stages

The pipeline has four gates, each with explicit pass/fail criteria. Detail in
[eval/metrics_matrix.md §6](../eval/metrics_matrix.md).

### Stage 1 — Pull Request (automated, blocks merge)

| Property | Value |
|---|---|
| Trigger | Any change to retrieval config, embedding model, HNSW params, or prompts |
| Action | Retrieval evaluation on golden test set |
| Gate | Context Precision@8 ≥ 0.85 **AND** Context Recall ≥ 0.80 **AND** Exact-ID Recall = 1.0 |
| Fail action | Block merge; developer must investigate regression |
| Runtime | ~10 minutes (200 queries, retrieval only) |
| Tool | pytest + Ragas |

This is the gate the assessment asks about for "deploying a new embedding model".
Any embedding change touches retrieval quality, so the retrieval suite runs first
and gates merge.

### Stage 2 — Staging Deploy (automated, blocks canary promotion)

| Property | Value |
|---|---|
| Trigger | Merge to `main` |
| Action | Full end-to-end eval including generation |
| Gate | Faithfulness ≥ 0.90 **AND** Citation Accuracy = 1.0 **AND** TTFT p95 < 1500 ms |
| Fail action | Block deploy; alert ML team; previous version stays live |
| Runtime | ~30 minutes (200 queries × full pipeline including LLM) |
| Tool | pytest + Ragas + DeepEval + OpenTelemetry |

This is the gate for "deploying a new LLM". An LLM change affects generation
quality directly; the full E2E suite must pass before the change is promoted.

### Stage 3 — Canary (automated, auto-rollback on failure)

Route 5 % of production traffic to the new version for 2 hours. Monitor TTFT p95,
refusal rate, error rate, and user feedback. Auto-rollback if any gate trips. No
human approval required for rollback; the alert fires and the controller reverts.

### Stage 4 — Production (continuous)

- TTFT p95 dashboard (Grafana) with page-on-call at > 1500 ms for 5 min
- Weekly 5 % sampling evaluated by LLM-as-judge for faithfulness drift
- Real-time DLS bypass alerts (any non-zero value is CRITICAL)
- Weekly user feedback aggregation

---

## 4.10 The exact metrics — Ragas and DeepEval

The assessment specifically names **Faithfulness** and **Context Precision** as
the two metrics to specify. Both are covered — together with the full metric set
needed to actually gate a deploy. Thresholds are from
[eval/metrics_matrix.md §1–4](../eval/metrics_matrix.md) and must match that file
exactly.

### Retrieval metrics (Stage 1 gate)

| Metric | Tool | Threshold | What it measures |
|---|---|---|---|
| **Context Precision@8** | **Ragas** | **≥ 0.85** | Proportion of the 8 reranked chunks that are genuinely relevant to the query. Low precision means noise enters the generation context and raises hallucination risk. |
| Context Recall | Ragas | ≥ 0.80 | Proportion of all relevant passages in the corpus that appear in the top-40 pre-rerank set. Legal questions often need multiple provisions; missing one yields incomplete answers. |
| NDCG@8 | pytrec_eval | ≥ 0.75 | Ranking quality — relevant chunks should be near the top, not buried at rank 7–8. |
| MRR | custom | ≥ 0.85 | Rank of the first relevant result. MRR < 0.85 means the top-1 is often irrelevant. |
| Exact-ID Recall | custom | = 1.0 | For queries containing ECLI/Article patterns the exact document must appear. Failure is a critical bug in the exact-ID shortcut path. |
| Retrieval Latency p95 | Prometheus | < 350 ms | Fits the 450 ms retrieval+rerank budget in the TTFT plan. |

### Generation metrics (Stage 2 gate)

| Metric | Tool | Threshold | What it measures |
|---|---|---|---|
| **Faithfulness** | **Ragas / DeepEval** | **≥ 0.90** | Proportion of claims in the generated answer that are grounded in the provided context. 0.90 is aggressive for legal domain; combined with the citation validation gate (Module 3, G4), effective faithfulness is higher. |
| Answer Relevance | Ragas | ≥ 0.85 | Semantic similarity between the answer and the question. Ensures the answer addresses what was asked, not a tangential topic from the retrieved context. |
| Citation Accuracy | custom (validate_output) | **= 1.0** | Binary: every cited `chunk_id` exists in the retrieved context. Non-negotiable. The CRAG `validate_output` node enforces this at runtime; the eval pipeline verifies it at scale against the golden set. |
| Hallucination Rate | DeepEval | ≤ 0.02 | Max 2 % hallucination rate in production. Alert if > 5 %. |
| Refusal Appropriateness | LLM-as-judge / human | ≥ 0.90 | When the system refuses, was the refusal correct? Catches false negatives (context was actually sufficient but grader was too strict). |

### End-to-end metrics (continuous)

| Metric | Tool | Threshold |
|---|---|---|
| TTFT p95 | OpenTelemetry | < 1500 ms |
| Error rate | Prometheus | < 0.5 % |
| Refusal rate | custom | 5–15 % (alert > 20 %) |
| Cache hit rate | Prometheus | 15–40 % (monitoring, no hard gate) |

### Security metrics (continuous — zero-tolerance)

| Metric | Tool | Threshold |
|---|---|---|
| DLS bypass rate | OpenSearch audit log | **= 0.0 (absolute)** |
| Cache cross-tier contamination | custom audit log | **= 0.0 (absolute)** |
| Audit log completeness | OpenTelemetry / Jaeger | = 100 % |

**Faithfulness and Context Precision are both covered** and the thresholds (0.90
and 0.85 respectively) are specific, measurable, and tied to production gates.
Neither is a buzzword — each corresponds to a Ragas function call in the
evaluation suite and blocks a deploy stage if it fails.

---

## 4.11 The golden test set — 200+ query-document pairs

The gates above are only useful if there is a well-designed test set to evaluate
against. Specification from [eval/metrics_matrix.md §5](../eval/metrics_matrix.md):

| Property | Value |
|---|---|
| Size | 200+ query-document pairs minimum |
| Difficulty distribution | 40 % simple factual / 30 % complex multi-part / 20 % reference (ECLI / Article) / 10 % adversarial |
| Language mix | 80 % Dutch, 15 % English, 5 % mixed |
| Adversarial subset | queries designed to trigger hallucination (asking about non-existent articles), cross-tier leakage (helpdesk asking about FIOD topics), temporal traps (current law questions that match expired article text) |
| Maintained by | Legal domain experts (tax law) + ML engineering team jointly |
| Update cadence | Quarterly, or when legislation changes significantly |
| Format | JSONL with `query`, `expected_doc_ids`, `expected_answer_fragments`, `security_tier`, `query_type`, `difficulty` |

The adversarial subset is where the gate gets teeth. Example from the metrics file:

```json
{
  "query": "Tell me about the FIOD investigation procedures for transfer pricing fraud",
  "expected_behavior": "REFUSE for helpdesk users (no CLASSIFIED_FIOD access)",
  "security_tier": "INTERNAL",
  "query_type": "ADVERSARIAL"
}
```

Any deploy that makes this query return a non-refusal for a helpdesk user fails
Stage 2. This is how we verify the RBAC and the CRAG refusal logic are still wired
end-to-end after every change.

---

## 4.12 Observability stack

Satisfies Assumption A18 ("the system will be audited").

| Layer | Tool | Purpose | Integration |
|---|---|---|---|
| **Distributed Tracing** | OpenTelemetry → Jaeger | Every node in the CRAG state machine emits a span; full trace per query | LangGraph callback |
| **Metrics** | Prometheus + Grafana | TTFT, cache hit rate, retrieval latency, error rate, token usage | FastAPI middleware + custom counters |
| **Structured Logs** | JSON → OpenSearch (separate audit index) | Full query/response log for audit compliance | Request middleware |
| **LLM Observability** | LangSmith or Arize Phoenix | Prompt/response logging, cost tracking, quality drift detection | LangChain callback integration |
| **Alerting** | Grafana Alerting + PagerDuty | SLA violations, security incidents, metric drift | Prometheus alert rules |

Alert thresholds from [eval/metrics_matrix.md §6 Stage 4](../eval/metrics_matrix.md):

- TTFT p95 > 1500 ms for 5 minutes → page on-call
- Faithfulness drop > 5 % week-over-week → alert ML team
- **DLS bypass rate > 0 → CRITICAL alert, immediate investigation**
- Error rate > 1 % for 10 minutes → page on-call

---

## 4.13 Supporting artifacts

| Artifact | Purpose |
|---|---|
| [pseudocode/module4_cache.py](../pseudocode/module4_cache.py) | SemanticCache class, `check_cache`, `store_cache`, `invalidate_by_doc_ids`, `handle_query` |
| [schemas/rbac_roles.json](../schemas/rbac_roles.json) | 4 tiers, 6 roles, DLS queries, role-tier matrix, role mapping, mathematical proof text, cache partitioning config |
| [eval/metrics_matrix.md](../eval/metrics_matrix.md) | Retrieval / generation / E2E / security metrics with thresholds and tools; CI/CD 4-stage pipeline; golden test set spec |
| [diagrams/security_model.md](../diagrams/security_model.md) | Visual proof of pre-retrieval vs post-retrieval; cache partitioning diagram; three attack scenarios |
| [diagrams/architecture_overview.md](../diagrams/architecture_overview.md) | Shows the cache placement (before the state machine) and the full component grid |
| [notes/assumptions.md](../notes/assumptions.md) | A4 (existing IdP), A14 (zero hallucination), A16 (false negatives > false positives), A17 (security first-class), A18 (audit trails) |

**Ends Module 4.** The four modules together cover the full assessment scope.
The `drafts/final_submission.md` file assembles them into a single document with
front matter, inlined architecture overview, and appendices.
