"""Cross-encoder reranking over the hybrid retriever's candidate set.

Bi-encoder retrieval (what `HybridIndex` does) embeds the query and every chunk
*independently*, so the score can only measure how close two summaries of
meaning are. A cross-encoder reads the query and the chunk **together** in one
forward pass and scores their actual relevance to each other. That is far more
accurate and far too slow to run over a whole corpus — hence the standard
shape: cheap retrieval to `candidate_k`, expensive reranking down to `top_k`.

This mattered here specifically. Before reranking, `log_evidence` questions
scored MRR 0.366: the right log chunk was usually *retrieved* and then ranked
below several near-identical neighbours. Ranking, not recall, was the weak part,
and a reranker attacks exactly that.

Runs on ONNX/CPU via FastEmbed with no API key, so it stays inside the property
that makes the eval harness runnable on every commit.
"""

from __future__ import annotations

import threading
from functools import lru_cache

from aiops.config import settings
from aiops.schemas import RetrievedChunk

_LOCK = threading.Lock()


@lru_cache(maxsize=2)
def _model(name: str):
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    from aiops.onnx_tuning import apply_onnx_memory_settings

    # The reranker dominates the memory profile — it scores 30 pairs of varying
    # length per query, which is exactly the shape churn the arena pools for.
    apply_onnx_memory_settings()

    return TextCrossEncoder(model_name=name)


class Reranker:
    """Query x chunk relevance scoring over a candidate list."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.reranker_model

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        with _LOCK:  # ONNX sessions are not guaranteed thread-safe
            return [float(s) for s in _model(self.model_name).rerank(query, texts)]

    def rerank(
        self,
        query: str,
        candidates: list[RetrievedChunk],
        top_k: int | None = None,
        *,
        blend: float | None = None,
    ) -> list[RetrievedChunk]:
        """Reorder `candidates` by cross-encoder relevance and cut to `top_k`.

        `blend` interpolates between the retriever's score and the reranker's.
        At 1.0 the reranker decides outright, which is the usual configuration;
        below that the first-stage score still carries weight. It is exposed
        because it is swept — a reranker that is *worse* than the retriever on
        some category should be discoverable rather than assumed away.

        The raw cross-encoder logit is preserved on `rerank_score` and the
        first-stage score is left untouched on `score`, so downstream
        confidence scoring can keep reading the retrieval signal it was
        calibrated against.
        """
        if not candidates:
            return []
        top_k = top_k or settings.top_k
        blend = settings.rerank_blend if blend is None else blend

        raw = self.score(query, [c.chunk.text for c in candidates])

        # Min-max within this candidate set: cross-encoder logits are unbounded
        # and their scale shifts with the query, so an absolute threshold or a
        # raw blend against a [0,1] retrieval score would be meaningless.
        lo, hi = min(raw), max(raw)
        span = (hi - lo) or 1.0

        for cand, logit in zip(candidates, raw, strict=True):
            cand.rerank_score = logit
            normalised = (logit - lo) / span
            cand.score = blend * normalised + (1.0 - blend) * cand.score

        ordered = sorted(candidates, key=lambda c: c.score, reverse=True)
        for rank, cand in enumerate(ordered):
            cand.rank = rank
        return ordered[:top_k]


@lru_cache(maxsize=1)
def get_reranker() -> Reranker:
    return Reranker()


def maybe_rerank(
    query: str, candidates: list[RetrievedChunk], top_k: int | None = None
) -> list[RetrievedChunk]:
    """Rerank when enabled, otherwise just cut to `top_k`.

    Kept as a single entry point so every caller gets the same behaviour and
    the feature can be switched off in one place — which the sweep needs in
    order to measure it.
    """
    top_k = top_k or settings.top_k
    if not settings.rerank_enabled or not candidates:
        return candidates[:top_k]
    return get_reranker().rerank(query, candidates, top_k=top_k)
