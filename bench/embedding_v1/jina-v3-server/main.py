"""Bench wrapper for jinaai/jina-embeddings-v3.

Compatible with the same API surface as the Qodo service:
  POST /embed    {texts: list[str]}                    -> list[list[float]]
  GET  /healthz  -> {"status":"ok"}
  GET  /         -> model info

Model loads once at startup. First call warms up. Uses retrieval.passage task
by default. Query encoding uses retrieval.query task when ?task=query is passed.
"""

from __future__ import annotations

import os

import torch
from fastapi import FastAPI, Query
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

MODEL_NAME = "jinaai/jina-embeddings-v3"
CACHE_DIR = os.environ.get("HF_HOME", "/models/jina")
EXPECTED_DIMS = 1024

device = "cuda" if torch.cuda.is_available() else "cpu"

model = SentenceTransformer(
    MODEL_NAME,
    trust_remote_code=True,
    device=device,
    cache_folder=CACHE_DIR,
    model_kwargs={"torch_dtype": torch.float16},
)

app = FastAPI(title="bench-jina-v3", version="1.0.0")


class EmbedRequest(BaseModel):
    texts: list[str]


@app.get("/")
async def info() -> dict:
    return {
        "model": MODEL_NAME,
        "dims": EXPECTED_DIMS,
        "device": device,
        "torch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/embed")
async def embed(
    request: EmbedRequest,
    task: str = Query("retrieval.passage", description="jina task prompt"),
) -> list[list[float]]:
    """Batch embed. task ∈ {retrieval.passage, retrieval.query, ...}.

    Default is retrieval.passage (corpus side). The bench runner passes
    ?task=retrieval.query for query-side encoding.
    """
    if not request.texts:
        return []
    embeddings = model.encode(
        request.texts,
        task=task,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return embeddings.tolist()
