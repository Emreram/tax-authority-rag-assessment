# Enterprise RAG Architecture — Dutch Tax Authority

**Technical Assessment Response · Lead AI Engineer**

---

## Start here

The full written response is in one document:

**[drafts/final_submission.md](drafts/final_submission.md)**

It answers all four assessment modules in order, inline, with parameter-specific justifications. Every section links out to supporting artifacts for deeper inspection.

---

## What is in this repository

```
assesmentemre/
│
├── drafts/
│   ├── final_submission.md          ← THE SUBMISSION — start here
│   ├── module1_draft.md             ← Module 1 extended draft
│   ├── module2_draft.md             ← Module 2 extended draft
│   ├── module3_draft.md             ← Module 3 extended draft
│   └── module4_draft.md             ← Module 4 extended draft
│
├── pseudocode/
│   ├── module1_ingestion.py         ← LlamaIndex legal document chunker + pipeline
│   ├── module2_retrieval.py         ← Hybrid retrieval: BM25 + kNN + RRF + reranker
│   ├── module3_crag_statemachine.py ← LangGraph 9-state CRAG state machine
│   ├── module3_grader.py            ← Retrieval quality grader (RELEVANT/AMBIGUOUS/IRRELEVANT)
│   └── module4_cache.py             ← Redis semantic cache with tier partitioning
│
├── diagrams/
│   ├── architecture_overview.md     ← Full system data flow + component grid + latency budget
│   ├── retrieval_flow.md            ← Three-path retrieval + RRF fusion worked example
│   ├── crag_state_machine.md        ← 9-state machine with trace examples
│   └── security_model.md            ← RBAC tiers, DLS proof, cache partitioning
│
├── schemas/
│   ├── chunk_metadata.json          ← 14-field metadata schema for legal chunks
│   ├── opensearch_index_mapping.json← HNSW config, SQ8 quantization, DLS, search pipeline
│   └── rbac_roles.json              ← 4 tiers × 6 roles + mathematical leakage proof
│
├── prompts/
│   ├── grader_prompt.txt            ← Retrieval grader system prompt + few-shot examples
│   ├── generator_system_prompt.txt  ← Answer generation prompt (citation format enforced)
│   ├── hyde_prompt.txt              ← HyDE hypothetical passage generation
│   └── decomposition_prompt.txt     ← Complex query decomposition
│
├── eval/
│   └── metrics_matrix.md            ← Ragas + DeepEval thresholds, CI/CD gate logic
│
├── notes/
│   └── assumptions.md               ← All 18 explicit design assumptions (A1–A18)
│
└── tools_and_technologies.txt       ← Full inventory: versions, purposes, justifications
```

---

## Quick navigation by assessment module

| Module | Question | Primary section | Supporting artifact |
|---|---|---|---|
| **Module 1** | Chunking strategy, metadata preservation | [§1.1–1.4](drafts/final_submission.md#L222) | [module1_ingestion.py](pseudocode/module1_ingestion.py) |
| **Module 1** | Vector DB selection, HNSW params, OOM prevention | [§1.5–1.6](drafts/final_submission.md#L332) | [opensearch_index_mapping.json](schemas/opensearch_index_mapping.json) |
| **Module 2** | Hybrid BM25 + kNN, RRF vs alpha blending | [§2.2–2.3](drafts/final_submission.md#L442) | [module2_retrieval.py](pseudocode/module2_retrieval.py) |
| **Module 2** | Reranker selection, Top-K cascade | [§2.4–2.5](drafts/final_submission.md#L498) | [retrieval_flow.md](diagrams/retrieval_flow.md) |
| **Module 3** | HyDE + query decomposition | [§3.3–3.4](drafts/final_submission.md#L629) | [hyde_prompt.txt](prompts/hyde_prompt.txt) |
| **Module 3** | LangGraph state machine design | [§3.5](drafts/final_submission.md#L663) | [module3_crag_statemachine.py](pseudocode/module3_crag_statemachine.py) |
| **Module 3** | Retrieval grader + fallback decision table | [§3.6–3.7](drafts/final_submission.md#L781) | [module3_grader.py](pseudocode/module3_grader.py) |
| **Module 4** | Semantic cache + 0.97 threshold justification | [§4.1–4.2](drafts/final_submission.md#L977) | [module4_cache.py](pseudocode/module4_cache.py) |
| **Module 4** | RBAC + pre-retrieval mathematical proof | [§4.6–4.7](drafts/final_submission.md#L1050) | [rbac_roles.json](schemas/rbac_roles.json) |
| **Module 4** | CI/CD gates + Faithfulness / Context Precision | [§4.9–4.10](drafts/final_submission.md#L1138) | [metrics_matrix.md](eval/metrics_matrix.md) |

---

## Ten decisions at a glance

1. **OpenSearch 2.15+** — unified dense + sparse + DLS in one self-hosted system
2. **Structure-aware chunking** — splits on Dutch legal boundaries (Artikel/Lid), never mid-article
3. **HNSW m=16, ef_construction=256, ef_search=128** + SQ8 quantization → ~60 GB for 20M chunks
4. **RRF (k=60)**, not alpha blending — rank-based, robust to BM25/cosine score distribution mismatch
5. **BAAI/bge-reranker-v2-m3** — multilingual cross-encoder, self-hosted, top-20+20 → 40 → 8
6. **LangGraph 9-state CRAG** — grading gate + citation validation gate + explicit REFUSE state
7. **MAX_RETRIES = 1** — one retry adds ~580 ms; two retries exceed the 1500 ms TTFT cap
8. **Semantic cache threshold ≥ 0.97** — "Box 1 2024" vs "Box 1 2023" score 0.94; 0.97 blocks it
9. **Pre-retrieval DLS** — post-filter leaks via result count: P(leak) = 1 − 0.95^40 ≈ 0.87
10. **Ragas + DeepEval CI/CD gate** — Faithfulness ≥ 0.90, Context Precision@8 ≥ 0.85, Citation Accuracy = 1.0
