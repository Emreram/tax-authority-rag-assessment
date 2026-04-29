#!/usr/bin/env sh
# Pre-ingest seed documents at first startup.
# Idempotent: skips when corpus already contains expected number of doc types.
# Tier-binding via filename prefix (zie seed_data/pdfs/README.md):
#   fiod_*       -> CLASSIFIED_FIOD
#   inspecteur_* -> RESTRICTED
#   intern_*     -> INTERNAL
#   *            -> PUBLIC

set -e

API_HOST="${API_HOST:-http://api:8000}"
DOC_DIR="${DOC_DIR:-/seed_data/pdfs}"
EXPECTED_DOCS=28  # threshold; corpus extended in 2026-04 with 18 new PUBLIC documents. Skip preingest if >= this many docs already.

if [ ! -d "$DOC_DIR" ]; then
  echo "preingest: $DOC_DIR not found, skipping"
  exit 0
fi

# Wait until API is ready (compose healthcheck should already gate us, but be defensive).
attempts=0
until curl -sf "$API_HOST/health" >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [ "$attempts" -gt 60 ]; then
    echo "preingest: API not reachable after 60 attempts, aborting"
    exit 1
  fi
  sleep 2
done

# Skip when corpus already populated.
COUNT=$(curl -sf "$API_HOST/v1/documents" 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('documents',[])))" 2>/dev/null || echo 0)
if [ "$COUNT" -ge "$EXPECTED_DOCS" ]; then
  echo "preingest: $COUNT docs already indexed (>= $EXPECTED_DOCS), skipping"
  exit 0
fi

echo "preingest: $COUNT docs found, ingesting from $DOC_DIR..."

for f in "$DOC_DIR"/*.txt "$DOC_DIR"/*.md "$DOC_DIR"/*.pdf; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  base="${name%.*}"

  case "$base" in
    fiod_*)        tier="CLASSIFIED_FIOD" ;;
    inspecteur_*)  tier="RESTRICTED"      ;;
    intern_*)      tier="INTERNAL"        ;;
    *)             tier="PUBLIC"          ;;
  esac

  # README.md is documentation, not corpus content — skip.
  if [ "$name" = "README.md" ]; then continue; fi

  echo "  -> $name ($tier)"
  # Stream the SSE response and discard; we just need ingest to complete.
  curl -sf -X POST "$API_HOST/v1/ingest" \
    -F "file=@$f" \
    -F "title=$base" \
    -F "security_classification=$tier" \
    >/dev/null 2>&1 || echo "    FAIL: $name"
done

echo "preingest: done"
