"""Entrypoint du shim — wiring env + uvicorn.

Env:
  LLAMA_URL               URL du serveur llama.cpp (défaut http://embedding-llama:8080)
  ONNX_DIR                dossier contenant model.onnx + tokenizer.json (défaut /app/onnx)
  SHIM_BEARER_TOKEN_FILE  secret bearer statique, fichier 0600 (absent = pas d'auth,
                          contrat actuel inchangé — ticket 530d796a point (a))
  SHIM_BEARER_MODE        'optional' (défaut : accepte + journalise) | 'required'
                          (401 — geste opérateur SÉPARÉ, après le ticket client 9ef5c69d)
"""

from __future__ import annotations

import os

from shim_app import bearer_from_env, create_app
from shim_backends import LlamaEmbedBackend, OnnxRerankBackend
from starlette.applications import Starlette


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
