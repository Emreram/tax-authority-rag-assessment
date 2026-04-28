#!/usr/bin/env bash
set -euo pipefail

# Start ollama server in background.
/bin/ollama serve &
OLLAMA_PID=$!

# Wait for server to accept requests.
echo "[entrypoint] waiting for ollama server..."
for i in $(seq 1 60); do
  if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "[entrypoint] ollama up"
    break
  fi
  sleep 1
done

# Pull models listed in OLLAMA_MODELS_TO_PULL (space-separated).
MODELS="${OLLAMA_MODELS_TO_PULL:-gemma3:4b qwen2.5:3b-instruct}"
for model in $MODELS; do
  echo "[entrypoint] pulling $model (this is a one-time cost)"
  if /bin/ollama pull "$model"; then
    echo "[entrypoint] pulled $model"
  else
    echo "[entrypoint] WARN: failed to pull $model, continuing"
  fi
done

# Warm the primary model so first inference is not cold.
PRIMARY="${OLLAMA_MODEL:-gemma3:4b}"
echo "[entrypoint] warming $PRIMARY"
curl -sf -X POST http://localhost:11434/api/generate \
  -d "{\"model\":\"$PRIMARY\",\"prompt\":\"ok\",\"stream\":false,\"options\":{\"num_predict\":1}}" \
  >/dev/null 2>&1 || echo "[entrypoint] warmup ping failed (non-fatal)"

# Signal readiness to docker healthcheck.
touch /tmp/ollama_ready
echo "[entrypoint] ready"

# Stay attached to the ollama server process.
wait $OLLAMA_PID
