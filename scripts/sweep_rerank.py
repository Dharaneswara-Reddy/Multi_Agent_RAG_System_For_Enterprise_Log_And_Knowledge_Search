#!/usr/bin/env python
"""Reranker grid: model x blend weight, over cached cross-encoder logits.

A cross-encoder pass is ~100ms per (query, chunk) pair on CPU, so a naive grid
over four models and five blend weights would run the model twenty times for
identical inputs. The logits do not depend on the blend weight, so they are
computed once per (model, query) and every blend is then scored from cache.
That turns a ~90 minute grid into a ~10 minute one.

    uv run python scripts/sweep_rerank.py
    uv run python scripts/sweep_rerank.py --models Xenova/ms-marco-MiniLM-L-6-v2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiops.config import settings
from aiops.evaluation.harness import load_golden
from aiops.retrieval.index import get_index
from aiops.retrieval.rerank import Reranker

MODELS = [
    "Xenova/ms-marco-MiniLM-L-6-v2",
    "Xenova/ms-marco-MiniLM-L-12-v2",
    "jinaai/jina-reranker-v1-turbo-en",
    "BAAI/bge-reranker-base",
]
BLENDS = [0.0, 0.25, 0.5, 0.75, 1.0]


def metrics_from(order, expected, k: int) -> tuple[float, float, float, float]:
    docs: list[str] = []
    for cand in order[:k]:
        if cand.chunk.doc_id not in docs:
            docs.append(cand.chunk.doc_id)
    found = expected & set(docs)
    recall = len(found) / len(expected)
    precision = len(found) / max(1, len(docs))
    rr = next((1.0 / i for i, d in enumerate(docs, 1) if d in expected), 0.0)
    return recall, precision, rr, 1.0 if found else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=MODELS)
    ap.add_argument("--fusion", default=settings.fusion)
    args = ap.parse_args()

    cases = [c for c in load_golden() if c.relevant_docs]
    index = get_index()
    k = settings.top_k

    # Stage-1 candidates are identical across every reranker configuration, so
    # retrieve them once.
    print(f"retrieving stage-1 candidates for {len(cases)} cases (fusion={args.fusion})...")
    staged = []
    for case in cases:
        cands = index.search(case.question, top_k=settings.candidate_k, fusion=args.fusion)
        staged.append((case, cands))

    baseline = [0.0, 0.0, 0.0, 0.0]
    for case, cands in staged:
        r, p, rr, h = metrics_from(cands, set(case.relevant_docs), k)
        baseline[0] += r
        baseline[1] += p
        baseline[2] += rr
        baseline[3] += h
    n = len(staged)
    print(
        f"\nstage-1 only:  recall {baseline[0]/n:.3f}  prec {baseline[1]/n:.3f}  "
        f"MRR {baseline[2]/n:.3f}  hit {baseline[3]/n:.3f}\n"
    )

    rows = []
    header = f"{'model':<38}{'blend':>7}{'recall':>8}{'prec':>8}{'MRR':>8}{'hit':>7}{'s/q':>7}"
    print(header)
    print("-" * len(header))

    for model_name in args.models:
        reranker = Reranker(model_name)
        # cache raw logits per case
        started = time.perf_counter()
        logits = []
        try:
            for _case, cands in staged:
                logits.append(reranker.score(_case.question, [c.chunk.text for c in cands]))
        except Exception as exc:  # a model that will not load should not kill the grid
            print(f"{model_name:<38}  FAILED: {type(exc).__name__}: {str(exc)[:60]}")
            continue
        per_query = (time.perf_counter() - started) / n

        for blend in BLENDS:
            agg = [0.0, 0.0, 0.0, 0.0]
            for (case, cands), raw in zip(staged, logits, strict=True):
                lo, hi = min(raw), max(raw)
                span = (hi - lo) or 1.0
                scored = sorted(
                    zip(cands, raw, strict=True),
                    key=lambda pair: blend * ((pair[1] - lo) / span) + (1 - blend) * pair[0].score,
                    reverse=True,
                )
                order = [c for c, _ in scored]
                r, p, rr, h = metrics_from(order, set(case.relevant_docs), k)
                agg[0] += r
                agg[1] += p
                agg[2] += rr
                agg[3] += h
            row = {
                "model": model_name,
                "blend": blend,
                "recall": round(agg[0] / n, 4),
                "precision": round(agg[1] / n, 4),
                "mrr": round(agg[2] / n, 4),
                "hit_rate": round(agg[3] / n, 4),
                "s_per_query": round(per_query, 3),
            }
            rows.append(row)
            print(
                f"{model_name:<38}{blend:>7.2f}{row['recall']:>8.3f}{row['precision']:>8.3f}"
                f"{row['mrr']:>8.3f}{row['hit_rate']:>7.3f}{per_query:>7.2f}"
            )

    if rows:
        # Rank by recall first, then MRR: top_k chunks reach the synthesiser
        # either way, so having the right document present outranks its
        # position within the context.
        best = max(rows, key=lambda r: (r["recall"], r["mrr"]))
        print(
            f"\nbest: {best['model']} blend={best['blend']} "
            f"recall={best['recall']:.3f} mrr={best['mrr']:.3f} ({best['s_per_query']:.2f}s/query)"
        )
    out = Path(__file__).resolve().parents[1] / "data" / "eval" / "rerank_sweep.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
