#!/usr/bin/env python
"""Do different kinds of query want different retrieval settings?

Adaptive retrieval — letting the query choose the plan rather than always
running one pipeline — is only worth building if the optimal settings actually
*differ* by query type. Otherwise it is a router that always routes the same
way, which is complexity with a nice name.

So this measures the thing directly. Queries are split into three classes and
`dense_weight` is swept independently for each:

- **identifier** — the question names an error code, ADR or incident id. These
  should favour BM25: the identifier is an exact token and the strongest signal
  in the corpus.
- **symptom** — no identifier, describes what is happening in prose. These
  should favour dense: there is no exact token to match on.
- **conceptual** — "why" and "when should I" questions about policy and
  design rationale, which are answered by ADRs and guides rather than runbooks.

If the per-class optima differ, adaptive routing is justified by data and the
gain is the difference between per-class tuning and one global weight. If they
do not, this script says so and the feature should not be built.

    uv run python scripts/sweep_adaptive.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiops.config import settings
from aiops.evaluation.harness import load_golden
from aiops.retrieval.index import get_index

IDENTIFIER_RE = re.compile(r"\b(?:INC-\d{4}-\d{4}(?:-\d{2})?|ADR-\d{4}|[A-Z]{2,6}-\d{4})\b")
CONCEPTUAL_RE = re.compile(r"^\s*(?:why|when should|what makes|which .* (?:cannot|should))", re.I)

WEIGHTS = [0.35, 0.5, 0.65, 0.8, 1.0]


def classify(question: str) -> str:
    if IDENTIFIER_RE.search(question.upper()):
        return "identifier"
    if CONCEPTUAL_RE.match(question):
        return "conceptual"
    return "symptom"


def score(index, cases, weight: float) -> tuple[float, float]:
    recalls, rrs = [], []
    for case in cases:
        hits = index.search(
            case.question, top_k=settings.top_k, dense_weight=weight, fusion="blend"
        )
        docs: list[str] = []
        for h in hits:
            if h.chunk.doc_id not in docs:
                docs.append(h.chunk.doc_id)
        expected = set(case.relevant_docs)
        found = expected & set(docs)
        recalls.append(len(found) / len(expected))
        rrs.append(next((1.0 / i for i, d in enumerate(docs, 1) if d in expected), 0.0))
    n = len(recalls) or 1
    return sum(recalls) / n, sum(rrs) / n


def main() -> int:
    index = get_index()
    cases = [c for c in load_golden() if c.relevant_docs]

    buckets: dict[str, list] = {}
    for case in cases:
        buckets.setdefault(classify(case.question), []).append(case)

    print("\ndense_weight swept per query class (stage-1 only, blend fusion)\n")
    for name, group in sorted(buckets.items()):
        print(f"  {name:<12} n={len(group)}")
    print()

    header = f"{'class':<12}" + "".join(f"{w:>8.2f}" for w in WEIGHTS) + f"{'best':>8}"
    print(header)
    print("-" * len(header))

    rows = []
    optima: dict[str, float] = {}
    for name, group in sorted(buckets.items()):
        recalls = []
        for weight in WEIGHTS:
            recall, mrr = score(index, group, weight)
            recalls.append(recall)
            rows.append(
                {"class": name, "n": len(group), "dense_weight": weight,
                 "recall": round(recall, 4), "mrr": round(mrr, 4)}
            )
        best_weight = WEIGHTS[max(range(len(recalls)), key=lambda i: recalls[i])]
        optima[name] = best_weight
        print(
            f"{name:<12}" + "".join(f"{r:>8.3f}" for r in recalls) + f"{best_weight:>8.2f}"
        )

    print(f"\nglobal setting: dense_weight={settings.dense_weight}")
    distinct = set(optima.values())
    if len(distinct) == 1:
        print(
            f"verdict: every class prefers {distinct.pop():.2f} — adaptive weighting is "
            "not justified by this data."
        )
    else:
        # Gain is only real if per-class tuning beats the best single global weight.
        global_best = max(
            WEIGHTS,
            key=lambda w: sum(
                r["recall"] * r["n"] for r in rows if r["dense_weight"] == w
            ),
        )
        total = sum(len(g) for g in buckets.values())
        adaptive = sum(
            next(
                r["recall"] for r in rows
                if r["class"] == name and r["dense_weight"] == optima[name]
            ) * len(group)
            for name, group in buckets.items()
        ) / total
        fixed = sum(
            next(r["recall"] for r in rows if r["class"] == name and r["dense_weight"] == global_best)
            * len(group)
            for name, group in buckets.items()
        ) / total
        print(f"optima differ: {optima}")
        print(
            f"verdict: adaptive recall {adaptive:.4f} vs best fixed weight "
            f"({global_best:.2f}) {fixed:.4f} -> gain {adaptive - fixed:+.4f}"
        )

    out = Path(__file__).resolve().parents[1] / "data" / "eval" / "adaptive_sweep.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
