"""
RBAC pre-retrieval filter — mirrors the DLS queries in schemas/rbac_roles.json.

In production this is enforced by OpenSearch Document-Level Security at the
database level (before any BM25/kNN scoring). In this demo, OpenSearch security
plugin is disabled (single-node simplicity), so we apply the same filter logic
as a query-time bool.filter clause — the semantics are identical.
"""

from app.models import SecurityTier


TIER_HIERARCHY = {
    SecurityTier.PUBLIC: 0,
    SecurityTier.INTERNAL: 1,
    SecurityTier.RESTRICTED: 2,
    SecurityTier.CLASSIFIED_FIOD: 3,
}

# Which classifications each tier can see (mirrors rbac_roles.json DLS filters)
TIER_ACCESS = {
    SecurityTier.PUBLIC: ["PUBLIC"],
    SecurityTier.INTERNAL: ["PUBLIC", "INTERNAL"],
    SecurityTier.RESTRICTED: ["PUBLIC", "INTERNAL", "RESTRICTED"],
    SecurityTier.CLASSIFIED_FIOD: ["PUBLIC", "INTERNAL", "RESTRICTED", "CLASSIFIED_FIOD"],
}


def build_rbac_filter(security_tier: SecurityTier) -> dict:
    """
    Returns an OpenSearch bool.filter clause that restricts search results
    to documents accessible by the given security tier.

    This is applied BEFORE BM25/kNN scoring — exactly mirroring pre-retrieval DLS.
    """
    accessible = TIER_ACCESS.get(security_tier, ["PUBLIC"])
    return {
        "terms": {
            "security_classification": accessible
        }
    }
