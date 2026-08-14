"""FastEmbed wrapper.

FastEmbed runs BAAI/bge-small-en-v1.5 through ONNX Runtime on CPU: no torch, no
GPU, ~130MB of model. That makes the whole retrieval stack runnable on a laptop
and in CI, which is the property that lets the eval harness run on every commit.

The model is loaded lazily and cached — importing this module must stay cheap so
the API process can start before the first request pays the load cost.
"""

from __future__ import annotations

import threading
from functools import lru_cache

import numpy as np

from aiops.config import settings

_LOCK = threading.Lock()


@lru_cache(maxsize=2)
def _model(name: str):
    from fastembed import TextEmbedding

    from aiops.onnx_tuning import apply_onnx_memory_settings

    # Must precede construction: the setting is installed as the default for
    # sessions created afterwards, and FastEmbed builds its session here.
    apply_onnx_memory_settings()

    return TextEmbedding(model_name=name)


class Embedder:
    """Batch text -> L2-normalised float32 vectors.

    Vectors are normalised at creation so cosine similarity is a plain dot
    product downstream — the index does one matmul per query instead of a
    per-row norm.
    """

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.embedding_model
        self.dim = settings.embedding_dim

    def _encode(self, texts: list[str]) -> np.ndarray:
        with _LOCK:  # ONNX sessions are not guaranteed thread-safe
            vectors = list(_model(self.model_name).embed(texts))
        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    def embed_documents(self, texts: list[str], batch_size: int = 128) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out = [self._encode(texts[i : i + batch_size]) for i in range(0, len(texts), batch_size)]
        return np.vstack(out)

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode([text])[0]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    return Embedder()
