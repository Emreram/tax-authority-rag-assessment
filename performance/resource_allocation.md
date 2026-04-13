# Performance & Resource Allocation — Supplementary Reference

> **This document is voluntary supplementary material.**
> It goes beyond what the assessment required. Its purpose is to show what a
> production-readiness review would look like before the system goes live.
>
> The assessment answers are in [drafts/final_submission_v2.md](../drafts/final_submission_v2.md).
> This file adds depth that a DevOps or platform engineering team would need
> to actually deploy and operate the system.
>
> Numbers in this document are engineering estimates derived from published
> model benchmarks, OpenSearch documentation, and vLLM throughput data.
> They must be validated against actual hardware in load testing before
> production sign-off.

---

## Contents

1. [Hardware Resource Budget](#1-hardware-resource-budget)
2. [Throughput Model & Bottleneck Analysis](#2-throughput-model--bottleneck-analysis)
3. [Horizontal Scaling Triggers](#3-horizontal-scaling-triggers)
4. [Cost Per Query](#4-cost-per-query)
5. [Request Queue & Backpressure](#5-request-queue--backpressure)
6. [Ingestion Performance](#6-ingestion-performance)
7. [Monitoring Dashboards](#7-monitoring-dashboards)

---

## 1. Hardware Resource Budget

### 1a. GPU Allocation

All GPU-resident models run on-premises to satisfy A2 (data sovereignty). The table below shows the minimum hardware allocation for the online query path.

| Component | Model | Assigned GPU | VRAM required | Batch config | p95 latency |
|---|---|---|---|---|---|
| Query embedding | `intfloat/multilingual-e5-large` | 1× NVIDIA A10G (24 GB) | ~3.5 GB | 1 query per call | ~30 ms |
| Cross-encoder reranker | `BAAI/bge-reranker-v2-m3` | shared A10G (with embedding) | ~2.8 GB | 40 pairs per call | ~200 ms |
| Retrieval grader (LLM) | Mixtral 8x22B | shared with generator | time-shared | 8 chunks per call | ~150 ms |
| Answer generator (LLM) | Mixtral 8x22B via vLLM | 4× NVIDIA A100 80 GB | ~148 GB (4-bit GPTQ) | 1–4 concurrent via continuous batching | ~800 ms TTFT |
| Ingestion embedding | `intfloat/multilingual-e5-large` | 1× NVIDIA A10G (24 GB) | ~3.5 GB | 64 chunks per call (batch ingestion) | ~50 ms/batch |

**Notes:**

- **Embedding + reranker share one A10G.** They do not run simultaneously per query — embedding runs first (~30 ms), then kNN retrieval, then reranking (~200 ms). Peak VRAM = max(3.5, 2.8) = 3.5 GB, well within 24 GB headroom.
- **A second A10G** is recommended: one serves the online query path (embed + rerank); the other runs batch ingestion without contending with live queries.
- **Mixtral 8x22B memory breakdown:** 141B total parameters, ~70B active per token (MoE routing). At 4-bit GPTQ: ~70B × 0.5 bytes ≈ 35 GB minimum. With KV cache for 4 concurrent requests at 4K context: +~48 GB. Total: ~83 GB minimum; 4× A100 80 GB (320 GB total) provides headroom for `tensor_parallel_size=4` and longer contexts.
- **Fallback to Azure OpenAI Government Cloud** (A3 fallback) eliminates the 4× A100 requirement entirely — the LLM becomes an API call — at significantly higher per-query cost (see §4).

**Total GPU rack minimum:**

```
Online path:    1× A10G (embed + rerank)
                4× A100 80 GB (Mixtral generator + grader)
Ingestion path: 1× A10G (batch embedding, separate from online)
Failover:       1× A10G (hot standby for embed/rerank)
─────────────────────────────────────────────────────────
Minimum:        6× A10G + 4× A100
```

---

### 1b. OpenSearch Cluster Sizing

| Dimension | Value | Derivation |
|---|---|---|
| Data nodes | 3 | Minimum for quorum + HA. Shard replicas mean 1 node can fail without downtime. |
| Coordinator node | 1 dedicated | Offloads query fan-out and merge from data nodes; significantly reduces p99 jitter at 200+ concurrent queries. |
| RAM per data node | 64 GB | 32 GB JVM heap (OpenSearch half-RAM rule) + 32 GB OS page cache for Lucene segment data. |
| JVM heap per node | 32 GB (`-Xms32g -Xmx32g`) | OpenSearch hard limit is 32 GB — above this, the JVM switches from 32-bit compressed OOPs to 64-bit, wasting memory. Set both `-Xms` and `-Xmx` equal to prevent heap resizing pauses. |
| GC policy | G1GC (default in OpenSearch / Java 17+) | G1GC max pause target: 200 ms. With 32 GB heap, GC pauses stay comfortably under the TTFT budget's 225 ms jitter buffer. |
| CPU per data node | 16 vCPU | Lucene BM25 scoring is CPU-bound. 16 vCPU supports ~50 concurrent BM25 queries before thread-pool queuing. At 8 QPS steady-state, this is ample. |
| Storage per data node | 2 TB NVMe SSD | fp16 index ~61 GB + BM25 inverted index ~30 GB + doc store ~15 GB + replicas = ~200 GB per node. 2 TB leaves room for growth, segment merges, and OS overhead. NVMe required for HNSW random-access patterns; spinning disk causes p99 spikes. |
| Shards | 6 primary + 6 replica | 6 primaries = ~10 GB fp16 vectors per shard (within OpenSearch's 10–50 GB recommendation). 1 replica doubles read throughput and provides failover. |
| `refresh_interval` | 30 s | Ingestion is nightly batch; no need for near-real-time refresh. Longer interval = better indexing throughput and fewer segment merges competing with queries. |
| `ef_search` | 128 (normal) / 192 (SQ8 mode) | 128 provides p99 kNN < 100 ms at 20M vectors. Raise to 192 if using SQ8 quantization to compensate for ~1–2% recall loss. |

**JVM tuning snippet** (`jvm.options`):

```
-Xms32g
-Xmx32g
-XX:+UseG1GC
-XX:MaxGCPauseMillis=200
-XX:G1HeapRegionSize=32m
-XX:InitiatingHeapOccupancyPercent=30
```

---

### 1c. Redis Stack Sizing

**Entry size estimate:**

| Field | Size |
|---|---|
| Query embedding (1024 × float32) | 4,096 B ≈ 4 KB |
| Response text (avg legal answer with citations) | ~2,000 B ≈ 2 KB |
| Metadata (security_tier, TTL, query_type, retrieved_doc_ids) | ~500 B |
| RediSearch index overhead per entry | ~200 B |
| **Total per cache entry** | **~6.8 KB** |

**Memory model:**

```
Scenario A — Conservative (top unique queries only):
  5,000 unique queries/day × 1-day TTL    =  5,000 live entries × 6.8 KB =  ~34 MB

Scenario B — Realistic (full 24h window, mixed TTLs):
  10,000 unique queries/day × 1-day TTL   =  10,000 entries × 6.8 KB    =  ~68 MB
  2,000 procedural queries × 7-day TTL    =  14,000 entries × 6.8 KB    =  ~95 MB
  Total live set                                                          = ~163 MB

Scenario C — 1-year steady state (with growth):
  Assuming 10% query volume growth/month over 12 months:
  Peak daily volume × 7-day retention      =  ~100,000 live entries      = ~680 MB
```

**Recommendation: 8 GB Redis instance.** This leaves >10× headroom over Scenario C, accommodating response body growth, RediSearch HNSW index overhead (~20–30% of vector data), and RDB snapshot working space. At 8 GB, Redis runs comfortably on a single `r6g.large` node.

**Configuration:**

```
maxmemory 7gb                    # leave 1 GB for OS and RDB snapshots
maxmemory-policy allkeys-lru     # evict least recently used entries
                                  # when full — degrades hit rate gracefully,
                                  # never corrupts results
save 300 100                     # RDB snapshot every 5 min if ≥100 keys changed
                                  # enables warm restart after node failure
```

**Expected cache hit rate:**

| User persona | Expected hit rate | Reason |
|---|---|---|
| Helpdesk staff | 35–55% | FAQ-heavy; "Box 1 tarief 2024" asked many times daily |
| Tax inspectors | 5–15% | Complex, unique queries; low repetition |
| Legal counsel | 10–20% | Specific ECLI lookups; moderate repetition on landmark cases |
| **Blended (A11 persona mix)** | **~25–35%** | Assumes helpdesk is ~50% of traffic volume |

At 30% hit rate: 30% of queries return in ~15 ms (cache hit), 70% run the full pipeline.

---

## 2. Throughput Model & Bottleneck Analysis

### 2a. QPS Target Derivation

Assumption A10 specifies 200–500 concurrent users, not 200–500 simultaneous queries. Users read responses, think, and navigate between queries.

```
Effective QPS = concurrent_users / avg_session_cycle_seconds

Conservative:  200 users / 90 s cycle  =  ~2.2 QPS
Realistic:     350 users / 60 s cycle  =  ~5.8 QPS  ← design target
Peak burst:    500 users / 30 s cycle  =  ~16.7 QPS  ← lunch/deadline spike

Design target: 6 QPS sustained, 18 QPS burst (3× sustained for 15 min)
```

### 2b. Per-Component Throughput Ceiling

| Component | Estimated max throughput | At 6 QPS steady | At 18 QPS burst | Bottleneck? |
|---|---|---|---|---|
| FastAPI API gateway | ~500 RPS (async, 8-core) | 1% | 4% | No |
| Redis cache check | ~100,000 ops/s | <1% | <1% | No |
| Embedding GPU (A10G, batch=1) | ~33 queries/s | 18% | 55% | No |
| OpenSearch BM25 | ~200 concurrent queries | 3% | 9% | No |
| OpenSearch kNN (ef_search=128) | ~25 concurrent queries | 24% | 72% | Marginal at burst |
| Reranker (A10G, 40 pairs/call) | ~5 reranks/s | 120% | **SATURATED** | **Primary bottleneck** |
| Grader LLM (Mixtral, 8 chunks) | ~7 calls/s | 86% | **SATURATED** | Secondary bottleneck |
| Generator LLM (vLLM, continuous batch) | ~4–8 concurrent requests | At capacity | **SATURATED** | Tertiary bottleneck |

**Key insight — reranker is the first bottleneck:**

At 6 QPS with 30% cache hit rate, 4.2 QPS reach the reranker. Each reranker call processes 40 pairs and takes ~200 ms. The reranker can handle 1/0.2 = 5 calls/second maximum on one A10G → **4.2 QPS just fits, with no headroom for burst.**

At 18 QPS burst: 12.6 QPS hit the reranker → 2.5× the single-GPU ceiling → queue builds.

**Mitigation options (in order of preference):**

1. **Reduce TOP_K_RERANK from 40 → 20 under load** — halves reranker latency (~100 ms), doubles throughput. Slight precision impact; acceptable for helpdesk FAQ queries.
2. **Add a second A10G dedicated to reranking** — dedicated reranker node doubles throughput ceiling to ~10 reranks/s.
3. **Priority queue by user tier** — FIOD investigators and legal counsel get dedicated GPU share; helpdesk queries go to a slightly slower path.

### 2c. LLM Throughput — The Sustained Ceiling

vLLM continuous batching for Mixtral 8x22B on 4× A100 (tensor_parallel=4):

```
Observed throughput (from vLLM benchmarks, ~comparable model size):
  ~3,000–5,000 output tokens/sec across all concurrent requests

At 500 output tokens/response and 800 ms TTFT:
  Concurrent requests served = 3,500 tokens/s / (500 tokens / 0.8 s) ≈ 5.6 concurrent

Round-trip including grader + generator (~950 ms total LLM time):
  Effective LLM QPS ≈ 5.6 / 0.95 ≈ ~5.9 LLM calls/sec
```

At 6 QPS steady with 30% cache hit rate → 4.2 LLM calls/s needed. **Just within capacity.**

At 18 QPS burst with 30% cache hit rate → 12.6 LLM calls/s needed. **2× over ceiling.** Queries queue; TTFT degrades linearly. With a 2 s p95 allowance (500 ms slack), the queue can hold ~3 seconds of backlog before clients see SLO violations.

### 2d. Cache Hit Rate as the Primary Lever

```
Effective LLM demand = QPS × (1 − cache_hit_rate)

At  6 QPS,  10% hit rate → 5.4 LLM calls/s  (near ceiling)
At  6 QPS,  30% hit rate → 4.2 LLM calls/s  (comfortable)
At  6 QPS,  50% hit rate → 3.0 LLM calls/s  (well within)
At 18 QPS,  30% hit rate → 12.6 LLM calls/s (over ceiling — queue builds)
At 18 QPS,  50% hit rate → 9.0 LLM calls/s  (manageable with degraded reranker)
```

**Conclusion:** The semantic cache is not just a latency optimisation — it is the primary mechanism for keeping the system within throughput limits at burst load. Maintaining ≥30% cache hit rate should be a first-class operational SLO.

---

## 3. Horizontal Scaling Triggers

### Trigger Matrix

| Component | Metric to watch | Scale trigger | Scale action | Downtime |
|---|---|---|---|---|
| OpenSearch data nodes | kNN p95 latency, heap usage | kNN p95 > 120 ms sustained 10 min, OR any node heap > 75% | Add 1–2 data nodes (rolling join; shards auto-rebalance via OpenSearch Rebalancer) | 0 — rolling |
| Reranker GPU | Reranker queue depth, GPU utilisation | Queue depth > 5 sustained 10 min, OR GPU util > 80% sustained 10 min | Add 1× A10G node, register as second reranker endpoint in load balancer | ~2 min |
| LLM inference | LLM p95 TTFT | LLM p95 TTFT > 1,200 ms sustained 5 min (leaves <300 ms buffer) | Add tensor-parallel LLM replica on 2× additional A100s; vLLM multi-node routing | ~5 min |
| Redis | Memory usage, cache hit rate | Redis memory > 70% maxmemory, OR cache hit rate drops > 10pp week-over-week | Increase `maxmemory` (live `CONFIG SET`, no restart), or promote to Redis Cluster | 0 — live config |
| API Gateway (FastAPI) | CPU, queue depth | CPU > 70% on gateway node, OR `asyncio` queue depth > 100 | Add replica behind load balancer (stateless; no shared state) | 0 |

### Scaling Notes

**OpenSearch shard split is not online.** The HNSW index does not support adding shards to an existing index without a full re-index. The safe procedure:

```
1. Create a new index with the target shard count (e.g., 9 instead of 6)
2. Run ingestion pipeline against the new index (parallel to production)
3. When new index is fully populated, atomically swap the index alias
4. Delete the old index
5. Downtime: 0 (alias swap is atomic in OpenSearch)
6. Time: ~7 hours for 20M chunks (see §6)
```

**vLLM multi-node:** vLLM 0.5+ supports distributed tensor parallelism across nodes using NCCL. Adding 2× A100 as a second replica doubles LLM throughput but requires a routing layer (nginx upstream or a vLLM-native load balancer) in front of both inference nodes.

**Do not scale OpenSearch before scaling the reranker.** OpenSearch kNN at ef_search=128 handles ~25 concurrent queries — far above the 6–18 QPS load range. Adding OpenSearch nodes without first addressing the reranker GPU ceiling wastes budget.

---

## 4. Cost Per Query

### 4a. Token Consumption (Worst Case, No Cache)

| Stage | Trigger | Tokens in | Tokens out |
|---|---|---|---|
| HyDE generation | ~50% of SIMPLE queries | ~200 | ~150 |
| Query rewrite | ~15% of all queries (AMBIGUOUS retry) | ~200 | ~100 |
| Grader LLM | Every non-cached query | ~3,000 (8 chunks × ~350 tokens + prompt) | ~400 (JSON grades) |
| Generator LLM | Every non-refused, non-cached query (~90% of non-cached) | ~4,000 (8 chunks + system prompt) | ~500 |
| **Weighted average/query** (accounting for trigger rates) | | **~4,550** | **~1,150** |
| **Worst-case total** | | **~5,700** | — |

### 4b. Infrastructure Cost Model

Estimates based on AWS on-demand pricing (April 2026). Government cloud reserved pricing is typically 30–40% lower.

| Component | Instance type | Monthly cost (on-demand) | Monthly cost (1-yr reserved) |
|---|---|---|---|
| LLM inference (4× A100 80 GB) | 1× `p4d.24xlarge` (8× A100) — use half | ~$17,000 | ~$10,000 |
| Embedding + reranker (2× A10G) | 2× `g5.2xlarge` | ~$2,300 | ~$1,400 |
| OpenSearch cluster (3× data + 1× coord) | 4× `r6g.2xlarge` (64 GB RAM, 8 vCPU) | ~$1,200 | ~$730 |
| Redis Stack (8 GB) | 1× `r6g.large` (ElastiCache-equivalent) | ~$200 | ~$130 |
| API Gateway, misc networking | ~2× `c6g.2xlarge` | ~$300 | ~$180 |
| Storage (NVMe per OpenSearch node) | 3× 2 TB `gp3` NVMe | ~$180 | ~$180 |
| **Total infrastructure** | | **~$21,180/month** | **~$12,620/month** |

### 4c. Cost Per Query

```
Monthly queries at 6 QPS steady, 30% cache hit rate:
  Total queries/month    = 6 × 3600 × 24 × 30        = 15,552,000
  Cache hits (~30%)      = 4,666,000  → LLM not invoked
  LLM queries (~70%)     = 10,886,400 → full pipeline

Infrastructure cost per query (reserved pricing):
  $12,620 / 15,552,000                                = $0.00081/query

LLM cost per query (self-hosted Mixtral, amortised GPU cost):
  LLM GPU cost: ~$10,000/month (reserved p4d fraction)
  $10,000 / 10,886,400 LLM queries                   = $0.00092/LLM query

Total blended cost per query (self-hosted):            ~$0.001/query
```

**Comparison — Azure OpenAI GPT-4 Government Cloud:**

```
Tokens per query (weighted average): ~5,700 tokens
Azure GPT-4 Gov pricing (estimated): $0.03/1K input + $0.06/1K output

Input cost:  4,550 × $0.03/1K  = $0.137
Output cost: 1,150 × $0.06/1K  = $0.069
Total per query (API):           = ~$0.21/query

At 6 QPS / 30% hit rate:
  10,886,400 LLM queries/month × $0.21 = ~$2,286,000/month in API fees alone
```

**Self-hosted Mixtral is ~210× cheaper per query than Azure OpenAI GPT-4 Gov at this volume.** The GPU infrastructure pays for itself after approximately the first few thousand API calls per month. This is the primary financial justification for Assumption A3 (GPU investment).

---

## 5. Request Queue & Backpressure

### 5a. Queue Depth Limits

| Stage | Max queue depth | On overflow |
|---|---|---|
| API Gateway (FastAPI asyncio) | 200 pending requests | Return HTTP 503 with `Retry-After: 5` header |
| Embedding GPU queue | 32 pending requests | Backpressure propagates to FastAPI (await blocks) |
| OpenSearch thread pool | Default: 2× vCPU = 32 threads | OpenSearch returns 429; API gateway converts to 503 |
| Reranker GPU queue | 8 pending batches | Dynamically reduce batch size from 40 → 20 to clear queue faster |
| vLLM KV cache | Controlled by vLLM internally | vLLM blocks new requests when KV cache is full; API gateway queues |

### 5b. Timeout Policy Per Stage

| Stage | Timeout | Behavior on timeout |
|---|---|---|
| Cache check (Redis) | 20 ms | Treat as cache miss; proceed to full pipeline. Never block on cache. |
| Query embedding | 200 ms | Retry once; if second attempt fails, return HTTP 503. |
| OpenSearch BM25 | 150 ms | Return partial (BM25-only) results; skip kNN. Flag in audit log. |
| OpenSearch kNN | 250 ms | Skip kNN; fall back to BM25-only top-20. Audit flag. |
| Reranker | 500 ms | Skip reranker; return top-8 by RRF score directly (graceful degradation). |
| Grader LLM | 400 ms | Default result to `AMBIGUOUS`; trigger one rewrite attempt or refuse (conservative — aligns with A16). |
| Generator LLM | 2,000 ms | Return HTTP 503. Do not send partial answer. Partial legal answers are worse than no answer. |

### 5c. Circuit Breaker Pattern

Three circuit breakers protect the system from cascade failure:

**Breaker 1 — Reranker circuit:**

```
CLOSED  → OPEN  : if reranker p95 latency > 400 ms for 60 consecutive seconds
OPEN state      : skip reranker; return top-8 by RRF score; emit alert
OPEN  → CLOSED  : after 60 seconds of healthy latency (< 250 ms p95)
Impact          : slight precision loss; throughput doubles; TTFT drops ~200 ms
```

**Breaker 2 — Grader circuit:**

```
CLOSED  → OPEN  : if grader p95 latency > 350 ms for 60 consecutive seconds
OPEN state      : default all queries to AMBIGUOUS; system enters safe mode
                  (every query gets one rewrite attempt, then refuses if still AMBIGUOUS)
OPEN  → CLOSED  : after 60 seconds of healthy latency
Impact          : higher refusal rate; zero hallucination risk maintained
```

**Breaker 3 — OpenSearch kNN circuit:**

```
CLOSED  → OPEN  : if kNN p95 latency > 200 ms for 60 consecutive seconds
OPEN state      : fall back to BM25-only retrieval; skip kNN leg
OPEN  → CLOSED  : after 30 seconds of healthy latency
Impact          : loss of semantic retrieval; exact-reference and keyword queries
                  still work correctly; conceptual queries degrade
```

All circuit breaker state changes are logged as CRITICAL-level events in the OpenSearch audit index and emit a PagerDuty alert.

### 5d. Request Priority (Optional — Phase 2)

If helpdesk volume crowds out inspector and legal counsel queries, implement a priority queue at the API gateway level:

| Role | Priority class | Max queue wait |
|---|---|---|
| `role_fiod_investigator` | P1 | 200 ms |
| `role_tax_inspector` | P1 | 200 ms |
| `role_legal_counsel` | P2 | 500 ms |
| `role_helpdesk` | P3 | 2,000 ms |
| `role_public_user` | P3 | 2,000 ms |

FastAPI with an asyncio `PriorityQueue` can implement this with no additional infrastructure.

---

## 6. Ingestion Performance

### 6a. Per-Stage Throughput

| Stage | Throughput | Bottleneck? | Notes |
|---|---|---|---|
| Document parsing (pdfplumber/lxml) | ~200 docs/min | No | 8-worker multiprocessing pool on 16-vCPU ingest node |
| LegalDocumentChunker (regex + split) | ~2,000 chunks/min | No | Pure Python, memory-bound |
| Embedding (multilingual-e5-large, batch=64) | ~3,200 chunks/min | **Yes** | A10G: ~50 ms per 64-chunk batch = 64/0.05 = 1,280 chunks/min per GPU; two ingestion GPUs = 2,560 chunks/min |
| OpenSearch bulk indexing | ~5,000 chunks/min | No | Bulk API with `refresh=false`; 6 shards absorb writes in parallel |
| **End-to-end pipeline** | **~2,500 chunks/min** | Embedding GPU | |

### 6b. Ingestion SLA

| Scenario | Volume | Duration |
|---|---|---|
| **Full initial index** (cold start) | 20M chunks | ~20M / 2,500 per min = ~133 hours ≈ 5.5 days |
| **Full re-index** (embedding model upgrade) | 20M chunks | Same: ~5.5 days; use blue-green strategy (see below) |
| **Nightly incremental** (typical: 500–2,000 changed docs × ~40 chunks) | ~80,000 chunks | ~32 min — well within nightly maintenance window |
| **Emergency patch** (e.g., fiscal rate correction in 1 article) | ~5 chunks | < 1 minute end-to-end |

### 6c. Blue-Green Re-Index Strategy

Full re-index without downtime:

```
Step 1: Create new index  tax_authority_rag_chunks_v2  (same mapping, new shard count if needed)
Step 2: Point ingestion pipeline at v2 index; run full re-index (~5.5 days)
         Production queries continue serving v1 index via alias  tax_rag_alias → v1
Step 3: When v2 is fully populated and validated (retrieval eval gate passes on golden set):
         Atomic alias swap: tax_rag_alias → v2   (OpenSearch alias API, <1 ms)
Step 4: Delete v1 index after 24 hours (safety window)

Downtime: 0
Cost: ~2× storage during the transition window (~400 GB extra NVMe)
```

### 6d. Cache Invalidation on Re-Index

After each document is re-indexed (nightly incremental run), the ingestion pipeline calls:

```python
semantic_cache.invalidate_by_doc_ids(re_indexed_doc_ids)
```

This scans cache entries by `retrieved_doc_ids` field and purges any entry that referenced the changed document. Prevents stale answers from the cache after a legal amendment.

**Invalidation latency:** For a nightly run affecting 2,000 chunks across ~50 documents: Redis scan over ~35,000 live entries ≈ **< 1 second**.

---

## 7. Monitoring Dashboards

Seven Prometheus/Grafana panels that should be on the production NOC screen.

### Panel 1 — TTFT Distribution (Primary SLO)

```
Metric:  histogram_quantile(0.95, rate(rag_ttft_seconds_bucket[1m]))
Alert:   p95 > 1.5s sustained for 5 min → page on-call
Visual:  Time series; p50 / p95 / p99 lines; 1500 ms red threshold line
```

### Panel 2 — Cache Hit Rate (Throughput lever)

```
Metric:  rate(rag_cache_hits_total[5m]) / rate(rag_queries_total[5m])
Alert:   Hit rate drops > 10pp compared to rolling 7-day avg → warn ML team
Visual:  Time series; target band 25–45%; red below 15%
```

### Panel 3 — Refusal Rate (Quality indicator)

```
Metric:  rate(rag_refusals_total[5m]) / rate(rag_queries_total[5m])
Alert:   Rate > 20% sustained 10 min → investigate grader or retrieval degradation
Visual:  Time series; informational; expected range 5–15%
Breakdown by reason: IRRELEVANT | BUDGET_EXHAUSTED | CITATION_INVALID
```

### Panel 4 — LLM Queue Depth (Capacity alert)

```
Metric:  vllm_num_requests_waiting
Alert:   Queue > 20 for > 30 sec → scale trigger; page on-call
Visual:  Gauge + time series; red above 10
```

### Panel 5 — Reranker GPU Utilisation (Scale trigger)

```
Metric:  nvidia_smi_utilization_gpu{job="reranker"}
Alert:   Utilisation > 80% sustained 10 min → add second reranker GPU
Visual:  Time series; yellow at 70%, red at 80%
```

### Panel 6 — OpenSearch kNN Latency (Scale trigger)

```
Metric:  opensearch_knn_query_latency_milliseconds{quantile="0.95"}
Alert:   p95 > 120 ms sustained 10 min → add OpenSearch data node
Visual:  Time series; threshold at 100 ms (yellow) and 120 ms (red)
```

### Panel 7 — DLS Bypass Rate (Security — Absolute Zero)

```
Metric:  rate(opensearch_audit_dls_bypass_total[1m])
Alert:   ANY non-zero value → CRITICAL; immediate incident response;
         page security team + on-call simultaneously
Visual:  Counter; permanently green (zero); any tick → red; auto-page
Note:    This panel should NEVER change colour in production.
         A single event is a P0 security incident.
```

---

## Summary Table — What to Build First

| Priority | Item | Effort | Impact |
|---|---|---|---|
| **P0** | DLS Bypass Rate alert (Panel 7) | 1 hour | Non-negotiable security gate |
| **P0** | TTFT p95 alert (Panel 1) | 1 hour | Primary SLO |
| **P1** | Reranker GPU utilisation alert (Panel 5) | 2 hours | First bottleneck to hit under load |
| **P1** | Redis `maxmemory allkeys-lru` configuration | 15 min | Prevents cache OOM |
| **P1** | OpenSearch JVM heap tuning | 30 min | Prevents GC-induced p99 spikes |
| **P2** | Circuit breakers (reranker + grader + kNN) | 1 day | Graceful degradation under failure |
| **P2** | LLM queue depth alert (Panel 4) | 1 hour | Early warning for LLM saturation |
| **P3** | Priority queue by user role | 2 days | Quality-of-life for inspectors/counsel |
| **P3** | Blue-green re-index tooling | 3 days | Zero-downtime embedding model upgrades |

---

*All numbers in this document are engineering estimates. Validate with load testing on production hardware before sign-off. Recommend running the evaluation pipeline at [.github/workflows/eval_gate.yml](../.github/workflows/eval_gate.yml) after any hardware change that affects retrieval latency.*
