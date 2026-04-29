# Operations — Justification slides

Source document for the 5 justification slides that complete the deck.
Each section follows the same pattern so the deck stays visually consistent.
Run `python build_slides.py` from this directory to render this markdown into `output/operations_justification.pptx` if a slide deck is preferred over the source notes.

---

## Slide 1 — Ingestie

**Title:** Chunk along the structure the text already carries

**Bullets (max 3):**
- Choice: a structural regex chunker for legislation (Hoofdstuk · Afdeling · Artikel · Lid · Sub) with an AI-semantic fallback for ECLI rulings and policy memos; `parent_chunk_id` and `hierarchy_path` explicit on every index record.
- Rejected: pure recursive splitter (ignores legal conventions, breaks mid-Lid), pure LLM cuts (non-deterministic, expensive, not auditable for a regulator).
- Trade-off: maintaining two paths instead of one — gain is that 90% of the corpus is chunked deterministically and cacheably; only the non-legal 10% costs LLM calls.

**Speaker notes:**
Legal text follows strict conventions — ignoring those conventions is wasted energy. For legislation a regex chunker is faster, deterministic and auditable; for ECLI rulings and policy memos with no fixed structure the pipeline falls back to an AI chunker that proposes break points with a reason, cached on `sha256(doc)` so re-ingestion remains reproducible. The hierarchy itself is not a text-extraction artefact but lives explicitly on every index record — that makes parent-expansion in retrieval an O(1) lookup, and it makes citations verifiable down to the exact Lid.

**UI anchor:** Operations → Ingestie · live stream + tree view · click a chunk → metadata modal shows `parent_chunk_id`.

---

## Slide 2 — Retrieval

**Title:** Hybrid search because no single method covers all legal queries

**Bullets (max 3):**
- Choice: BM25 + kNN (e5-small, 384-dim, multilingual) fused via Reciprocal Rank Fusion `k=60`, optional LLM rerank on the top-20.
- Rejected: pure vector (misses exact article numbers — `art. 3.114` blends with `art. 3.115`), pure BM25 (misses paraphrase and synonyms), alpha blending (BM25 scores and cosine live in incompatible spaces).
- Trade-off: two retrieval paths = two indices in OpenSearch; in return RRF needs no score normalization and is stable under corpus changes.

**Speaker notes:**
A Tax Authority query mixes two kinds of precision. *"What is article 3.114, paragraph 2?"* needs an exact article reference — that's where BM25 wins. *"When may I not deduct household expenses?"* needs semantics — that's where vector wins. RRF only sees ranks, not scores, which lets you fuse two fundamentally different rankers without one signal drowning the other. The `k=60` is a widely-accepted default from the RRF paper; we did not derive it ourselves but did validate it on the golden set. LLM rerank on top-20 is optional because it adds ~700ms; for SIMPLE queries it isn't needed.

**UI anchor:** Operations → Retrieval · 4 rivers (BM25 / Vector / Fusion / Rerank) · live timings.

---

## Slide 3 — CRAG-pipeline

**Title:** Self-correcting retrieval — better silent than wrong

**Bullets (max 3):**
- Choice: imperative 9-state machine (cache → classify → retrieve → grade → optional rewrite/parent-expansion → generate → validate → respond/refuse), `MAX_RETRIES=1`, deterministic refuse paths on IRRELEVANT or citation-fail.
- Rejected: LangGraph (overkill for 9 states, hides the control-flow story behind framework magic), no-retry (too many ambiguous cases lost), unlimited retries (TTFT explosion on poorly worded queries).
- Trade-off: state transitions hand-maintained; in return we get full observability — every turn produces a trace that reads 1:1 in the UI.

**Speaker notes:**
For a Tax Authority *answering wrong* is more expensive than *not answering*. So before the generator there is a grader scoring each chunk, and the path only continues if at least one chunk is RELEVANT. On AMBIGUOUS the pipeline gets one retry with a rewritten query; on INVALID_CITATIONS it goes to refuse. We empirically set `MAX_RETRIES=1` against the golden set — a second retry doubles TTFT with no measurable recall gain. LangGraph was considered and consciously rejected: at 9 states a DAG framework loses you more than it gives, and imperative code is easier to audit for a regulator.

**UI anchor:** Operations → CRAG-pipeline · click a turn in the selector · diagram lights up the path + grader-verdict pill.

---

## Slide 4 — Toegang

**Title:** Pre-retrieval RBAC — information cannot leak through ranking

**Bullets (max 3):**
- Choice: 4-tier filter (Publiek · Juridisch · Inspecteur · FIOD) injected into the OpenSearch `bool.filter` clause before BM25 and kNN scoring; cache keys partitioned per tier.
- Rejected: post-filter (classified chunks would still influence IDF normalization and kNN neighborhood — a ranking signal leaks even if the chunk is hidden), full JWT auth (red herring for this assessment; the tier is passed per request).
- Trade-off: tier context must be explicit per request; no audit trail layer in this prototype — that belongs in a production deployment.

**Speaker notes:**
The difference between privacy and security lives in this detail: with a post-filter the mere existence of a classified chunk would already perturb TF-IDF statistics for public queries — less-frequent terms get higher weight, vector neighborhoods shift. That's a *ranking leak*: the user does not see the chunk but it does influence which other chunks come out on top. Placing the filter pre-scoring makes `P(leak) = 0` provable. The cache is partitioned by tier so the same semantic query from a Public user and a FIOD user yields different keys — no cross-tier hits possible.

**UI anchor:** Operations → Toegang · switch role in the top-left · "Visible to you / Not accessible" pills flip · cache entries on a foreign tier get a `blocked` label.

---

## Slide 5 — Kwaliteit

**Title:** CI/CD eval gate — no model promotion without proof

**Bullets (max 3):**
- Choice: golden set with Ragas (retrieval + generation quality) and DeepEval (safety) runs per build; explicit ship/hold gate per metric with pre-set thresholds.
- Rejected: hand evaluation (does not scale to millions of chunks; reviewer bias), unit tests only (misses semantic correctness — a regex test says nothing about whether the generated text is right).
- Trade-off: the golden set must be maintained on every legislative change; metrics are sensitive to false positives on paraphrase — we mitigate that with multiple accepted-answer formulations per gold item.

**Speaker notes:**
For a production RAG, evaluation is as important as the system itself — without it you don't know whether a new Gemma version or a different chunking scheme makes your answers better or worse. The ship/hold gate principle comes from classical CI: if context recall drops below 0.85 or citation precision below 0.90, the build fails and the model cannot be rolled out. The golden set itself is small (5 queries in the demo, 50+ in production) but covers the four query archetypes we distinguish in retrieval: SIMPLE, COMPLEX, ECLI lookup, and adversarial.

**UI anchor:** Operations → Kwaliteit · 4 metric cards · click "Run" · ship/hold pills give an immediate green/red verdict.
