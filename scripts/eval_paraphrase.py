#!/usr/bin/env python
"""Does multi-query rewriting earn its place? Measured on conversational phrasing.

The cumulative ablation says no: multi-query moves recall 0.958 -> 0.958 on the
golden set. That result is real but the test is biased, and the bias runs in a
predictable direction. I wrote the golden questions *and* the corpus, so the
questions already use the vocabulary the documents use ("connection pool
exhaustion", "PAY-5021"). Multi-query exists to rescue queries that do **not**
share the corpus's vocabulary, which is precisely the case the golden set
under-represents.

So this is the same 20 questions asked the way a stressed on-call engineer or a
non-specialist would actually type them — vague, conversational, no error codes,
symptom-first. If multi-query does not help here either, it does not help
anywhere and should be switched off rather than defended.

    uv run python scripts/eval_paraphrase.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiops.config import settings
from aiops.retrieval.index import get_index
from aiops.retrieval.pipeline import retrieve

# (conversational phrasing, documents that should be found)
# Answers are the same labels the golden set uses; only the phrasing changes.
CASES: list[tuple[str, list[str]]] = [
    ("carts keep emptying themselves when the site gets busy", ["RB-CART-2140"]),
    ("customers are getting charged twice for one order", ["PM-2025-1103-double-charge"]),
    ("orders are stuck and nobody can check out", ["RB-PAY-5021"]),
    ("the books don't add up this morning", ["RB-LED-1001"]),
    ("we sent money back to someone twice by mistake", ["RB-REF-2201"]),
    ("someone's gift card balance went below zero", ["RB-WAL-3301"]),
    ("all our dashboards stopped working and metrics look broken", ["RB-MET-6601"]),
    ("one queue is way behind but the others are fine", ["RB-BUS-1201"]),
    ("the numbers on the live dashboard stopped moving", ["RB-STRM-2240"]),
    ("people say their login codes don't work even though they're right", ["RB-MFA-5501"]),
    ("we can't change any settings right now", ["RB-CFG-1150"]),
    ("search isn't showing products we added yesterday", ["RB-IDX-7701"]),
    ("a supplier upload wiped out loads of products", ["RB-SUPP-7702"]),
    ("we promised delivery slots we can't actually staff", ["RB-SLOT-6601"]),
    ("marketing emails are bouncing and now order confirmations are late", ["RB-EMAIL-2201"]),
    ("the recommendations look rubbish but nothing is erroring", ["RB-REC-4401"]),
    ("we gave away way too much discount on some baskets", ["RB-PROMO-5502"]),
    ("a warehouse can't confirm picks and work has stopped", ["RB-WH-5501"]),
    ("bots are hammering our login page", ["RB-RATE-5501"]),
    ("finance says some invoice numbers are missing", ["RB-BILL-7701"]),
]


def score(index, multiquery: bool) -> dict:
    recalls, rrs, hits = [], [], []
    started = time.perf_counter()
    for question, expected_docs in CASES:
        found_hits, _ = retrieve(question, index, top_k=settings.top_k, multiquery=multiquery)
        docs: list[str] = []
        for h in found_hits:
            if h.chunk.doc_id not in docs:
                docs.append(h.chunk.doc_id)
        expected = set(expected_docs)
        found = expected & set(docs)
        recalls.append(len(found) / len(expected))
        rrs.append(next((1.0 / i for i, d in enumerate(docs, 1) if d in expected), 0.0))
        hits.append(1.0 if found else 0.0)
    n = len(CASES)
    return {
        "multiquery": multiquery,
        "n": n,
        "recall": round(sum(recalls) / n, 4),
        "mrr": round(sum(rrs) / n, 4),
        "hit_rate": round(sum(hits) / n, 4),
        "s_per_query": round((time.perf_counter() - started) / n, 3),
    }


def main() -> int:
    index = get_index()
    print(f"\nconversational phrasing, {len(CASES)} questions, k={settings.top_k}\n")
    header = f"{'multi-query':>12}{'recall':>9}{'MRR':>8}{'hit':>7}{'s/query':>10}"
    print(header)
    print("-" * len(header))

    rows = [score(index, False), score(index, True)]
    for row in rows:
        label = "on" if row["multiquery"] else "off"
        print(
            f"{label:>12}{row['recall']:>9.3f}{row['mrr']:>8.3f}"
            f"{row['hit_rate']:>7.3f}{row['s_per_query']:>10.2f}"
        )

    off, on = rows
    d_recall = on["recall"] - off["recall"]
    d_mrr = on["mrr"] - off["mrr"]
    print(f"\ndelta: recall {d_recall:+.3f}  MRR {d_mrr:+.3f}  "
          f"latency {on['s_per_query'] - off['s_per_query']:+.2f}s")
    print(
        "verdict: keep multi-query enabled"
        if d_recall > 0.01 or d_mrr > 0.02
        else "verdict: multi-query does not earn its place — default it off"
    )

    out = Path(__file__).resolve().parents[1] / "data" / "eval" / "paraphrase.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
