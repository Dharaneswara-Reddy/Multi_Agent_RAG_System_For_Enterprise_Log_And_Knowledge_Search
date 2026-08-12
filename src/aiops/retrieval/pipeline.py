"""The full retrieval pipeline, composed as one entry point.

    query
      -> rewrite into N formulations              (multi-query)
      -> hybrid search per formulation            (dense + BM25, RRF or blend)
      -> fuse the N ranked lists                  (RRF across variants)
      -> cross-encoder rerank the candidate pool  (stage 2)
      -> follow explicit cross-references         (multi-hop)
      -> ranked chunks

Every stage is individually switchable, which is not decoration: it is what
lets `scripts/ablate.py` attribute a metric change to a specific component.
A stage that cannot be turned off cannot be shown to be worth having.

Stage order is deliberate. Reranking runs *before* hop expansion so the
cross-encoder sees only genuinely retrieved candidates — reranking hop results
against the original query would let a cited-but-tangential document outrank a
direct answer, which is the failure mode the damping in `multihop` exists to
prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiops.config import settings
from aiops.retrieval.index import HybridIndex, get_index
from aiops.schemas import RetrievedChunk


@dataclass
class RetrievalTrace:
    """What each stage did. Surfaced in the UI and attached to the OTel span."""

    variants: list[str] = field(default_factory=list)
    candidates: int = 0
    reranked: bool = False
    hops: int = 0
    stages: list[str] = field(default_factory=list)


def _rrf_merge(
    lists: list[list[RetrievedChunk]], rrf_k: int, top_n: int
) -> list[RetrievedChunk]:
    """Fuse several ranked lists over the same corpus by reciprocal rank.

    Used across *query variants* rather than across retrievers. The property
    that matters is the same: variants disagree about score scale (a short
    identifier query produces very different magnitudes from a full sentence),
    and rank fusion is indifferent to that.
    """
    scores: dict[str, float] = {}
    best: dict[str, RetrievedChunk] = {}
    for ranked in lists:
        for rank, hit in enumerate(ranked):
            cid = hit.chunk.chunk_id
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
            # Keep the appearance with the strongest raw cosine: confidence
            # scoring reads dense_score and must see the best evidence for the
            # chunk, not whichever variant happened to be scored last.
            if cid not in best or hit.dense_score > best[cid].dense_score:
                best[cid] = hit
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    out: list[RetrievedChunk] = []
    for rank, (cid, score) in enumerate(ordered[:top_n]):
        hit = best[cid].model_copy()
        hit.score = score
        hit.rank = rank
        out.append(hit)
    return out


def retrieve(
    question: str,
    index: HybridIndex | None = None,
    *,
    top_k: int | None = None,
    services: list[str] | None = None,
    source_types: list[str] | None = None,
    error_codes: list[str] | None = None,
    multiquery: bool | None = None,
    rerank: bool | None = None,
    multihop: bool | None = None,
    fusion: str | None = None,
    dense_weight: float | None = None,
) -> tuple[list[RetrievedChunk], RetrievalTrace]:
    """Run the pipeline. Returns (hits, trace)."""
    from aiops.retrieval.multihop import expand as hop_expand
    from aiops.retrieval.query import deterministic_variants, llm_variants
    from aiops.retrieval.rerank import maybe_rerank

    index = index or get_index()
    top_k = top_k or settings.top_k
    use_mq = settings.multiquery_enabled if multiquery is None else multiquery
    use_rr = settings.rerank_enabled if rerank is None else rerank
    use_mh = settings.multihop_enabled if multihop is None else multihop

    trace = RetrievalTrace()

    if use_mq:
        variants = llm_variants(question) if settings.multiquery_use_llm else deterministic_variants(question)
    else:
        variants = [question]
    trace.variants = variants
    trace.stages.append(f"query x{len(variants)}")

    pool = max(top_k, settings.candidate_k) if use_rr else top_k

    ranked_lists = [
        index.search(
            variant,
            top_k=pool,
            services=services,
            source_types=source_types,
            error_codes=error_codes,
            dense_weight=dense_weight,
            fusion=fusion,
        )
        for variant in variants
    ]
    candidates = (
        ranked_lists[0]
        if len(ranked_lists) == 1
        else _rrf_merge(ranked_lists, settings.rrf_k, pool)
    )
    trace.candidates = len(candidates)
    trace.stages.append(f"fuse -> {len(candidates)}")

    if use_rr and candidates:
        candidates = maybe_rerank(question, candidates, top_k=top_k)
        trace.reranked = True
        trace.stages.append(f"rerank -> {len(candidates)}")
    else:
        candidates = candidates[:top_k]

    if use_mh and candidates:
        candidates, hops = hop_expand(question, candidates, index)
        trace.hops = hops
        if hops:
            trace.stages.append(f"hop +{hops}")

    _restate_dense_against_question(question, candidates, index)
    return candidates, trace


def _restate_dense_against_question(
    question: str, hits: list[RetrievedChunk], index: HybridIndex
) -> None:
    """Recompute `dense_score` against the *original* question.

    Confidence reads `top_dense` as its retrieval signal, and its floor is
    calibrated on cosines measured for questions as users actually phrase them.
    Multi-query retrieval breaks that silently: fusion keeps whichever variant
    scored a chunk highest, so an aggressive rewrite inflates `top_dense` for
    every query — including off-corpus ones, which is precisely where an
    inflated score stops an escalation that should have happened.

    This cost a real regression. After multi-query landed, "what is the airspeed
    velocity of an unladen swallow" scored 0.61 and was answered rather than
    escalated, because the keyword variant "airspeed velocity unladen swallow"
    matched the corpus better than the sentence did.

    Rewrites are allowed to improve *what is found*; they are not allowed to
    change *how confident we are* that the corpus covers the question.
    """
    if not hits:
        return
    qv = index.embedder.embed_query(question)
    positions = {c.chunk_id: i for i, c in enumerate(index.chunks)}
    for hit in hits:
        position = positions.get(hit.chunk.chunk_id)
        if position is not None:
            hit.dense_score = float(index.matrix[position] @ qv)
