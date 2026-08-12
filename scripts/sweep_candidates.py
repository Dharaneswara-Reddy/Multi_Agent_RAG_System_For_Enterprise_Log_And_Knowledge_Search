#!/usr/bin/env python
"""Accuracy vs latency as the reranker's candidate pool shrinks.

Reranking cost is linear in `candidate_k` — every candidate is a cross-encoder
forward pass — and on CPU that is the dominant cost in the whole pipeline. The
question this answers is how small the pool can get before quality moves.

Candidates are already ordered by first-stage score, so a pool of size N is
exactly the first N of a pool of 30. That means one scoring pass at 30 yields
every smaller configuration for free, and the whole curve costs one model run.

    uv run python scripts/sweep_candidates.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiops.config import settings
from aiops.evaluation.harness import load_golden
from aiops.retrieval.index import get_index
from aiops.retrieval.rerank import Reranker

POOLS = [8, 12, 16, 20, 24, 30]


def main() -> int:
    cases = [c for c in load_golden() if c.relevant_docs]
    index = get_index()
    k = settings.top_k
    reranker = Reranker(settings.reranker_model)
    blend = settings.rerank_blend

    print(
        f"model={settings.reranker_model}  blend={blend}  fusion={settings.fusion}  "
        f"k={k}  n={len(cases)}\n"
    )

    staged = []
    for case in cases:
        cands = index.search(case.question, top_k=max(POOLS), fusion=settings.fusion)
        staged.append((case, cands))

    started = time.perf_counter()
    logits = [reranker.score(c.question, [x.chunk.text for x in cands]) for c, cands in staged]
    per_candidate = (time.perf_counter() - started) / max(1, sum(len(x) for x in logits))

    rows = []
    header = f"{'candidate_k':>12}{'recall':>9}{'prec':>8}{'MRR':>8}{'hit':>7}{'est s/query':>13}"
    print(header)
    print("-" * len(header))

    for pool in POOLS:
        agg = [0.0, 0.0, 0.0, 0.0]
        for (case, cands), raw in zip(staged, logits, strict=True):
            sub, sub_raw = cands[:pool], raw[:pool]
            if not sub:
                continue
            lo, hi = min(sub_raw), max(sub_raw)
            span = (hi - lo) or 1.0
            order = sorted(
                zip(sub, sub_raw, strict=True),
                key=lambda pair: blend * ((pair[1] - lo) / span) + (1 - blend) * pair[0].score,
                reverse=True,
            )
            docs: list[str] = []
            for cand, _ in order[:k]:
                if cand.chunk.doc_id not in docs:
                    docs.append(cand.chunk.doc_id)
            expected = set(case.relevant_docs)
            found = expected & set(docs)
            agg[0] += len(found) / len(expected)
            agg[1] += len(found) / max(1, len(docs))
            agg[2] += next((1.0 / i for i, d in enumerate(docs, 1) if d in expected), 0.0)
            agg[3] += 1.0 if found else 0.0

        n = len(staged)
        row = {
            "candidate_k": pool,
            "recall": round(agg[0] / n, 4),
            "precision": round(agg[1] / n, 4),
            "mrr": round(agg[2] / n, 4),
            "hit_rate": round(agg[3] / n, 4),
            "est_s_per_query": round(per_candidate * pool, 3),
        }
        rows.append(row)
        print(
            f"{pool:>12}{row['recall']:>9.3f}{row['precision']:>8.3f}{row['mrr']:>8.3f}"
            f"{row['hit_rate']:>7.3f}{row['est_s_per_query']:>13.2f}"
        )

    best = max(rows, key=lambda r: (r["recall"], r["mrr"]))
    print(f"\nbest quality: candidate_k={best['candidate_k']} recall={best['recall']:.3f}")
    # The smallest pool within one point of the best recall — the latency choice.
    frugal = min(
        (r for r in rows if r["recall"] >= best["recall"] - 0.01),
        key=lambda r: r["candidate_k"],
    )
    print(
        f"cheapest within 1pt: candidate_k={frugal['candidate_k']} "
        f"recall={frugal['recall']:.3f} mrr={frugal['mrr']:.3f} "
        f"({frugal['est_s_per_query']:.2f}s/query vs {best['est_s_per_query']:.2f}s)"
    )
    out = Path(__file__).resolve().parents[1] / "data" / "eval" / "candidate_sweep.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
