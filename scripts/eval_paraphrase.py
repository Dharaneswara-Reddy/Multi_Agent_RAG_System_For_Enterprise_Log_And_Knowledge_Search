#!/usr/bin/env python
"""Robustness to conversational phrasing — the system's largest measured gap.

The golden set is written in the corpus's own vocabulary, because I wrote both.
That flatters retrieval in a specific way: it never tests whether the system
can find the right evidence when someone describes a *symptom* in their own
words rather than naming the thing. This is the same 20 incidents phrased the
way a stressed on-call engineer or a non-specialist actually types.

Two metrics, deliberately separated:

- **primary recall** — did the single best document appear? Strict, and the
  number to quote when comparing against the golden set.
- **evidence-region hit** — did *any* document that genuinely answers the
  question appear? A question about metrics ingest failing is legitimately
  answered by either the cardinality runbook or the post-mortem of the incident
  it caused, and a benchmark that calls the second one wrong is measuring
  label-matching rather than usefulness.

The acceptable sets were widened by **reading the alternatives and deciding
they answer the question**, never by looking at what the system retrieved.
Widening labels to match output is how a benchmark becomes theatre.

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

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "docs"

# (conversational phrasing, primary document, other documents that also answer it)
CASES: list[tuple[str, str, list[str]]] = [
    ("carts keep emptying themselves when the site gets busy",
     "RB-CART-2140", ["ADR-0034-cart-ttl-first-lever", "PM-2025-1219-02-cart-eviction"]),
    ("customers are getting charged twice for one order",
     "PM-2025-1103-double-charge", ["RB-PAYMENT-TIMEOUT", "GUIDE-ambiguous-failures"]),
    # RB-PAY-5021 does not exist: PAY-5021 is an original hand-written code whose
    # runbook is RB-PAYMENT-TIMEOUT. Only the 66 expansion faults use RB-<CODE>.
    ("orders are stuck and nobody can check out",
     "RB-PAYMENT-TIMEOUT", ["PM-2026-0714-checkout", "RB-ORDER-SAGA", "SVC-order-service"]),
    ("the books don't add up this morning",
     "RB-LED-1001", ["ADR-0020-ledger-immutability", "PM-2025-0912-02-ledger-imbalance"]),
    ("we sent money back to someone twice by mistake",
     "RB-REF-2201", ["GUIDE-ambiguous-failures"]),
    ("someone's gift card balance went below zero",
     "RB-WAL-3301", ["ADR-0029-wallet-serialisable", "PM-2025-0806-01-wallet-negative"]),
    ("all our dashboards stopped working and metrics look broken",
     "RB-MET-6601", ["PM-2026-0203-02-metrics-cardinality", "ADR-0051-metrics-cardinality-budget"]),
    ("one queue is way behind but the others are fine",
     "RB-BUS-1201", ["GUIDE-partition-and-skew", "PM-2025-1007-01-event-bus-skew"]),
    ("the numbers on the live dashboard stopped moving",
     "RB-STRM-2240", ["ADR-0053-stream-event-time", "PM-2026-0415-02-stream-watermark"]),
    ("people say their login codes don't work even though they're right",
     "RB-MFA-5501", ["GUIDE-clock-discipline", "PM-2025-0918-01-mfa-clock-drift"]),
    ("we can't change any settings right now",
     "RB-CFG-1150", ["PM-2026-0405-02-config-etcd-storm", "SVC-config-service"]),
    ("search isn't showing products we added yesterday",
     "RB-IDX-7701", ["PM-2026-0801-01-catalog-fanout-oversized", "RB-CAT-4455"]),
    ("a supplier upload wiped out loads of products",
     "RB-SUPP-7702", ["ADR-0042-delta-guard-supplier-feeds", "PM-2026-0809-01-supplier-truncation"]),
    ("we promised delivery slots we can't actually staff",
     "RB-SLOT-6601", ["ADR-0056-slot-capacity-hard-limit", "PM-2026-0802-02-slot-oversell"]),
    ("marketing emails are bouncing and now order confirmations are late",
     "RB-EMAIL-2201", ["ADR-0048-email-priority-separation", "PM-2026-0726-01-email-reputation"]),
    ("the recommendations look rubbish but nothing is erroring",
     "RB-REC-4401", ["GUIDE-graceful-degradation", "PM-2026-0227-01-recommendation-silent-degradation"]),
    ("we gave away way too much discount on some baskets",
     "RB-PROMO-5502", ["PM-2025-1128-04-promo-stacking"]),
    ("a warehouse can't confirm picks and work has stopped",
     "RB-WH-5501", ["ADR-0057-warehouse-adapter-buffering", "PM-2026-0620-01-warehouse-adapter"]),
    ("bots are hammering our login page",
     "RB-RATE-5501", ["ADR-0028-rate-limiter-fail-open", "PM-2026-0705-01-rate-limiter-fail-open"]),
    ("finance says some invoice numbers are missing",
     "RB-BILL-7701", ["ADR-0024-tax-quote-retention", "GUIDE-irreversible-actions"]),
]


def validate() -> list[str]:
    """Every label must name a document that exists.

    This exists because it did not, and a broken label silently reported a
    working retrieval as a failure. `extend_golden.py` has had this check from
    the start; this benchmark was written later and did not inherit it.
    """
    on_disk = {p.stem for p in DOCS.glob("*.md")}
    problems = []
    for question, primary, alternates in CASES:
        for doc in [primary, *alternates]:
            if doc not in on_disk:
                problems.append(f"{doc!r} (from {question[:44]!r}) does not exist")
    return problems


def score(index, multiquery: bool | None = None) -> dict:
    primary_hits, region_hits, rrs = [], [], []
    started = time.perf_counter()
    for question, primary, alternates in CASES:
        hits, _ = retrieve(question, index, top_k=settings.top_k, multiquery=multiquery)
        docs: list[str] = []
        for h in hits:
            if h.chunk.doc_id not in docs:
                docs.append(h.chunk.doc_id)
        acceptable = {primary, *alternates}
        primary_hits.append(1.0 if primary in docs else 0.0)
        region_hits.append(1.0 if acceptable & set(docs) else 0.0)
        rrs.append(next((1.0 / i for i, d in enumerate(docs, 1) if d in acceptable), 0.0))
    n = len(CASES)
    return {
        "multiquery": multiquery,
        "n": n,
        "primary_recall": round(sum(primary_hits) / n, 4),
        "evidence_hit": round(sum(region_hits) / n, 4),
        "mrr": round(sum(rrs) / n, 4),
        "s_per_query": round((time.perf_counter() - started) / n, 3),
    }


def main() -> int:
    problems = validate()
    if problems:
        print("LABEL VALIDATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1

    index = get_index()
    print(f"\nconversational phrasing, {len(CASES)} questions, k={settings.top_k}\n")
    header = f"{'config':>14}{'primary':>10}{'evidence':>10}{'MRR':>8}{'s/query':>10}"
    print(header)
    print("-" * len(header))

    row = score(index)
    print(
        f"{'current':>14}{row['primary_recall']:>10.3f}{row['evidence_hit']:>10.3f}"
        f"{row['mrr']:>8.3f}{row['s_per_query']:>10.2f}"
    )

    out = ROOT / "data" / "eval" / "paraphrase.json"
    out.write_text(json.dumps([row], indent=2))
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
