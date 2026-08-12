#!/usr/bin/env python
"""Append expansion-corpus cases to the golden set, then validate every label.

Run once. It is idempotent: existing `exp-*` cases are replaced rather than
duplicated, so re-running after editing a case below updates it in place.

Every case is validated against the documents actually on disk before being
written. A golden set with a label pointing at a document that does not exist
silently depresses recall and is very hard to notice afterwards, so the check
is mandatory rather than advisory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

GOLDEN = ROOT / "data" / "eval" / "golden.jsonl"
DOCS = ROOT / "data" / "docs"

# ---------------------------------------------------------------------------
# New cases.
#
# Deliberate emphasis on *distractor pressure*: the expanded corpus contains a
# dozen connection-pool faults, several upstream-timeout faults, two clock-skew
# faults and three fail-open decisions. Several cases below are chosen
# specifically because a plausible wrong document now exists.
# ---------------------------------------------------------------------------

CASES: list[dict] = [
    # --- runbook lookup under distractor pressure -------------------------
    {
        "id": "exp-001",
        "question": "Redis for carts has hit its memory ceiling and carts are being evicted. What should I change first?",
        "route": "knowledge",
        "relevant_docs": ["RB-CART-2140", "ADR-0034-cart-ttl-first-lever"],
        "must_mention": ["TTL", "noeviction"],
        "error_codes": ["CART-2140"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-002",
        "question": "The tax provider is returning 429 rate limit errors. Should I add more pods?",
        "route": "knowledge",
        "relevant_docs": ["RB-TAX-8801"],
        "must_mention": ["per account", "cache hit"],
        "error_codes": ["TAX-8801"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-003",
        "question": "The balance check job reports a non-zero ledger imbalance. How do I find which transaction caused it?",
        "route": "knowledge",
        "relevant_docs": ["RB-LED-1001"],
        "must_mention": ["bisect", "compensating"],
        "error_codes": ["LED-1001"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-004",
        "question": "Reconciliation found two processor refunds for one order. What do I do?",
        "route": "knowledge",
        "relevant_docs": ["RB-REF-2201"],
        "must_mention": ["idempotency key", "processor"],
        "error_codes": ["REF-2201"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-005",
        "question": "A wallet has gone negative. What is the first thing to do?",
        "route": "knowledge",
        "relevant_docs": ["RB-WAL-3301"],
        "must_mention": ["freeze", "ledger"],
        "error_codes": ["WAL-3301"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-006",
        "question": "etcd keeps changing leader every few seconds and config reads are timing out. What now?",
        "route": "knowledge",
        "relevant_docs": ["RB-CFG-1150"],
        "must_mention": ["remove", "election timeout"],
        "error_codes": ["CFG-1150"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-007",
        "question": "Active series in the metrics store jumped to 11 million and ingest is rejecting samples. How do I contain it?",
        "route": "knowledge",
        "relevant_docs": ["RB-MET-6601"],
        "must_mention": ["cardinality", "drop rule"],
        "error_codes": ["MET-6601"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-008",
        "question": "One Kafka partition has huge lag while the others are flat. Should I add partitions?",
        "route": "knowledge",
        "relevant_docs": ["RB-BUS-1201", "ADR-0052-topic-repartition-procedure"],
        "must_mention": ["repartition", "hashing"],
        "error_codes": ["BUS-1201"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-009",
        "question": "Windowed aggregates have stopped advancing but event throughput looks normal. What is wrong?",
        "route": "knowledge",
        "relevant_docs": ["RB-STRM-2240"],
        "must_mention": ["watermark", "idleness"],
        "error_codes": ["STRM-2240"],
        "category": "diagnosis",
    },
    {
        "id": "exp-010",
        "question": "Invoices have a gap in their sequence numbers. Can I renumber them to close it?",
        "route": "knowledge",
        "relevant_docs": ["RB-BILL-7701"],
        "must_mention": ["void", "renumber"],
        "error_codes": ["BILL-7701"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-011",
        "question": "A delivery slot is oversold. Can I move the affected customers to a nearby slot automatically?",
        "route": "knowledge",
        "relevant_docs": ["RB-SLOT-6601"],
        "must_mention": ["rebook", "freeze"],
        "error_codes": ["SLOT-6601"],
        "category": "risky_action",
    },
    {
        "id": "exp-012",
        "question": "A supplier feed removed a third of a catalogue in one import. What happened and how do I undo it?",
        "route": "knowledge",
        "relevant_docs": ["RB-SUPP-7702", "ADR-0042-delta-guard-supplier-feeds"],
        "must_mention": ["delta", "rollback"],
        "error_codes": ["SUPP-7702"],
        "category": "diagnosis",
    },
    {
        "id": "exp-013",
        "question": "Fraud scoring is logging that it exceeded its budget and is failing open. Should I raise the timeout?",
        "route": "knowledge",
        "relevant_docs": ["RB-FRAUD-6601", "ADR-0021-fraud-fail-open"],
        "must_mention": ["budget", "feature"],
        "error_codes": ["FRAUD-6601"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-014",
        "question": "Users say their TOTP codes are being rejected even though they are correct. Where do I start?",
        "route": "knowledge",
        "relevant_docs": ["RB-MFA-5501"],
        "must_mention": ["chronyd", "node"],
        "error_codes": ["MFA-5501"],
        "category": "diagnosis",
    },
    {
        "id": "exp-015",
        "question": "Sellers were not paid because the nightly payout batch aborted partway. Why does it not just skip the failure?",
        "route": "knowledge",
        "relevant_docs": ["RB-PAYOUT-4401", "ADR-0023-payout-all-or-nothing"],
        "must_mention": ["all-or-nothing", "exclude"],
        "error_codes": ["PAYOUT-4401"],
        "category": "policy_rationale",
    },
    {
        "id": "exp-016",
        "question": "Checkout is failing at the address step with 401 responses from the provider. Is the provider down?",
        "route": "knowledge",
        "relevant_docs": ["RB-CHK-7044"],
        "must_mention": ["authentication", "restart"],
        "error_codes": ["CHK-7044"],
        "category": "diagnosis",
    },
    {
        "id": "exp-017",
        "question": "A product is sellable but has no price and the page has no buy button. What do I do first?",
        "route": "knowledge",
        "relevant_docs": ["RB-PRC-3301", "RB-CAT-4410"],
        "must_mention": ["non-sellable", "price list"],
        "error_codes": ["PRC-3301", "CAT-4410"],
        "category": "runbook_lookup",
    },
    {
        "id": "exp-018",
        "question": "Baskets are settling at 78% discount. Which lever stops this fastest?",
        "route": "knowledge",
        "relevant_docs": ["RB-PROMO-5502"],
        "must_mention": ["disable", "priority"],
        "error_codes": ["PROMO-5502"],
        "category": "runbook_lookup",
    },
    # --- design rationale -------------------------------------------------
    {
        "id": "exp-019",
        "question": "Why does fraud scoring fail open when consent lookups fail closed?",
        "route": "knowledge",
        "relevant_docs": [
            "ADR-0021-fraud-fail-open",
            "ADR-0022-consent-fail-closed",
            "GUIDE-fail-open-fail-closed",
        ],
        "must_mention": ["regulatory", "availability"],
        "error_codes": [],
        "category": "design_rationale",
    },
    {
        "id": "exp-020",
        "question": "Why can't I just UPDATE a wrong ledger entry instead of posting a compensating one?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0020-ledger-immutability"],
        "must_mention": ["audit", "append-only"],
        "error_codes": ["LED-1001"],
        "category": "design_rationale",
    },
    {
        "id": "exp-021",
        "question": "Why don't scheduled jobs retry automatically when one is missed?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0026-scheduler-at-most-once"],
        "must_mention": ["idempot", "at-most-once"],
        "error_codes": ["SCH-3310"],
        "category": "design_rationale",
    },
    {
        "id": "exp-022",
        "question": "Why does rotating a secret require restarting the service?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0027-secrets-cached-at-boot"],
        "must_mention": ["boot", "restart"],
        "error_codes": ["SEC-9002"],
        "category": "design_rationale",
    },
    {
        "id": "exp-023",
        "question": "Why doesn't inventory-service use a read replica to relieve pool pressure?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0036-inventory-no-read-replica"],
        "must_mention": ["consistent", "oversell"],
        "error_codes": ["INV-3007"],
        "category": "design_rationale",
    },
    {
        "id": "exp-024",
        "question": "Why can't we withdraw a B2B quote that was priced wrongly?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0033-quotes-are-binding", "RB-QUOTE-9101"],
        "must_mention": ["binding", "renewal"],
        "error_codes": ["QUOTE-9101"],
        "category": "design_rationale",
    },
    {
        "id": "exp-025",
        "question": "Why do wallet balance mutations use serialisable isolation when it causes retries?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0029-wallet-serialisable"],
        "must_mention": ["retry", "concurren"],
        "error_codes": ["WAL-3301"],
        "category": "design_rationale",
    },
    {
        "id": "exp-026",
        "question": "Why are prices versioned with effective dates instead of being updated in place?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0030-pricing-effective-dating"],
        "must_mention": ["effective", "dispute"],
        "error_codes": ["PRC-3301"],
        "category": "design_rationale",
    },
    {
        "id": "exp-027",
        "question": "Why shouldn't a ranking model fill in a missing feature with zero?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0039-ranking-no-imputation", "RB-RNK-3301"],
        "must_mention": ["imput", "confidently wrong"],
        "error_codes": ["RNK-3301"],
        "category": "design_rationale",
    },
    {
        "id": "exp-028",
        "question": "Why is the supplier import not allowed to mark products sellable?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0041-supplier-sellable-gate"],
        "must_mention": ["merchandiser", "price"],
        "error_codes": ["CAT-4410"],
        "category": "design_rationale",
    },
    {
        "id": "exp-029",
        "question": "Why is the TOTP acceptance window not widened to tolerate clock drift?",
        "route": "knowledge",
        "relevant_docs": ["ADR-0047-mfa-totp-window"],
        "must_mention": ["phish", "window"],
        "error_codes": ["MFA-5501"],
        "category": "design_rationale",
    },
    # --- policy and cross-cutting guides ----------------------------------
    {
        "id": "exp-030",
        "question": "Which operations at Meridian cannot be undone?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-irreversible-actions"],
        "must_mention": ["append-only", "audit"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    {
        "id": "exp-031",
        "question": "When should an automated assistant escalate to a human instead of answering?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-escalating-to-humans"],
        "must_mention": ["irreversible", "two independent"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    {
        "id": "exp-032",
        "question": "How do I confirm a kill switch actually took effect on every pod?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-config-change-safety", "RB-FLAG-4401"],
        "must_mention": ["ack", "restart"],
        "error_codes": ["FLAG-4401"],
        "category": "policy_rationale",
    },
    {
        "id": "exp-033",
        "question": "What kinds of failure produce no error signal at all, and how are they detected?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-silent-failure-classes"],
        "must_mention": ["verification", "fallback"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    {
        "id": "exp-034",
        "question": "What makes an alert actually useful during an incident?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-alert-design"],
        "must_mention": ["subsystem", "evaluation window"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    {
        "id": "exp-035",
        "question": "Which of our data stores have legal retention obligations and how long?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-data-retention"],
        "must_mention": ["seven years", "archive"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    {
        "id": "exp-036",
        "question": "What do I need to do before activating a new model on live traffic?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-model-deployment-safety"],
        "must_mention": ["shadow", "rollback"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    {
        "id": "exp-037",
        "question": "Where does node clock drift cause user-visible failures?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-clock-discipline"],
        "must_mention": ["nbf", "TOTP"],
        "error_codes": ["AUTH-1015", "MFA-5501"],
        "category": "policy_rationale",
    },
    {
        "id": "exp-038",
        "question": "What has to be handed over between on-call shifts?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-oncall-handover"],
        "must_mention": ["muted", "expiry"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    {
        "id": "exp-039",
        "question": "Why does every runbook have a 'what not to do' section?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-runbook-standards"],
        "must_mention": ["intuitive", "pressure"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    {
        "id": "exp-040",
        "question": "Which severity pages immediately and who else has to be told?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-severity-definitions"],
        "must_mention": ["critical", "Compliance"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    {
        "id": "exp-041",
        "question": "Why must every graceful degradation path have its own metric?",
        "route": "knowledge",
        "relevant_docs": ["GUIDE-graceful-degradation"],
        "must_mention": ["fallback", "green"],
        "error_codes": [],
        "category": "policy_rationale",
    },
    # --- causality / multi-hop -------------------------------------------
    {
        "id": "exp-042",
        "question": "How did recommendation quality collapse for nine days without any alert firing?",
        "route": "knowledge",
        "relevant_docs": [
            "PM-2026-0227-01-recommendation-silent-degradation",
            "ADR-0059-recommendation-fallback-visibility",
        ],
        "must_mention": ["fallback", "health"],
        "error_codes": ["REC-4401"],
        "category": "causality",
    },
    {
        "id": "exp-043",
        "question": "Why did a routine key rotation make account records unreadable?",
        "route": "knowledge",
        "relevant_docs": ["PM-2026-0114-01-secrets-retirement", "RB-ACC-3301"],
        "must_mention": ["retired", "re-encrypt"],
        "error_codes": ["SEC-9002", "ACC-3301"],
        "category": "causality",
    },
    {
        "id": "exp-044",
        "question": "Why did flipping a kill switch not stop the bad behaviour during an incident?",
        "route": "knowledge",
        "relevant_docs": [
            "PM-2026-0117-03-flag-partial-killswitch",
            "ADR-0055-config-broadcast-best-effort",
        ],
        "must_mention": ["broadcast", "ack"],
        "error_codes": ["FLAG-4401", "CFG-1120"],
        "category": "causality",
    },
    {
        "id": "exp-045",
        "question": "How did a Redis problem end up causing account compromises?",
        "route": "knowledge",
        "relevant_docs": ["PM-2026-0705-01-rate-limiter-fail-open", "ADR-0028-rate-limiter-fail-open"],
        "must_mention": ["fail open", "fail-closed"],
        "error_codes": ["RATE-5501"],
        "category": "causality",
    },
    {
        "id": "exp-046",
        "question": "Why did an ETL job report success while loading only 60% of the rows?",
        "route": "knowledge",
        "relevant_docs": ["PM-2026-0318-03-etl-silent-partial", "ADR-0054-etl-row-count-verification"],
        "must_mention": ["replica", "row count"],
        "error_codes": ["ETL-4401"],
        "category": "causality",
    },
    {
        "id": "exp-047",
        "question": "How did the search index lose twelve days of updates without any error?",
        "route": "knowledge",
        "relevant_docs": ["PM-2026-0718-03-indexer-checkpoint", "ADR-0038-indexer-checkpoint-after-flush"],
        "must_mention": ["checkpoint", "flush"],
        "error_codes": ["IDX-7730"],
        "category": "causality",
    },
    {
        "id": "exp-048",
        "question": "Why did aggregate consumer lag look healthy while catalogue updates were four hours stale?",
        "route": "knowledge",
        "relevant_docs": ["PM-2025-1007-01-event-bus-skew", "GUIDE-partition-and-skew"],
        "must_mention": ["partition", "average"],
        "error_codes": ["BUS-1201"],
        "category": "causality",
    },
    # --- service facts ----------------------------------------------------
    {
        "id": "exp-049",
        "question": "Which team owns pricing-service and what does it store prices in?",
        "route": "knowledge",
        "relevant_docs": ["SVC-pricing-service"],
        "must_mention": ["Commerce", "Postgres"],
        "error_codes": [],
        "category": "service_facts",
    },
    {
        "id": "exp-050",
        "question": "How long does event-bus retain messages and what happens to a consumer offline longer than that?",
        "route": "knowledge",
        "relevant_docs": ["SVC-event-bus", "RB-BUS-1240"],
        "must_mention": ["7 days", "earliest"],
        "error_codes": ["BUS-1240"],
        "category": "service_facts",
    },
    {
        "id": "exp-051",
        "question": "What is the latency budget for fraud scoring and what happens when it is exceeded?",
        "route": "knowledge",
        "relevant_docs": ["SVC-fraud-service", "RB-FRAUD-6601"],
        "must_mention": ["250ms", "fail"],
        "error_codes": ["FRAUD-6601"],
        "category": "service_facts",
    },
    {
        "id": "exp-052",
        "question": "What does audit-log-service do to make its records tamper-evident?",
        "route": "knowledge",
        "relevant_docs": ["SVC-audit-log-service", "ADR-0025-audit-hash-chain"],
        "must_mention": ["hash", "anchor"],
        "error_codes": ["AUD-9901"],
        "category": "service_facts",
    },
]


def main() -> int:
    existing = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
    kept = [c for c in existing if not c["id"].startswith("exp-")]

    on_disk = {p.stem for p in DOCS.glob("*.md")}
    problems: list[str] = []

    seen_ids = {c["id"] for c in kept}
    for case in CASES:
        if case["id"] in seen_ids:
            problems.append(f"{case['id']}: duplicate id")
        seen_ids.add(case["id"])
        for doc in case["relevant_docs"]:
            if doc not in on_disk:
                problems.append(f"{case['id']}: relevant_doc '{doc}' does not exist")
        if case["route"] not in {"knowledge", "logs", "hybrid", "any"}:
            problems.append(f"{case['id']}: bad route {case['route']!r}")

    if problems:
        print("VALIDATION FAILED — golden set not written:")
        for p in problems:
            print(f"  - {p}")
        return 1

    combined = kept + CASES
    GOLDEN.write_text("\n".join(json.dumps(c) for c in combined) + "\n", encoding="utf-8")
    print(f"wrote {len(combined)} cases ({len(kept)} existing + {len(CASES)} expansion)")

    by_cat: dict[str, int] = {}
    for c in combined:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat:<20} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
