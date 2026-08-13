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
from aiops.schemas import RetrievedChunk, SourceType

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

    Crucially it records **which document defines an identifier**, not merely
    which documents mention it. A citation to `SEC-9002` means "see the thing
    that explains SEC-9002", not "see anything containing that string". Without
    that distinction a hop lands on whichever mentioning document happens to
    look most like the query — which is systematically the document direct
    retrieval already found, making the hop worthless.
    """

    def __init__(self, index: HybridIndex) -> None:
        self._by_ref: dict[str, list[tuple[int, bool]]] = {}
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
                self._by_ref.setdefault(key, []).append((position, self._defines(chunk, key)))

    @staticmethod
    def _defines(chunk, ref: str) -> bool:
        """Is this chunk's document the canonical explanation of `ref`?

        A runbook carrying the code in its frontmatter owns it — that covers
        both the generated `RB-<CODE>` documents and hand-written ones such as
        `RB-INVENTORY-POOL`, which owns `INV-3007` without naming it in the id.
        An ADR or post-mortem owns its own identifier. Everything else — service
        docs, guides, other runbooks that merely cross-reference, and log chunks
        that carry the code as metadata — is a mention.
        """
        from aiops.schemas import SourceType

        doc = chunk.doc_id
        if ref.startswith("ADR-"):
            return doc.startswith(ref)
        if ref.startswith("INC-"):
            return doc.startswith(f"PM-{ref.removeprefix('INC-')}")
        return chunk.source_type == SourceType.RUNBOOK and ref in chunk.error_codes

    def resolve(self, ref: str) -> list[int]:
        return [position for position, _defines in self._by_ref.get(ref, [])]

    def resolve_ranked(self, ref: str) -> list[tuple[int, bool]]:
        """Positions with a flag for whether each defines `ref`."""
        return list(self._by_ref.get(ref, []))

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
        # Keep the edge, not just the destination: which chunk cited which
        # identifier. Collapsing this to a set of references loses the traversal
        # path, and the path is what distinguishes reasoning from coincidence.
        refs: dict[str, str] = {}  # identifier -> doc_id that cited it
        for hit in frontier:
            # A reference to a code the citing document already owns is
            # self-description, not a cross-reference. Service docs enumerate
            # every code of their service in an "## Error codes" section, so
            # without this they act as hubs that spray hops across all of a
            # service's unrelated faults — reaching related documents while
            # traversing nothing the question turns on.
            own = set() if settings.multihop_follow_own_codes else set(hit.chunk.error_codes)
            for ref in extract_references(hit.chunk.text):
                if ref in own:
                    continue
                refs.setdefault(ref, hit.chunk.doc_id)
        if not refs:
            break

        scored: list[tuple[bool, float, int, str, str]] = []
        for ref, source_doc in refs.items():
            for position, defines in graph.resolve_ranked(ref):
                chunk = index.chunks[position]
                if chunk.chunk_id in seen_chunks or chunk.doc_id in seen_docs:
                    continue
                if chunk.doc_id == source_doc:
                    continue  # a document citing its own code is not a hop
                if not settings.multihop_allow_logs and chunk.source_type == SourceType.LOG:
                    continue
                similarity = float(index.matrix[position] @ qv)
                scored.append((defines, similarity, position, ref, source_doc))

        if not scored:
            break
        # Defining documents first, then by similarity within each group. This
        # is the whole point of the hop: follow a citation to the thing that
        # explains it, not to whichever mentioning document most resembles the
        # query — that one is what direct retrieval is already for.
        #
        # This costs golden-set recall (0.979 -> 0.962) and buys traversal
        # correctness (reasoned 0.167 -> 0.417, scripts/eval_multihop.py). The
        # recall it gives up came from hops landing on incidentally-relevant
        # documents, which is reaching without reasoning — so the lower number
        # describes the more correct system, the same way the corpus fix that
        # took recall from 0.983 to 0.979 did.
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

        added: list[RetrievedChunk] = []
        for defines, similarity, position, ref, source_doc in scored[:per_hop_cap]:
            # A defining document is followed on the strength of the citation
            # alone; a mere mention still has to look relevant to the question.
            if not defines and similarity < settings.multihop_min_similarity:
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
                    hop_from=source_doc,
                    hop_via=ref,
                )
            )
        if not added:
            break
        out.extend(added)
        total += len(added)
        frontier = added

    return out, total
