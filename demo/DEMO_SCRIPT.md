# Demo Script — Belastingdienst KennisAssistent

> **Voor de presentator, niet voor de assessor.** De onderbouwingsvragen worden beantwoord met het deck (zie [`slides/operations_justification.md`](../slides/operations_justification.md) en [`slides/output/operations_justification.pptx`](../slides/output/operations_justification.pptx)); dit document houdt jou op het juiste tabblad.
>
> **Doel:** ~9 minuten live demonstratie op je eigen laptop die aan Tim's vier criteria voldoet — werkend, wezenlijk, onderbouwd, live.

---

## T-10 min pre-flight checklist

- [ ] Laptop aan netstroom, batterij >60%.
- [ ] Slack / Teams / zware apps gesloten (memory budget ~8 GB).
- [ ] Externe monitor **mirrored**, niet extended.
- [ ] `docker compose down -v && docker compose up -d --build` voor schone state. (Anders: alleen `up -d`.)
- [ ] Wacht tot alle 6 splash-stages groen zijn (~30-60s, plus model-pull bij eerste run).
- [ ] `curl localhost:8000/health` → `"warmup_complete":true`.
- [ ] Hard refresh (Ctrl+Shift+R) zodat asset-versies `?v=16` actief zijn.
- [ ] **Pre-warm:** stel deze 4 throwaway-queries voor de demo zodat het model warm is en de cache vol staat:
  - "Wat is de arbeidskorting in 2024?"
  - "ECLI:NL:HR:2021:1523"
  - "arbeidskorting" *(triggert HyDE)*
  - "Ik ben ZZP'er met thuiskantoor wat aftrekken en hoe BTW?" *(triggert decompose)*
- [ ] **Run Ragas:** klik "Run" op Operations → Kwaliteit en wacht tot het klaar is (~1-3 min). Noteer wat de cijfers zijn.
- [ ] Browser op 100% zoom, incognito (geen extensies).
- [ ] WiFi uit op het moment van presenteren — bewijst on-device.

---

## Sidebar-routekaart

```
WERKRUIMTE          (= eindgebruikers-product)
  · Gesprek         (#chat)         sneltoets 1
  · Documenten      (#documents)    sneltoets 2

OPERATIONS          (= operator-tools)
  · Ingestie        (#ingest)       sneltoets 3
  · Retrieval       (#retrieval)    sneltoets 4
  · CRAG-pipeline   (#crag)         sneltoets 5
  · Toegang         (#security)     sneltoets 6
  · Kwaliteit       (#eval)         sneltoets 7
```

Rol-switch zit linksboven: Publiek · Juridisch medewerker · Inspecteur · FIOD-rechercheur.

---

## 8 acts, ~70 sec elk

### Act 1 — Cache-hit + TTFT bewijs (0:00 – 1:00)

**Trigger:** sneltoets `1`, klik de eerste suggested prompt — *"Wat is de arbeidskorting in 2024?"*. Deze zit al in cache (uit pre-warm), dus de TTFT pill verschijnt op groen.

**Wat er gebeurt:**
- Bovenaan de bubble verschijnt **TTFT XX ms · drempel 1500 ms · via cache** (groen).
- Antwoord verschijnt vrijwel instant.
- Pipeline-trace: `cache_lookup → HIT`.

**Talking point (1 zin):** *"Het TTFT-budget uit de assessment is 1500 ms. Cache-hit zit hier op enkele tientallen milliseconden — semantisch gematcht via 384-dim e5-small embeddings boven cosine 0.97."*

---

### Act 2 — Live generatie + HyDE (1:00 – 2:30)

**Trigger:** typ in chat: *"arbeidskorting"* (terse query — triggert HyDE).

**Wat er gebeurt:**
- Pipeline-trace toont expliciet `🎭 HyDE hypothese-passage` met de hypothetische passage als preview.
- Tokens streamen.
- TTFT pill verschijnt (warm cache: amber/groen, koude eerste call: rood — gebruik dat als talking-point).

**Talking point:** *"Bij korte queries faalt vector-search vaak omdat het query-embedding ver staat van de document-vocabulaire. HyDE laat de LLM eerst een hypothetisch antwoord genereren, embedt dat, en gebruikt die vector voor kNN. Dit is een live optimalisatie van de retrieval-recall."*

---

### Act 3 — Query decompositie + parallel retrieval (2:30 – 4:00)

**Trigger:** typ: *"Ik ben ZZP'er met een thuiskantoor — wat kan ik aftrekken en hoe zit het met BTW?"*

**Wat er gebeurt:**
- Pipeline-trace toont `🪓 Vraag splitsen` met 2-3 sub-queries als detail.
- Retrieve trace zegt expliciet `tier=PUBLIC · sub-RRF merged`.
- Antwoord raakt zowel werkruimte-aftrek als BTW-plicht.

**Talking point:** *"Voor multi-aspect vragen wordt de query gesplitst in onafhankelijke sub-vragen, parallel opgehaald, en gemerged via RRF over de sub-resultaten. Dat voorkomt dat één sterk-scorend chunk over één aspect het andere aspect verdringt."*

---

### Act 4 — Live ingestie + hiërarchie + retrieval-highlight (4:00 – 5:30)

**Trigger:** sleep een PDF/TXT naar de **Ingestie-stream** sidebar in Gesprek (of klik "+ Upload"). Suggestie: gebruik [`demo/seed_data/pdfs/wet_ib_2001_hfd4_arbeidskorting_uitgebreid.txt`](seed_data/pdfs/wet_ib_2001_hfd4_arbeidskorting_uitgebreid.txt) — toont 15 boundaries.

**Wat er gebeurt:**
- Per chunk verschijnt een kaart: `chunk_id`, hierarchy_path, topic, entities, ✓ geïndexeerd.

**Vervolg:** sneltoets `3` (Operations → Ingestie). Kies in dropdown het zojuist geüploade document.
- Hiërarchische tree opent: Hoofdstuk → Artikel → Lid → Sub.
- Onder de tree: **Vector quantization-widget** — 4 kaarten (fp32 / fp16 / int8 / pq8) met huidig corpus + projectie naar 20M chunks.

**Vervolg 2:** spring terug naar Gesprek (`1`), stel een vraag over het zojuist ingelezen artikel. Spring weer naar Ingestie (`3`): tree-nodes pulsen blauw (retrieved), groen (relevant), 🎯 oranje (cited).

**Talking point:** *"Tim's letterlijke punt — metadata voor hiërarchische relaties — gebouwd in 30 seconden. De boom is niet decoratief: parent-expansion fired automatisch wanneer een paragraaf wordt geciteerd. En de kwantisering-widget rechts toont waarom we 384-dim e5-small kozen — bij 20M chunks past int8-quantization in 14GB i.p.v. 56GB."*

---

### Act 5 — Tier-switch en pre-retrieval RBAC (5:30 – 6:45)

**Trigger:** sta op **Publiek** linksboven. Vraag: *"Welke FIOD-opsporingsbevoegdheden bestaan er?"*.

**Wat er gebeurt:**
- Refuse-bubble verschijnt met **amber border + 🛡 "Gefilterd antwoord"** label.
- Tekst noemt expliciet de tier (`PUBLIC`) en geeft constructieve vervolgstappen.

**Trigger 2:** klik **FIOD-rechercheur**. Stel exact dezelfde vraag.
- Antwoord komt nu wel met FIOD-citaten.

**Vervolg:** sneltoets `6` (Operations → Toegang). Wissel rollen — de "Zichtbaar voor jou / Niet toegankelijk"-pills verspringen live.
- Onderaan: **Audit-trail tabel** toont laatste queries met tier, grade, TTFT.

**Talking point:** *"Het tier-filter zit vóór de scoring, niet erna. Een geclassificeerd document kan letterlijk geen ranking-signaal geven — niet via TF-IDF, niet via vector-buurschap, niet via cache. De audit-trail logt elke query met grading-uitkomst en TTFT, retentie 7 dagen — vereist voor productie."*

---

### Act 6 — Adversarial refuse + CRAG fail-closed (6:45 – 7:45)

**Trigger:** typ: *"Who built the Eiffel Tower?"*

**Wat er gebeurt:**
- Retrieval levert geen relevante chunks → grader IRRELEVANT → CRAG slaat naar `refuse`.
- Amber refuse-bubble met "Gefilterd antwoord" label.

**Spring naar CRAG (`5`):** kies de zojuist gefailede turn in de selector. Diagram licht het pad op, eindigend op de rode `refuse`-state.

**Talking point:** *"Voor een belastingautoriteit is een fout antwoord duurder dan een eerlijk 'ik weet het niet'. CRAG's refuse-pad is daar het hardcore antwoord op: geen geverifieerde context, geen antwoord — gegenereerd uit de pipeline, niet door een hard-coded blocklist."*

---

### Act 7 — Live Ragas-run / eval-gate (7:45 – 8:45)

**Trigger:** sneltoets `7` (Operations → Kwaliteit). Klik "Run" als de cijfers nog niet zichtbaar zijn (of toon de cijfers van de pre-warm-run).

**Wat er gebeurt:**
- 6 metric-cards: Faithfulness, Context Recall, Answer Relevancy, Hallucination, Bias, Toxicity — allemaal echte Ragas/DeepEval cijfers.
- Ship/Hold gate-pills: groen of rood per drempel.
- Footer: "Laatste run: <timestamp> · 25 queries · <duration>s · judge `ai/gemma4:E2B`".

**Talking point:** *"Dit is wat een CI/CD eval-gate is. Bij elke nieuwe model-versie of pipeline-wijziging draait deze test op de golden-set van 25 queries. De drempels zijn vooraf vastgelegd: faithfulness ≥ 0.90, context-recall ≥ 0.85, hallucination ≤ 0.10. Falt een metric, dan blokkeert de PR-merge in productie."*

**Eerlijkheid:** de metrics op een laptop-Gemma als judge zijn lager dan met GPT-4 als external judge. *Vertel dat zelf voordat Tim er om vraagt.*

---

### Act 8 — Onderbouwingsslides + reliability (8:45 – 9:30)

**Trigger:** open `slides/output/operations_justification.pptx` op tweede scherm.

Per Operations-tab is er één slide met **Keuze · Afgewezen · Trade-off**. Laat Tim kiezen welke hij wil onderbouwd zien — open de bijbehorende slide.

**Optionele closer:** laat de circuit-breaker zien als Tim doorvraagt over reliability:
- *"De circuit-breaker rond Model Runner gaat OPEN na 3 failures binnen 30s, met 20s cooldown voor automatisch herstel. Dat voorkomt dat een gestresste backend de hele frontend stuk maakt — productie-pattern."*

---

## Recovery inventory

| Probleem | Actie | Talking point |
|---|---|---|
| Eerste generatie voelt langzaam | Wacht; na 8s verschijnt hint vanzelf | *"eerste model-warmup is eenmalig; cache vangt herhalingen op"* |
| Upload faalt mid-demo | Naar Werkruimte → Documenten, klik bestaand seed-doc | *"dit is een eerder geïngest document — zelfde flow"* |
| WiFi per ongeluk aan | Schakel uit | *"dit is juist het punt — data blijft on-device"* |
| API antwoord lijkt corrupt | `docker compose restart api` | *"5 sec — pipeline onveranderd"* |
| Ragas-run is nog niet gerund | Klik "Run", praat ondertussen door bij Act 5/6 | *"draait op de achtergrond — dit is de live workload"* |
| Ragas-getallen zijn onverwacht laag | Open slide, leg framing uit | *"strenge drempels gekozen; lage waarde is informatie, geen falen"* |
| Circuit-breaker triggered tijdens demo (zou niet moeten) | Wacht 20s, breaker resette automatisch | *"productie-pattern werkt — dit is precies waarom hij er is"* |
| OpenSearch yellow → red | `docker compose logs opensearch`; re-index zo nodig | *"niet ideaal, wel recoverable"* |
| UI toont oude content | Hard refresh (Ctrl+Shift+R) | *"browser-cache; assets zijn versie-gebonden via `?v=16`"* |
| Decompose splitst niet (1 sub-query) | Geen probleem — fallback naar single-query path | *"de classifier is conservatief — niet alles wordt geforceerd gesplitst"* |

---

## Wanneer Tim doorvraagt op het waarom

Open je deck. Per Operations-tab is er één slide. **Praat daar uit, niet uit de UI** — de UI is bewust gestript van uitleg.

Belangrijke ondersteunende artefacten:
- [`slides/operations_justification.md`](../slides/operations_justification.md) — bron-tekst per slide
- [`drafts/final_submission_v2.md`](../drafts/final_submission_v2.md) — productie-architectuur (met afwijkings-banner)
- [`SENIOR_REVIEW_AND_PLAN.md`](../SENIOR_REVIEW_AND_PLAN.md) — eigen meta-review op deze inzending

---

## Post-demo

- [ ] Screen-record (achteraf, of vooraf als backup) van een succesvolle run.
- [ ] Deck open op tweede scherm tijdens Q&A.
- [ ] [TIM_FEEDBACK.md](../TIM_FEEDBACK.md), [SENIOR_REVIEW_AND_PLAN.md](../SENIOR_REVIEW_AND_PLAN.md), [EXECUTION_PLAN.md](../EXECUTION_PLAN.md) klaar om te delen op aanvraag.
