#!/usr/bin/env python
"""Did multi-hop reason along the right chain, or merely reach a related document?

Retrieval metrics ask whether the answer document appeared. That is not the same
question as whether the system *got there for the right reason*. A document can
show up because:

1. first-stage retrieval matched it directly (no reasoning involved);
2. a hop followed the citation the question actually turns on (correct);
3. a hop followed some other citation and landed somewhere that happens to be
   related (coincidence that scores identically on recall).

Case 3 is the one that makes multi-hop look better than it is. Distinguishing it
needs the traversal *edge*, which is why `RetrievedChunk` carries `hop_from` and
`hop_via`.

Each case below declares the chain a correct answer has to traverse — a start
document that should be retrieved directly, then the edges out of it. Every edge
is validated against the corpus before scoring: the source document must
actually contain the identifier, and the identifier must resolve to the target.
A hand-written path that the corpus does not support would silently measure
nothing.

Metrics:

- **destination hit** — did the chain's final document appear at all? This is
  what recall already measures, kept for comparison.
- **path precision** — of the hops taken, how many were on an expected edge?
  Low precision means the system is wandering.
- **path recall** — of the expected edges, how many were traversed?
- **reasoned (not merely reached)** — the destination appeared *and* arrived via
  the expected edge, rather than by direct retrieval or a lucky hop.

    uv run python scripts/eval_multihop.py
    uv run python scripts/eval_multihop.py --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiops.config import settings
from aiops.retrieval.index import get_index
from aiops.retrieval.multihop import extract_references, get_reference_graph
from aiops.retrieval.pipeline import retrieve

# (question, start document, [(via identifier, destination document), ...])
#
# Chains are chosen where the corpus genuinely encodes the link, and where the
# question cannot be answered from the start document alone — the start states
# the symptom, the destination states the cause or the constraint.
CHAINS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "why did a key rotation make account records unreadable",
        "RB-ACC-3301",
        [("SEC-9002", "RB-SEC-9002")],
    ),
    (
        "a supplier import is exhausting the inventory connection pool, why can't I just raise the pool size",
        "RB-SUPP-7740",
        [("INV-3007", "RB-INVENTORY-POOL")],
    ),
    (
        "TOTP codes are being rejected on one node, has this happened before in another service",
        "RB-MFA-5501",
        [("AUTH-1015", "RB-AUTH-CLOCKSKEW")],
    ),
    (
        "the ledger partition rollover did not happen, what upstream job is responsible",
        "RB-LED-1030",
        [("SCH-3310", "RB-SCH-3310")],
    ),
    (
        "a kill switch did not take effect on every pod, what is the underlying mechanism",
        "RB-FLAG-4401",
        [("CFG-1120", "RB-CFG-1120")],
    ),
    (
        "checkout is blocked on a tax quote timeout, what is happening at the tax provider",
        "RB-CHK-7010",
        [("TAX-8801", "RB-TAX-8801")],
    ),
    (
        "refunds were authorised without receipt, what refund failure does that cause",
        "RB-RET-8801",
        [("REF-2201", "RB-REF-2201")],
    ),
    (
        "discount stacking went wrong, how does that affect loyalty points",
        "RB-PROMO-5502",
        [("LOY-6603", "RB-LOY-6603")],
    ),
    (
        "a product is sellable with no price, which supplier import causes that",
        "RB-CAT-4410",
        [("SUPP-7702", "RB-SUPP-7702")],
    ),
    (
        "the stream watermark stalled, which downstream model features go stale",
        "RB-STRM-2240",
        [("FRAUD-6601", "RB-FRAUD-6601")],
    ),
    (
        "one kafka partition is lagging badly, what does that do to the search index",
        "RB-BUS-1201",
        [("IDX-7701", "RB-IDX-7701")],
    ),
    (
        "the rate limiter is failing open, which abuse path does that expose",
        "RB-RATE-5501",
        [("WAL-3350", "RB-WAL-3350")],
    ),
]


def validate(index) -> list[str]:
    """Every declared edge must exist in the corpus."""
    graph = get_reference_graph(index)
    by_doc: dict[str, str] = {}
    for chunk in index.chunks:
        by_doc[chunk.doc_id] = by_doc.get(chunk.doc_id, "") + "\n" + chunk.text

    problems: list[str] = []
    for question, start, edges in CHAINS:
        if start not in by_doc:
            problems.append(f"{start!r} does not exist (from {question[:40]!r})")
            continue
        source = start
        for via, dest in edges:
            if dest not in by_doc:
                problems.append(f"{dest!r} does not exist (from {question[:40]!r})")
                continue
            if via not in extract_references(by_doc[source]):
                problems.append(f"{source} does not cite {via} — edge is not in the corpus")
            resolved = {index.chunks[p].doc_id for p in graph.resolve(via)}
            if dest not in resolved:
                problems.append(f"{via} does not resolve to {dest}")
            source = dest
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    index = get_index()

    problems = validate(index)
    if problems:
        print("CHAIN VALIDATION FAILED — these paths are not in the corpus:")
        for p in problems:
            print(f"  - {p}")
        return 1

    rows = []
    print(f"\nmulti-hop path correctness, {len(CHAINS)} chains, k={settings.top_k}\n")
    header = f"{'destination':>12}{'reasoned':>10}{'path prec':>11}{'path recall':>13}"
    print(header)
    print("-" * len(header))

    dest_hits, reasoned, precisions, recalls = [], [], [], []
    mechanisms: dict[str, int] = {}

    for question, start, edges in CHAINS:
        hits, _ = retrieve(question, index, top_k=settings.top_k)
        by_doc = {h.chunk.doc_id: h for h in hits}
        expected_edges = {(src, via, dst) for src, (via, dst) in zip(
            [start, *[d for _v, d in edges[:-1]]], edges, strict=True
        )}

        taken = {
            (h.hop_from, h.hop_via, h.chunk.doc_id)
            for h in hits
            if h.provenance == "reference_hop" and h.hop_from
        }
        final_doc = edges[-1][1]

        dest_hit = final_doc in by_doc
        # "Reasoned" is stricter: the destination is present *and* arrived via
        # the expected edge. Reaching it by direct retrieval proves nothing
        # about traversal.
        arrived = by_doc.get(final_doc)
        reached_by_expected_edge = bool(
            arrived
            and arrived.provenance == "reference_hop"
            and (arrived.hop_from, arrived.hop_via, final_doc) in expected_edges
        )

        precision = len(taken & expected_edges) / len(taken) if taken else 0.0
        recall = len(taken & expected_edges) / len(expected_edges)

        # How the destination was reached is the actual answer to "reasoned or
        # merely reached". Direct retrieval finding it is not a failure — it
        # means the hop was not needed for this question — but it is a
        # different result from traversing to it, and conflating the two is
        # what makes multi-hop look more capable than it is.
        if not arrived:
            mechanism = "missing"
        elif arrived.provenance != "reference_hop":
            mechanism = "direct"
        elif reached_by_expected_edge:
            mechanism = "hop_expected"
        else:
            mechanism = "hop_other"
        mechanisms[mechanism] = mechanisms.get(mechanism, 0) + 1

        dest_hits.append(1.0 if dest_hit else 0.0)
        reasoned.append(1.0 if reached_by_expected_edge else 0.0)
        precisions.append(precision)
        recalls.append(recall)

        rows.append({
            "mechanism": mechanism,
            "question": question,
            "start": start,
            "destination": final_doc,
            "destination_hit": dest_hit,
            "reasoned": reached_by_expected_edge,
            "path_precision": round(precision, 4),
            "path_recall": round(recall, 4),
            "edges_taken": sorted(f"{a}--{b}-->{c}" for a, b, c in taken),
        })

        if args.verbose:
            print(f"\n  Q: {question[:70]}")
            print(f"     want {start} --{edges[0][0]}--> {final_doc}")
            print(f"     dest_hit={dest_hit}  reasoned={reached_by_expected_edge}")
            for a, b, c in sorted(taken):
                mark = "OK " if (a, b, c) in expected_edges else "off"
                print(f"     {mark} {a} --{b}--> {c}")

    n = len(CHAINS)
    if not args.verbose:
        print(
            f"{sum(dest_hits)/n:>12.3f}{sum(reasoned)/n:>10.3f}"
            f"{sum(precisions)/n:>11.3f}{sum(recalls)/n:>13.3f}"
        )
    # Precision has a structural ceiling: each chain declares one expected edge
    # while the retriever may take up to `multihop_per_hop_cap` hops, so even a
    # perfect system cannot exceed 1/cap. Reporting the raw figure without this
    # would understate performance by a factor of three.
    ceiling = 1.0 / max(1, settings.multihop_per_hop_cap)

    print(f"\n  destination hit   {sum(dest_hits)/n:.3f}   (recall already measures this)")
    print(f"  reasoned          {sum(reasoned)/n:.3f}   (reached via the expected edge)")
    print(
        f"  path precision    {sum(precisions)/n:.3f}   (hops on an expected edge; "
        f"ceiling {ceiling:.3f} at per_hop_cap={settings.multihop_per_hop_cap})"
    )
    print(f"  path recall       {sum(recalls)/n:.3f}   (expected edges traversed)")

    print("\n  how the destination was reached:")
    for name, label in (
        ("hop_expected", "traversed the expected edge"),
        ("hop_other", "arrived via a different hop"),
        ("direct", "found by direct retrieval — the hop was not needed"),
        ("missing", "not retrieved at all"),
    ):
        count = mechanisms.get(name, 0)
        if count:
            print(f"    {count:>2}/{n}  {label}")

    out = Path(__file__).resolve().parents[1] / "data" / "eval" / "multihop_paths.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
