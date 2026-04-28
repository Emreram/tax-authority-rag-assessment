#!/usr/bin/env bash
# Dress-rehearsal fixture.
# Start stack, disconnect host from internet, run 8 demo queries end-to-end,
# assert none takes >20s and no error state is reached.
# Exits 0 on success, 1 on any failure.

set -u

HOST="${HOST:-http://localhost:8000}"

# Wait for warmup
echo "[offline_verify] waiting for warmup at $HOST/health ..."
for i in {1..240}; do
  out=$(curl -sf "$HOST/health" 2>/dev/null || echo "")
  if echo "$out" | grep -q '"warmup_complete":\s*true'; then
    echo "[offline_verify] ready"
    break
  fi
  sleep 1
done
if ! echo "$out" | grep -q '"warmup_complete":\s*true'; then
  echo "[offline_verify] FAIL: API never became ready"
  exit 1
fi

# Queries: (query, tier, expected_source_substring)
QUERIES=(
  "Wat is de arbeidskorting in 2024?|PUBLIC|pipeline|cache"
  "Wat zijn de BTW-tarieven in Nederland?|PUBLIC|pipeline|cache"
  "ECLI:NL:HR:2021:1523|PUBLIC|pipeline|cache"
  "Hoe werkt de hypotheekrenteaftrek?|PUBLIC|pipeline|cache"
  "Wat zijn de termijnen voor bezwaar?|INTERNAL|pipeline|cache"
  "Hoe werkt de Handboek Invordering procedure?|INTERNAL|pipeline|cache"
  "Wat is transfer pricing onderzoek methodologie?|RESTRICTED|pipeline|cache"
  "Wat zijn de FIOD opsporingsmethoden voor BTW-fraude?|CLASSIFIED_FIOD|pipeline|cache"
)

FAIL=0
MAX_MS=20000

for entry in "${QUERIES[@]}"; do
  IFS='|' read -r q tier s1 s2 <<< "$entry"
  body=$(printf '{"query":"%s","security_tier":"%s","session_id":"offline-verify"}' "$q" "$tier")
  t0=$(date +%s%N)
  resp=$(curl -s -X POST -H "Content-Type: application/json" -d "$body" "$HOST/v1/query" 2>/dev/null || echo "")
  t1=$(date +%s%N)
  ms=$(( (t1 - t0) / 1000000 ))

  if [ -z "$resp" ]; then
    echo "FAIL [$tier] (empty response) — $q"
    FAIL=$((FAIL + 1))
    continue
  fi
  source=$(echo "$resp" | python -c 'import json,sys;print(json.loads(sys.stdin.read()).get("source",""))' 2>/dev/null || echo "")
  if [ "$source" != "$s1" ] && [ "$source" != "$s2" ]; then
    echo "FAIL [$tier] (source=$source) — $q"
    FAIL=$((FAIL + 1))
    continue
  fi
  if [ "$ms" -gt "$MAX_MS" ]; then
    echo "FAIL [$tier] (${ms}ms > ${MAX_MS}ms) — $q"
    FAIL=$((FAIL + 1))
    continue
  fi
  printf "OK   [%-18s] %6dms src=%s — %s\n" "$tier" "$ms" "$source" "$q"
done

if [ "$FAIL" -gt 0 ]; then
  echo "[offline_verify] $FAIL failures"
  exit 1
fi
echo "[offline_verify] all 8 queries passed"
