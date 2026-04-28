# Operations — Onderbouwingsslides

Bron-document voor de 5 onderbouwingsslides die het deck completeren.
Elke sectie volgt hetzelfde stramien zodat het deck visueel consistent is.
`build_slides.py` parseert dit bestand en genereert `output/operations_justification.pptx`.

---

## Slide 1 — Ingestie

**Titel:** Chunken volgens de structuur die de tekst zelf draagt

**Bullets (max 3):**
- Keuze: structurele regex-chunker voor wetgeving (Hoofdstuk · Afdeling · Artikel · Lid · Sub) met AI-semantische fallback voor ECLI-uitspraken en beleidsmemo's; `parent_chunk_id` en `hierarchy_path` expliciet in elk indexrecord.
- Afgewezen: pure recursive splitter (negeert juridische conventies, breekt midden in een Lid), pure LLM-cuts (niet-deterministisch, duur, niet auditbaar voor een toezichthouder).
- Trade-off: twee paden onderhouden i.p.v. één — winst is dat 90% van het corpus deterministisch en cacheable wordt gechunkt; alleen de niet-juridische 10% kost LLM-calls.

**Spreker-notes:**
Juridische tekst volgt strikte conventies — die conventies negeren is energie weggooien. Voor wetgeving is een regex-chunker sneller, deterministisch en auditbaar; voor ECLI-uitspraken en beleid waar geen vaste structuur is, valt de pipeline terug op een AI-chunker die breukpunten voorstelt met reden, gecached op `sha256(doc)` zodat re-ingestie reproduceerbaar blijft. De hiërarchie zelf is geen tekst-extractie maar staat expliciet in elk indexrecord — dat maakt parent-expansion in retrieval een O(1)-lookup, en het maakt citaties verifieerbaar tot op het exacte Lid.

**UI-anker:** Operations → Ingestie · live-stream + boom-view · klik een chunk → metadata-modal toont `parent_chunk_id`.

---

## Slide 2 — Retrieval

**Titel:** Hybride zoek omdat geen enkele methode alle juridische queries dekt

**Bullets (max 3):**
- Keuze: BM25 + kNN (e5-small, 384-dim, multilingual) gefuseerd met Reciprocal Rank Fusion `k=60`, optionele LLM-rerank op de top-20.
- Afgewezen: pure vector (mist exacte artikelnummers — `art. 3.114` blendt met `art. 3.115`), pure BM25 (mist paraphrase en synoniemen), alpha-blending (BM25-scores en cosine leven in onverenigbare ruimtes).
- Trade-off: twee zoekpaden = twee indices in OpenSearch; in ruil heeft RRF geen score-normalisatie nodig en is het stabiel onder corpus-veranderingen.

**Spreker-notes:**
Een Belastingdienst-query mengt twee soorten precisie. *"Wat is artikel 3.114, lid 2?"* heeft een exacte artikelreferentie nodig — daar wint BM25. *"Wanneer mag ik geen huishoudelijke uitgaven aftrekken?"* heeft semantiek nodig — daar wint vector. RRF ziet alleen rangs, niet scores, en daarom kun je twee fundamenteel verschillende rankers fuseren zonder dat één signaal de ander overstemt. De `k=60` is een breed-geaccepteerde default uit de RRF-paper; we hebben hem niet zelf afgeleid maar wél gevalideerd op de golden set. LLM-rerank op top-20 is optioneel omdat hij ~700ms toevoegt; voor SIMPLE queries is hij niet nodig.

**UI-anker:** Operations → Retrieval · 4 rivers (BM25 / Vector / Fusion / Rerank) · live timings.

---

## Slide 3 — CRAG-pipeline

**Titel:** Zelf-corrigerende retrieval — liever zwijgen dan fout antwoorden

**Bullets (max 3):**
- Keuze: imperatieve 9-state machine (cache → classify → retrieve → grade → optionele rewrite/parent-expansion → generate → validate → respond/refuse), `MAX_RETRIES=1`, deterministische refuse-paden bij IRRELEVANT of citation-fail.
- Afgewezen: LangGraph (overkill voor 9 states, verbergt het control-flow-verhaal achter framework-magie), no-retry (te veel ambiguous cases gaan verloren), unlimited retries (TTFT-explosie bij slecht gestelde vragen).
- Trade-off: state-overgangen handmatig onderhouden; in ruil hebben we volledige observability — elke turn produceert een trace die in de UI 1-op-1 te lezen is.

**Spreker-notes:**
Voor een belastingautoriteit is *fout antwoorden* duurder dan *niet antwoorden*. Daarom zit er voor de generator een grader die elke chunk scoort, en wordt het pad alleen vervolgd als minstens één chunk RELEVANT is. Bij AMBIGUOUS krijgt de pipeline één tweede kans met een herschreven query; bij INVALID_CITATIONS gaat het naar refuse. We hebben `MAX_RETRIES=1` empirisch vastgesteld op de golden set — een tweede retry verdubbelt TTFT zonder meetbare recall-winst. LangGraph is overwogen en bewust verworpen: bij 9 states verliest een DAG-framework je meer dan het je geeft, en imperatieve code is voor een toezichthouder makkelijker te auditen.

**UI-anker:** Operations → CRAG-pipeline · klik een turn in de selector · diagram licht het pad op + grader-verdict-pill.

---

## Slide 4 — Toegang

**Titel:** Pre-retrieval RBAC — informatie kan niet lekken via ranking

**Bullets (max 3):**
- Keuze: 4-tier filter (Publiek · Juridisch · Inspecteur · FIOD) wordt geïnjecteerd in de OpenSearch `bool.filter`-clause vóór BM25- en kNN-scoring; cache-keys per tier gepartitioneerd.
- Afgewezen: post-filter (geclassificeerde chunks zouden nog steeds IDF-normalisatie en kNN-buurschap beïnvloeden — ranking-signaal lekt zelfs als het chunk verborgen wordt), volledige JWT-auth (red herring voor dit assessment; tier wordt nu per request meegegeven).
- Trade-off: tier-context moet per request expliciet zijn; geen audit-trail-laag in dit prototype — die hoort in een productie-deployment.

**Spreker-notes:**
Het verschil tussen privacy en security zit in dit detail: bij post-filter zou het loutere bestaan van een geclassificeerde chunk al de TF-IDF-statistieken voor publieke queries verstoren — minder vaak voorkomende termen worden zwaarder gewogen, vector-buurschappen verschuiven. Dat is een *ranking-leak*: de gebruiker ziet het chunk niet, maar het beïnvloedt wel welke andere chunks bovenaan komen. Door het filter pre-scoring te plaatsen, is `P(leak) = 0` aantoonbaar. De cache wordt op tier gepartitioneerd zodat dezelfde semantische query van een Publiek-gebruiker en een FIOD-gebruiker verschillende keys oplevert — geen cross-tier hits mogelijk.

**UI-anker:** Operations → Toegang · wissel rol linksboven · "Zichtbaar voor jou / Niet toegankelijk"-pills verspringen · cache-entries op vreemde tier krijgen `blocked`-label.

---

## Slide 5 — Kwaliteit

**Titel:** CI/CD eval-gate — geen model-promotie zonder bewijs

**Bullets (max 3):**
- Keuze: golden-set met Ragas (retrieval + generation kwaliteit) en DeepEval (safety) draait per build; expliciete ship/hold-gate per metric met vooraf vastgestelde drempels.
- Afgewezen: hand-eval (niet schaalbaar bij groei naar miljoenen chunks; reviewer-bias), enkel unit-tests (mist semantische correctheid — een regex-test zegt niets over of de gegenereerde tekst klopt).
- Trade-off: golden set moet onderhouden worden bij elke nieuwe wetswijziging; metrics zijn gevoelig voor false-positives bij paraphrase — we lossen dat op met meerdere accepted-answer-formuleringen per gold-item.

**Spreker-notes:**
Voor een productie-RAG is de evaluatie net zo belangrijk als het systeem zelf — anders weet je niet of een nieuwe Gemma-versie of een ander chunking-schema je antwoorden beter of slechter maakt. Het ship/hold-gate principe komt uit klassieke CI: als context-recall onder 0.85 zakt of citation-precision onder 0.90, faalt de build en kan het model niet uitgerold worden. De golden set zelf is klein (5 queries in de demo, 50+ in productie) maar dekt de vier query-archetypen die we in retrieval onderscheiden: SIMPLE, COMPLEX, ECLI-lookup, en adversarial.

**UI-anker:** Operations → Kwaliteit · 4 metric-cards · klik "Run" · ship/hold-pills geven directe groen/rood verdict.
