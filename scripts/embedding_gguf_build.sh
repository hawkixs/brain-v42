#!/usr/bin/env bash
# Reproduit le GGUF Q8_0 de Qodo-Embed-1-1.5B (produit initialement le
# 2026-07-06). Sortie : $OUT_DIR/qodo-embed-1.5b-{f16,q8_0}.gguf
#
# Usage: scripts/embedding_gguf_build.sh [OUT_DIR]
set -euo pipefail

OUT_DIR="${1:-/home/hawixs/models/qodo-gguf}"
HF_REPO="Qodo/Qodo-Embed-1-1.5B"
SNAP_DIR="$OUT_DIR/hf_snapshot"

mkdir -p "$SNAP_DIR"

echo "→ download HF snapshot ($HF_REPO)"
docker run --rm -v "$OUT_DIR":/w python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de bash -c "
  pip install --quiet --no-cache-dir huggingface_hub &&
  python -c \"
from huggingface_hub import snapshot_download
snapshot_download('$HF_REPO', local_dir='/w/hf_snapshot',
                  allow_patterns=['*.json', '*.safetensors', '*.txt', '*.md', 'LICENSE'])
print('snapshot ok')\"
"

echo "→ convert F16"
docker run --rm -v "$OUT_DIR":/w ghcr.io/ggml-org/llama.cpp:full@sha256:0d70482d19f8a4a513e64c8cd839fa114070bfb0c29c8754d68f44691a8c5d22 \
  --convert /w/hf_snapshot --outfile /w/qodo-embed-1.5b-f16.gguf --outtype f16

echo "→ quantize Q8_0"
docker run --rm -v "$OUT_DIR":/w ghcr.io/ggml-org/llama.cpp:full@sha256:0d70482d19f8a4a513e64c8cd839fa114070bfb0c29c8754d68f44691a8c5d22 \
  --quantize /w/qodo-embed-1.5b-f16.gguf /w/qodo-embed-1.5b-q8_0.gguf Q8_0

ls -lh "$OUT_DIR"/*.gguf
echo "✓ done"
