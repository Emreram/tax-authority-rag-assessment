# Demo Script — Belastingdienst KennisAssistent

> **Voor de presentator, niet voor de assessor.** Tim's onderbouwingsvragen worden beantwoord met het deck (zie `slides/operations_justification.md`); dit document houdt jou op het juiste tabblad.
>
> **Doel:** ~8 minuten live demonstratie op je eigen laptop die aan Tim's vier criteria voldoet — werkend, wezenlijk, onderbouwd, live.

---

## T-10 min pre-flight checklist

- [ ] Laptop aan netstroom, batterij >60%.
- [ ] Slack / Teams / zware apps gesloten (memory budget ~8 GB).
- [ ] Externe monitor **mirrored**, niet extended (tab-switches gaan mis op extended).
- [ ] `docker compose up -d` in `demo/` — laat 30s warmup gebeuren.
- [ ] Open http://localhost:8000 — wacht tot de splash verdwijnt.
- [ ] `curl localhost:8000/health` → `"warmup_complete":true`.
- [ ] Hard refresh (Ctrl+Shift+R) zodat asset-versies `?v=15` actief zijn.
- [ ] Stel één throwaway-vraag voor de demo zodat het model warm is en de eerste echte query niet 60s duurt.
- [ ] Browser op 100% zoom, incognito (geen extensies).
- [ ] WiFi uit op het moment van presenteren — bewijst on-device.

---

## Sidebar-routekaart (ken deze blind)

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

## 6 acts, ~80 sec elk

### Act 1 — Opening: chat als product (0:00 – 1:20)

**Trigger:** sneltoets `1` of klik **Werkruimte → Gesprek**. Klik de eerste suggested prompt, of typ: *"Wat is de arbeidskorting in 2024?"*.

**Wat er gebeurt:** SSE streamt tokens live. Onder het bubble verschijnt de progress-strip die door `Voorbereiden → Retrieval → Grading → Generatie → Validatie` loopt, eindigend met groene check. Citation pills onder het antwoord linken naar de exacte chunk.

**Talking point (1 zin):** "Dit is de productkant — een gebruiker bij de Belastingdienst stelt een vraag, ziet het antwoord groeien terwijl het wordt gevormd, en kan elke claim terugvolgen naar het brondocument."

**Recovery:** als de eerste generation > 15s voelt, na 8s verschijnt automatisch de hint "eerste query is traag op CPU; daarna <200ms via cache". Lees die voor.

---

### Act 2 — Follow-up met conversation memory (1:20 – 2:30)

**Trigger:** zonder iets te wissen, typ: *"En voor zelfstandigen?"*.

**Wat er gebeurt:** de classifier herkent een follow-up; de query wordt herschreven tegen de vorige turn ("Wat is de arbeidskorting in 2024 voor zelfstandigen?") voordat retrieval opnieuw fired.

**Talking point:** "Korte vervolgvragen werken zoals je verwacht — het systeem onthoudt de context van de laatste zes turns in Redis en herschrijft kortgesloten queries voordat het opnieuw zoekt."

**Recovery:** als de rewrite niet goed valt, zeg: "in productie zou hier ook query-decompositie spelen — die is in de pipeline aanwezig maar staat hier op `alleen COMPLEX`."

---

### Act 3 — Live upload, chunking en hiërarchische tree (2:30 – 4:30)

Dit is de kern van Tim's *"chunking pipeline + metadata voor hiërarchische relaties"*. Drie sub-stappen, geen pauze ertussen.

**3a — Upload.** Sleep een PDF naar de **Ingestie-stream**-sidebar rechts in Gesprek (of klik "+ Upload"). De ingest-status loopt mee: parser → structurele markers → chunker → AI-metadata → embeddings → indexering. Per chunk verschijnt een kaart met een gekleurde `doc::artikel::lid::seq`-id en pills voor topic / entity / 384-dim / ✓ geïndexeerd.

**Talking point:** "Hier zie je 'chunken door AI' letterlijk: regex op juridische structuur waar die voorspelbaar is, AI op uitspraken en beleidsmemo's waar die niet voorspelbaar is."

**3b — Hiërarchie.** Sneltoets `3` of klik **Operations → Ingestie**. Kies in de selector het zojuist geüploade document. De boom klapt open — Hoofdstuk → Artikel → Lid → Sub.

**Talking point:** "Dit is Tim's letterlijke vraag: metadata voor hiërarchische relaties. Elk chunk weet wie zijn parent is, en die kennis zit in het indexrecord, niet afgeleid uit de tekst."

**3c — Retrieval lichten op.** Spring terug naar **Werkruimte → Gesprek** (`1`), stel een vraag over het zojuist geüploade artikel. Spring na het antwoord weer naar **Ingestie**: de tree-nodes pulsen blauw waar retrieval ze raakte, krijgen groen waar de grader ze RELEVANT vond, en oranje 🎯 waar de generator ze citeerde. Als parent-expansion fired, krijgt de Artikel-node een oranje pulse met label `added as parent context`.

**Talking point:** "De boom is niet decoratief — wanneer een paragraaf-chunk wordt geciteerd, haalt de retriever automatisch het bovenliggende artikel erbij voor context. Dat is wat hiërarchische metadata waardevol maakt."

**Recovery:** als upload mid-demo faalt, schakel naar **Werkruimte → Documenten**, kies een seed-document, en demonstreer 3b en 3c daarop ("dit is een document dat we eerder geïndexeerd hebben").

---

### Act 4 — Tier-switch en RBAC (4:30 – 5:45)

**Trigger:** sta op **Publiek** in de role-selector linksboven. Vraag: *"Welke bevoegdheden heeft de FIOD bij huiszoeking?"*.

**Wat er gebeurt:** retrieval vindt geen toegankelijke FIOD-chunks → grade IRRELEVANT → nette refuse, géén FIOD-citaten zichtbaar.

**Trigger 2:** klik **FIOD-rechercheur**. Stel exact dezelfde vraag.

**Wat er gebeurt:** zelfde query, andere `terms`-filter → FIOD-chunks zijn nu kandidaat, BM25+kNN scoren ze, antwoord citeert ze.

Spring naar **Operations → Toegang** (`6`). Wissel daar live tussen rollen — de "Zichtbaar voor jou" en "Niet toegankelijk" pills verspringen mee. Onder in het Cache-blok zie je entries met label `blocked` voor een hogere tier dan jouw rol.

**Talking point:** "De tier-filter zit vóór de scoring, niet erna. Dat betekent dat een geclassificeerd document letterlijk geen ranking-signaal kan geven aan een lagere tier — niet via TF-IDF, niet via vector-buurschap, niet via cache."

---

### Act 5 — Adversarial refuse (5:45 – 6:45)

**Trigger:** typ in Gesprek: *"Who built the Eiffel Tower?"*.

**Wat er gebeurt:** retrieval vindt 0 relevante chunks → grader geeft IRRELEVANT → CRAG slaat naar `refuse` → nette Nederlandstalige weigering, geen hallucinatie.

Spring naar **Operations → CRAG-pipeline** (`5`), kies de zojuist gefailede turn in de selector. Het diagram lijst af waar de pipeline doorheen liep, eindigend op de rode `refuse`-state.

**Talking point:** "Voor een belastingautoriteit is een fout antwoord duurder dan een eerlijk 'ik weet het niet'. Het CRAG-refuse-pad is het hardcore antwoord op die afweging: geen context, geen antwoord."

---

### Act 6 — Cache-hit re-ask (6:45 – 7:45)

**Trigger:** scroll terug en herhaal de eerste vraag (*"Wat is de arbeidskorting in 2024?"*) — of een paraphrase ervan.

**Wat er gebeurt:** progress-strip toont `Cache doorzoeken → HIT`, antwoord verschijnt in <200ms.

**Talking point:** "De cache-key is een embedding, geen hash. Drempel staat op cosine ≥ 0.97 — niet 0.95 (dan klontert *arbeidskorting* met *werknemerskorting*) en niet 0.99 (dan dekt-ie niets meer). En de cache is per tier gepartitioneerd, dus geen cross-tier leak."

---

## Recovery inventory

| Probleem | Actie | Talking point |
|---|---|---|
| Eerste generatie voelt langzaam | Wacht; na 8s verschijnt de hint vanzelf | "eerste model-warmup is eenmalig; cache vangt herhalingen op" |
| Upload faalt mid-demo | Naar Werkruimte → Documenten, klik seed-document | "dit is een eerder geïndexeerd document — zelfde flow" |
| WiFi per ongeluk aan | Schakel uit | "dit is juist het punt — data blijft on-device" |
| API antwoord lijkt corrupt | `docker compose restart api` | "5 sec — pipeline onveranderd" |
| LLM te traag op deze laptop | `LLM_MODEL=ai/qwen2.5:1.5b docker compose up -d api` | "één env var — zelfde product" |
| OpenSearch yellow → red | `docker compose logs opensearch`; re-index zo nodig | "niet ideaal, wel recoverable" |
| UI toont oude content | Hard refresh (Ctrl+Shift+R) | "browser-cache; assets zijn versie-gebonden via `?v=15`" |
| Metadata-modal toont leeg | Klik andere chunk; bulk werkt | "edge case op dit ene chunk; valt buiten happy path" |

---

## Wanneer Tim doorvraagt op het waarom

Open je deck (`slides/operations_justification.md` → `slides/output/operations_justification.pptx`). Per Operations-tab is er één slide met keuze, afgewezen alternatief en trade-off. **Praat daar uit, niet uit de UI** — de UI is bewust gestript van uitleg.

---

## Post-demo

- [ ] Screen-record van een succesvolle run als backup (vóór de demo, met cache koud → warm).
- [ ] Deck open op tweede scherm tijdens Q&A.
- [ ] [TIM_FEEDBACK.md](../TIM_FEEDBACK.md) en [SENIOR_LEVEL_PLAN.md](../SENIOR_LEVEL_PLAN.md) klaar om te delen op aanvraag.
