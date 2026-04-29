# Demo Script — Belastingdienst KennisAssistent

> **For the presenter, not the assessor.** Justification questions are answered from the markdown source [`slides/operations_justification.md`](../slides/operations_justification.md) (render to .pptx via `python slides/build_slides.py` if a deck is needed); this document keeps you on the right tab.
>
> **Goal:** ~9 minutes of live demonstration on your own laptop that meets the four assessment criteria — working, substantive, justified, live.
>
> **Note on language:** the UI is Dutch (this is a Dutch civil-service tool). The presenter may switch to Dutch when pointing at NL UI elements; English when explaining architecture to the assessor.

---

## T-10 min pre-flight checklist

- [ ] Laptop on AC power, battery > 60%.
- [ ] Slack / Teams / heavy apps closed (memory budget ~8 GB).
- [ ] External monitor **mirrored**, not extended.
- [ ] `docker compose down -v && docker compose up -d --build` for clean state. (Otherwise just `up -d`.)
- [ ] Wait until all 6 splash stages are green (~30–60s, plus model pull on first run).
- [ ] `curl localhost:8000/readyz` → `"ready":true`.
- [ ] Hard refresh (Ctrl+Shift+R) so asset versions `?v=19` are active.
- [ ] **Pre-warm:** run these 4 throwaway queries before the demo so the model is warm and the cache is populated:
  - "Wat is de arbeidskorting in 2024?"
  - "ECLI:NL:HR:2021:1523"
  - "arbeidskorting" *(triggers HyDE)*
  - "Ik ben ZZP'er met thuiskantoor — wat aftrekken en hoe BTW?" *(triggers decompose)*
- [ ] **Run Ragas:** click "Run" on Operations → Kwaliteit and wait until done (~1–3 min). Note the numbers.
- [ ] Browser at 100% zoom, incognito (no extensions).
- [ ] Wi-Fi off when presenting — proves on-device.

---

## Sidebar map

```
WERKRUIMTE          (= end-user product)
  · Gesprek         (#chat)         shortcut 1
  · Documenten      (#documents)    shortcut 2

OPERATIONS          (= operator tools)
  · Ingestie        (#ingest)       shortcut 3
  · Retrieval       (#retrieval)    shortcut 4
  · CRAG-pipeline   (#crag)         shortcut 5
  · Toegang         (#security)     shortcut 6
  · Kwaliteit       (#eval)         shortcut 7
```

Role switch sits in the top-left: Publiek · Juridisch medewerker · Inspecteur · FIOD-rechercheur.

---

## 8 acts, ~70 sec each

### Act 1 — Cache hit + TTFT proof (0:00 – 1:00)

**Trigger:** shortcut `1`, click the first suggested prompt — *"Wat is de arbeidskorting in 2024?"*. Already in cache from pre-warm, so the TTFT pill turns green.

**What happens:**
- Above the bubble: **TTFT XX ms · drempel 1500 ms · via cache** (green).
- Answer appears almost instantly.
- Pipeline trace: `cache_lookup → HIT`.

**Talking point (1 sentence):** *"The TTFT budget from the assessment is 1500 ms. Cache hit lands here at tens of milliseconds — semantically matched via 384-dim e5-small embeddings above cosine 0.97."*

---

### Act 2 — Live generation + HyDE (1:00 – 2:30)

**Trigger:** type in chat: *"arbeidskorting"* (terse query — triggers HyDE).

**What happens:**
- Pipeline trace explicitly shows `🎭 HyDE hypothesis passage` with the hypothetical passage as preview.
- Tokens stream in.
- TTFT pill appears (warm cache: amber/green, cold first call: red — use as a talking point).

**Talking point:** *"Vector search often fails on terse queries because the query embedding sits far from document vocabulary. HyDE has the LLM generate a hypothetical answer first, embeds it, and uses that vector for kNN. This is a live optimization of retrieval recall."*

---

### Act 3 — Query decomposition + parallel retrieval (2:30 – 4:00)

**Trigger:** type: *"Ik ben ZZP'er met een thuiskantoor — wat kan ik aftrekken en hoe zit het met BTW?"*

**What happens:**
- Pipeline trace shows `🪓 Split query` with 2–3 sub-queries as detail.
- Retrieve trace says explicitly `tier=PUBLIC · sub-RRF merged`.
- Answer covers both home-office deduction and VAT obligation.

**Talking point:** *"For multi-aspect questions, the query is split into independent sub-questions, retrieved in parallel, and merged via RRF over the sub-results. That prevents one strong-scoring chunk on one aspect from drowning out the other aspect."*

---

### Act 4 — Live ingestion + hierarchy + retrieval-highlight (4:00 – 5:30)

**Trigger:** drop a PDF/TXT into the **Ingestion stream** sidebar in Gesprek (or click "+ Upload"). Suggested: [`demo/seed_data/pdfs/wet_ib_2001_hfd4_arbeidskorting_uitgebreid.txt`](seed_data/pdfs/wet_ib_2001_hfd4_arbeidskorting_uitgebreid.txt) — produces 15 boundaries.

**What happens:**
- Per chunk a card appears: `chunk_id`, hierarchy_path, topic, entities, ✓ indexed.

**Follow-up:** shortcut `3` (Operations → Ingestie). In the dropdown, pick the just-uploaded document.
- Hierarchical tree opens: Hoofdstuk → Artikel → Lid → Sub.
- Below the tree: **Vector quantization widget** — 4 cards (fp32 / fp16 / int8 / pq8) with current corpus + projection to 20M chunks.

**Follow-up 2:** jump back to Gesprek (`1`), ask a question about the freshly ingested article. Jump back to Ingestie (`3`): tree nodes pulse blue (retrieved), green (relevant), 🎯 orange (cited).

**Talking point:** *"The assessment is explicit: recursive text splitters destroy the hierarchical context of legal documents. Metadata for hierarchical relations — built in 30 seconds. The tree isn't decorative: parent-expansion fires automatically when a paragraph is cited. And the quantization widget on the right shows why we chose 384-dim e5-small — at 20M chunks, int8 quantization fits in 14GB instead of 56GB."*

---

### Act 5 — Tier switch and pre-retrieval RBAC (5:30 – 6:45)

**Trigger:** stay on **Publiek** in the top-left. Ask: *"Welke FIOD-opsporingsbevoegdheden bestaan er?"*.

**What happens:**
- Refuse bubble appears with **amber border + 🛡 "Filtered answer" label**.
- Text explicitly names the tier (`PUBLIC`) and gives constructive next steps.
- New: refuse-category badge says **TIER_GAP** ("Tier-blokkade"), and indicates which higher tier holds the relevant content.

**Trigger 2:** click **FIOD-rechercheur**. Ask the exact same question.
- Answer now comes through with FIOD citations.

**Follow-up:** shortcut `6` (Operations → Toegang). Switch roles — the "Visible to you / Not accessible" pills flip live.
- At the bottom: **Audit trail table** shows recent queries with tier, grade, TTFT.

**Talking point:** *"The tier filter sits before scoring, not after. A classified document literally cannot give a ranking signal — not via TF-IDF, not via vector neighborhood, not via cache. The audit trail logs every query with grading verdict and TTFT, retention 7 days — required for production."*

---

### Act 6 — Adversarial refuse + CRAG fail-closed (6:45 – 7:45)

**Trigger:** type: *"Who built the Eiffel Tower?"*

**What happens:**
- Retrieval returns no relevant chunks → grader IRRELEVANT → CRAG falls into `refuse`.
- Amber refuse bubble with refuse-category badge **CORPUS_GAP** ("Corpus-gat") — explicitly says *"the corpus contains no documents on this topic"*. Distinct from the TIER_GAP case in Act 5.

**Jump to CRAG (`5`):** select the just-failed turn in the selector. The diagram lights up the path, ending on the red `refuse` state.

**Talking point:** *"For a tax authority, a wrong answer is more expensive than an honest 'I don't know'. CRAG's refuse path is the hardcore answer to that: no verified context, no answer — generated by the pipeline, not a hard-coded blocklist. And the new refuse classifier tells the user **why** — corpus gap, tier block, or semantic mismatch — three different remediations."*

---

### Act 7 — Live Ragas run / eval gate (7:45 – 8:45)

**Trigger:** shortcut `7` (Operations → Kwaliteit). Click "Run" if numbers aren't visible yet (or display the numbers from the pre-warm run).

**What happens:**
- 6 metric cards: Faithfulness, Context Recall, Answer Relevancy, Hallucination, Bias, Toxicity — all real Ragas/DeepEval numbers.
- Ship/Hold gate pills: green or red per threshold.
- Footer: "Last run: <timestamp> · 25 queries · <duration>s · judge `ai/gemma4:E2B`".

**Talking point:** *"This is what a CI/CD eval gate is. On every new model version or pipeline change, this test runs against the golden set of 25 queries. Thresholds are pre-set: faithfulness ≥ 0.90, context recall ≥ 0.85, hallucination ≤ 0.10. If a metric fails, the PR merge is blocked in production."*

**Honesty:** metrics with a laptop Gemma as judge are lower than with GPT-4 as external judge. *Volunteer this before being asked.*

---

### Act 8 — Justification slides + reliability (8:45 – 9:30)

**Trigger:** open the source notes [`slides/operations_justification.md`](../slides/operations_justification.md) on a second screen (or render to .pptx beforehand via `python slides/build_slides.py`).

Per Operations tab there is one slide with **Choice · Rejected · Trade-off**. Let the assessor pick which one to justify — open the matching slide.

**Optional closer:** show the circuit breaker if the assessor probes on reliability:
- *"The circuit breaker around Model Runner trips OPEN after 3 failures within 30s, with a 20s cooldown for automatic recovery. That prevents a stressed backend from breaking the whole frontend — production pattern."*

---

## Recovery inventory

| Problem | Action | Talking point |
|---|---|---|
| First generation feels slow | Wait; after 8s the warmup hint appears | *"first model warmup is one-off; cache catches repeats"* |
| Upload fails mid-demo | Go to Werkruimte → Documenten, click an existing seed doc | *"this is a previously ingested document — same flow"* |
| Wi-Fi accidentally on | Turn it off | *"that's exactly the point — data stays on-device"* |
| API response looks corrupt | `docker compose restart api` | *"5 seconds — pipeline unchanged"* |
| Ragas run hasn't run yet | Click "Run", talk through Acts 5/6 in the meantime | *"running in the background — this is the live workload"* |
| Ragas numbers unexpectedly low | Open slide, frame the result | *"strict thresholds chosen; a low number is information, not failure"* |
| Circuit breaker triggered during demo (shouldn't) | Wait 20s, breaker resets automatically | *"production pattern works — exactly why it's there"* |
| OpenSearch yellow → red | `docker compose logs opensearch`; reindex if needed | *"not ideal, but recoverable"* |
| UI shows stale content | Hard refresh (Ctrl+Shift+R) | *"browser cache; assets are version-pinned via `?v=19`"* |
| Decompose doesn't split (1 sub-query) | Not a problem — falls back to single-query path | *"the classifier is conservative — it doesn't force-split everything"* |

---

## When the assessor probes on the why

Open your deck. Per Operations tab there is one slide. **Talk from there, not from the UI** — the UI is intentionally stripped of explanatory text.

Key supporting artefacts:
- [`slides/operations_justification.md`](../slides/operations_justification.md) — source text per slide
- [`drafts/final_submission_v2.md`](../drafts/final_submission_v2.md) — production architecture (with deviations banner)
- [`SENIOR_REVIEW_AND_PLAN.md`](../SENIOR_REVIEW_AND_PLAN.md) — own meta-review of this submission (Dutch internal notes)

---

## Post-demo

- [ ] Screen recording (afterwards, or recorded in advance as backup) of a successful run.
- [ ] Deck open on a second screen during Q&A.
- [ ] [ASSESSMENT_REVIEW_FEEDBACK.md](../ASSESSMENT_REVIEW_FEEDBACK.md), [SENIOR_REVIEW_AND_PLAN.md](../SENIOR_REVIEW_AND_PLAN.md), [EXECUTION_PLAN.md](../EXECUTION_PLAN.md) ready to share on request.
