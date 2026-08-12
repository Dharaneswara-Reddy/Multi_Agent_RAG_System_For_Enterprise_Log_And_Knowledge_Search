"""Architecture decision records for the expanded Meridian estate.

Numbering continues from the original hand-written set (0007, 0009, 0012), so
these run 0020-0059. Several are referenced by name from the runbooks in
`services_core` and `services_platform` — the cross-links are what give the
multi-hop evaluation questions something real to traverse.

Each record states options that were genuinely considered and a decision that
rejects at least one of them for a stated reason. An ADR whose options are all
straw men teaches a retrieval system nothing about "why" questions.
"""

from __future__ import annotations

from aiops.ingestion.expansion.specs import Decision

DECISIONS: tuple[Decision, ...] = (
    Decision(
        adr_id="ADR-0020",
        slug="ledger-immutability",
        title="Ledger entries are immutable; corrections are compensating entries",
        service="ledger-service",
        status="Accepted (2025-04-18)",
        codes=("LED-1001",),
        context=(
            "Early ledger code allowed an UPDATE to correct a mis-posted entry. During the "
            "2025 Q1 close, Finance could not explain a balance change because the original "
            "value no longer existed anywhere. The audit trail was the point of the ledger "
            "and we had built it without one."
        ),
        options=(
            ("Allow UPDATE with a change-log side table",
             "Keeps the current-state query simple, but the change log becomes the real ledger "
             "and is not itself protected. We would have moved the problem, not solved it."),
            ("Immutable entries with compensating corrections",
             "Standard double-entry practice. Current state becomes a sum rather than a lookup, "
             "which costs read performance and requires a materialised balance."),
            ("Event sourcing the entire payments domain",
             "Theoretically cleanest and by far the largest change. Rejected on scope: the "
             "problem is the ledger specifically, not the domain."),
        ),
        decision=(
            "Ledger entries are strictly append-only. Corrections are posted as compensating "
            "entries carrying a mandatory reason reference. Balances are maintained in a "
            "materialised view refreshed on write."
        ),
        consequences=(
            "The database role used by ledger-service has INSERT and SELECT only — no UPDATE "
            "or DELETE grant. This is enforced at the role level so application bugs cannot "
            "violate it.",
            "Every correction requires an incident or ticket reference, which makes the "
            "compensating-entry path deliberately slower than the original write. Finance "
            "considers this a feature.",
            "Reads pay for the materialised view refresh. At current volumes this is under "
            "10ms and it is the main thing to watch as the ledger grows.",
        ),
    ),
    Decision(
        adr_id="ADR-0021",
        slug="fraud-fail-open",
        title="Fraud scoring fails open, with mandatory alerting",
        service="fraud-service",
        status="Accepted (2025-06-02)",
        codes=("FRAUD-6601",),
        context=(
            "fraud-service sits inline in checkout with a 250ms budget. When it is slow or "
            "unavailable we must decide, in that moment, whether to block checkout or to "
            "proceed without a fraud score."
        ),
        options=(
            ("Fail closed — block checkout when scoring is unavailable",
             "Maximum fraud protection. Also means a fraud-service outage is a full revenue "
             "outage, and fraud-service is a model-serving system with a much lower natural "
             "availability than checkout."),
            ("Fail open silently",
             "Protects revenue. Removes fraud protection with no signal, which is how you "
             "discover the outage from a chargeback report six weeks later."),
            ("Fail open with mandatory paging and a Risk notification",
             "Protects revenue and makes the unprotected window explicit and time-bounded."),
        ),
        decision=(
            "Scoring fails open. `fraud_fail_open_rate` above 1% pages Payments, and sustained "
            "fail-open beyond 10 minutes is reported to Risk so they can compensate elsewhere."
        ),
        consequences=(
            "The fail-open path is a designed relief valve, not a bug. Raising the 250ms budget "
            "to avoid triggering it is explicitly the wrong response — it moves the failure into "
            "checkout's funnel SLO.",
            "Risk accepts a bounded unprotected window in exchange for availability. That "
            "acceptance is conditional on the alerting working, so the alert itself is tier-1.",
            "We cannot quantify fraud losses during a fail-open window after the fact, which "
            "means the 10-minute reporting threshold is a judgement call rather than a "
            "calculated one.",
        ),
    ),
    Decision(
        adr_id="ADR-0022",
        slug="consent-fail-closed",
        title="Consent lookups fail closed",
        service="consent-service",
        status="Accepted (2025-06-02)",
        codes=("CNS-4401",),
        context=(
            "The mirror image of ADR-0021. When consent-service is unavailable, do we send "
            "marketing messages without a verified consent check?"
        ),
        options=(
            ("Fail open to protect campaign delivery",
             "Every message sent during the window is a potential regulatory breach, each "
             "individually actionable. Rejected outright."),
            ("Fail closed and drop the sends",
             "Compliant, but the messages are lost and campaigns cannot be recovered."),
            ("Fail closed and requeue",
             "Compliant, and campaign-service holds the messages until consent can be verified."),
        ),
        decision=(
            "Consent lookups fail closed. Absence of a positive consent record is treated as "
            "absence of consent, never as implied consent. Suppressed sends are requeued by "
            "campaign-service rather than dropped."
        ),
        consequences=(
            "A consent-service outage silently reduces campaign volume. The visible signal is "
            "on the send side, which is why the alert lives in email-service rather than here.",
            "There is deliberately no configuration flag to fail open. A flag would be used "
            "during an incident by someone who did not know what it meant.",
            "Transactional messages are unaffected: they rest on a different lawful basis and "
            "do not consult consent-service at all.",
        ),
    ),
    Decision(
        adr_id="ADR-0023",
        slug="payout-all-or-nothing",
        title="Payout batches are all-or-nothing",
        service="payout-service",
        status="Accepted (2025-08-14)",
        codes=("PAYOUT-4401",),
        context=(
            "A payout batch assembles thousands of seller payments. A failure partway leaves "
            "the question of whether to settle what was assembled."
        ),
        options=(
            ("Best-effort — settle what assembled, skip failures",
             "Sellers get paid sooner on average. Skipped sellers have no signal, and "
             "reconciling a partially settled batch against the ledger is genuinely difficult."),
            ("All-or-nothing",
             "One bad seller record delays every seller in the cycle. Reconciliation is trivial "
             "because a batch either exists in full or not at all."),
        ),
        decision=(
            "Batches are all-or-nothing. A single unresolvable seller aborts the batch; the "
            "operator excludes that seller explicitly and re-runs."
        ),
        consequences=(
            "An abort is loud and requires human judgement, which is intentional for money "
            "movement at this scale.",
            "The explicit exclusion step creates a record of who was skipped and why — the thing "
            "best-effort settlement would have lost.",
            "Batch duration matters more than it would otherwise, since the whole cycle waits. "
            "This is why `payout_batch_duration_minutes` is monitored separately from failure.",
        ),
    ),
    Decision(
        adr_id="ADR-0024",
        slug="tax-quote-retention",
        title="Tax quotes are retained for seven years in an append-only ledger",
        service="tax-service",
        status="Accepted (2025-02-27)",
        codes=("TAX-8830",),
        context=(
            "Several markets require us to reproduce the tax calculation for any transaction "
            "on demand for seven years. Recomputing is not sufficient — rates change, and the "
            "obligation is to show what was applied at the time."
        ),
        options=(
            ("Recompute on demand from historical rate tables",
             "Cheap in storage. Fails the actual requirement: it shows what we would compute "
             "now, not what we charged then."),
            ("Retain every quote, append-only, partitioned by month with archival to object storage",
             "Meets the requirement exactly. Storage grows monotonically and forever."),
        ),
        decision=(
            "Every quote is written to an append-only ledger before the response is returned. "
            "Partitions older than the hot window are archived to object storage and detached, "
            "never deleted."
        ),
        consequences=(
            "A ledger write failure means we served a quote we cannot evidence, which is why "
            "TAX-8830 is a critical fault rather than a storage nuisance.",
            "Disk growth is predictable and the archival job is therefore load-bearing. Its "
            "failure is what turns into TAX-8830 a week later.",
            "Deleting from the ledger is never a valid remediation, including under disk "
            "pressure. This is stated explicitly in the runbook because it is the obvious "
            "wrong move at 3am.",
        ),
    ),
    Decision(
        adr_id="ADR-0025",
        slug="audit-hash-chain",
        title="Audit log entries are hash-chained and anchored externally",
        service="audit-log-service",
        status="Accepted (2025-01-30)",
        codes=("AUD-9901",),
        context=(
            "An audit log stored in a database we control is only as trustworthy as our access "
            "controls. An auditor reasonably asked what would detect a privileged engineer "
            "deleting a row, and we had no answer."
        ),
        options=(
            ("Rely on database permissions and backups",
             "Backups are also under our control. A sufficiently privileged actor defeats both."),
            ("Hash-chain entries within the database",
             "Detects modification of any single entry, but a rewrite of the whole chain still "
             "verifies."),
            ("Hash-chain plus periodic anchors to immutable external storage",
             "A full-chain rewrite is detectable because the anchors are write-once and outside "
             "our mutable control."),
        ),
        decision=(
            "Each entry includes the hash of its predecessor. Every hour the current head hash "
            "is written to an object-storage bucket with object lock enabled, retained for "
            "seven years."
        ),
        consequences=(
            "Verification must actually run for any of this to mean anything, so the verifier "
            "is a monitored scheduled job and its failure to run is itself an alert.",
            "A detected break must never be repaired. The break is the evidence, and repairing "
            "it is indistinguishable from the tampering we are detecting.",
            "Anchoring costs one small object per hour, which is negligible; the object-lock "
            "retention policy is the part that needs periodic review.",
        ),
    ),
    Decision(
        adr_id="ADR-0026",
        slug="scheduler-at-most-once",
        title="Scheduled jobs are at-most-once with alerting, not at-least-once with retries",
        service="scheduler-service",
        status="Accepted (2025-09-09)",
        codes=("SCH-3310",),
        context=(
            "A missed ledger partition rollover blocked payments writes for 40 minutes. The "
            "obvious response was automatic retries, which would have been dangerous for "
            "several other jobs on the same scheduler."
        ),
        options=(
            ("At-least-once with automatic retry for all jobs",
             "Fixes the missed-schedule case. Payout batches and ledger rollovers are not "
             "idempotent; a retry would double-settle or corrupt."),
            ("At-least-once, opt-in per job",
             "Safer, but the default is then the dangerous one for any job whose author did not "
             "think about it."),
            ("At-most-once with a loud miss alert",
             "No job ever runs twice. A miss requires human action, which is slower but never "
             "destructive."),
        ),
        decision=(
            "Jobs fire at most once. A missed schedule pages. Individual jobs may opt into "
            "retry only after demonstrating idempotency in a test that is part of their "
            "registration."
        ),
        consequences=(
            "Recovery from a miss is manual, so the `--as-of` flag exists to let an operator "
            "supply the nominal time and get the correct window.",
            "The miss alert is the only thing standing between a silent skip and a downstream "
            "failure hours later, which makes it tier-1.",
            "Job authors must think about idempotency explicitly to get retries. Most do not "
            "bother, which is the correct default outcome.",
        ),
    ),
    Decision(
        adr_id="ADR-0027",
        slug="secrets-cached-at-boot",
        title="Workloads cache secrets at boot rather than fetching per use",
        service="secrets-broker",
        status="Accepted (2025-03-12)",
        codes=("SEC-9002", "CHK-7044"),
        context=(
            "Fetching a secret on every use makes secrets-broker a synchronous dependency of "
            "every request in the estate. Caching at boot makes rotation require a restart."
        ),
        options=(
            ("Fetch per use",
             "Rotation is instant. secrets-broker becomes tier-0 — its p99 lands in every "
             "service's p99, and its outage is a total estate outage."),
            ("Cache at boot",
             "Broker outages are survivable. Rotation requires a rolling restart, and a rotation "
             "done without one produces confusing partial failures."),
            ("Cache with background refresh",
             "Best of both in theory. In practice a refresh failure is silent and the workload "
             "carries on with a stale secret until it expires mid-request."),
        ),
        decision=(
            "Secrets are fetched at boot and held for the process lifetime. Rotation is a "
            "two-phase procedure: provision the new version, roll all consumers, then retire "
            "the old version after the reference count reaches zero."
        ),
        consequences=(
            "Rotation is a deployment event, not a configuration change. Runbooks that rotate "
            "a credential must include the restart step or the rotation silently does nothing.",
            "Retiring a version before consumers have rolled produces SEC-9002 and, for "
            "encryption keys, unreadable data (ACC-3301). The reference check exists because "
            "this is effectively irreversible.",
            "secrets-broker can be down for the length of an incident without customer impact, "
            "which is the main thing this decision buys.",
        ),
    ),
    Decision(
        adr_id="ADR-0028",
        slug="rate-limiter-fail-open",
        title="Rate limiting fails open by default, fails closed per route",
        service="rate-limiter",
        status="Accepted (2025-07-21)",
        codes=("RATE-5501",),
        context=(
            "When the limiter's Redis backend is unavailable we either stop limiting or stop "
            "serving. Neither is right for every route."
        ),
        options=(
            ("Fail open globally",
             "The site stays up. Abuse-sensitive routes are unprotected exactly when an attack "
             "may be causing the Redis pressure in the first place."),
            ("Fail closed globally",
             "Redis becomes a hard dependency for all traffic. A Redis blip becomes a total outage."),
            ("Fail open by default, fail closed on an explicit per-route list",
             "Requires someone to think about each sensitive route, which is the point."),
        ),
        decision=(
            "Default is fail-open. Routes handling credentials, gift-card lookup, and account "
            "enumeration are configured fail-closed individually."
        ),
        consequences=(
            "The fail-closed list is a security control that lives in configuration, so changes "
            "to it are reviewed like code and audited via audit-log-service.",
            "During a Redis outage the estate is partly protected and partly not, and responders "
            "need to know which. The list is reproduced in the rate-limiter runbook for that reason.",
            "Switching the global default to fail-closed during an incident is explicitly "
            "forbidden — it converts degraded protection into a full outage.",
        ),
    ),
    Decision(
        adr_id="ADR-0029",
        slug="wallet-serialisable",
        title="Wallet balance mutations use serialisable isolation",
        service="wallet-service",
        status="Accepted (2025-05-06)",
        codes=("WAL-3301",),
        context=(
            "Stored value is customer money. Concurrent spends on one wallet under read-committed "
            "isolation produced a negative balance in a load test, and would have in production."
        ),
        options=(
            ("Read-committed with an application-level check",
             "Fast, and wrong under concurrency — the check-then-act window is exactly the bug."),
            ("Row-level locking (SELECT FOR UPDATE)",
             "Correct for single-row mutations. Multi-wallet transfers can deadlock and the "
             "lock ordering discipline is easy to get wrong in new code."),
            ("Serialisable isolation",
             "Correct by construction. Conflicting transactions abort and must be retried, so "
             "retry rate becomes a normal operating metric rather than an error."),
        ),
        decision=(
            "All balance mutations run at SERIALIZABLE. The client library retries serialisation "
            "failures with jittered backoff up to five attempts."
        ),
        consequences=(
            "A retry rate of a few percent is healthy, not a problem to fix. The alert threshold "
            "is set at 5% to catch genuine hot-wallet contention rather than normal conflict.",
            "Any new code path touching balances must go through the mutation helper. A path "
            "that bypasses it loses the isolation guarantee silently, which is how WAL-3301 happens.",
            "Throughput per wallet is bounded by conflict rate rather than by hardware. This has "
            "not been a constraint in practice because contention on a single wallet is rare.",
        ),
    ),
    Decision(
        adr_id="ADR-0030",
        slug="pricing-effective-dating",
        title="Prices are effective-dated and never mutated",
        service="pricing-service",
        status="Accepted (2025-02-05)",
        codes=("PRC-3301", "PRC-3320"),
        context=(
            "Support could not answer what a customer was shown last Tuesday because the price "
            "row had been overwritten since. Disputes were being resolved on assertion."
        ),
        options=(
            ("Mutate the price row, keep an audit table",
             "The audit table becomes the real answer and is not itself queried by the resolution "
             "logic, so the two can drift."),
            ("Effective-dated versions, resolution picks the version live at a given instant",
             "One code path answers both 'what is the price' and 'what was the price', which is "
             "the property we actually want."),
        ),
        decision=(
            "A price change publishes a new version with an effective range. Resolution selects "
            "the version covering the requested instant, defaulting to now."
        ),
        consequences=(
            "Resolution can legitimately find nothing — a gap in the version chain produces "
            "PRC-3301 rather than a stale price, which is the safer failure.",
            "Clock correctness now affects prices, not just timestamps. A skewed clock resolves "
            "the wrong version and produces wrong prices with no error at all.",
            "The version chain grows without bound per SKU. Archival of versions older than the "
            "dispute window is a planned but unimplemented follow-up.",
        ),
    ),
    Decision(
        adr_id="ADR-0031",
        slug="promotion-compiled-rules",
        title="Promotion rules compile to a decision tree at publish time",
        service="promotion-service",
        status="Accepted (2025-10-14)",
        codes=("PROMO-5502", "PROMO-5530"),
        context=(
            "Merchandiser-authored rules were interpreted at request time. A malformed rule "
            "therefore failed in production during evaluation rather than at authoring, and the "
            "evaluation cost scaled with rule count on the checkout critical path."
        ),
        options=(
            ("Interpret at request time",
             "Simple and already built. Errors surface in production and latency grows with the "
             "rule catalogue."),
            ("Compile at publish time to an in-process decision tree",
             "Malformed rules fail at publish, where a merchandiser can see and fix them. "
             "Introduces a cache that must be invalidated."),
        ),
        decision=(
            "Rules compile at publish. The compiled tree is cached in-process with a 30-second "
            "TTL and invalidated by broadcast on publish or withdrawal."
        ),
        consequences=(
            "Rule errors move from runtime to publish time, which is the main win.",
            "The cache introduces PROMO-5530: a pod that misses the invalidation broadcast keeps "
            "applying a withdrawn promotion. Restart is always authoritative because the tree is "
            "rebuilt from Postgres at boot.",
            "The 30-second TTL is a deliberate compromise. Shorter turns every pod into a load "
            "generator against the promotions database; longer makes withdrawal too slow to be "
            "useful as an incident control.",
        ),
    ),
    Decision(
        adr_id="ADR-0032",
        slug="checkout-tax-estimate",
        title="Checkout may serve an estimated tax rate when the provider is unavailable",
        service="checkout-service",
        status="Accepted (2026-01-19)",
        codes=("CHK-7010", "TAX-8801"),
        context=(
            "A tax provider outage blocked checkout entirely. Finance was asked whether an "
            "estimated rate with post-hoc correction was acceptable, and confirmed that it is, "
            "within limits."
        ),
        options=(
            ("Block checkout when tax cannot be quoted",
             "Always correct, and converts a vendor outage into a revenue outage."),
            ("Serve the last good rate for the destination and flag for correction",
             "Checkout continues. A small number of orders need correcting afterwards, which "
             "Finance already has a process for."),
            ("Serve a zero rate and correct later",
             "Rejected. Undercharging tax is a worse regulatory position than estimating it."),
        ),
        decision=(
            "A cached-estimate fallback exists behind `checkout.tax.allow_estimate`, off by "
            "default and enabled during an incident. Orders priced with an estimate are flagged "
            "for Finance reconciliation."
        ),
        consequences=(
            "Enabling the fallback is an explicit incident decision, not an automatic behaviour, "
            "because it creates downstream work for Finance.",
            "Finance must be notified if the fallback runs beyond 30 minutes so they can size "
            "the correction batch.",
            "The estimate uses the last good rate for the destination, so it is wrong only when "
            "rates changed during the outage — rare, and bounded.",
        ),
    ),
    Decision(
        adr_id="ADR-0033",
        slug="quotes-are-binding",
        title="Issued B2B quotes are binding and are never retracted",
        service="quote-service",
        status="Accepted (2025-11-03)",
        codes=("QUOTE-9101",),
        context=(
            "A pricing error produced quotes below the negotiated floor. The commercial team "
            "asked whether we could withdraw them; legal advised that we had lost a contract "
            "dispute on a similar point previously."
        ),
        options=(
            ("Allow retraction of erroneous quotes within a grace period",
             "Recovers margin on the error. The customer has already relied on the quote, and "
             "our own terms do not reserve this right."),
            ("Honour every issued quote, block renewal at the bad price",
             "Costs margin once. Preserves the commercial meaning of a quote, which is the "
             "reason B2B customers use us."),
        ),
        decision=(
            "An issued quote stands until it expires. Errors are recorded, the account manager "
            "is notified, and renewal is blocked at the erroneous price."
        ),
        consequences=(
            "Correctness before issuance matters far more than availability, which is why "
            "quote-service has a lower availability SLO and a much stricter validation path than "
            "consumer checkout.",
            "The `quote_issued_below_floor` alert is a page even though nothing is broken — the "
            "damage is already done and the response is commercial, not technical.",
            "There is deliberately no retraction tooling. Building it would create the temptation "
            "to use it.",
        ),
    ),
    Decision(
        adr_id="ADR-0034",
        slug="cart-ttl-first-lever",
        title="Cart TTL reduction is the first lever under Redis memory pressure",
        service="cart-service",
        status="Accepted (2025-12-08)",
        codes=("CART-2140",),
        context=(
            "A traffic surge pushed the cart Redis cluster to its memory ceiling. The responder "
            "changed the eviction policy to `noeviction`, and add-to-cart began failing outright "
            "for everyone."
        ),
        options=(
            ("Scale the cluster first",
             "Correct long-term. Takes tens of minutes during which eviction continues, and "
             "resharding under memory pressure is itself risky."),
            ("Change the eviction policy to `noeviction`",
             "Superficially appealing because the setting's name matches the symptom. In fact it "
             "makes Redis reject writes once memory is full, so shoppers cannot add to cart at "
             "all — trading the loss of old abandoned carts for a total failure of the feature."),
            ("Reduce the anonymous cart TTL",
             "Retires the long tail of abandoned anonymous carts within seconds, which is where "
             "most of the memory is. Takes effect via config broadcast, no deployment."),
        ),
        decision=(
            "Under memory pressure, reduce `cart.anon_ttl_hours` first, then scale. The eviction "
            "policy stays `allkeys-lru` and is not an incident control."
        ),
        consequences=(
            "Some shoppers lose older anonymous carts, which the design already tolerates — "
            "anonymous carts are explicitly best-effort.",
            "Logged-in carts are unaffected because they have a Postgres cold copy.",
            "The TTL is a config value specifically so it can be changed during an incident "
            "without a deploy.",
        ),
    ),
    Decision(
        adr_id="ADR-0035",
        slug="catalog-fanout-eventual",
        title="Catalogue publishes are eventually consistent across consumers",
        service="catalog-service",
        status="Accepted (2025-03-28)",
        codes=("CAT-4455", "CAT-4410"),
        context=(
            "A catalogue publish must reach pricing, promotion, search, and recommendations. "
            "There is no practical transaction spanning four services and a search index."
        ),
        options=(
            ("Two-phase commit across consumers",
             "Any consumer being unavailable blocks all catalogue changes. Rejected on "
             "availability grounds alone."),
            ("Synchronous fan-out with rollback on failure",
             "Rollback of an applied search index update is not meaningfully possible."),
            ("Asynchronous fan-out via event-bus with a lag SLO",
             "The estate is briefly inconsistent after every publish, which we accept and monitor."),
        ),
        decision=(
            "Publishes fan out asynchronously with a 60-second p95 lag objective. Consumers are "
            "idempotent per event so replay is safe."
        ),
        consequences=(
            "Brief inconsistency after a publish is expected behaviour, not an incident. Only "
            "lag beyond the SLO is.",
            "A sellable-without-price window exists by construction, which is why "
            "`sellable_without_price_count` is monitored independently at the catalogue rather "
            "than being assumed away.",
            "Very large publishes exceed the SLO regardless of bus health, so the staged rollout "
            "option exists for subtree changes above roughly 500k events.",
        ),
    ),
    Decision(
        adr_id="ADR-0036",
        slug="inventory-no-read-replica",
        title="Inventory reads and writes share one connection pool for now",
        service="inventory-service",
        status="Accepted (2025-07-02)",
        codes=("INV-3007",),
        context=(
            "INV-3007 pool exhaustion is aggravated by reservation reads competing with writes. "
            "A read replica was proposed as the fix."
        ),
        options=(
            ("Add a read replica for reservation reads",
             "Halves pool pressure. Reservation reads must be strongly consistent — a stale read "
             "oversells stock — so most of them cannot use a replica anyway."),
            ("Split the pool into read and write halves against the same primary",
             "Prevents reads from starving writes without changing consistency. Does not increase "
             "total capacity."),
            ("Fix the queries and throttle the bulk import",
             "Addresses the actual cause. ADR-0012's index plus the 02:00 import window removed "
             "the majority of incidents."),
        ),
        decision=(
            "No read replica. The index from ADR-0012 and the scheduled import window are the "
            "mitigation. Revisit if INV-3007 recurs outside import windows."
        ),
        consequences=(
            "A supplier import outside its window remains a reliable way to cause INV-3007, "
            "which is why SUPP-7740 exists as a distinct fault pointing back here.",
            "Raising `maximumPoolSize` remains explicitly the wrong response: the database's own "
            "connection limit is the next ceiling and it is shared with other services.",
            "The decision is dated and conditional. If reservation volume grows enough that "
            "reads alone saturate the pool, it should be revisited.",
        ),
    ),
    Decision(
        adr_id="ADR-0037",
        slug="shipment-status-ranking",
        title="Shipment status is computed from ranked events, not the latest received",
        service="shipping-service",
        status="Accepted (2025-09-25)",
        codes=("SHIP-4430",),
        context=(
            "Carriers replay webhooks after their own outages, delivering old events last. "
            "Customers saw shipments regress from 'delivered' to 'in transit' and received "
            "contradictory notifications."
        ),
        options=(
            ("Order by carrier timestamp",
             "Better, but several carriers emit unreliable timestamps and two of them emit none."),
            ("Rank events by lifecycle position and take the highest seen",
             "Status becomes monotonic. Genuine regressions — a failed delivery attempt after a "
             "delivery scan — need explicit modelling as their own higher-ranked events."),
        ),
        decision=(
            "Each carrier event maps to a lifecycle rank. Status is the highest rank observed. "
            "Genuine regressions are modelled as distinct higher-ranked events rather than as "
            "reversals."
        ),
        consequences=(
            "Status is a pure function of the event set, so `bin/shipping recompute` is always "
            "safe and is the standard remedy for any status dispute.",
            "Adding a carrier requires mapping its event vocabulary to our ranks, which is the "
            "main integration cost for a new carrier.",
            "Out-of-order and duplicate webhooks stop mattering, which removes an entire class "
            "of race condition rather than merely mitigating it.",
        ),
    ),
    Decision(
        adr_id="ADR-0038",
        slug="indexer-checkpoint-after-flush",
        title="Index checkpoints commit strictly after segment flush",
        service="indexer-service",
        status="Accepted (2026-02-16)",
        codes=("IDX-7730",),
        context=(
            "A refactor reordered checkpoint commit before segment flush. A crash in the window "
            "between them silently lost catalogue updates, and the indexer reported perfect "
            "health throughout."
        ),
        options=(
            ("Checkpoint before flush",
             "Slightly faster and loses data on crash, silently. This is what the refactor "
             "accidentally did."),
            ("Checkpoint after flush",
             "A crash reprocesses recent events, which is safe because application is idempotent."),
            ("Two-phase commit between the offset store and the segment store",
             "Correct and disproportionate; idempotent reprocessing already gives us the property "
             "we need."),
        ),
        decision=(
            "Checkpoints commit only after the segment flush is durably acknowledged. "
            "Reprocessing on restart is accepted and is safe by idempotency."
        ),
        consequences=(
            "Restart may reprocess up to one flush interval of events. This is invisible "
            "downstream and is the price of never losing an update.",
            "`bin/indexer verify` exists because this fault class produces no error signal at "
            "all — health metrics look perfect while the index diverges.",
            "When rewinding a checkpoint, rewinding too far is free and rewinding too little is "
            "not, so the runbook says to over-rewind deliberately.",
        ),
    ),
    Decision(
        adr_id="ADR-0039",
        slug="ranking-no-imputation",
        title="Ranking models reject null features rather than imputing them",
        service="ranking-service",
        status="Accepted (2026-03-04)",
        codes=("RNK-3301",),
        context=(
            "A renamed metric made a feature null. The serving code imputed zero, and the model "
            "produced confident but badly wrong rankings for a week before anyone noticed."
        ),
        options=(
            ("Impute zero",
             "Serving continues. Zero is a meaningful value for most of our features, so the "
             "model is confidently wrong rather than obviously broken."),
            ("Impute the training-set mean",
             "Less wrong, still silent, and still produces a model operating outside its "
             "training distribution without saying so."),
            ("Reject and fall back to a model version that does not need the feature",
             "Loud, and degrades to a known-good state rather than an unknown one. Requires every "
             "model version to declare its feature dependencies at registration, which is extra "
             "work at training time."),
        ),
        decision=(
            "A null rate above threshold on a required feature causes the model version to be "
            "rejected and an earlier version without that dependency to be activated. Feature "
            "dependencies are recorded per model version."
        ),
        consequences=(
            "Ranking quality degrades to an older model rather than silently to nonsense, and "
            "the degradation is visible in the version metric.",
            "Every model version must declare its feature dependencies at registration, which "
            "is extra work at training time and the reason rollback is possible at all.",
            "Renaming a metric in metrics-store is now a breaking change for ranking, so "
            "MET-6601 and RNK-3301 cross-reference each other.",
        ),
    ),
    Decision(
        adr_id="ADR-0040",
        slug="suggest-blocklist-normalised",
        title="Suggestion blocklist matches on normalised forms, not literals",
        service="suggest-service",
        status="Accepted (2025-11-19)",
        codes=("SUG-2201",),
        context=(
            "A blocked term surfaced in type-ahead using a unicode homoglyph. The blocklist "
            "matched literals, so the variant passed every rule."
        ),
        options=(
            ("Expand the blocklist with known variants",
             "Endless. The variant space is effectively infinite and attackers enumerate it faster "
             "than we can."),
            ("Normalise candidates before matching",
             "Handles homoglyphs, diacritics, and spacing tricks with one rule each. Normalisation "
             "can over-match and suppress legitimate terms."),
            ("Raise the frequency threshold",
             "Delays every legitimate new suggestion and does not stop a coordinated push."),
        ),
        decision=(
            "Candidates are NFKC-normalised, confusable-folded, and stripped of separators before "
            "blocklist matching. The frequency threshold is unchanged."
        ),
        consequences=(
            "Some legitimate terms that normalise onto a blocked form are suppressed. This is "
            "accepted; an allowlist exists for the handful of cases reported.",
            "The blocklist is smaller and more maintainable because one entry now covers a family "
            "of variants.",
            "Raising the frequency threshold remains explicitly the wrong lever, which the runbook "
            "states because it is the intuitive one.",
        ),
    ),
    Decision(
        adr_id="ADR-0041",
        slug="supplier-sellable-gate",
        title="Supplier feeds cannot set products sellable directly",
        service="supplier-sync",
        status="Accepted (2025-10-30)",
        codes=("CAT-4410", "SUPP-7702"),
        context=(
            "A supplier feed set `sellable=true` on products with no price list, producing "
            "unbuyable product pages at scale (CAT-4410). The feed was doing what it was told; "
            "the problem was that it was allowed to decide at all."
        ),
        options=(
            ("Validate price existence during import",
             "Import becomes coupled to pricing availability and fails when pricing is slow, which "
             "is a bad trade for a batch process."),
            ("Let the feed set sellable and alert on unpriced-sellable products",
             "Detects the problem after customers have already seen it."),
            ("Remove the capability — sellability is a merchandiser decision",
             "The feed proposes; a human or an explicit rule disposes. Slower to launch products."),
        ),
        decision=(
            "supplier-sync may create and update products but may not set `sellable`. Sellability "
            "is set by merchandisers or by an explicit automated rule that checks price existence."
        ),
        consequences=(
            "New products from a feed require an explicit enablement step, which merchandisers "
            "considered acceptable given the alternative.",
            "If the importer is ever found writing `sellable` again, that is a regression to file "
            "rather than a situation to work around — stated in the CAT-4410 runbook.",
            "The unpriced-sellable alert remains in place as a backstop, because the gate is a "
            "policy and policies get bypassed.",
        ),
    ),
    Decision(
        adr_id="ADR-0042",
        slug="delta-guard-supplier-feeds",
        title="Supplier imports abort above a row-delta threshold",
        service="supplier-sync",
        status="Accepted (2025-08-27)",
        codes=("SUPP-7702",),
        context=(
            "A truncated supplier snapshot parsed cleanly and was interpreted as mass deletion, "
            "taking thousands of SKUs out of the catalogue. Nothing was malformed; the file was "
            "simply short."
        ),
        options=(
            ("Require deltas rather than snapshots",
             "Correct, and not within our power — several suppliers only produce snapshots."),
            ("Checksum or row-count header validation",
             "Depends on suppliers producing one. Most do not, and the ones that do are not the "
             "ones that truncate."),
            ("Compare row count against trailing average and abort beyond a threshold",
             "Works with any supplier and catches the failure mode directly."),
        ),
        decision=(
            "An import whose row delta exceeds 30% of the trailing average aborts and requires "
            "explicit operator approval to proceed."
        ),
        consequences=(
            "Legitimate large catalogue changes require approval, which is a small ongoing cost "
            "paid by Fulfilment.",
            "Lowering the threshold because it 'keeps blocking legitimate imports' has been tried "
            "and immediately preceded the next truncated feed going through. The runbook says so "
            "explicitly.",
            "Raw feeds are retained for 90 days so an approved-but-wrong import can still be "
            "rolled back.",
        ),
    ),
    Decision(
        adr_id="ADR-0043",
        slug="label-no-number-reuse",
        title="Tracking numbers are never reused",
        service="label-service",
        status="Accepted (2025-06-30)",
        codes=("LBL-2201",),
        context=(
            "Pool exhaustion during a peak week prompted a proposal to recycle tracking numbers "
            "from cancelled shipments."
        ),
        options=(
            ("Reuse numbers from cancelled shipments",
             "Free capacity immediately. Carriers treat reuse as a contract violation, and "
             "tracking becomes ambiguous for customers and for our own support."),
            ("Request larger allocations and suppress regeneration under pressure",
             "Requires carrier lead time, which is why the pool alert fires at 5000 remaining "
             "rather than at zero."),
        ),
        decision=(
            "Numbers are consumed permanently. Under pool pressure, regeneration is suppressed "
            "and an emergency allocation is requested from the carrier."
        ),
        consequences=(
            "The 5000-remaining threshold exists to give the partner manager time to negotiate; "
            "it is a lead-time alert, not a capacity alert.",
            "Elevated regeneration is a leading indicator of pool exhaustion, so it is monitored "
            "separately at 2%.",
            "Suppressing regeneration does not invalidate labels already issued, which makes it "
            "a safe incident control.",
        ),
    ),
    Decision(
        adr_id="ADR-0044",
        slug="returns-approval-coupling",
        title="Auto-approval threshold and receipt requirement must be changed together",
        service="returns-service",
        status="Proposed (2026-05-11)",
        codes=("RET-8801",),
        context=(
            "The auto-refund value threshold was raised to reduce support load. The receipt "
            "requirement was a separate setting and was not re-evaluated, so higher-value returns "
            "began auto-refunding without physical receipt (RET-8801)."
        ),
        options=(
            ("Documentation and review process",
             "Relies on the reviewer knowing the coupling exists, which is exactly what failed."),
            ("Merge the two settings into one policy object validated as a unit",
             "Makes the coupling explicit in code. Requires a migration of existing configuration."),
            ("Add a validation rule rejecting unsafe combinations",
             "Smaller change, catches the specific known-bad combinations, does not generalise."),
        ),
        decision=(
            "Proposed: merge into a single validated returns policy object where threshold and "
            "receipt requirement cannot be set independently. Not yet implemented."
        ),
        consequences=(
            "Until implemented, the coupling is enforced only by the RET-8801 runbook warning, "
            "which is why that runbook calls it out at length.",
            "The migration must handle in-flight returns approved under the old policy.",
            "This ADR is referenced from the runbook specifically so that anyone hitting the "
            "fault finds the pending fix rather than reinventing it.",
        ),
    ),
    Decision(
        adr_id="ADR-0045",
        slug="account-pii-column-encryption",
        title="PII is encrypted per column with versioned keys",
        service="account-service",
        status="Accepted (2025-01-14)",
        codes=("ACC-3301",),
        context=(
            "Full-disk encryption protects against physical theft and nothing else. A database "
            "credential leak would have exposed all customer PII in plaintext."
        ),
        options=(
            ("Rely on disk encryption and access control",
             "No protection against the credential-compromise threat, which is the realistic one."),
            ("Encrypt the whole row",
             "Makes every non-PII query decrypt PII, and breaks indexing on non-sensitive columns."),
            ("Encrypt PII columns individually with versioned keys from secrets-broker",
             "Targeted. Queries on encrypted columns need deterministic encryption or a separate "
             "search index, and key lifecycle becomes load-bearing."),
        ),
        decision=(
            "PII columns are encrypted individually. Keys are versioned and served by "
            "secrets-broker. Email uses deterministic encryption to preserve equality lookup; "
            "other fields use randomised encryption."
        ),
        consequences=(
            "Key retirement now has a data-availability consequence: retiring a version before "
            "re-encryption completes makes rows unreadable (ACC-3301, SEC-9002).",
            "Deterministic encryption on email leaks equality, which is an accepted trade for "
            "being able to look an account up by email at all.",
            "Nulling out unreadable columns is never a valid remediation — it is irreversible "
            "destruction of customer data.",
        ),
    ),
    Decision(
        adr_id="ADR-0046",
        slug="session-separate-from-token",
        title="Sessions are tracked separately from access tokens",
        service="session-service",
        status="Accepted (2025-04-02)",
        codes=("SESS-1101",),
        context=(
            "Access tokens are 15-minute JWTs validated statelessly. That is good for "
            "performance and means we cannot revoke anything before expiry, which is "
            "unacceptable for a compromised account."
        ),
        options=(
            ("Shorten token lifetime toward zero",
             "Approaches a stateful check with worse performance and still leaves a window."),
            ("Check a revocation list on every token validation",
             "Makes every request stateful, which is what stateless JWTs were adopted to avoid."),
            ("Track a longer-lived session separately; refresh consults it",
             "Revocation takes effect at the next refresh, bounded by token lifetime, without "
             "making validation stateful."),
        ),
        decision=(
            "A session outlives individual tokens and is consulted at refresh. Revoking a session "
            "stops refresh, so access ends within one token lifetime — at most 15 minutes."
        ),
        consequences=(
            "Revocation is not instantaneous, and the 15-minute bound is the deliberate ceiling. "
            "Anyone expecting immediate cutoff needs to know this.",
            "Cross-region propagation adds up to 10 seconds on top, which is why SESS-1101 "
            "distinguishes normal propagation from a genuine stall.",
            "The session cookie's 30-day lifetime makes session theft more valuable than token "
            "theft, which shapes where we spend security effort.",
        ),
    ),
    Decision(
        adr_id="ADR-0047",
        slug="mfa-totp-window",
        title="TOTP acceptance window stays at ±1 step",
        service="mfa-service",
        status="Accepted (2026-04-08)",
        codes=("MFA-5501",),
        context=(
            "Clock drift on one node produced TOTP rejections. Widening the acceptance window "
            "was proposed as a fix, mirroring the same suggestion made and rejected for "
            "AUTH-1015."
        ),
        options=(
            ("Widen the window to ±3 steps",
             "Hides drift up to 90 seconds. Also extends the usable life of a phished code to 90 "
             "seconds, which is the entire attack TOTP defends against."),
            ("Keep ±1 and fix clocks",
             "Requires clock discipline to actually work, which is a solved problem we simply "
             "have to monitor."),
        ),
        decision=(
            "The window stays at ±1 step (30 seconds either side). Clock drift is fixed at the "
            "node, and nodes exceeding 50ms offset alert."
        ),
        consequences=(
            "MFA-5501 and AUTH-1015 share a root cause and a fix, and both runbooks say plainly "
            "that widening the window is not the remedy.",
            "Users experience rejections while a node is drifted, which is the cost of not "
            "extending the phishing window.",
            "Node clock health is a security control, not merely an operational nicety, and is "
            "monitored as one.",
        ),
    ),
    Decision(
        adr_id="ADR-0048",
        slug="email-priority-separation",
        title="Transactional and marketing email use separate priority queues",
        service="email-service",
        status="Accepted (2025-05-20)",
        codes=("EMAIL-2240", "EMAIL-2201"),
        context=(
            "A large campaign delayed order confirmations by eleven minutes. Both streams shared "
            "one queue and one ESP connection pool."
        ),
        options=(
            ("Separate ESP accounts per stream",
             "Cleanest isolation. Sender reputation is per domain, so a bad campaign still "
             "damages transactional deliverability — the isolation is partial and expensive."),
            ("Priority queues over a shared pool",
             "Transactional always drains first. Does not isolate reputation."),
            ("Both",
             "Deferred on cost; the ESP contract is priced per account."),
        ),
        decision=(
            "Two priority classes over the shared pool, transactional strictly ahead of "
            "marketing. Reputation remains shared and is managed by pausing campaigns."
        ),
        consequences=(
            "Latency isolation is solved; reputation isolation is not. EMAIL-2201 remains a "
            "critical fault precisely because bounce damage crosses the streams.",
            "A marketing batch enqueued at transactional priority defeats the whole mechanism, "
            "which is the priority-inversion case in EMAIL-2240.",
            "Failing over to the second ESP does not escape reputation damage, since that follows "
            "the sending domain.",
        ),
    ),
    Decision(
        adr_id="ADR-0049",
        slug="loyalty-accrual-on-paid-amount",
        title="Points accrue on the amount actually charged",
        service="loyalty-service",
        status="Accepted (2026-01-27)",
        codes=("LOY-6603",),
        context=(
            "Points were accruing on pre-discount basket value. With a promotion applied, "
            "members earned points on money nobody paid, inflating a balance-sheet liability."
        ),
        options=(
            ("Accrue on pre-discount value",
             "Generous and indefensible to Finance, since the liability is not backed by revenue."),
            ("Accrue on the charged amount",
             "Defensible. Members earn fewer points on discounted orders, which Growth expected "
             "to be unpopular."),
            ("Accrue on charged amount with explicit promotional bonuses",
             "Keeps the accounting clean and lets Growth run bonus-point promotions deliberately, "
             "with the cost visible."),
        ),
        decision=(
            "Accrual base is the amount charged. Bonus points are a separate, explicitly "
            "budgeted promotion component."
        ),
        consequences=(
            "An accrual base above the charged amount is now definitionally a bug, which is what "
            "`bin/loyalty explain` checks for.",
            "Bonus-point promotions have a visible cost line, so over-accrual from stacking "
            "(PROMO-5502 interacting with tier multipliers) is attributable.",
            "Clawing back already-visible points remains a customer-trust decision for Finance "
            "and Growth, not an engineering one.",
        ),
    ),
    Decision(
        adr_id="ADR-0050",
        slug="campaign-audience-staleness-guard",
        title="Campaigns refuse to send against audiences older than 48 hours",
        service="campaign-service",
        status="Accepted (2025-12-16)",
        codes=("CMP-8801", "ETL-4401"),
        context=(
            "A churn-winback campaign was scheduled three weeks in advance against an audience "
            "built at authoring time. A large fraction of recipients had ordered since."
        ),
        options=(
            ("Rebuild audiences automatically at send time",
             "Always fresh. Audience builds are heavy warehouse queries and a large campaign "
             "could not complete its build inside the send window."),
            ("Warn the author and send anyway",
             "Warnings at authoring time are read; warnings at send time are not read by anyone."),
            ("Refuse to send beyond a staleness threshold",
             "Forces a rebuild before send. Blocks the campaign if the warehouse is unavailable."),
        ),
        decision=(
            "A campaign whose audience is older than 48 hours fails to start and requires a "
            "rebuild. An override exists but is audited and requires a stated reason."
        ),
        consequences=(
            "warehouse-etl failures now block campaigns rather than silently degrading them, "
            "which is why ETL-4401 lists Growth as a notification target.",
            "The override is deliberately awkward. Disabling the guard for 'time-sensitive' "
            "campaigns is precisely the case that generates complaints.",
            "Audience size and delivered volume still legitimately differ, because consent is "
            "checked at send time (ADR-0022).",
        ),
    ),
    Decision(
        adr_id="ADR-0051",
        slug="metrics-cardinality-budget",
        title="Metrics have an enforced per-team cardinality budget",
        service="metrics-store",
        status="Accepted (2026-02-02)",
        codes=("MET-6601",),
        context=(
            "A service deployed a metric labelled with request id. Active series went from 4M to "
            "11M in under an hour and ingest began rejecting samples for every team."
        ),
        options=(
            ("Review metrics in code review",
             "Already nominally the case. Did not catch it, and will not reliably."),
            ("Scale the store to absorb growth",
             "Cardinality from an unbounded label grows without bound by definition. The cost "
             "lands on every team for a metric that is useless at that cardinality."),
            ("Per-team series budget enforced at ingest, with automatic drop rules",
             "Contains the blast radius to the offending team."),
        ),
        decision=(
            "Each team has a series budget. Exceeding it drops the largest offending metric at "
            "ingest and notifies the team, rather than degrading the store for everyone."
        ),
        consequences=(
            "A team can lose its own metric visibility by exceeding budget, which is a strong "
            "and fast incentive.",
            "`bin/metrics drop-rule` remains available for immediate manual containment during "
            "an incident, since budget enforcement operates on a slower cycle.",
            "Metric renames are a breaking change for model features, so removals need "
            "coordination with ranking and fraud (ADR-0039).",
        ),
    ),
    Decision(
        adr_id="ADR-0052",
        slug="topic-repartition-procedure",
        title="Topic repartitioning requires a consumer-side migration, not partition addition",
        service="event-bus",
        status="Accepted (2025-10-07)",
        codes=("BUS-1201",),
        context=(
            "A skewed partition key caused sustained backpressure. Adding partitions was "
            "attempted and did not help, because existing keys keep hashing to their current "
            "partitions."
        ),
        options=(
            ("Add partitions to the existing topic",
             "Does not rebalance existing keys. Breaks ordering for keys that do move. This was "
             "tried and made things marginally worse."),
            ("Create a new topic with a better key and migrate consumers",
             "Correct and operationally involved: dual-write, drain, cut over, retire."),
            ("Accept the skew and scale the affected consumer",
             "Works for one-off fanouts, not for a structurally skewed key."),
        ),
        decision=(
            "Structural skew is fixed by creating a new topic with a better partition key and "
            "running a dual-write migration. Adding partitions to an existing topic is not a "
            "remedy for skew."
        ),
        consequences=(
            "The migration is a multi-day procedure and is documented separately as a runbook "
            "annexe.",
            "For a one-off fanout — a large catalogue publish — the correct response remains to "
            "let it drain while scaling the consumer, not to repartition.",
            "Partition key choice is now a review item for any new topic, since changing it later "
            "is this expensive.",
        ),
    ),
    Decision(
        adr_id="ADR-0053",
        slug="stream-event-time",
        title="Stream processing uses event time with idleness detection",
        service="stream-processor",
        status="Accepted (2025-11-25)",
        codes=("STRM-2240",),
        context=(
            "A producer with a skewed clock held the global watermark back and stalled every "
            "windowed aggregate on the stream. Switching to processing time was proposed."
        ),
        options=(
            ("Switch to processing time",
             "Stalls disappear. Aggregates become non-deterministic on replay, which makes fraud "
             "features impossible to reproduce during an investigation."),
            ("Event time with idleness detection",
             "Keeps determinism. An idle or lagging partition stops blocking the global watermark "
             "after a timeout, at the cost of possibly excluding genuinely late events."),
        ),
        decision=(
            "Event time is retained. Source idleness detection is enabled with a 60-second "
            "timeout so a stalled partition cannot hold the global watermark."
        ),
        consequences=(
            "Replay determinism is preserved, which is the property fraud investigations depend on.",
            "Genuinely late events beyond the idleness timeout are excluded from their window. "
            "This is a real correctness cost, accepted because the alternative is worse.",
            "A skewed producer clock is still a bug to fix at the producer; idleness detection is "
            "a containment measure, not a solution.",
        ),
    ),
    Decision(
        adr_id="ADR-0054",
        slug="etl-row-count-verification",
        title="ETL job success requires a row-count check, not merely an absence of exceptions",
        service="warehouse-etl",
        status="Accepted (2026-03-19)",
        codes=("ETL-4401",),
        context=(
            "A pipeline read from a lagging replica and loaded a fraction of the expected rows. "
            "It reported success because nothing threw. Campaign audiences and finance reports "
            "were quietly incomplete for two days."
        ),
        options=(
            ("Trust job exit status",
             "The status quo, which failed exactly as described."),
            ("Compare row counts against a trailing average and fail outside a band",
             "Catches truncation and silent partial loads. Produces false alarms on genuine "
             "business volume changes."),
            ("Full source-target reconciliation",
             "Definitive and too expensive to run daily at our volumes."),
        ),
        decision=(
            "Every load compares row count against the trailing seven-day average and fails "
            "outside a configured band. Failure blocks downstream consumers rather than "
            "publishing."
        ),
        consequences=(
            "Job success now means something. 'No exception was raised' is explicitly not "
            "evidence of a complete load, which the runbook states directly.",
            "Genuine volume changes — a sale weekend — trip the band and need an operator "
            "acknowledgement. This is the accepted false-positive cost.",
            "Loads are idempotent per window so a failed load can simply be re-run against the "
            "primary.",
        ),
    ),
    Decision(
        adr_id="ADR-0055",
        slug="config-broadcast-best-effort",
        title="Configuration broadcast is best-effort; etcd is the source of truth",
        service="config-service",
        status="Accepted (2025-07-15)",
        codes=("CFG-1120", "FLAG-4401"),
        context=(
            "Making broadcast reliable would require per-client delivery tracking and retry "
            "state in config-service, turning a simple fan-out into a queue with a durable "
            "outbox per consumer."
        ),
        options=(
            ("Guaranteed delivery with per-client outboxes",
             "Removes partial application. Substantially more complex and makes config-service "
             "stateful per client, which is a large step for a tier-1 dependency."),
            ("Best-effort broadcast plus authoritative read at boot",
             "Simple, and partial application becomes a normal failure mode that operators must "
             "check for."),
        ),
        decision=(
            "Broadcast is best-effort fan-out. Clients read authoritative state from etcd at "
            "boot. An acknowledgement ratio is exposed so partial application is detectable."
        ),
        consequences=(
            "A config write succeeding is not evidence the change took effect. For anything "
            "safety-relevant the ack ratio must be checked — this is the core lesson of both "
            "CFG-1120 and FLAG-4401.",
            "Restarting a pod is always an authoritative fix, because boot reads from etcd.",
            "A kill switch that is 90% applied is not a kill switch, which is why the flags "
            "runbook says to restart rather than wait during an incident.",
        ),
    ),
    Decision(
        adr_id="ADR-0056",
        slug="slot-capacity-hard-limit",
        title="Delivery slot capacity is a hard limit enforced with row-level locks",
        service="slot-service",
        status="Accepted (2025-09-02)",
        codes=("SLOT-6601",),
        context=(
            "Slot capacity represents vans and drivers. Overselling is not a virtual problem "
            "that can be resolved by apologising — someone does not receive their delivery."
        ),
        options=(
            ("Optimistic reservation with reconciliation",
             "Higher throughput and permits oversell between reconciliation runs, which is the "
             "one outcome we cannot accept."),
            ("Row-level lock on capacity decrement",
             "Serialises reservations per slot. Throughput per slot is bounded, which is fine "
             "because capacity is small by nature."),
        ),
        decision=(
            "Capacity decrements take a row-level lock on the slot. Any code path reserving "
            "capacity must go through the decrement helper."
        ),
        consequences=(
            "Reservation throughput per slot is bounded by lock contention. Slots hold tens of "
            "deliveries, so this has never been the constraint.",
            "A new code path bypassing the helper reintroduces oversell silently, which is one of "
            "the two causes listed in SLOT-6601.",
            "Capacity reduced after reservations are taken still oversells. That is an operations "
            "sequencing problem the lock cannot solve, and the runbook distinguishes the two cases.",
        ),
    ),
    Decision(
        adr_id="ADR-0057",
        slug="warehouse-adapter-buffering",
        title="3PL confirmations buffer locally when a partner adapter fails",
        service="warehouse-service",
        status="Accepted (2026-04-21)",
        codes=("WH-5501",),
        context=(
            "A partner API change stopped accepting pick confirmations at one site. Picking "
            "halted because confirmations were synchronous, idling a full shift."
        ),
        options=(
            ("Halt picking when confirmations fail",
             "Keeps our state and the partner's state aligned, at the cost of stopping physical "
             "work in a building full of people."),
            ("Skip confirmation and continue",
             "Physical stock and recorded stock diverge, costing the site a full cycle count."),
            ("Buffer confirmations locally and replay on recovery",
             "Picking continues and alignment is restored on recovery, provided the buffer is "
             "durable."),
        ),
        decision=(
            "Confirmations buffer to durable local storage when the adapter fails, and replay "
            "automatically on recovery. Buffering is enabled per site during an incident."
        ),
        consequences=(
            "The buffer must be durable and bounded. An unbounded buffer during a multi-day "
            "partner outage becomes its own problem.",
            "Disabling confirmation entirely remains the wrong response, and the runbook says so "
            "— divergence costs a cycle count.",
            "Each partner's adapter fails differently, so buffering is a per-site control rather "
            "than a global one.",
        ),
    ),
    Decision(
        adr_id="ADR-0058",
        slug="review-cluster-moderation",
        title="Coordinated review spam is caught by clustering, not per-review scoring",
        service="review-service",
        status="Accepted (2026-05-28)",
        codes=("REV-5501",),
        context=(
            "A paid campaign submitted paraphrased reviews each scoring below the spam "
            "threshold. Per-review classification cannot catch text specifically written to sit "
            "under whatever threshold is chosen."
        ),
        options=(
            ("Lower the per-review threshold",
             "Rejects legitimate short reviews at a much higher rate than it catches campaigns, "
             "which are written to evade any fixed threshold."),
            ("Cluster by submission window, account age, and text similarity",
             "Catches the coordination signal, which is the thing that actually distinguishes a "
             "campaign from genuine feedback."),
        ),
        decision=(
            "Submissions are clustered and suspicious clusters are quarantined to the human "
            "moderation queue. The per-review threshold is unchanged."
        ),
        consequences=(
            "Quarantine rather than auto-reject, because removing legitimate customer feedback is "
            "its own harm and clustering has false positives.",
            "Weekend clusters land on Monday, since the moderation queue is staffed in business "
            "hours only.",
            "Verified-purchaser checks do not help against compromised accounts with genuine "
            "order history, which is why clustering was needed at all.",
        ),
    ),
    Decision(
        adr_id="ADR-0059",
        slug="recommendation-fallback-visibility",
        title="Recommendation fallbacks are alerted on, not treated as success",
        service="recommendation-service",
        status="Accepted (2026-06-09)",
        codes=("REC-4401",),
        context=(
            "Recommendation quality collapsed for nine days while every health check passed. "
            "The service was serving editorial fallbacks, which return HTTP 200."
        ),
        options=(
            ("Treat fallback as success",
             "The status quo, which hid a nine-day quality outage behind green dashboards."),
            ("Return an error when the model path fails",
             "Makes the failure visible and breaks the page render, which the fallback exists "
             "specifically to prevent."),
            ("Serve the fallback and alert on the fallback ratio",
             "Page renders, and the degradation is visible as a first-class metric."),
        ),
        decision=(
            "Fallbacks continue to serve. `recommendation_fallback_ratio` above 10% pages, and "
            "the ratio is on the team's primary dashboard."
        ),
        consequences=(
            "A class of silent quality failure becomes a monitored condition. This pattern — "
            "graceful degradation must be alerted, not just implemented — is now a review "
            "checklist item across Growth.",
            "Raising the 120ms budget to reduce fallbacks is the wrong lever, since "
            "recommendations sit on the page-render path for every shopper.",
            "The same reasoning applies to fraud fail-open (ADR-0021) and search zero-result rate: "
            "wherever we degrade gracefully, we must measure the degradation.",
        ),
    ),
)
