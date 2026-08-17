# ============================================================
# Dockerfile — Brain v42 Embedding + Reranker Service
#
# Unified GPU inference: Qodo-Embed-1-1.5B (embeddings, 1536d)
# + ms-marco-MiniLM-L-6-v2 (cross-encoder reranking).
#
# Model weights are downloaded at build time into /app/model_cache
# for fast container starts (no network fetch at runtime).
#
# GPU: requires nvidia-container-toolkit on the Docker host.
# CPU fallback: works without GPU (slower inference).
# ============================================================

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch (CUDA 12.1) — cached separately from other deps
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cu121

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    "transformers>=4.39.2,<4.47.0" \
    sentence-transformers \
    pydantic

# Download both models at build time
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer( \
    'Qodo/Qodo-Embed-1-1.5B', \
    trust_remote_code=True, \
    cache_folder='/app/model_cache' \
); \
CrossEncoder( \
    'cross-encoder/ms-marco-MiniLM-L-6-v2', \
    cache_folder='/app/model_cache' \
); \
print('Both models downloaded successfully') \
"

COPY main.py .

EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=120s \
    CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:8003/healthz || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "1"]
