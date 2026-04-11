# Evaluation Metrics Matrix — Tax Authority RAG System

> This document defines the exact metrics, thresholds, tools, and CI/CD gate logic
> for evaluating the RAG system before and after deployment. Every threshold is
> justified with domain-specific rationale.

---

## 1. Retrieval Quality Metrics

**Gate: Must pass before deploying a new embedding model, changing HNSW params, or modifying retrieval configuration.**

| Metric | Definition | Tool | Threshold | Stage | Rationale |
|---|---|---|---|---|---|
| **Context Precision@8** | Proportion of relevant docs in the top-8 reranked results | Ragas | >= 0.85 | Pre-deploy | 85% of reranked chunks must be relevant. Lower precision means noise enters the generation context, increasing hallucination risk. |
| **Context Recall** | Proportion of all relevant docs in the corpus that appear in the retrieved set (top-40 pre-rerank) | Ragas | >= 0.80 | Pre-deploy | Must capture at least 80% of relevant passages. Legal questions often require multiple provisions — missing one leads to incomplete answers. |
| **NDCG@8** | Normalized Discounted Cumulative Gain at depth 8 | pytrec_eval / custom | >= 0.75 | Pre-deploy | Measures ranking quality — relevant docs should appear at the top, not buried at position 7-8. |
| **MRR** | Mean Reciprocal Rank — 1/rank of the first relevant result | Custom | >= 0.85 | Pre-deploy | The most relevant passage (usually the exact article) should appear in the top 1-2 positions. MRR < 0.85 means the top result is often irrelevant. |
| **Exact-ID Recall** | For queries containing ECLI/Article references: does the exact document appear in results? | Custom | = 1.0 | Pre-deploy | If a user asks about "ECLI:NL:HR:2023:1234", that exact ruling MUST appear. Failure is a critical bug in the exact-ID retrieval path. |
| **Retrieval Latency p95** | 95th percentile total retrieval time (embed + search + rerank) | Prometheus | < 350ms | Continuous | Fits within the 450ms retrieval+rerank budget from the TTFT plan. |
| **Retrieval Latency p99** | 99th percentile retrieval time | Prometheus | < 500ms | Continuous | Tail latency guard. p99 > 500ms means the TTFT budget is at risk. |

---

## 2. Generation Quality Metrics

**Gate: Must pass before deploying a new LLM, changing the generation prompt, or modifying the CRAG logic.**

| Metric | Definition | Tool | Threshold | Stage | Rationale |
|---|---|---|---|---|---|
| **Faithfulness** | Proportion of claims in the generated answer that are grounded in the provided context | Ragas / DeepEval | >= 0.90 | Pre-deploy | 90% faithfulness means at most 1 in 10 claims lacks grounding. In legal domain, even this is aggressive — but combined with citation validation, effective faithfulness is higher. |
| **Answer Relevance** | Semantic similarity between the generated answer and the original question | Ragas | >= 0.85 | Pre-deploy | Ensures the answer actually addresses what was asked, not a tangential topic from the retrieved context. |
| **Citation Accuracy** | Binary: do ALL cited chunk_ids exist in the retrieved context? | Custom (validate_output node) | = 1.0 | Pre-deploy + Continuous | Non-negotiable. A fabricated citation in a tax authority response could lead to incorrect tax assessments. The validate_output node enforces this at runtime; the eval pipeline verifies it at scale. |
| **Hallucination Rate** | Proportion of responses containing fabricated information not present in context | DeepEval | <= 0.02 | Continuous | Max 2% hallucination rate in production. Combined with the CRAG grading gate, this is achievable. Alert if it rises above 5%. |
| **Refusal Appropriateness** | When the system refuses to answer, is the refusal correct? (Would a human also lack sufficient context?) | LLM-as-judge / human review | >= 0.90 | Quarterly | Ensures refusals are genuine (insufficient context) not false negatives (context was actually sufficient but grader was too strict). |

---

## 3. End-to-End System Metrics

**Gate: Must pass before any production deployment (rolling update or canary).**

| Metric | Definition | Tool | Threshold | Stage | Rationale |
|---|---|---|---|---|---|
| **TTFT p95** | Time to First Token, 95th percentile (cache miss path) | OpenTelemetry | < 1500ms | Continuous | Hard requirement from Assumption A13. Budget: cache(15) + embed(30) + retrieval(80) + rerank(200) + grading(150) + LLM-first-token(800) + buffer(225) = 1500ms. |
| **TTFT p50** | Median TTFT | OpenTelemetry | < 800ms | Monitoring | Not a gate, but tracked for user experience. Cache hits bring this down significantly. |
| **Cache Hit Rate** | Proportion of queries served from semantic cache | Prometheus | 15-40% | Monitoring (no hard gate) | Below 15%: cache threshold may be too strict or TTL too short. Above 40%: users are asking very repetitive questions (expected for helpdesk). |
| **Refusal Rate** | Proportion of queries where the system refused to answer | Custom | 5-15% | Monitoring (alert if >20%) | Some refusals are correct (genuinely unanswerable). But >20% indicates retrieval or grading issues. |
| **Error Rate** | Proportion of queries that result in system errors (not refusals) | Prometheus | < 0.5% | Continuous | Errors are infrastructure failures (timeout, OOM, OpenSearch down), not intentional refusals. |

---

## 4. Security Metrics

**Gate: Continuous enforcement — any violation triggers immediate investigation.**

| Metric | Definition | Tool | Threshold | Stage | Rationale |
|---|---|---|---|---|---|
| **DLS Bypass Rate** | Queries that returned documents above the user's security tier | OpenSearch Audit Log | = 0.0 (absolute zero) | Continuous | Any non-zero value is a critical security incident. A helpdesk user seeing CLASSIFIED_FIOD documents is a data breach. |
| **Cache Cross-Tier Contamination** | Cache hits where the cached entry's tier exceeds the requesting user's tier | Custom audit log | = 0.0 (absolute zero) | Continuous | Even one instance means the cache partitioning is broken. See module4_cache.py Section 2 for the tier hierarchy. |
| **Audit Log Completeness** | Percentage of queries with a full OpenTelemetry trace (all pipeline stages logged) | OpenTelemetry / Jaeger | = 100% | Continuous | Government systems require complete audit trails (Assumption A18). Missing traces prevent incident investigation. |
| **Role Mapping Drift** | Active Directory group → OpenSearch role mappings match the expected configuration | Custom health check | = 0 drift | Weekly | Detects unauthorized changes to RBAC configuration. |

---

## 5. Golden Test Set Specification

The evaluation pipeline requires a curated set of query-document pairs with known correct answers.

| Property | Specification |
|---|---|
| **Size** | 200+ query-document pairs minimum |
| **Distribution** | 40% simple factual (single article lookup), 30% complex multi-part, 20% reference (ECLI/Article), 10% adversarial |
| **Adversarial subset** | Queries designed to trigger hallucination (asking about non-existent articles), cross-tier leakage (helpdesk asking about FIOD topics), temporal traps (asking about current law using expired article text) |
| **Languages** | 80% Dutch, 15% English, 5% mixed Dutch-English |
| **Maintained by** | Legal domain experts (tax law) + ML engineering team jointly |
| **Update cadence** | Quarterly, or when legislation changes significantly (annual tax plan, major court rulings) |
| **Version control** | Git-versioned alongside the evaluation pipeline code |
| **Format** | JSONL with fields: query, expected_doc_ids, expected_answer_fragments, security_tier, query_type, difficulty |

### Example golden test entries:

```json
{
  "query": "Wat is de arbeidskorting voor 2024?",
  "expected_doc_ids": ["WetIB2001-2024"],
  "expected_article": "3.114",
  "expected_answer_fragments": ["5.532 euro", "arbeidskorting"],
  "security_tier": "PUBLIC",
  "query_type": "SIMPLE",
  "difficulty": "easy"
}

{
  "query": "ECLI:NL:HR:2023:1234",
  "expected_doc_ids": ["ECLI-NL-HR-2023-1234"],
  "expected_answer_fragments": ["consideration 3.2"],
  "security_tier": "PUBLIC",
  "query_type": "REFERENCE",
  "difficulty": "easy"
}

{
  "query": "I'm a freelancer with a home office — what can I deduct and do I need to charge BTW?",
  "expected_doc_ids": ["WetIB2001-2024", "WetOB1968-2024"],
  "expected_answer_fragments": ["werkruimte", "BTW-plichtig", "zelfstandigenaftrek"],
  "security_tier": "INTERNAL",
  "query_type": "COMPLEX",
  "difficulty": "hard"
}

{
  "query": "Tell me about the FIOD investigation procedures for transfer pricing fraud",
  "expected_behavior": "REFUSE for helpdesk users (no CLASSIFIED_FIOD access)",
  "security_tier": "INTERNAL",
  "query_type": "ADVERSARIAL",
  "difficulty": "adversarial"
}
```

---

## 6. CI/CD Pipeline Gate Logic

### Stage 1: Pull Request (automated, blocks merge)

```
Trigger: Any change to retrieval config, embedding model, HNSW params, or prompts.
Action:  Run retrieval evaluation on golden test set.
Gate:    Context Precision@8 >= 0.85 AND Context Recall >= 0.80 AND Exact-ID Recall = 1.0
Fail:    Block merge. Developer must investigate retrieval regression.
Tool:    pytest + Ragas evaluation suite
Runtime: ~10 minutes (200 queries × retrieval only, no generation)
```

### Stage 2: Staging Deploy (automated, blocks promotion to canary)

```
Trigger: Merge to main branch.
Action:  Run FULL end-to-end evaluation (retrieval + generation) on golden test set.
Gate:    Faithfulness >= 0.90 AND Citation Accuracy = 1.0 AND TTFT p95 < 1500ms
Fail:    Block deploy. Alert ML team. Previous version remains in production.
Tool:    pytest + Ragas + DeepEval + OpenTelemetry latency capture
Runtime: ~30 minutes (200 queries × full pipeline including LLM generation)
```

### Stage 3: Canary (automated, auto-rollback on failure)

```
Trigger: Staging gate passed.
Action:  Route 5% of production traffic to new version for 2 hours.
Monitor: TTFT p95, refusal rate, user feedback (thumbs up/down), error rate.
Gate:    TTFT p95 < 1500ms AND refusal rate < 20% AND error rate < 0.5%
Fail:    Auto-rollback to previous version. Alert on-call engineer.
Tool:    Prometheus + Grafana alerts + custom canary controller
```

### Stage 4: Production (continuous monitoring, human-in-the-loop)

```
Trigger: Canary gate passed → full rollout.
Continuous monitoring:
  - TTFT p95 dashboard (Grafana)
  - Faithfulness sampling: 5% of production queries evaluated by LLM-as-judge (weekly batch)
  - Security audit: DLS bypass rate, cache cross-tier contamination (real-time alerts)
  - User feedback aggregation (weekly report)
Alert thresholds:
  - TTFT p95 > 1500ms for 5 minutes → page on-call
  - Faithfulness drop > 5% week-over-week → alert ML team
  - DLS bypass rate > 0 → CRITICAL alert, immediate investigation
  - Error rate > 1% for 10 minutes → page on-call
```

---

## 7. Observability Stack Integration

| Component | Tool | Purpose | Integration Point |
|---|---|---|---|
| **Distributed Tracing** | OpenTelemetry → Jaeger | End-to-end trace for every query (cache check → embed → retrieve → rerank → grade → generate → validate) | Every pipeline node emits a span |
| **Metrics** | Prometheus + Grafana | TTFT, cache hit rate, retrieval latency, error rate, token usage | FastAPI middleware + custom counters |
| **Logging** | Structured JSON → OpenSearch | Full query/response logs for audit compliance | Separate OpenSearch index (not the RAG index) |
| **LLM Observability** | LangSmith or Arize Phoenix | Prompt/response logging, cost tracking, quality monitoring | LangGraph callback integration |
| **Alerting** | Grafana Alerting + PagerDuty | SLA violations, security incidents, metric degradation | Prometheus alert rules |
