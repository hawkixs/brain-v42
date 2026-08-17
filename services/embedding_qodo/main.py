"""
Brain v42 Embedding + Reranker Service — unified GPU inference.

Combines embedding (Qodo-Embed-1-1.5B) and cross-encoder reranking
(ms-marco-MiniLM-L-6-v2) in a single service to avoid running two
separate PyTorch processes.

API contract:
  POST /embed                     { texts: list[str] } -> number[][]
  POST /embed/query               { text: str }  OR  ?text=<str>  -> number[]
  POST /embed/single              { text: str }  OR  ?text=<str>  -> number[]
  POST /rerank                    { query: str, candidates: list[str] } -> { scores: list[float] }
  GET  /                          -> model info JSON
  GET  /healthz                   -> {"status": "ok"}

Note: /embed/query and /embed/single accept the text either as a URL
query parameter (legacy) or as a JSON body `{"text": "..."}` (preferred).
The body form removes the URL-length ceiling (~8KB) that made long
brain_learn insights unembeddable — see brain-v42 2026-04-24 audit.

GPU: nvidia-container-toolkit, falls back to CPU if unavailable.
"""

import torch
from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sentence_transformers import CrossEncoder, SentenceTransformer

EMBED_MODEL = "Qodo/Qodo-Embed-1-1.5B"
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
EXPECTED_DIMS = 1536
MODEL_CACHE = "/app/model_cache"

# Detect device at startup — GPU if available, CPU fallback
device = "cuda" if torch.cuda.is_available() else "cpu"

# Load models at module level.
# Weights are downloaded at Docker build time into /app/model_cache.
embed_model = SentenceTransformer(
    EMBED_MODEL,
    trust_remote_code=True,
    device=device,
    cache_folder=MODEL_CACHE,
    model_kwargs={"torch_dtype": torch.float16},
)

rerank_model = CrossEncoder(
    RERANK_MODEL,
    device=device,
    cache_folder=MODEL_CACHE,
)

app = FastAPI(title="brain-v42-embedding", version="2.0.0")


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedSingleRequest(BaseModel):
    text: str


class RerankRequest(BaseModel):
    query: str
    candidates: list[str]


class RerankResponse(BaseModel):
    scores: list[float]


# --- Info & Health ---


@app.get("/")
async def info() -> dict:
    """Return service info: models, dimensions, compute device."""
    return {
        "embed_model": EMBED_MODEL,
        "rerank_model": RERANK_MODEL,
        "dims": EXPECTED_DIMS,
        "device": device,
        "torch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
    }


@app.get("/healthz")
async def healthz() -> dict:
    """Health check — returns 200 OK once both models are loaded."""
    return {"status": "ok"}


# --- Legacy health endpoint (brain_v42 reranker compat) ---


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": RERANK_MODEL}


# --- Embedding endpoints ---


@app.post("/embed")
async def embed(request: EmbedRequest) -> list[list[float]]:
    """Batch embed texts. Returns raw number[][] (no wrapper dict)."""
    if not request.texts:
        return []
    embeddings = embed_model.encode(
        request.texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return embeddings.tolist()


def _resolve_text(q: str | None, body: EmbedSingleRequest | None) -> str:
    """Pick text from JSON body (preferred) or legacy ?text= query param.

    Body wins when both are present. HTTP 400 when neither is set.
    """
    if body is not None and body.text:
        return body.text
    if q is not None and q:
        return q
    raise HTTPException(
        status_code=400,
        detail='Missing \'text\' — provide via JSON body {"text": "..."} or ?text= query param',
    )


@app.post("/embed/query")
async def embed_query(
    text: str | None = Query(None, description="Query text to embed (legacy — prefer JSON body)"),
    body: EmbedSingleRequest | None = Body(None),  # noqa: B008 - FastAPI metadata
) -> list[float]:
    """Single query embed. Accepts JSON body {"text": "..."} (preferred) or ?text= query param."""
    actual_text = _resolve_text(text, body)
    embeddings = embed_model.encode(
        [actual_text],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return embeddings[0].tolist()


@app.post("/embed/single")
async def embed_single(
    text: str | None = Query(
        None, description="Document text to embed (legacy — prefer JSON body)"
    ),
    body: EmbedSingleRequest | None = Body(None),  # noqa: B008 - FastAPI metadata
) -> list[float]:
    """Single document embed. Accepts JSON body {"text": "..."} (preferred) or ?text= query param."""
    actual_text = _resolve_text(text, body)
    embeddings = embed_model.encode(
        [actual_text],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return embeddings[0].tolist()


# --- Reranking endpoint ---


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest) -> RerankResponse:
    """Score query-candidate relevance via cross-encoder."""
    if not req.candidates:
        return RerankResponse(scores=[])
    pairs = [[req.query, c] for c in req.candidates]
    scores = rerank_model.predict(pairs).tolist()
    return RerankResponse(scores=scores)
