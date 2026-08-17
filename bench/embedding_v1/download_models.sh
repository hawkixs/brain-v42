#!/usr/bin/env bash
# Populate the `bench_models` docker volume on dev-pc with the Qwen3-4B GGUF.
# jina-v3 HF weights are auto-downloaded on first container start into
# /models/jina (same volume). Qodo model is baked into its image.
set -euo pipefail

CONTEXT="${DOCKER_CONTEXT:-dev-pc}"
VOLUME="bench_models"
QWEN_URL="https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/resolve/main/Qwen3-Embedding-4B-Q4_K_M.gguf"
QWEN_FILE="qwen3-embedding-4b-q4_k_m.gguf"

echo "→ using docker context: $CONTEXT"

# Create volume if missing (idempotent).
docker --context "$CONTEXT" volume create "$VOLUME" >/dev/null

# Check if the Qwen GGUF is already in the volume.
if docker --context "$CONTEXT" run --rm -v "$VOLUME:/models" alpine:3.20 \
    test -s "/models/$QWEN_FILE"; then
  size=$(docker --context "$CONTEXT" run --rm -v "$VOLUME:/models" alpine:3.20 \
    sh -c "du -h /models/$QWEN_FILE | cut -f1")
  echo "✓ Qwen3-4B already present ($size)"
else
  echo "→ downloading Qwen3-Embedding-4B-Q4_K_M (~2.5 GiB) into volume $VOLUME …"
  # alpine runs as root by default, so it can write the volume mount.
  # curlimages/curl uses a non-root user and fails with permission denied.
  docker --context "$CONTEXT" run --rm -v "$VOLUME:/models" \
    alpine:3.20 sh -c "
      apk add --no-cache curl >/dev/null &&
      curl -L --fail --progress-bar -o /models/$QWEN_FILE '$QWEN_URL'
    "
  echo "✓ download complete"
fi

echo ""
echo "Volume inventory:"
docker --context "$CONTEXT" run --rm -v "$VOLUME:/models" alpine:3.20 ls -lh /models
