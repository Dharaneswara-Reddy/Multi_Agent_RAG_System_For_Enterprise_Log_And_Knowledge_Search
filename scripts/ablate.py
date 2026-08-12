#!/usr/bin/env python
"""Ablation harness: measure each retrieval stage's contribution in isolation.

The sweep answers "which parameter value is best". This answers the different
and more important question: "does this component earn its place at all".
Every capability added to the pipeline gets a row here, and a row that does not
improve on the one above it is a component to remove rather than defend.

    uv run python scripts/ablate.py                 # all configurations
    uv run python scripts/ablate.py --only rerank   # one family
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


def score(cases, retrieve_fn) -> dict:
    """Document-level recall / MRR / precision over the labelled cases."""
    recalls, rrs, precisions, hits = [], [], [], []
    for case in cases:
        if not case.relevant_docs:
            continue
        hits_out = retrieve_fn(case.question)
        docs: list[str] = []
        for h in hits_out:
            if h.chunk.doc_id not in docs:
                docs.append(h.chunk.doc_id)
        expected = set(case.relevant_docs)
        found = expected & set(docs)
        recalls.append(len(found) / len(expected))
        precisions.append(len(found) / max(1, len(docs)))
        rrs.append(next((1.0 / i for i, d in enumerate(docs, 1) if d in expected), 0.0))
        hits.append(1.0 if found else 0.0)
    n = len(recalls) or 1
    return {
        "n": len(recalls),
        "recall": round(sum(recalls) / n, 4),
        "precision": round(sum(precisions) / n, 4),
        "mrr": round(sum(rrs) / n, 4),
        "hit_rate": round(sum(hits) / n, 4),
    }


def by_category(cases, retrieve_fn) -> dict[str, dict]:
    groups: dict[str, list] = {}
    for case in cases:
        if case.relevant_docs:
            groups.setdefault(case.category, []).append(case)
    return {cat: score(group, retrieve_fn) for cat, group in sorted(groups.items())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="substring filter on configuration name")
    ap.add_argument("--categories", action="store_true", help="also print per-category MRR")
    args = ap.parse_args()

    cases = load_golden()
    index = get_index()
    k = settings.top_k

    from aiops.retrieval.pipeline import retrieve as pipe

    def staged(**kw):
        return lambda q: pipe(q, index, top_k=k, **kw)[0]

    # Cumulative: each row adds one capability to the row above it, so the delta
    # between adjacent rows is that capability's contribution. A row that does
    # not beat the one above it is a component to remove, not to defend.
    configs: list[tuple[str, callable]] = [
        (
            "1. dense only (naive)",
            lambda q: index.search(q, top_k=k, dense_weight=1.0, fusion="blend"),
        ),
        (
            "2. + BM25 hybrid (blend)",
            lambda q: index.search(q, top_k=k, fusion="blend"),
        ),
        (
            "   alt: hybrid via RRF",
            lambda q: index.search(q, top_k=k, fusion="rrf"),
        ),
        (
            "3. + cross-encoder rerank",
            staged(multiquery=False, rerank=True, multihop=False),
        ),
        (
            "4. + multi-query rewriting",
            staged(multiquery=True, rerank=True, multihop=False),
        ),
        (
            "5. + multi-hop references",
            staged(multiquery=True, rerank=True, multihop=True),
        ),
    ]

    rows = []
    print(f"\nablation over {len(cases)} golden cases, k={k}, corpus={len(index)} chunks\n")
    header = f"{'configuration':<32}{'recall':>8}{'prec':>8}{'MRR':>8}{'hit':>7}{'s/query':>9}"
    print(header)
    print("-" * len(header))

    for name, fn in configs:
        if args.only and args.only not in name:
            continue
        started = time.perf_counter()
        metrics = score(cases, fn)
        elapsed = (time.perf_counter() - started) / max(1, metrics["n"])
        metrics["config"] = name
        metrics["s_per_query"] = round(elapsed, 4)
        rows.append(metrics)
        print(
            f"{name:<32}{metrics['recall']:>8.3f}{metrics['precision']:>8.3f}"
            f"{metrics['mrr']:>8.3f}{metrics['hit_rate']:>7.3f}{elapsed:>9.3f}"
        )
        if args.categories:
            for cat, m in by_category(cases, fn).items():
                print(f"    {cat:<28}{m['recall']:>8.3f}{m['precision']:>8.3f}{m['mrr']:>8.3f}")

    out = Path(__file__).resolve().parents[1] / "data" / "eval" / "ablation.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
