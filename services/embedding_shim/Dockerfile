# ============================================================
# Dockerfile — Brain v42 Embedding Shim
#
# Traduit le contrat legacy (:8003 /embed /embed/query /rerank)
# vers llama.cpp server (/v1/embeddings) + exécute le reranker
# cross-encoder en ONNX CPU (pas de PyTorch : image ~600 MB au
# lieu de ~8 GB, RAM ~300 MB au lieu de 12.8 GB).
#
# L'ONNX pré-exporté vient de Xenova/ms-marco-MiniLM-L-6-v2
# (même modèle que cross-encoder/ms-marco-MiniLM-L-6-v2) — la
# parity des scores est validée par scripts/embedding_cutover_check.py
# (gate Pearson >= 0.995 vs le service PyTorch).
# ============================================================

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app

RUN pip install --no-cache-dir \
    starlette \
    "uvicorn[standard]" \
    httpx \
    anyio \
    onnxruntime \
    tokenizers \
    numpy \
    huggingface_hub

# Modèle ONNX + tokenizer téléchargés au build (démarrage sans réseau).
RUN python -c "\
from huggingface_hub import hf_hub_download; \
p1 = hf_hub_download('Xenova/ms-marco-MiniLM-L-6-v2', 'onnx/model.onnx'); \
p2 = hf_hub_download('Xenova/ms-marco-MiniLM-L-6-v2', 'tokenizer.json'); \
import shutil, os; os.makedirs('/app/onnx', exist_ok=True); \
shutil.copy(p1, '/app/onnx/model.onnx'); \
shutil.copy(p2, '/app/onnx/tokenizer.json'); \
print('ONNX assets ready')"

COPY shim_backends.py shim_app.py main.py ./

EXPOSE 8003

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "1"]
