#!/usr/bin/env python
"""Why do conversational queries fail? Diagnose before fixing.

The paraphrase experiment says recall drops to 0.600 on conversational
phrasing. That is a symptom, not a cause, and there are at least four distinct
causes it could have:

1. The right chunk is retrieved but ranked below k (a ranking problem).
2. The right chunk is in the corpus but never enters the candidate pool at all
   (a first-stage recall problem).
3. The right *document* is found via the wrong chunk — the symptom text lives
   in one section and the query matches a different one.
4. The query genuinely describes something the corpus does not contain.

Each has a different fix, and three of them are not "add another retrieval
stage". This prints, per failing case, where in the pipeline the answer was
lost — including the rank of the correct document in the full corpus, which
tells us whether the ceiling is retrieval or ranking.

    uv run python scripts/diagnose_paraphrase.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eval_paraphrase import CASES  # sibling script, same directory

from aiops.config import settings
from aiops.retrieval.index import get_index
from aiops.retrieval.pipeline import retrieve


def main() -> int:
    index = get_index()
    k = settings.top_k

    print(f"\ndiagnosing {len(CASES)} conversational queries (k={k})\n")
    failures = []

    for question, primary, alternates in CASES:
        # Diagnose against the *acceptable* set: a case that found a valid
        # alternative document is not a retrieval failure to investigate.
        expected = {primary, *alternates}
        hits, _ = retrieve(question, index, top_k=k)
        top_docs: list[str] = []
        for h in hits:
            if h.chunk.doc_id not in top_docs:
                top_docs.append(h.chunk.doc_id)
        if expected & set(top_docs):
            continue

        # Where does the correct document sit in the *whole* ranked corpus?
        deep = index.search(question, top_k=200, fusion=settings.fusion)
        deep_docs: list[str] = []
        for h in deep:
            if h.chunk.doc_id not in deep_docs:
                deep_docs.append(h.chunk.doc_id)
        rank = next((i for i, d in enumerate(deep_docs, 1) if d in expected), None)

        # Which section of the correct document is its best match?
        target = primary
        best_chunk, best_sim = None, -1.0
        qv = index.embedder.embed_query(question)
        for position, chunk in enumerate(index.chunks):
            if chunk.doc_id != target:
                continue
            sim = float(index.matrix[position] @ qv)
            if sim > best_sim:
                best_sim, best_chunk = sim, chunk

        heading = ""
        if best_chunk:
            for line in best_chunk.text.splitlines():
                if line.startswith("[") and "]" in line:
                    heading = line.split("]", 1)[1].strip()
                    break

        failures.append((question, target, rank, best_sim, heading, top_docs[:3]))

    print(f"{len(failures)} of {len(CASES)} failed\n")
    for question, target, rank, sim, heading, got in failures:
        where = f"rank {rank}" if rank else "not in top 200"
        print(f"  Q: {question}")
        print(f"     want {target}  ({where}, best-chunk cos {sim:.3f})")
        print(f"     best section: {heading or '(none)'}")
        print(f"     got instead : {', '.join(got)}")
        print()

    reachable = [f for f in failures if f[2] and f[2] <= 50]
    print(f"summary: {len(reachable)}/{len(failures)} failures have the correct document")
    print("         within the top 50 — those are ranking problems, not recall problems.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
