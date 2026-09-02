"""Shim entrypoint — env wiring + uvicorn.

Env:
  LLAMA_URL               llama.cpp server URL (default http://embedding-llama:8080)
  ONNX_DIR                directory holding model.onnx + tokenizer.json (default /app/onnx)
  SHIM_BEARER_TOKEN_FILE  static bearer secret, a 0600 file (absent = no auth,
                          current contract unchanged — ticket 530d796a point (a))
  SHIM_BEARER_MODE        'optional' (default: accepts + logs) | 'required'
                          (401 — a SEPARATE operator gesture, after client ticket 9ef5c69d)
"""

from __future__ import annotations

import logging
import os

from shim_app import bearer_from_env, create_app
from shim_backends import LlamaEmbedBackend, OnnxRerankBackend
from starlette.applications import Starlette

# Without root wiring, the bearer census WARNING falls through to
# logging.lastResort: bare stderr, no timestamp and no level — unreadable next
# to uvicorn's access log. uvicorn configures ITS loggers, never the root.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def build_app() -> Starlette:
    llama_url = os.environ.get("LLAMA_URL", "http://embedding-llama:8080")
    onnx_dir = os.environ.get("ONNX_DIR", "/app/onnx")
    return create_app(
        LlamaEmbedBackend(llama_url),
        OnnxRerankBackend(f"{onnx_dir}/model.onnx", f"{onnx_dir}/tokenizer.json"),
        bearer=bearer_from_env(os.environ),
    )


app = build_app()

if __name__ == "__main__":
    import uvicorn  # container only (lazy)

    uvicorn.run(app, host="0.0.0.0", port=8003, workers=1)
