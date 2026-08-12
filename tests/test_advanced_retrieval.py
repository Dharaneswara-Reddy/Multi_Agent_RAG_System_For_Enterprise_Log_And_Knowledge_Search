"""Tests for the stage-2+ retrieval capabilities.

The cross-encoder itself is not exercised here — loading it costs seconds per
test and its quality is measured by `scripts/sweep_rerank.py`, which is the
right tool for "is this model good". What these tests cover is the logic
*around* it, which is where the silent breakage lives: blending, ordering,
score provenance, hop damping, and the invariants that stop an enhancement
from quietly inflating confidence.
"""

from __future__ import annotations

import numpy as np
import pytest

from aiops.config import settings
from aiops.retrieval.multihop import ReferenceGraph, extract_references
from aiops.retrieval.query import (
    deterministic_variants,
    identifier_query,
    imperative_query,
    keyword_query,
)
from aiops.retrieval.rerank import Reranker
from aiops.schemas import Chunk, RetrievedChunk, SourceType


def _chunk(doc_id: str, text: str, codes: list[str] | None = None, ordinal: int = 0) -> Chunk:
    return Chunk(
        chunk_id=f"{doc_id}::{ordinal}",
        doc_id=doc_id,
        text=text,
        source_type=SourceType.RUNBOOK,
        title=doc_id,
        error_codes=codes or [],
        ordinal=ordinal,
    )


def _hit(doc_id: str, text: str, score: float, **kw) -> RetrievedChunk:
    return RetrievedChunk(chunk=_chunk(doc_id, text, **kw), score=score, dense_score=score)


# --- query rewriting ------------------------------------------------------


def test_identifier_query_extracts_codes():
    assert identifier_query("why is PAY-5021 firing again") == "PAY-5021"
    assert identifier_query("see ADR-0052 and INC-2025-1007-01") == "ADR-0052 INC-2025-1007-01"


def test_identifier_query_is_none_without_identifiers():
    assert identifier_query("why do carts keep vanishing") is None


def test_keyword_query_strips_filler_but_keeps_domain_words():
    out = keyword_query("what should I do when the connection pool is exhausted")
    assert out is not None
    assert "connection" in out and "pool" in out and "exhausted" in out
    assert "what" not in out.split() and "the" not in out.split()


def test_imperative_query_drops_interrogative_opening():
    assert imperative_query("How do I fix pool exhaustion?") == "fix pool exhaustion"


def test_variants_always_lead_with_the_original_question():
    q = "Why does PAY-5021 keep firing?"
    variants = deterministic_variants(q)
    assert variants[0] == q, "the user's own phrasing must win ties in fusion"
    assert len(variants) == len(set(variants)), "duplicate variants waste a retrieval pass"


def test_variants_respect_the_configured_cap():
    variants = deterministic_variants("Why does PAY-5021 keep firing on checkout?", limit=2)
    assert len(variants) == 2


# --- reference extraction / multi-hop ------------------------------------


def test_extract_references_finds_all_identifier_kinds():
    text = "See ADR-0052 and RB-PAY-5021; this repeated INC-2025-1007-01."
    refs = extract_references(text)
    assert {"ADR-0052", "PAY-5021", "INC-2025-1007-01"} <= refs


def test_extract_references_does_not_return_adr_ids_as_error_codes():
    refs = extract_references("ADR-0052 explains it")
    assert refs == {"ADR-0052"}, "ADR ids match the code shape and must not double-count"


class _StubIndex:
    def __init__(self, chunks):
        self.chunks = chunks


def test_reference_graph_resolves_codes_adrs_and_incidents():
    chunks = [
        _chunk("RB-PAY-5021", "runbook body", codes=["PAY-5021"]),
        _chunk("ADR-0052-topic-repartition-procedure", "adr body"),
        _chunk("PM-2025-1007-01-event-bus-skew", "postmortem body"),
    ]
    graph = ReferenceGraph(_StubIndex(chunks))
    assert graph.resolve("PAY-5021")
    assert graph.resolve("ADR-0052")
    assert graph.resolve("INC-2025-1007-01")
    assert graph.resolve("NOPE-9999") == []


# --- reranking ------------------------------------------------------------


def test_rerank_reorders_by_cross_encoder_score(monkeypatch):
    """A reranker that cannot overturn the first stage is pointless."""
    candidates = [_hit("A", "a", 0.9), _hit("B", "b", 0.5), _hit("C", "c", 0.1)]
    monkeypatch.setattr(Reranker, "score", lambda self, q, texts: [0.0, 5.0, 1.0])

    out = Reranker().rerank("q", candidates, top_k=3, blend=1.0)
    assert [h.chunk.doc_id for h in out] == ["B", "C", "A"]
    assert [h.rank for h in out] == [0, 1, 2], "ranks must be renumbered after reordering"


def test_rerank_blend_zero_preserves_first_stage_order(monkeypatch):
    """blend=0 must be a true no-op so the ablation measures a real baseline."""
    candidates = [_hit("A", "a", 0.9), _hit("B", "b", 0.5), _hit("C", "c", 0.1)]
    monkeypatch.setattr(Reranker, "score", lambda self, q, texts: [0.0, 5.0, 1.0])

    out = Reranker().rerank("q", candidates, top_k=3, blend=0.0)
    assert [h.chunk.doc_id for h in out] == ["A", "B", "C"]


def test_rerank_preserves_raw_logit_and_dense_score(monkeypatch):
    """Confidence is calibrated on raw cosine; reranking must not overwrite it."""
    candidates = [_hit("A", "a", 0.82), _hit("B", "b", 0.44)]
    monkeypatch.setattr(Reranker, "score", lambda self, q, texts: [3.0, -1.0])

    out = Reranker().rerank("q", candidates, top_k=2, blend=1.0)
    assert out[0].dense_score == pytest.approx(0.82)
    assert out[0].rerank_score == pytest.approx(3.0)


def test_rerank_handles_identical_scores_without_dividing_by_zero(monkeypatch):
    candidates = [_hit("A", "a", 0.5), _hit("B", "b", 0.5)]
    monkeypatch.setattr(Reranker, "score", lambda self, q, texts: [2.0, 2.0])
    out = Reranker().rerank("q", candidates, top_k=2, blend=1.0)
    assert len(out) == 2 and all(np.isfinite(h.score) for h in out)


def test_rerank_of_empty_candidates_is_empty():
    assert Reranker().rerank("q", [], top_k=5) == []


# --- fusion ---------------------------------------------------------------


def test_rrf_is_insensitive_to_score_scale():
    """The whole point of RRF: only the ordering matters, never the magnitudes."""
    from aiops.retrieval.index import _rrf

    dense = np.array([0.9, 0.8, 0.1], dtype=np.float32)
    lexical = np.array([2.0, 40.0, 1.0], dtype=np.float32)
    scaled = lexical * 1000.0

    a = _rrf(dense, lexical, 0.5, settings.rrf_k)
    b = _rrf(dense, scaled, 0.5, settings.rrf_k)
    assert np.argsort(-a).tolist() == np.argsort(-b).tolist()


def test_rrf_weighting_shifts_the_winner():
    from aiops.retrieval.index import _rrf

    dense = np.array([0.9, 0.1], dtype=np.float32)
    lexical = np.array([0.1, 0.9], dtype=np.float32)

    dense_heavy = _rrf(dense, lexical, 1.0, settings.rrf_k)
    lexical_heavy = _rrf(dense, lexical, 0.0, settings.rrf_k)
    assert int(np.argmax(dense_heavy)) == 0
    assert int(np.argmax(lexical_heavy)) == 1


def test_multiquery_fusion_keeps_the_strongest_dense_score():
    """Fusion must not lose the best evidence for a chunk across variants."""
    from aiops.retrieval.pipeline import _rrf_merge

    weak = _hit("A", "a", 0.30)
    strong = _hit("A", "a", 0.77)
    merged = _rrf_merge([[weak], [strong]], settings.rrf_k, top_n=5)
    assert merged[0].dense_score == pytest.approx(0.77)
