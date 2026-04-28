# Outdated / Dead-Weight Audit

Datum: 2026-04-28 — na de UI-stripdown, slides-creatie en DEMO_SCRIPT-rewrite.

De repo bevat drie chronologische lagen die door elkaar staan:

1. **v1 (Apr 10–13):** de oorspronkelijke "papieren" inzending — pseudocode, diagrammen, drafts, prompts, schemas, performance-analyse. Deze laag is gebouwd rond LangGraph + Mixtral + bge-reranker + Gemini API + 1024-dim e5-large + 20M chunks scale.
2. **v2 demo (Apr 13):** een eerste live Docker-prototype met Gemini API, Module-tags in de UI, Rondleiding-tour, Beslissingen-tabblad. Deze laag bestaat in commits maar is grotendeels overschreven.
3. **v3 huidig (Apr 17–28):** post-feedback refactor — Docker Model Runner met Gemma 4 / Qwen 2.5, 384-dim e5-small, Werkruimte/Operations-sidebar, geen Beslissingen-tab, geen Rondleiding, geen module-tags. De huidige `demo/`-stack draait hier op.

De v3 laag matcht de live UI; v1 en v2 verwijzen naar features en stack-keuzes die niet meer kloppen met wat Tim in de browser zal zien.

---

## 🔴 KRITIEK — actief misleidend, fix of verwijder vóór Tim

### 1. `requirements.txt` (root)
Claimt `langgraph`, `vllm`, `llama-index`, `bge-reranker`, `multilingual-e5-large`, `ragas`, `deepeval`, `python-jose`. **De demo gebruikt geen van deze.** Als Tim `pip install -r requirements.txt` doet voor reproductie, krijgt hij ~5 GB aan dependencies die niet matchen met `demo/requirements-demo.txt` (`opensearch-py`, `redis`, `sentence-transformers`, `openai`, `fastapi`, `pdfplumber`).
- **Actie:** vervang door één regel pointer (`# Zie demo/requirements-demo.txt voor de runtime-deps`) of verwijder helemaal.

### 2. `README.txt` (root)
- Bevat: *"cd demo && cp .env.example .env # add your Gemini API key"*. Er is **geen** Gemini API meer; alles draait op Docker Model Runner met `ai/gemma4:E2B`.
- Verwijst naar `demo video.mp4` als "ready to use environment example", maar die video toont de oude UI.
- Verwijst naar `assessment_AI_USE_emresemerci.pptx` (klopt) als hoofd-deck, maar mist `slides/output/operations_justification.pptx` (de nieuwe onderbouwingsslides).
- **Actie:** herschrijven naar de huidige stack (Docker Model Runner, `localhost:8000`, hard-refresh tip), of converteren naar `README.md` met de nieuwe inhoud.

### 3. `demo video.mp4` (133 MB)
Datum 2026-04-13. Toont:
- "Beslissingen"-tabblad in de sidebar (verwijderd)
- Module-tags M1/M2/M3/M4 (verwijderd)
- "Rondleiding"-knop rechtsonder (verwijderd)
- Strategie A/B chunker-codeblokken op Ingestie (verwijderd)
- HNSW parametertabel + Memory Math (verwijderd)
- "MODULE 1 · Ingestion & Knowledge Structuring" hero (vervangen door rustige header)

**Niets** in deze video matcht meer met wat Tim live ziet. Pointing-to is contraproductief.
- **Actie:** ofwel opnieuw opnemen ná dress-rehearsal van de nieuwe DEMO_SCRIPT, ofwel verwijderen + uit README schrappen. Geen middenweg.

### 4. `assessment_presentation_final (2) (1).pptx`
- 440 KB, untracked in git, andere MD5 dan de "echte" deck.
- Commit `2790f38` heet *"Replace presentation with final version (assessment_AI_USE_emresemerci.pptx)"* — dit is de **vorige** versie die werd vervangen.
- **Actie:** verwijderen. Veiliger niet om hem rond te laten slingeren naast `assessment_AI_USE_emresemerci.pptx` — kans op verkeerde versie meenemen naar het gesprek.

---

## 🟡 STALE — v1-design, conflicteert met huidige demo, blijft nuttig als context

Deze artefacten zijn niet "fout" — ze waren correct toen ze geschreven werden. Maar ze beschrijven een architectuur die **niet** is wat draait. Houd ze als achtergrondmateriaal voor het verslag, niet als bron-van-waarheid.

### 5. `drafts/final_submission_v2.md` (57 KB)
Het hoofd-document van de schriftelijke inzending. Architectuur is nog goed, maar tech-stack-claims kloppen niet met de demo:
| Claim in draft | Werkelijk in demo |
|---|---|
| LangGraph 9-state machine | Imperatieve Python state machine (geen LangGraph) |
| Mixtral 8x22B / Llama 3.1 70B via vLLM | Gemma 4 E2B via Docker Model Runner |
| `multilingual-e5-large` (1024-dim) | `multilingual-e5-small` (384-dim) |
| `BAAI/bge-reranker-v2-m3` cross-encoder | LLM-as-reranker (zelfde Gemma-call) |
| `MAX_RETRIES=1` | ✓ klopt |
| Pre-retrieval RBAC | ✓ klopt |
| RRF k=60 | ✓ klopt |
| 4-tier RBAC | ✓ klopt |

- **Actie:** óf updaten naar wat draait (~2u werk), óf bovenaan een banner *"v1 design — actuele implementatie wijkt af op model/embedding/orchestrator, zie demo/"* + één tabel met de drie afwijkingen.

### 6. `drafts/module1-4_draft.md` (4 bestanden, ~105 KB)
Voorgangers van `final_submission_v2.md`. Inhoud is opgenomen in de definitieve versie.
- **Actie:** verwijderen, of verplaatsen naar `drafts/v1_module_drafts/` zodat het duidelijk is dat ze gesuperseed zijn.

### 7. `pseudocode/*.py` (5 bestanden, ~170 KB)
Design-pseudocode voor v1. Sommige modules zijn nog 1-op-1 herkenbaar in de demo (CRAG state machine, structurele chunker met hiërarchie); andere zijn divergent (`LegalDocumentChunker` gebruikt Mixtral-promptcalls; demo gebruikt regex + Gemma).
- **Actie:** behouden — Tim kan ernaar willen kijken voor "hoe zou dit op productieschaal eruitzien". Wel duidelijk maken in `README` dat dit geen runtime-code is.

### 8. `schemas/opensearch_index_mapping.json` (11 KB)
- Index-naam `tax_authority_rag_chunks` (demo gebruikt `tax_authority_rag_chunks_e5` voor de e5-small dim)
- 1024-dim vectors, fp16-quantization
- Sizing voor 20M chunks
- HNSW m=16 / ef_construction=256

De runtime-mapping wordt gegenereerd door [demo/app/opensearch/setup.py](demo/app/opensearch/setup.py) en heeft 384-dim, geen quantization, demo-scale.
- **Actie:** behouden als productie-blueprint, maar bovenaan een comment dat de demo een gereduceerde versie gebruikt.

### 9. `diagrams/*.md` (4 bestanden, ~78 KB)
Architectuur-diagrammen van v1 — bevatten waarschijnlijk LangGraph/Mixtral-referenties. Heb niet inhoudelijk gecheckt, maar gezien hun datum (Apr 11–12) onveranderd sinds v1.
- **Actie:** snel scannen op stack-claims; alleen `security_model.md` is nog 100% correct (RBAC-ontwerp is ongewijzigd).

### 10. `prompts/*.txt` (4 bestanden, ~18 KB)
v1 prompt-templates voor grader/generator/HyDE/decomposition. De runtime-prompts zitten inline in `demo/app/pipeline/{grader,generator,hyde}.py` en wijken af.
- **Actie:** behouden als bron voor verslag; bij twijfel sync de runtime-versie naar deze files zodat ze de canonieke versies zijn.

### 11. `eval/golden_test_set_spec.json` (4 KB) + `metrics_matrix.md` (12 KB)
v1 eval-spec. De Kwaliteit-pagina in de demo gebruikt een eigen `/v1/eval/*` endpoint dat losser staat van deze specs.
- **Actie:** controleren of deze nog accuraat zijn voor wat de Kwaliteit-tab toont; zo niet, korte toelichting toevoegen.

### 12. `performance/resource_allocation.md` (26 KB)
Sizing-analyse voor 20M chunks met multi-node OpenSearch + GPU-LLM. Niet wat draait, wel wat Tim wil zien als "hoe zou je dit naar productie schalen".
- **Actie:** behouden, het is een sterk onderbouwingsdocument.

### 13. `reference/tools_and_technologies.txt` (24 KB) + `assumptions.md` (7 KB)
v1 tech-inventaris en aannamelijst (A1–A18). De assumptions zijn nog steeds relevant; de tech-stack-lijst niet.
- **Actie:** assumptions houden; tech-stack updaten of taggen als "ontwerpfase, demo gebruikt subset".

---

## 🟢 ONGEMOEID — actueel en correct

| Bestand / map | Status |
|---|---|
| `demo/` | Live product, draait nu op localhost:8000 |
| `slides/` | Net gegenereerd, matcht UI |
| `assessment_AI_USE_emresemerci.pptx` | Huidige presentatie-deck |
| `TIM_FEEDBACK.md` | Net gemaakt, letterlijke feedback |
| `SENIOR_LEVEL_PLAN.md` | Plan dat naar v3 leidde — historisch waardevol |
| `CLAUDE.md` | Behavioural guidelines, niet stack-gevoelig |
| `assesment.txt` | De originele opdracht — onveranderlijk |

---

## 🛠 HOUSEKEEPING — git-hygiëne

### 14. Veel ongecommitte v3-code
`git status` toont 18 modified + 18 untracked bestanden in `demo/app/`, plus `slides/`, `TIM_FEEDBACK.md`, `SENIOR_LEVEL_PLAN.md`, `CLAUDE.md`, `DEMO_SCRIPT.md`. **Alles wat de strip-down + slide-creatie heeft opgeleverd staat alleen lokaal.**
- **Actie:** committen in logische chunks: (a) v3 demo-code, (b) UI-strip + nieuwe sidebar, (c) slides + DEMO_SCRIPT + TIM_FEEDBACK + SENIOR_LEVEL_PLAN. Anders ben je één laptop-fout verwijderd van alles kwijt.

### 15. `.gitignore` mist nieuwe build-artefacten
Niet uitgesloten:
- `slides/.venv/`
- `slides/__pycache__/`
- `slides/output/` (debatable — kan nuttig zijn als je 'm wilt versionen, maar normaal generatie-output)
- **Actie:** drie regels toevoegen aan `.gitignore`.

### 16. `.github/workflows/eval_gate.yml`
v1 CI-workflow voor een architectuur die niet draait. Onbekend of hij überhaupt syntactisch werkt zonder de claimde dependencies.
- **Actie:** óf bijwerken om tegen `demo/`-stack te draaien, óf taggen als "spec-only".

---

## Aanbevolen volgorde van schoonmaakacties

| # | Tijd | Actie | Risico |
|---|---|---|---|
| 1 | 2 min | Verwijder `assessment_presentation_final (2) (1).pptx` (duplicaat) | Geen |
| 2 | 5 min | Voeg `slides/.venv/`, `slides/__pycache__/`, `slides/output/` toe aan `.gitignore` | Geen |
| 3 | 15 min | Herschrijf `README.txt` → `README.md` voor v3 (geen Gemini-stap, nieuwe stack, link naar slides) | Geen |
| 4 | 10 min | Vervang `requirements.txt` (root) door pointer naar `demo/requirements-demo.txt` | Laag |
| 5 | 30 min | Beslis: `demo video.mp4` opnieuw opnemen of verwijderen | Middel — als je ervoor kiest opnieuw op te nemen, plan minstens een dress-rehearsal eerst |
| 6 | 60 min | `drafts/final_submission_v2.md` van banner voorzien + 1 tabel met afwijkingen | Laag |
| 7 | 90 min | Commit alles in 3 logische chunks naar git | Geen — gewoon doen |
| 8 | optioneel | Per stale-bestand individueel beslissen: bijwerken, taggen of behouden as-is | Geen |

---

## Wat je kunt doen tijdens je dress-rehearsal

Loop **alleen langs de groene en de gele rij** — accepteer dat ze er niet 100% bij elkaar passen, en *vertel dat tegen Tim als hij ernaar vraagt*: **"De papieren architectuur in `drafts/` beschrijft de productieversie; de live demo is een gereduceerde implementatie die op een laptop draait. Het verschil is bewust en gedocumenteerd."** Dat is een sterker antwoord dan proberen alles in lijn te brengen.

De rode rij moet weg of bijgewerkt vóórdat Tim de repo opent. Anders krijg je *"waarom werkt `pip install -r requirements.txt` niet?"* en is het gesprek begonnen op een verkeerde noot.
