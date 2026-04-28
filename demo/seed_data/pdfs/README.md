# Pre-ingest seed documents

Bestanden in deze map worden bij `docker compose up` automatisch geïngest door
[`scripts/preingest.sh`](../../scripts/preingest.sh).

## Naming convention voor tier-binding

Het script leidt de security-tier af uit het bestandsnaam-prefix:

| Prefix          | Tier               |
|-----------------|--------------------|
| `fiod_*`        | CLASSIFIED_FIOD    |
| `inspecteur_*`  | RESTRICTED         |
| `intern_*`      | INTERNAL           |
| (anders)        | PUBLIC             |

## Bestandsformaten

`.pdf`, `.txt` en `.md` worden geaccepteerd. PDF wordt geparsed via `pypdf`;
TXT/MD gaan rechtstreeks naar de chunker.

## Huidige corpus (12 bestanden)

### PUBLIC — echte bron, opgehaald via WebFetch

| Bestand | Bron | Onderwerp |
|---|---|---|
| `bd_arbeidskorting_belastingdienst.txt` | belastingdienst.nl | Arbeidskorting overzichtspagina |
| `ecli_nl_hr_2021_1963_kerstarrest_box3.txt` | data.rechtspraak.nl | Hoge Raad — Kerstarrest box 3 (substantief) |
| `ecli_nl_hr_2024_0704_box3_rechtsherstel.txt` | data.rechtspraak.nl | Hoge Raad — Wet rechtsherstel box 3 (substantief) |
| `ecli_nl_hr_2023_1062_omzetbelasting_naheffing.txt` | data.rechtspraak.nl | Hoge Raad — BTW naheffing 81-RO |
| `ecli_nl_hr_2023_1244_bpm.txt` | data.rechtspraak.nl | Hoge Raad — BPM 81-RO |
| `ecli_nl_hr_2023_1517_strafkamer_artikel81ro.txt` | data.rechtspraak.nl | Hoge Raad — strafkamer 81-RO |
| `ecli_nl_hr_2022_1351_aanslag_ib.txt` | data.rechtspraak.nl | Hoge Raad — aanslag IB 81-RO |

### PUBLIC — fictief (vult onderwerps-gaten)

| Bestand | Reden |
|---|---|
| `beleidsmemo_btw_thuiskantoor_2024.txt` | Echt beleidsmemo niet beschikbaar via WebFetch (JS-rendered). Vult BTW-werkruimte topic. |
| `successiewet_bedrijfsopvolging_2024.txt` | Echte tekst van Successiewet niet beschikbaar via WebFetch. Vult schenk/erfbelasting topic. |

### INTERNAL / RESTRICTED / CLASSIFIED_FIOD — fictief (per definitie)

| Bestand | Tier |
|---|---|
| `intern_transferpricing_advance_pricing_2024.txt` | INTERNAL |
| `inspecteur_invordering_dwangbevel_2024.txt` | RESTRICTED |
| `fiod_opsporing_huiszoeking_procedure_2024.txt` | CLASSIFIED_FIOD |

Geclassificeerde inhoud is per definitie fictief — die mag niet in een
publieke repository staan.

## Echte tekst toevoegen / vervangen

1. Download een PDF van wetten.overheid.nl (klik "Druk-versie" → "PDF")
   of van uitspraken.rechtspraak.nl.
2. Plaats het bestand in deze map met een betekenisvolle naam.
3. Geen prefix → PUBLIC. Voeg `intern_`, `inspecteur_`, of `fiod_` toe
   voor andere tiers.
4. Bij volgende `docker compose up` wordt het automatisch geïngest.
   Of handmatig via:
   ```bash
   curl -X POST http://localhost:8000/v1/ingest \
     -F "file=@seed_data/pdfs/your-file.pdf" \
     -F "title=your-title" \
     -F "security_classification=PUBLIC"
   ```

## Bronnen voor extra echte content

- https://wetten.overheid.nl — wetgeving (klik "PDF" linksboven artikel-pagina)
- https://uitspraken.rechtspraak.nl — jurisprudentie / ECLI
- https://data.rechtspraak.nl/uitspraken/content?id=ECLI:NL:HR:JJJJ:NNNN — ruwe ECLI-tekst
- https://www.belastingdienst.nl — publieke beleidsmemo's
- https://www.rijksoverheid.nl — beleidsdocumenten
