"""Context engineering: turning retrieved chunks into a prompt.

Retrieval returns a ranked list. What actually goes into the prompt is a
separate decision, and the one that most affects answer quality:

- **Diversify by document.** Eight chunks from one runbook look great on recall@k
  and give the model one perspective. A per-document cap forces the context to
  span sources, which is also what makes citation-grounding meaningful.
- **Expand log hits by trace.** If a log chunk is retrieved, its sibling chunks
  sharing a trace id are pulled in even if they scored below the cut. Half a
  cascade is worse than none — it points at the wrong service.
- **Budget in characters, not chunks.** A fixed `k` silently overflows when
  chunks are long. The budget is enforced here and reported, so a truncated
  context is visible rather than mysterious.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiops.config import settings
from aiops.retrieval.index import HybridIndex
from aiops.schemas import Citation, RetrievedChunk, SourceType


@dataclass
class AssembledContext:
    text: str
    citations: list[Citation] = field(default_factory=list)
    used: list[RetrievedChunk] = field(default_factory=list)
    dropped_for_budget: int = 0
    trace_expansions: int = 0
    # Blended rank score, min-max normalised within the query — useful for
    # ordering, useless as an absolute signal (the top hit is always 1.0).
    top_score: float = 0.0
    # Raw cosine of the best hit. This is the one comparable across queries, so
    # it is what the confidence heuristic reads.
    top_dense: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.used

    @property
    def distinct_sources(self) -> int:
        return len({r.chunk.doc_id for r in self.used})


def assemble(
    hits: list[RetrievedChunk],
    index: HybridIndex | None = None,
    *,
    max_chars: int | None = None,
    per_doc_cap: int = 3,
    expand_traces: bool = True,
) -> AssembledContext:
    max_chars = max_chars or settings.max_context_chars
    if not hits:
        return AssembledContext(text="")

    selected: list[RetrievedChunk] = []
    per_doc: dict[str, int] = {}
    overflow: list[RetrievedChunk] = []

    for hit in sorted(hits, key=lambda h: -h.score):
        doc = hit.chunk.doc_id
        if per_doc.get(doc, 0) >= per_doc_cap:
            overflow.append(hit)
            continue
        per_doc[doc] = per_doc.get(doc, 0) + 1
        selected.append(hit)

    trace_expansions = 0
    if expand_traces and index is not None:
        seen = {h.chunk.chunk_id for h in selected}
        for hit in list(selected):
            tid = hit.chunk.trace_id
            if not tid or hit.chunk.source_type != SourceType.LOG:
                continue
            for sibling in index.by_trace(tid):
                if sibling.chunk_id in seen:
                    continue
                seen.add(sibling.chunk_id)
                selected.append(
                    RetrievedChunk(chunk=sibling, score=hit.score * 0.9, rank=999)
                )
                trace_expansions += 1

    # Fill remaining budget from the per-doc overflow rather than wasting it.
    blocks: list[str] = []
    citations: list[Citation] = []
    used: list[RetrievedChunk] = []
    total = 0
    dropped = 0

    def try_add(hit: RetrievedChunk) -> bool:
        nonlocal total
        ref = hit.chunk.citation()
        block = (
            f"<source ref=\"{ref}\" type=\"{hit.chunk.source_type.value}\""
            + (f" service=\"{hit.chunk.service}\"" if hit.chunk.service else "")
            + f" score=\"{hit.score:.3f}\">\n{hit.chunk.text}\n</source>"
        )
        if total + len(block) > max_chars:
            return False
        blocks.append(block)
        total += len(block)
        used.append(hit)
        citations.append(
            Citation(
                ref=ref,
                source_type=hit.chunk.source_type,
                snippet=hit.chunk.text[:280].replace("\n", " ").strip(),
            )
        )
        return True

    for hit in selected:
        if not try_add(hit):
            dropped += 1
    for hit in overflow:
        if total >= max_chars:
            dropped += 1
            continue
        if not try_add(hit):
            dropped += 1

    return AssembledContext(
        text="\n\n".join(blocks),
        citations=citations,
        used=used,
        dropped_for_budget=dropped,
        trace_expansions=trace_expansions,
        top_score=max((h.score for h in hits), default=0.0),
        top_dense=max((h.dense_score for h in hits), default=0.0),
    )


def format_error_entries(entries) -> str:
    """Render catalog rows for the prompt. Deterministic facts, clearly labelled."""
    if not entries:
        return ""
    parts = []
    for e in entries:
        causes = "\n".join(f"  - {c}" for c in e.likely_causes)
        parts.append(
            f"<error_code code=\"{e.code}\" service=\"{e.service}\" severity=\"{e.severity}\">\n"
            f"{e.title}\n{e.description}\nLikely causes:\n{causes}\n"
            f"Remediation: {e.remediation}\n"
            + (f"Runbook: {e.runbook_ref}\n" if e.runbook_ref else "")
            + "</error_code>"
        )
    return "\n".join(parts)
