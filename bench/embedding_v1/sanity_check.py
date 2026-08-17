"""Wait for all 3 bench services to be healthy, then smoke-test /embed.

Run after `docker compose up -d`. Exits 0 on success, 1 on any failure.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import dataclass

DEV_PC = "192.168.1.11"

# Qwen3 via llama.cpp uses a different endpoint path and response shape,
# so we normalize per-candidate in _embed_one. Qwen3 native dim is 2560,
# truncated via Matryoshka to 1024 for fair head-to-head vs jina-v3.
CANDIDATES = [
    ("qodo", f"http://{DEV_PC}:8023/embed", 1536, "native", None),
    ("qwen3-4b", f"http://{DEV_PC}:8024/embedding", 1024, "llama_cpp", 1024),
    ("jina-v3", f"http://{DEV_PC}:8025/embed", 1024, "native", None),
]


@dataclass
class SanityResult:
    name: str
    ok: bool
    dim: int | None
    latency_ms: float
    error: str | None = None


def _post(url: str, payload: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _embed_one(url: str, backend: str, text: str, matryoshka_dim: int | None = None) -> list[float]:
    """Return a single embedding vector, normalizing across backends."""
    if backend == "native":
        data = _post(url, {"texts": [text]})
        # both qodo and jina return list[list[float]]
        return data[0]
    if backend == "llama_cpp":
        # llama.cpp /embedding with {"content": str} returns
        # [{"index":0, "embedding":[[float,...]]}] — embedding is a list-of-lists
        # even for single-string input. Take the first (and only) inner list.
        data = _post(url, {"content": text})
        raw = data[0]["embedding"] if isinstance(data, list) else data["embedding"]
        vec = raw[0] if raw and isinstance(raw[0], list) else raw
        # Matryoshka truncation: qwen3 native=2560, bench dim=1024 → truncate + renorm.
        if matryoshka_dim is not None and len(vec) > matryoshka_dim:
            vec = vec[:matryoshka_dim]
        # llama.cpp doesn't normalize by default
        import math

        n = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / n for v in vec]
    raise ValueError(f"unknown backend: {backend}")


def check_candidate(
    name: str, url: str, expected_dim: int, backend: str, matryoshka: int | None
) -> SanityResult:
    start = time.monotonic()
    try:
        vec = _embed_one(url, backend, "brain-v42 embedding bench sanity check", matryoshka)
        latency = (time.monotonic() - start) * 1000
        dim = len(vec)
        if dim != expected_dim:
            return SanityResult(
                name, False, dim, latency, error=f"wrong dim: got {dim}, expected {expected_dim}"
            )
        return SanityResult(name, True, dim, latency)
    except Exception as exc:  # noqa: BLE001
        latency = (time.monotonic() - start) * 1000
        return SanityResult(name, False, None, latency, error=str(exc))


def main() -> int:
    print(f"{'candidate':<12} {'status':<6} {'dim':>6} {'latency':>10} {'error':<50}")
    print("-" * 90)
    all_ok = True
    for name, url, exp_dim, backend, matryoshka in CANDIDATES:
        r = check_candidate(name, url, exp_dim, backend, matryoshka)
        status = "OK" if r.ok else "FAIL"
        err = (r.error or "")[:50]
        print(f"{name:<12} {status:<6} {r.dim!s:>6} {r.latency_ms:>8.1f}ms {err:<50}")
        if not r.ok:
            all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
