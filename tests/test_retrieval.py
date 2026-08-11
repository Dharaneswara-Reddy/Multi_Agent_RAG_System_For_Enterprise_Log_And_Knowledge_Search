"""Retrieval, chunking, and context-assembly tests."""

from __future__ import annotations

import numpy as np
import pytest

from aiops.ingestion.documents import _pack, _parse_frontmatter, _split_sections, load_documents
from aiops.retrieval.context import assemble
from aiops.retrieval.index import HybridIndex, _minmax, _tokenize, get_index
from aiops.schemas import Chunk, RetrievedChunk, SourceType

# --- tokenizer ------------------------------------------------------------


def test_error_codes_survive_tokenization():
    """Splitting PAY-5021 on the hyphen would destroy lexical search for the
    single most searchable string in the corpus."""
    assert "pay-5021" in _tokenize("What does PAY-5021 mean?")


def test_tokenizer_lowercases_and_splits_words():
    assert _tokenize("Connection Pool Exhausted!") == ["connection", "pool", "exhausted"]


# --- score normalisation --------------------------------------------------


def test_minmax_handles_constant_and_empty_input():
    assert _minmax(np.array([], dtype=np.float32)).size == 0
    out = _minmax(np.array([0.5, 0.5, 0.5], dtype=np.float32))
    assert np.allclose(out, 1.0)


def test_minmax_ignores_masked_infinities():
    out = _minmax(np.array([-np.inf, 0.0, 1.0], dtype=np.float32))
    assert out[0] == 0.0
    assert out[2] == pytest.approx(1.0)


# --- chunking -------------------------------------------------------------


def test_frontmatter_parses_scalars_and_lists():
    raw = '---\ntitle: "A Doc"\nservice: payment-service\nerror_codes: [PAY-5021, ORD-4102]\n---\n# Body\ntext'
    meta, body = _parse_frontmatter(raw)
    assert meta["title"] == "A Doc"
    assert meta["error_codes"] == ["PAY-5021", "ORD-4102"]
    assert body.startswith("# Body")


def test_sections_carry_their_heading_path():
    body = "# Top\nintro\n\n## Sub A\nalpha\n\n## Sub B\nbeta"
    sections = _split_sections(body)
    paths = [p for p, _ in sections]
    assert "Top > Sub A" in paths
    assert "Top > Sub B" in paths


def test_pack_keeps_substantial_sections_separate():
    """Section boundaries are author-chosen semantic units; merging them to fill
    a token budget dilutes the embedding across two topics."""
    big_a = ("alpha " * 90).strip()
    big_b = ("beta " * 90).strip()
    packed = _pack([("H > A", big_a), ("H > B", big_b)], budget=320, overlap=64)
    assert len(packed) == 2


def test_pack_merges_tiny_sections():
    packed = _pack([("H > A", "short one"), ("H > B", "short two")], budget=320, overlap=64)
    assert len(packed) == 1


def test_pack_splits_oversized_section():
    huge = "\n\n".join(["para " * 60] * 10)
    packed = _pack([("H > Big", huge)], budget=200, overlap=40)
    assert len(packed) > 1


def test_documents_load_with_metadata():
    chunks = load_documents()
    assert chunks, "no documents found — run `python -m aiops.ingestion.corpus`"
    runbooks = [c for c in chunks if c.source_type is SourceType.RUNBOOK]
    assert runbooks
    payment = [c for c in chunks if c.doc_id == "RB-PAYMENT-TIMEOUT"]
    assert payment
    assert "PAY-5021" in payment[0].error_codes
    # heading path is embedded so the subject noun is present even when the body
    # never repeats it
    assert payment[0].text.startswith("[Runbook:")


# --- search ---------------------------------------------------------------


@pytest.fixture(scope="module")
def index() -> HybridIndex:
    return get_index()


@pytest.mark.parametrize(
    "query,expected_doc",
    [
        ("how do I fix connection pool exhaustion", "RB-INVENTORY-POOL"),
        ("JWT token not yet valid clock skew", "RB-AUTH-CLOCKSKEW"),
        ("poison message dead letter queue", "RB-NOTIF-POISON"),
        ("search index rebuild out of memory", "RB-SEARCH-OOM"),
        ("schema registry compatibility mode", "ADR-0007-schema-registry"),
    ],
)
def test_known_questions_retrieve_the_right_document(index, query, expected_doc):
    hits = index.search(query, top_k=8)
    assert expected_doc in {h.chunk.doc_id for h in hits}


def test_error_code_lookup_ranks_its_runbook_first(index):
    hits = index.search("PAY-5021", top_k=3)
    assert hits[0].chunk.doc_id == "RB-PAYMENT-TIMEOUT"


def test_source_type_filter_is_respected(index):
    hits = index.search("checkout failure", top_k=10, source_types=["log"])
    assert hits
    assert all(h.chunk.source_type is SourceType.LOG for h in hits)


def test_off_corpus_query_scores_below_relevance_floor(index):
    """The separation this relies on is measured by scripts/calibrate.py."""
    from aiops.guardrails.rules import RELEVANCE_FLOOR

    hits = index.search("who won the 1998 world cup", top_k=1)
    assert hits[0].dense_score < RELEVANCE_FLOOR


def test_on_corpus_query_scores_above_relevance_floor(index):
    from aiops.guardrails.rules import RELEVANCE_FLOOR

    hits = index.search("why did checkout fail", top_k=1)
    assert hits[0].dense_score > RELEVANCE_FLOOR


# --- context assembly -----------------------------------------------------


def _chunk(doc_id: str, ordinal: int, text: str = "body text here") -> Chunk:
    return Chunk(
        chunk_id=f"{doc_id}-{ordinal}",
        doc_id=doc_id,
        text=text,
        source_type=SourceType.RUNBOOK,
        title=doc_id,
        ordinal=ordinal,
    )


def test_per_document_cap_forces_source_diversity():
    hits = [
        RetrievedChunk(chunk=_chunk("SAME", i), score=1.0 - i * 0.01, rank=i) for i in range(8)
    ]
    ctx = assemble(hits, None, per_doc_cap=3, max_chars=100_000)
    # overflow is still appended when budget allows, but the first three win
    assert [r.chunk.ordinal for r in ctx.used[:3]] == [0, 1, 2]


def test_character_budget_is_enforced_and_reported():
    hits = [
        RetrievedChunk(chunk=_chunk(f"D{i}", 0, "x" * 900), score=1.0 - i * 0.01, rank=i)
        for i in range(10)
    ]
    ctx = assemble(hits, None, max_chars=2500)
    assert len(ctx.text) <= 2600  # allows for the per-source tag wrapper
    assert ctx.dropped_for_budget > 0


def test_top_dense_is_raw_cosine_not_normalised_rank():
    hits = [
        RetrievedChunk(chunk=_chunk("A", 0), score=1.0, dense_score=0.62, rank=0),
        RetrievedChunk(chunk=_chunk("B", 0), score=0.4, dense_score=0.51, rank=1),
    ]
    ctx = assemble(hits, None)
    assert ctx.top_score == 1.0
    assert ctx.top_dense == pytest.approx(0.62)


def test_empty_hits_produce_empty_context():
    ctx = assemble([], None)
    assert ctx.is_empty
    assert ctx.text == ""
