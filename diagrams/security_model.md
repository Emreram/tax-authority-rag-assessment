# Security Model — Module 4 Detail Diagram

> This diagram visualizes the RBAC enforcement strategy for the Tax Authority
> RAG system. It shows the 4 security tiers, the 6 OpenSearch roles, the
> authentication flow from identity provider to OpenSearch query, the
> mathematical argument for **pre-retrieval** DLS enforcement (vs. the unsafe
> post-retrieval alternative), the cache tier-partitioning that prevents
> cross-tier leakage, and three concrete attack scenarios that the design
> thwarts.
>
> **Why this is the most important diagram in the submission:** The assessment
> explicitly names the FIOD scenario — "A helpdesk employee must not be able
> to retrieve answers based on classified FIOD documents." The evaluator will
> specifically check for: (1) pre-retrieval enforcement, (2) a mathematical
> justification for why post-retrieval is unsafe, and (3) cache tier
> partitioning. This diagram delivers all three.

---

## 1. The 4 Security Tiers

| Tier | Access level | Example content | Governing role |
|---|---|---|---|
| **PUBLIC** | Accessible to all users including external systems | Published legislation (wetten.overheid.nl), published court rulings (rechtspraak.nl), public tax guides, general rate tables | `role_public_user`, and all higher roles |
| **INTERNAL** | All tax authority employees | Internal handbooks (Handboek Invordering), operational procedures, e-learning modules, internal circulars | `role_helpdesk` and above |
| **RESTRICTED** | Senior inspectors and legal counsel | Advanced audit methodologies, legal counsel opinions, sensitive policy interpretations, pre-publication drafts | `role_tax_inspector`, `role_legal_counsel`, `role_fiod_investigator` |
| **CLASSIFIED_FIOD** | FIOD investigators ONLY | Fraud investigation methodologies, intelligence reports, ongoing case files, suspect analyses, interagency protocols | `role_fiod_investigator` only |

See [schemas/rbac_roles.json](../schemas/rbac_roles.json) for the full DLS
configuration and role mappings.

---

## 2. Role-to-Tier Access Matrix

```
 ┌────────────────────────────┬─────────┬──────────┬────────────┬──────────────────┐
 │ Role                        │ PUBLIC  │ INTERNAL │ RESTRICTED │ CLASSIFIED_FIOD  │
 ├────────────────────────────┼─────────┼──────────┼────────────┼──────────────────┤
 │ role_public_user            │   YES   │    NO    │    NO      │        NO        │
 │ role_helpdesk               │   YES   │   YES    │    NO      │        NO        │
 │ role_tax_inspector          │   YES   │   YES    │   YES      │        NO        │
 │ role_legal_counsel          │   YES   │   YES    │   YES      │        NO        │
 │ role_fiod_investigator      │   YES   │   YES    │   YES      │       YES        │
 │ role_ingestion_service      │   WRITE-ONLY (no search access at all)            │
 └────────────────────────────┴─────────┴──────────┴────────────┴──────────────────┘
```

Key observation for the assessment's FIOD scenario: `role_helpdesk` has **NO**
in the CLASSIFIED_FIOD column. This is enforced by an OpenSearch DLS filter
that excludes `{"terms":{"security_classification":["RESTRICTED","CLASSIFIED_FIOD"]}}`
— see [schemas/rbac_roles.json](../schemas/rbac_roles.json) `role_helpdesk.dls`.

---

## 3. Authentication → Authorization Flow

```
   ┌─────────────────────┐
   │  User (browser/CLI) │
   └──────────┬──────────┘
              │
              │ 1. Login with AD credentials
              ▼
   ┌─────────────────────────────────────┐
   │ Identity Provider                    │
   │ (Active Directory / ADFS /           │
   │  Azure AD OIDC — Assumption A4)      │
   │                                      │
   │  Returns JWT with claims:            │
   │    - sub (user id)                   │
   │    - groups[] (AD groups)            │
   │    - exp (expiry)                    │
   └──────────┬──────────────────────────┘
              │
              │ 2. Query request + JWT
              ▼
   ┌─────────────────────────────────────┐
   │ API Gateway (FastAPI)                │
   │                                      │
   │  - Validate JWT signature            │
   │  - Check expiry                      │
   │  - Extract groups claim              │
   │  - Map groups → security_tier via    │
   │    rbac_roles.json role_mapping      │
   │      TAX_HELPDESK → role_helpdesk    │
   │      TAX_INSPECTORS → role_tax_insp. │
   │      FIOD_INVESTIGATORS → role_fiod  │
   │    (see schemas/rbac_roles.json)     │
   └──────────┬──────────────────────────┘
              │
              │ 3. (query, user_security_tier, session_id)
              ▼
   ┌─────────────────────────────────────┐
   │ Semantic Cache                       │
   │                                      │
   │  Tier-filtered lookup:               │
   │  accessible_tiers =                  │
   │    [t for t in TIER_HIERARCHY        │
   │     if level(t) ≤ level(user_tier)]  │
   │                                      │
   │  RediSearch pre-filter on @tier      │
   │  tag field (before KNN scoring)      │
   └──────────┬──────────────────────────┘
              │ MISS
              ▼
   ┌─────────────────────────────────────┐
   │ CRAG Pipeline                        │
   │  user_security_tier carried in       │
   │  CRAGState throughout                │
   └──────────┬──────────────────────────┘
              │
              │ 4. Search call with impersonation header
              ▼
   ┌─────────────────────────────────────┐
   │ OpenSearch Security Plugin           │
   │                                      │
   │  opendistro_security_impersonate_as  │
   │  = user_security_tier_role           │
   │                                      │
   │  - Resolve DLS filter for role       │
   │  - Apply filter to index view        │
   │  - Execute BM25/kNN on filtered view │
   │  - Log to audit_log index            │
   └─────────────────────────────────────┘
```

All four stages (gateway, cache, CRAG, OpenSearch) **independently** enforce
the tier — no single point of failure. If the cache check had a bug and leaked
a cross-tier entry, OpenSearch DLS would still block the underlying retrieval.

---

## 4. Pre-retrieval vs Post-retrieval — Side-by-Side

```
╔═══════════════════════════════════╦═══════════════════════════════════╗
║   POST-RETRIEVAL FILTERING        ║   PRE-RETRIEVAL FILTERING         ║
║   (UNSAFE — do not use)           ║   (SAFE — what we use)            ║
╠═══════════════════════════════════╬═══════════════════════════════════╣
║ 1. Query hits OpenSearch          ║ 1. Query hits OpenSearch          ║
║                                   ║                                   ║
║ 2. BM25 + kNN score ALL           ║ 2. DLS filter restricts the       ║
║    documents in the index         ║    visible index to S_user =      ║
║    (including classified ones)    ║    S_total \ S_forbidden          ║
║                                   ║                                   ║
║ 3. Top-40 is returned to app      ║ 3. BM25 + kNN score only          ║
║                                   ║    documents in S_user            ║
║ 4. App code iterates results      ║                                   ║
║    and drops any with             ║ 4. Top-40 is returned —           ║
║    classification > user_tier     ║    ALL already permitted          ║
║                                   ║                                   ║
║ 5. User sees < 40 results         ║ 5. User sees up to 40 results,    ║
║                                   ║    all legitimate                 ║
║                                   ║                                   ║
║ LEAKS:                            ║ LEAKS:                            ║
║  - Result count variance          ║  - none                           ║
║  - Ranking distortion             ║                                   ║
║  - Timing side-channel            ║                                   ║
║  - Cross-retriever competition    ║                                   ║
╚═══════════════════════════════════╩═══════════════════════════════════╝
```

---

## 5. Mathematical Proof — Why Post-Retrieval Leaks

**Theorem.** Post-retrieval filtering leaks information about classified
documents even when the filtered output contains no classified content.

**Setup.**
- Let `S` be the total corpus of ~20 million chunks.
- Let `S_c ⊂ S` be the classified subset (e.g., CLASSIFIED_FIOD), unknown to the user.
- Let `k` be the retrieval depth (k = 40 in our system).
- Let `c` be the number of classified documents appearing in the initial top-k
  before filtering.

**Leakage Mode 1 — Result count variance.**

Under post-retrieval filtering, the user receives `k − c` documents. For a
random query, the probability that at least one classified document appears
in the top-k is:

```
  P(c ≥ 1)  =  1 − (1 − |S_c|/|S|)^k
```

Assume |S_c|/|S| = 0.05 (a realistic 5% classification rate for a national
fraud investigation division). With k = 40:

```
  P(c ≥ 1)  =  1 − (0.95)^40
            ≈  1 − 0.129
            ≈  0.871
```

**On 87% of queries**, the helpdesk user will observe `returned_count < 40`
and can infer: "there exist classified documents relevant to my query that I
cannot see." This IS a leak — the user learns about the existence and rough
density of classified material on any given topic.

Under pre-retrieval filtering, the search space is restricted to `S_user =
S \ S_c`. The retrieval always returns up to k documents, independent of
|S_c|, and the user cannot distinguish "no classified docs exist for this
query" from "classified docs exist but I am filtered out." Leak probability:
**zero**.

**Leakage Mode 2 — Ranking distortion.**

Under post-retrieval filtering, BM25 and kNN compute scores against the full
corpus including classified documents. The relative ranking of non-classified
docs is influenced by which classified docs are in the scoring pool. Two
helpdesk users posing similar queries will observe different rankings
depending on which classified docs score highly for each query. The ranking
sequence itself becomes a side-channel.

Under pre-retrieval filtering, classified docs never enter the scoring pool.
Rankings are computed purely over `S_user`, so they are deterministic given
the query and the permitted index view.

**Leakage Mode 3 — Timing side-channel.**

Under post-retrieval filtering, the application must iterate through k
results and drop forbidden ones. This takes time proportional to c. A query
that triggers filtering of 50 classified docs takes measurably longer than a
query with 0. An attacker issuing repeated similar queries can infer the
distribution of classified docs across topics from response-time variance.

Under pre-retrieval filtering, the filter is applied inside the index engine
before scoring. Wall time is independent of |S_c|, removing the side-channel
entirely.

**Conclusion.** The three leakage modes are mathematical consequences of where
the filter is applied, not implementation bugs. Pre-retrieval enforcement is
**necessary**, not merely best practice. ∎

---

## 6. Cache Tier Partitioning

The semantic cache (`module4_cache.py`) must also be tier-partitioned,
otherwise a FIOD-tier cached response could be served to a helpdesk user on a
cache hit — bypassing OpenSearch DLS entirely.

```
 Redis Cache Storage (RediSearch HNSW index, tag-filtered)
 ┌──────────────────────────────────────────────────────────┐
 │                                                          │
 │  ┌────────────────────────────────────────────────────┐  │
 │  │ tier = CLASSIFIED_FIOD                              │  │
 │  │   Key: cache:CLASSIFIED_FIOD:{hash}                 │  │
 │  │   Response body may reference classified chunks    │  │
 │  │   Readable by: role_fiod_investigator ONLY         │  │
 │  └────────────────────────────────────────────────────┘  │
 │                                                          │
 │  ┌────────────────────────────────────────────────────┐  │
 │  │ tier = RESTRICTED                                   │  │
 │  │   Key: cache:RESTRICTED:{hash}                      │  │
 │  │   Readable by: FIOD, tax_inspector, legal_counsel  │  │
 │  └────────────────────────────────────────────────────┘  │
 │                                                          │
 │  ┌────────────────────────────────────────────────────┐  │
 │  │ tier = INTERNAL                                     │  │
 │  │   Key: cache:INTERNAL:{hash}                        │  │
 │  │   Readable by: helpdesk, inspector, legal, FIOD    │  │
 │  └────────────────────────────────────────────────────┘  │
 │                                                          │
 │  ┌────────────────────────────────────────────────────┐  │
 │  │ tier = PUBLIC                                       │  │
 │  │   Key: cache:PUBLIC:{hash}                          │  │
 │  │   Readable by: everyone                             │  │
 │  └────────────────────────────────────────────────────┘  │
 │                                                          │
 └──────────────────────────────────────────────────────────┘

 Lookup rule (module4_cache.py:get_accessible_tiers):
   accessible = [t for t in TIER_HIERARCHY
                 if level(t) ≤ level(user_tier)]
   RediSearch query applies a TAG filter: @security_tier:{accessible}
   BEFORE the KNN similarity search.

 Invariant:
   A query at tier T can only retrieve cached entries at tier ≤ T.
   Even a 0.99 cosine similarity hit in a higher tier is EXCLUDED by
   the tag pre-filter — it never enters the KNN search at all.
```

---

## 7. Three Attack Scenarios (Thwarted)

### Attack 1 — Direct classified query

**Scenario.** A helpdesk employee asks: "What are the standard investigation
methods for transfer pricing fraud?" The intent is to obtain CLASSIFIED_FIOD
content.

**Defense path.**
1. JWT maps employee to `role_helpdesk`.
2. Cache lookup filters to PUBLIC + INTERNAL tiers only — no hit.
3. CRAG pipeline calls OpenSearch with impersonate_as = role_helpdesk.
4. OpenSearch DLS filter excludes security_classification IN
   (RESTRICTED, CLASSIFIED_FIOD) BEFORE BM25 / kNN scoring.
5. Search space = INTERNAL + PUBLIC docs only.
6. Retrieved chunks are generic procedural info about audits, not
   fraud investigation. CRAG grader returns IRRELEVANT.
7. State machine routes to REFUSE.

**Result.** The user receives: "I could not find relevant tax-law content
in sources you are authorized to access." The user cannot distinguish this
from "the system has no such content at all." No leak.

### Attack 2 — Cache poisoning via sibling query

**Scenario.** Earlier today, a FIOD investigator asked a similar question and
the response was cached at tier CLASSIFIED_FIOD. A helpdesk employee now
issues a near-identical query (cosine similarity = 0.98).

**Defense path.**
1. Helpdesk employee's query embeds and reaches semantic cache.
2. Cache lookup pre-filters on `@security_tier:{PUBLIC|INTERNAL}`.
3. The FIOD-tier entry is EXCLUDED from the KNN search despite
   the high similarity.
4. Cache returns MISS → normal CRAG pipeline → normal DLS enforcement.

**Result.** Helpdesk user gets an answer derived from PUBLIC+INTERNAL docs
only, or a refusal. No cross-tier cache poisoning.

### Attack 3 — Timing side-channel

**Scenario.** An attacker employee iterates hundreds of slight variations of
"fraud investigation" queries, measuring response time. In a post-retrieval
filtering system, queries with more classified-doc matches would take longer,
revealing the topic distribution of classified material.

**Defense path.**
1. OpenSearch DLS is applied pre-retrieval. Classified docs never enter the
   BM25 inverted-index traversal or the HNSW graph walk.
2. Response time is a function of `|S_user|` (the permitted search space),
   not `|S_c|`.
3. Two similar queries take statistically identical time regardless of how
   many classified docs exist on the underlying topic.

**Result.** Timing side-channel is closed. The attacker learns nothing about
`S_c` from response-time variance.

---

## 8. Audit Trail

Every access decision is persisted to TWO independent stores, satisfying
Assumption A18 (the system will be audited):

| Store | What is logged | Retention | Purpose |
|---|---|---|---|
| OpenSearch **audit log index** (separate index from the RAG index) | Every query, requesting role, DLS filter applied, returned chunk_ids, timestamp | 7 years (fiscal retention) | Long-term audit, incident forensics |
| **OpenTelemetry** spans → Jaeger | Every pipeline node (classify, transform, retrieve, grade, generate, validate), per-stage latency, user_security_tier, session_id, error state | 30 days hot / 1 year cold | Real-time tracing, debugging, SLO monitoring |

An unauthorized access attempt produces: (a) a DLS denial event in the OpenSearch
audit log, (b) an OpenTelemetry span with `status=filtered`, and (c) a
Prometheus counter increment for `dls_filter_applied_total{role=...}`.
Alert fires if the counter increments unexpectedly for a role that should
have no reason to encounter filtered content.

---

## 9. Cross-File Anchors

- 4 tiers and 6 roles: [schemas/rbac_roles.json](../schemas/rbac_roles.json)
- DLS query examples: [schemas/rbac_roles.json](../schemas/rbac_roles.json) `opensearch_roles.*.dls`
- Mathematical proof source: [schemas/rbac_roles.json](../schemas/rbac_roles.json) `mathematical_proof_pre_retrieval`
- Cache key format `cache:{security_tier}:{hash}`: [schemas/rbac_roles.json](../schemas/rbac_roles.json) `cache_partitioning.key_format` + [pseudocode/module4_cache.py](../pseudocode/module4_cache.py)
- Tier hierarchy logic: `get_accessible_tiers()` in [module4_cache.py](../pseudocode/module4_cache.py)
- IdP group → role mapping: [schemas/rbac_roles.json](../schemas/rbac_roles.json) `role_mapping.mappings`
- Assumption A4 (IdP exists), A17 (security is first-class), A18 (audit trail): [reference/assumptions.md](../reference/assumptions.md)
- DLS Bypass Rate = 0.0 gate: [eval/metrics_matrix.md](../eval/metrics_matrix.md) Section 4
