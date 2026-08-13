#!/usr/bin/env python
"""Run the evaluation suite and print a report.

    uv run python scripts/evaluate.py              # full run
    uv run python scripts/evaluate.py --no-quality # retrieval + behaviour only
    uv run python scripts/evaluate.py --gate       # exit non-zero on regression (CI)
    uv run python scripts/evaluate.py --sweep      # chunk/weight parameter sweep
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiops.evaluation.harness import (
    check_thresholds,
    load_golden,
    run_all,
)


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def print_report(report: dict) -> None:
    r, b, q = report["retrieval"], report["behaviour"], report["quality"]
    mode = "OFFLINE (no model calls)" if report["offline_mode"] else "ONLINE"

    print(f"\n{'=' * 66}")
    print(f"  AI Ops Copilot — evaluation report   [{mode}]")
    print(f"  {report['generated_at']}   ({report['duration_s']}s)")
    print(f"{'=' * 66}\n")

    print("RETRIEVAL")
    print(f"  cases            {r['n']}  (k={r['k']})")
    print(f"  recall@k         {_fmt(r['recall@k'])}")
    print(f"  precision@k      {_fmt(r['precision@k'])}")
    print(f"  MRR              {_fmt(r['mrr'])}")
    print(f"  hit rate         {_fmt(r['hit_rate'])}")
    print("\n  by category:")
    for cat, m in r["by_category"].items():
        print(f"    {cat:<20} n={m['n']:<3} recall={_fmt(m['recall@k'])}  mrr={_fmt(m['mrr'])}")

    print("\nBEHAVIOUR")
    print(
        f"  routing accuracy         {_fmt(b['routing_accuracy'])}  (n={b['routing_n']}, "
        f"measured on: {b.get('routing_measured_on', 'unknown')})"
    )
    print(f"  injection blocked        {_fmt(b['injection_blocked'])}")
    print(f"  out-of-scope escalated   {_fmt(b['out_of_scope_escalated'])}")
    if b["failures"]:
        print(f"\n  {len(b['failures'])} failure(s):")
        for f in b["failures"][:10]:
            print(f"    {f['case_id']:<10} expected={f['expected']:<10} got={f['actual']:<10} {f.get('detail','')[:44]}")

    a = report.get("answers") or {}
    if a.get("available"):
        print("\nANSWER (end-to-end, deterministic)")
        print(f"  cases                 {a['n']}")
        print(f"  answer rate           {_fmt(a['answer_rate'])}")
        print(f"  abstention accuracy   {_fmt(a['abstention_accuracy'])}")
        print(
            f"  keyword coverage      {_fmt(a['keyword_coverage'])}  "
            f"(measured on: {a.get('keyword_coverage_measured_on', 'unknown')})"
        )
        print(f"  claim support         {_fmt(a['claim_support'])}")
        print(f"  citation validity     {_fmt(a['citation_validity'])}")
        print(f"  fabrication rate      {_fmt(a['fabrication_rate'])}")
        if a.get("failures"):
            print(f"\n  {len(a['failures'])} case(s) with a wrong abstention or an unsupported claim:")
            for f in a["failures"][:6]:
                note = ", ".join(f["unsupported_claims"][:2]) or "wrong answer/abstain decision"
                print(f"    {f['case_id']:<10} {f['verdict']:<10} {note[:44]}")

    print("\nJUDGED QUALITY")
    if not q.get("available"):
        print(f"  not measured — {q.get('reason', 'unavailable')}")
    else:
        print(f"  cases                 {q['n']}")
        print(f"  faithfulness          {_fmt(q['faithfulness'])}")
        print(f"  coverage              {_fmt(q['coverage'])}")
        print(f"  citation validity     {_fmt(q['citation_validity'])}")
        print(f"  keyword coverage      {_fmt(q['keyword_coverage'])}")
    print()


def sweep() -> None:
    """Chunking / blend-weight sweep. This is the experiment log, in code."""
    from aiops.config import settings
    from aiops.ingestion.documents import build_all_chunks
    from aiops.retrieval.index import HybridIndex

    cases = load_golden()
    rows = []
    print("\nsweeping chunk size x dense weight (rebuilds the index per chunk size)\n")
    print(f"{'chunk_tok':>9} {'dense_w':>8} {'recall@k':>9} {'mrr':>7} {'chunks':>7}")
    print("-" * 46)

    original = settings.doc_chunk_tokens
    try:
        for chunk_tokens in (160, 320, 640):
            settings.doc_chunk_tokens = chunk_tokens
            chunks, _ = build_all_chunks()
            index = HybridIndex()
            index.build(chunks)
            for weight in (0.35, 0.5, 0.65, 0.8, 1.0):
                # dense_weight is read per-search, so score with an explicit
                # override rather than through evaluate_retrieval, which would
                # use the configured weight and answer the wrong question.
                results = []
                for case in cases:
                    if not case.relevant_docs:
                        continue
                    hits = index.search(case.question, dense_weight=weight)
                    docs, expected = [], set(case.relevant_docs)
                    for h in hits:
                        if h.chunk.doc_id not in docs:
                            docs.append(h.chunk.doc_id)
                    found = expected & set(docs)
                    rr = next(
                        (1.0 / i for i, d in enumerate(docs, 1) if d in expected), 0.0
                    )
                    results.append((len(found) / len(expected), rr))
                recall = sum(r[0] for r in results) / len(results)
                mrr = sum(r[1] for r in results) / len(results)
                rows.append(
                    {
                        "chunk_tokens": chunk_tokens,
                        "dense_weight": weight,
                        "recall": round(recall, 4),
                        "mrr": round(mrr, 4),
                        "chunks": len(chunks),
                    }
                )
                print(f"{chunk_tokens:>9} {weight:>8.2f} {recall:>9.3f} {mrr:>7.3f} {len(chunks):>7}")
    finally:
        settings.doc_chunk_tokens = original

    best = max(rows, key=lambda r: (r["recall"], r["mrr"]))
    print(f"\nbest: chunk_tokens={best['chunk_tokens']} dense_weight={best['dense_weight']} "
          f"recall={best['recall']:.3f} mrr={best['mrr']:.3f}")
    out = Path(__file__).resolve().parents[1] / "data" / "eval" / "sweep.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"written to {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-quality", action="store_true", help="skip LLM-judge grading")
    ap.add_argument("--gate", action="store_true", help="exit non-zero if thresholds fail")
    ap.add_argument("--sweep", action="store_true", help="run the parameter sweep instead")
    ap.add_argument("--k", type=int, default=None)
    args = ap.parse_args()

    if args.sweep:
        sweep()
        return 0

    report = run_all(include_quality=not args.no_quality, k=args.k)
    print_report(report)

    if args.gate:
        ok, failures = check_thresholds(report)
        if not ok:
            print("GATE FAILED:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("GATE PASSED — all thresholds met.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
