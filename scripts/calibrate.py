#!/usr/bin/env python
"""Measure the cosine separation between on-corpus and off-corpus questions.

The constants `RELEVANCE_FLOOR` / `RELEVANCE_CEIL` in `guardrails.rules` are
properties of (embedding model x corpus), not universal values. This script is
what produced them; re-run it after changing either, and update the constants
if the bands move.

    uv run python scripts/calibrate.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiops.retrieval.index import get_index

# Probes must sample the whole corpus, not one corner of it. After the corpus
# grew from 18 to 219 documents the original twelve probes covered only the
# seven founding services, so the measured floor described a slice rather than
# the corpus — the second block below covers the expansion.
ON_CORPUS = [
    # original hand-written corpus
    "why did checkout fail",
    "how to fix connection pool exhaustion",
    "JWT token not yet valid clock skew",
    "poison message notification consumer",
    "search index rebuild out of memory",
    "what does PAY-5021 mean",
    "gateway 504 root cause",
    "duplicate charges post-mortem",
    "why is the notification consumer lagging",
    "when should I escalate to a human",
    "what index does inventory reservations need",
    "how does the checkout saga handle payment timeouts",
    # expansion corpus
    "cart redis memory ceiling eviction",
    "ledger imbalance compensating entry",
    "why does fraud scoring fail open",
    "etcd leader election storm",
    "metrics cardinality explosion unbounded label",
    "kafka partition skew consumer lag",
    "tax provider rate limit 429",
    "secrets rotation requires restart",
    "delivery slot oversold capacity",
    "supplier feed truncated mass deletion",
    "which operations cannot be undone",
    "how do I verify a kill switch applied",
]

OFF_CORPUS = [
    "airspeed velocity of an unladen swallow",
    "best recipe for sourdough bread",
    "who won the 1998 world cup",
    "how tall is mount everest",
    "translate hello into french",
    "what is the capital of Peru",
    "how do I knit a scarf",
    "explain the plot of Hamlet",
]


def top_cosine(index, query: str) -> float:
    hits = index.search(query, top_k=1)
    return hits[0].dense_score if hits else 0.0


def main() -> int:
    index = get_index()
    on = [top_cosine(index, q) for q in ON_CORPUS]
    off = [top_cosine(index, q) for q in OFF_CORPUS]

    report = {
        "corpus_chunks": len(index),
        "on_corpus": {
            "n": len(on),
            "min": round(min(on), 4),
            "mean": round(statistics.mean(on), 4),
            "max": round(max(on), 4),
        },
        "off_corpus": {
            "n": len(off),
            "min": round(min(off), 4),
            "mean": round(statistics.mean(off), 4),
            "max": round(max(off), 4),
        },
        "separation": round(min(on) - max(off), 4),
        "suggested_floor": round((min(on) + max(off)) / 2, 2),
        "suggested_ceil": round(statistics.mean(sorted(on)[-3:]), 2),
    }
    print(json.dumps(report, indent=2))

    if report["separation"] <= 0:
        print(
            "\nWARNING: on-corpus and off-corpus ranges overlap. The confidence "
            "floor cannot separate them — investigate the embedding model or "
            "the chunking before trusting escalation.",
            file=sys.stderr,
        )
        return 1
    print(
        f"\nClean separation of {report['separation']:.3f}. "
        f"Floor {report['suggested_floor']} / ceil {report['suggested_ceil']} "
        "-> guardrails.rules.RELEVANCE_FLOOR / RELEVANCE_CEIL"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
