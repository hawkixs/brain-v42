# Bench-only Qodo container — identical API to services/embedding/main.py
# but on PyTorch 2.7.1 + cu128 so it can run on RTX 5070 Ti (Blackwell).
# Prod image stays untouched (runs on RTX 1000 Ada, sm_89).
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    "uvicorn[standard]==0.30.6" \
    pydantic==2.9.2 \
    "transformers>=4.39.2,<4.47.0" \
    "sentence-transformers>=5.0"

# Pre-download weights so container start is fast.
# NOTE: sentence-transformers 3.2+ renamed CrossEncoder's cache_folder → cache_dir.
# The prod service doesn't pin the version so it got an older release; we pin it here.
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('Qodo/Qodo-Embed-1-1.5B', trust_remote_code=True, cache_folder='/app/model_cache'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2', cache_folder='/app/model_cache'); \
print('models cached')"

# Reuse the prod service code verbatim.
COPY main.py .

EXPOSE 8003
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
