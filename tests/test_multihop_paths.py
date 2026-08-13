"""Multi-hop traversal: the *path*, not just the destination.

Reaching the right document proves nothing about reasoning. A hop that follows
an irrelevant citation and lands somewhere useful scores identically on recall
to one that follows the citation the question turns on. These tests pin the
distinctions that make the two separable, and the ranking rules that decide
which document a citation actually leads to.
"""

from __future__ import annotations

import numpy as np

from aiops.retrieval.multihop import ReferenceGraph, expand, extract_references
from aiops.schemas import Chunk, RetrievedChunk, SourceType


def _chunk(doc_id: str, text: str, codes=None, source=SourceType.RUNBOOK) -> Chunk:
    return Chunk(
        chunk_id=f"{doc_id}::0",
        doc_id=doc_id,
        text=text,
        source_type=source,
        title=doc_id,
        error_codes=list(codes or []),
    )


class _FakeIndex:
    """Minimal index stand-in: identical unit vectors so ranking is decided by
    the hop policy rather than by similarity noise."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.matrix = np.ones((len(chunks), 4), dtype=np.float32) / 2.0
        self.embedder = self

    def embed_query(self, _text: str) -> np.ndarray:
        return np.ones(4, dtype=np.float32) / 2.0


def _hit(chunk: Chunk, score: float = 0.9) -> RetrievedChunk:
    return RetrievedChunk(chunk=chunk, score=score, dense_score=score)


# --- edge provenance ------------------------------------------------------


def test_hop_records_the_edge_it_followed():
    """Without hop_from/hop_via, path correctness cannot be measured at all."""
    source = _chunk("RB-ACC-3301", "decrypt failures, see SEC-9002", codes=["ACC-3301"])
    target = _chunk("RB-SEC-9002", "retired secret version", codes=["SEC-9002"])
    index = _FakeIndex([source, target])

    out, hops = expand("why", [_hit(source)], index)
    assert hops == 1
    hop = next(h for h in out if h.provenance == "reference_hop")
    assert hop.chunk.doc_id == "RB-SEC-9002"
    assert hop.hop_from == "RB-ACC-3301"
    assert hop.hop_via == "SEC-9002"
    assert hop.hop == 1


# --- definer preference ---------------------------------------------------


def test_hop_prefers_the_document_that_defines_the_identifier():
    """A citation means 'see the thing that explains this', not 'see anything
    mentioning it'. Mentioning documents are what direct retrieval is for."""
    source = _chunk("RB-ACC-3301", "see SEC-9002 for the cause", codes=["ACC-3301"])
    definer = _chunk("RB-SEC-9002", "retired version still referenced", codes=["SEC-9002"])
    mention = _chunk(
        "PM-2025-0521-02-checkout-address-key",
        "this incident also involved SEC-9002",
        source=SourceType.POSTMORTEM,
    )
    index = _FakeIndex([source, mention, definer])

    out, _ = expand("why", [_hit(source)], index, per_hop_cap=1)
    hop = next(h for h in out if h.provenance == "reference_hop")
    assert hop.chunk.doc_id == "RB-SEC-9002", "a mention outranked the defining runbook"


def test_definer_recognised_when_the_id_does_not_contain_the_code():
    """RB-INVENTORY-POOL owns INV-3007 without naming it in the document id."""
    graph = ReferenceGraph(
        _FakeIndex([_chunk("RB-INVENTORY-POOL", "pool exhaustion", codes=["INV-3007"])])
    )
    assert graph.resolve_ranked("INV-3007") == [(0, True)]


def test_service_doc_listing_a_code_is_a_mention_not_a_definition():
    graph = ReferenceGraph(
        _FakeIndex([
            _chunk("SVC-inventory-service", "codes: INV-3007", codes=["INV-3007"],
                   source=SourceType.SERVICE_DOC)
        ])
    )
    assert graph.resolve_ranked("INV-3007") == [(0, False)]


# --- what must not be hopped to ------------------------------------------


def test_logs_are_never_hop_destinations():
    """Log chunks carry error codes as metadata, so an unfiltered hop lands on
    raw log lines instead of the runbook that explains the code."""
    source = _chunk("RB-SUPP-7740", "starves the pool, see INV-3007", codes=["SUPP-7740"])
    log = _chunk("log-meridian-platform", "INV-3007 timeout", codes=["INV-3007"],
                 source=SourceType.LOG)
    index = _FakeIndex([source, log])

    out, hops = expand("why", [_hit(source)], index)
    assert hops == 0
    assert all(h.chunk.source_type != SourceType.LOG for h in out)


def test_self_enumeration_is_not_a_cross_reference():
    """Service docs list every code they own; hopping on those turns them into
    hubs that spray hops across a service's unrelated faults."""
    svc = _chunk(
        "SVC-supplier-sync",
        "Error codes: SUPP-7702, SUPP-7740",
        codes=["SUPP-7702", "SUPP-7740"],
        source=SourceType.SERVICE_DOC,
    )
    other = _chunk("RB-SUPP-7702", "truncated feed", codes=["SUPP-7702"])
    index = _FakeIndex([svc, other])

    _out, hops = expand("why", [_hit(svc)], index)
    assert hops == 0, "a document's own codes must not generate hops"


def test_document_does_not_hop_to_itself():
    source = _chunk("RB-CFG-1120", "CFG-1120 broadcast partial", codes=["CFG-1120"])
    index = _FakeIndex([source])
    _out, hops = expand("why", [_hit(source)], index)
    assert hops == 0


# --- containment ----------------------------------------------------------


def test_hops_rank_below_every_direct_hit():
    """Supporting evidence must not change what an answer is about."""
    source = _chunk("RB-ACC-3301", "see SEC-9002", codes=["ACC-3301"])
    target = _chunk("RB-SEC-9002", "cause", codes=["SEC-9002"])
    index = _FakeIndex([source, target])

    out, _ = expand("why", [_hit(source, score=0.4)], index)
    hop = next(h for h in out if h.provenance == "reference_hop")
    assert hop.score < 0.4


def test_per_hop_cap_is_respected():
    # Codes must be 2-6 uppercase letters to match the extractor — the same
    # shape real codes use (PAY-5021), so single-letter fixtures silently
    # extract nothing and the test would pass for the wrong reason.
    source = _chunk("RB-AA-0001", "see BB-1111 and CC-2222 and DD-3333", codes=["AA-0001"])
    targets = [
        _chunk("RB-BB-1111", "b", codes=["BB-1111"]),
        _chunk("RB-CC-2222", "c", codes=["CC-2222"]),
        _chunk("RB-DD-3333", "d", codes=["DD-3333"]),
    ]
    index = _FakeIndex([source, *targets])
    _out, hops = expand("why", [_hit(source)], index, per_hop_cap=2)
    assert hops == 2


def test_disabling_multihop_takes_no_hops():
    source = _chunk("RB-AA-0001", "see BB-1111", codes=["AA-0001"])
    index = _FakeIndex([source, _chunk("RB-BB-1111", "b", codes=["BB-1111"])])
    # Guard against passing for the wrong reason: with hops enabled this
    # fixture must produce one.
    _enabled, enabled_hops = expand("why", [_hit(source)], index)
    assert enabled_hops == 1

    out, hops = expand("why", [_hit(source)], index, max_hops=0)
    assert hops == 0 and len(out) == 1


# --- reference extraction -------------------------------------------------


def test_extract_references_separates_adrs_from_error_codes():
    refs = extract_references("caused by SEC-9002, decided in ADR-0027, repeat of INC-2025-1103")
    assert refs == {"SEC-9002", "ADR-0027", "INC-2025-1103"}
