"""Multi-hop retrieval by following explicit cross-references.

Single-shot retrieval answers "which chunks look like this question". It cannot
answer "and what does the document *those* chunks point at say", which is what
a causal question actually needs: a runbook names an anti-pattern and cites the
ADR that explains why, or a post-mortem cites the incident it repeated.

Two hop strategies, both deliberate:

1. **Reference hops.** Documents in this corpus cite each other by identifier —
   error codes (`PAY-5021`), ADRs (`ADR-0052`), incidents (`INC-2025-1007-01`).
   Those identifiers are extracted from retrieved text and resolved to
   documents. This is a *graph* hop over real edges, not a guess: if a chunk
   says "see ADR-0052", the edge exists and following it is not speculative.

2. **Trace hops.** Log chunks sharing a trace id, already handled in
   `context.assemble`. Left there because it is a property of context
   assembly rather than of retrieval.

The design constraint that matters: a hop must not be able to flood the
context. Hops enter *below* every first-stage result and are capped, because a
question is asked about the thing retrieved directly — the hop is supporting
evidence, not a replacement for the answer.
"""

from __future__ import annotations

import re

from aiops.config import settings
from aiops.retrieval.index import HybridIndex
from aiops.schemas import RetrievedChunk

# Identifiers as they appear in document text.
ERROR_CODE_RE = re.compile(r"\b([A-Z]{2,6}-\d{4})\b")
ADR_RE = re.compile(r"\b(ADR-\d{4})\b")
INCIDENT_RE = re.compile(r"\b(INC-\d{4}-\d{4}(?:-\d{2})?)\b")


def extract_references(text: str) -> set[str]:
    """Document identifiers referenced by a chunk of text.

    Returns identifier *stems* rather than filenames: an error code maps to
    several documents (its runbook, its service doc, the ADRs citing it), so
    resolution is a lookup rather than a string concatenation.
    """
    refs: set[str] = set()
    refs.update(ADR_RE.findall(text))
    refs.update(INCIDENT_RE.findall(text))
    # ADR-/INC- identifiers also match the error-code shape; exclude them.
    refs.update(
        code
        for code in ERROR_CODE_RE.findall(text)
        if not code.startswith(("ADR-", "INC-"))
    )
    return refs


class ReferenceGraph:
    """Identifier -> documents, built once from the index.

    Built from chunk metadata and document ids rather than by re-parsing the
    corpus, so it stays correct when the corpus changes and costs one pass.
    """

    def __init__(self, index: HybridIndex) -> None:
        self._by_ref: dict[str, list[int]] = {}
        for position, chunk in enumerate(index.chunks):
            keys: set[str] = set(chunk.error_codes)
            doc = chunk.doc_id
            # Document ids carry their own identifier: ADR-0052-topic-... ,
            # PM-2025-1007-01-event-bus-skew, RB-PAY-5021.
            adr = ADR_RE.match(doc)
            if adr:
                keys.add(adr.group(1))
            if doc.startswith("PM-"):
                stem = doc[3:]
                incident = INCIDENT_RE.match(f"INC-{stem}")
                if incident:
                    keys.add(incident.group(1))
            if doc.startswith("RB-"):
                keys.update(ERROR_CODE_RE.findall(doc))
            for key in keys:
                self._by_ref.setdefault(key, []).append(position)

    def resolve(self, ref: str) -> list[int]:
        return self._by_ref.get(ref, [])

    def __len__(self) -> int:
        return len(self._by_ref)


_GRAPH: tuple[int, ReferenceGraph] | None = None


def get_reference_graph(index: HybridIndex) -> ReferenceGraph:
    """Cached per index identity, rebuilt when the index changes size."""
    global _GRAPH
    if _GRAPH is None or _GRAPH[0] != len(index.chunks):
        _GRAPH = (len(index.chunks), ReferenceGraph(index))
    return _GRAPH[1]


def expand(
    query: str,
    hits: list[RetrievedChunk],
    index: HybridIndex,
    *,
    max_hops: int | None = None,
    per_hop_cap: int | None = None,
) -> tuple[list[RetrievedChunk], int]:
    """Follow cross-references out of `hits`. Returns (hits + hops, n_hops).

    Hop chunks are scored against the *query*, not the referring chunk, so a
    document that is cited but irrelevant to what was actually asked does not
    displace better evidence. They are then damped below every first-stage
    result: the hop is corroboration, and letting it outrank the direct hit
    would change what the answer is about.
    """
    if not hits or not settings.multihop_enabled:
        return hits, 0

    max_hops = settings.multihop_max_hops if max_hops is None else max_hops
    per_hop_cap = settings.multihop_per_hop_cap if per_hop_cap is None else per_hop_cap
    if max_hops <= 0 or per_hop_cap <= 0:
        return hits, 0

    graph = get_reference_graph(index)
    qv = index.embedder.embed_query(query)

    out = list(hits)
    seen_chunks = {h.chunk.chunk_id for h in hits}
    seen_docs = {h.chunk.doc_id for h in hits}
    frontier = list(hits)
    total = 0

    # Everything reached by a hop sits strictly below the weakest direct hit.
    floor = min((h.score for h in hits), default=0.0)

    for hop in range(1, max_hops + 1):
        refs: set[str] = set()
        for hit in frontier:
            refs |= extract_references(hit.chunk.text)
        if not refs:
            break

        scored: list[tuple[float, int]] = []
        for ref in refs:
            for position in graph.resolve(ref):
                chunk = index.chunks[position]
                if chunk.chunk_id in seen_chunks or chunk.doc_id in seen_docs:
                    continue
                similarity = float(index.matrix[position] @ qv)
                scored.append((similarity, position))

        if not scored:
            break
        scored.sort(reverse=True)

        added: list[RetrievedChunk] = []
        for similarity, position in scored[:per_hop_cap]:
            if similarity < settings.multihop_min_similarity:
                continue
            chunk = index.chunks[position]
            seen_chunks.add(chunk.chunk_id)
            seen_docs.add(chunk.doc_id)
            added.append(
                RetrievedChunk(
                    chunk=chunk,
                    dense_score=similarity,
                    # Damped below the weakest direct hit, decaying per hop.
                    score=floor * (0.9**hop) - 1e-6 * len(added),
                    rank=len(out) + len(added),
                    provenance="reference_hop",
                    hop=hop,
                )
            )
        if not added:
            break
        out.extend(added)
        total += len(added)
        frontier = added

    return out, total
