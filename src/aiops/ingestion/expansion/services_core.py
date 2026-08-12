"""Meridian services: commerce, payments, and fulfilment domains.

Every fault below is a distinct failure mode with its own detection step, fix
and anti-pattern. Where a fault rhymes with one of the original seven — a pool
exhaustion, an upstream timeout, an OOM — that is intentional distractor
pressure for the evaluation set, not copy-paste.
"""

from __future__ import annotations

from aiops.ingestion.expansion.specs import Fault, Service

# ---------------------------------------------------------------------------
# Commerce
# ---------------------------------------------------------------------------

COMMERCE: tuple[Service, ...] = (
    Service(
        name="cart-service",
        domain="commerce",
        team="Commerce",
        language="Go 1.24",
        purpose=(
            "Holds the pre-checkout basket. Carts are session-scoped for anonymous "
            "shoppers and account-scoped once authenticated, and the merge on login "
            "is the single most bug-prone path in the service."
        ),
        datastore="Redis (primary, 72h TTL) with a Postgres `carts` table as cold storage for logged-in users",
        dependencies=("catalog-service", "pricing-service", "session-service"),
        hazards=(
            "Redis is the source of truth for anonymous carts. A Redis failover loses "
            "them outright; this is accepted, but it means cart loss during a failover "
            "is not a bug to chase.",
            "The login merge is last-write-wins per SKU, not additive. Two tabs open on "
            "the same account will silently drop a quantity change.",
        ),
        alerts=(
            "`cart_merge_conflict_rate > 2%` (ticket)",
            "`redis_evicted_keys > 0` (page — the TTL should be doing this work, not eviction)",
        ),
        slo="99.95% availability, p99 read under 40ms",
        faults=(
            Fault(
                code="CART-2101",
                title="Cart merge conflict on login",
                severity="medium",
                symptom=(
                    "A shopper reports items disappearing after signing in. The log shows "
                    "`merge conflict sku=... anon_qty=... acct_qty=...` immediately after a "
                    "session upgrade."
                ),
                causes=(
                    "The same account active in two clients, each with a divergent anonymous cart",
                    "A stale anonymous cart resurrected from a bookmarked session cookie",
                ),
                detection=(
                    "Filter cart-service logs by `sub=` for the affected user and look for two "
                    "`session upgraded` lines under 60s apart. Two upgrades means two carts."
                ),
                fix=(
                    "Restore from the Postgres cold copy: `bin/cart-restore --account <id> "
                    "--at <timestamp>`. The cold copy lags Redis by up to 30s, so confirm the "
                    "restored quantity with the shopper before applying."
                ),
                antipattern=(
                    "Do not switch the merge to additive as a hotfix. Additive merge double-counts "
                    "every shopper who adds the same item before and after login, which is a far "
                    "more common path than the conflict it fixes."
                ),
                escalation="Commerce on-call during business hours. Not a paging fault.",
            ),
            Fault(
                code="CART-2140",
                title="Redis eviction under memory pressure",
                severity="high",
                symptom=(
                    "Carts vanish for a broad population at once, and `redis_evicted_keys` climbs "
                    "while `used_memory` sits at `maxmemory`."
                ),
                causes=(
                    "A traffic surge growing the anonymous cart population faster than the 72h TTL retires it",
                    "A bulk import writing oversized cart payloads (a promotion campaign attaching metadata per line)",
                ),
                detection=(
                    "`redis-cli INFO memory` on the primary. If `used_memory` equals `maxmemory` and "
                    "`maxmemory_policy` is `allkeys-lru`, eviction is already happening."
                ),
                fix=(
                    "Shed load first by dropping the anonymous TTL to 24h via config-service "
                    "(`cart.anon_ttl_hours=24`), which retires the long tail immediately. Then "
                    "size the cluster up. See ADR-0034 for why the TTL is the first lever."
                ),
                antipattern=(
                    "Do not switch `maxmemory_policy` to `noeviction`. Writes then fail outright and "
                    "shoppers cannot add to cart at all — a worse failure than losing old carts."
                ),
                escalation="Page Commerce on-call; notify Growth if a campaign is implicated.",
                related=("CART-2101",),
            ),
        ),
    ),
    Service(
        name="checkout-service",
        domain="commerce",
        team="Commerce",
        language="Go 1.24",
        purpose=(
            "Drives the checkout funnel: address validation, shipping-option selection, "
            "tax quote, and handoff to order-service. It owns no money and no stock — it "
            "assembles a proposal and hands it on."
        ),
        datastore="Postgres `checkout` cluster, PgBouncer in transaction pooling mode",
        dependencies=("cart-service", "tax-service", "shipping-service", "order-service", "promotion-service"),
        hazards=(
            "The tax quote is cached for 15 minutes per (address, basket hash). A tax-rate "
            "change does not take effect for existing sessions inside that window.",
            "PgBouncer transaction pooling means session-level Postgres features (advisory "
            "locks, prepared statements outside a transaction) silently misbehave.",
        ),
        alerts=(
            "`checkout_funnel_dropoff > 15%` over 10m (page)",
            "`tax_quote_stale_served > 0` (ticket)",
        ),
        slo="99.9% availability, p95 funnel step under 600ms",
        faults=(
            Fault(
                code="CHK-7010",
                title="Checkout blocked on tax quote timeout",
                severity="high",
                symptom=(
                    "Shoppers stall on the review step. Logs show `tax quote timeout after 2000ms "
                    "provider=vertexa` followed by the funnel step returning 503."
                ),
                causes=(
                    "tax-service degraded or its upstream provider rate-limiting us",
                    "A basket with an unusually high line count exceeding the provider's per-request budget",
                ),
                detection=(
                    "Take the trace id and check tax-service for TAX-8801 on the same trace. If "
                    "tax-service is healthy, look at the line count in the request — over 200 lines "
                    "we exceed the provider budget regardless of provider health."
                ),
                fix=(
                    "Enable the cached-estimate fallback (`checkout.tax.allow_estimate=true`). It "
                    "serves the last good rate for the destination and flags the order for "
                    "post-hoc correction, which Finance already has a process for."
                ),
                antipattern=(
                    "Do not raise the 2000ms timeout. The funnel budget is 600ms p95; a longer tax "
                    "timeout converts a fast, correctable failure into an abandoned checkout."
                ),
                escalation="Page Commerce on-call. Notify Finance if the estimate fallback runs over 30 minutes.",
                related=("TAX-8801",),
            ),
            Fault(
                code="CHK-7044",
                title="Address validation provider rejecting all requests",
                severity="critical",
                symptom="Every checkout fails at the address step with `validation provider returned 401`.",
                causes=(
                    "The provider API key rotated without the secret being updated in secrets-broker",
                    "The provider revoked the key after a billing failure",
                ),
                detection=(
                    "A 401 rather than a 5xx is decisive: this is authentication, not availability. "
                    "Check `bin/secrets-broker get checkout/addressa_key --metadata` for the "
                    "rotation timestamp and compare it against the last deploy."
                ),
                fix=(
                    "Roll the secret forward from secrets-broker and restart the deployment to "
                    "pick it up. checkout-service caches the credential at boot — see SEC-9002 for "
                    "why that cache exists and why it makes rotation a restart-triggering event."
                ),
                antipattern=(
                    "Do not disable address validation to clear the incident. Unvalidated addresses "
                    "become undeliverable shipments, and the cost lands on Fulfilment weeks later."
                ),
                escalation="Page Commerce and Platform Security together.",
                related=("SEC-9002",),
            ),
        ),
    ),
    Service(
        name="pricing-service",
        domain="commerce",
        team="Commerce",
        language="Java 21",
        purpose=(
            "Resolves the price of a SKU for a given market, currency, and customer tier. "
            "Prices are versioned and effective-dated; the service never mutates a price, "
            "it publishes a new version."
        ),
        datastore="Postgres `pricing` cluster, read replicas per region",
        dependencies=("catalog-service", "promotion-service", "config-service"),
        hazards=(
            "Effective-dated rows mean a clock problem produces *wrong prices*, not errors. "
            "This is the most dangerous failure mode in the domain.",
            "The regional read replicas lag the primary by up to 4s. A price publish is not "
            "globally visible immediately, and the funnel can quote two different prices.",
        ),
        alerts=(
            "`price_version_lag_seconds > 10` (page)",
            "`price_resolution_miss_rate > 0.1%` (page — a miss means we could not price a sellable SKU)",
        ),
        slo="99.99% availability, p99 resolution under 25ms",
        faults=(
            Fault(
                code="PRC-3301",
                title="Price resolution miss for a sellable SKU",
                severity="critical",
                symptom=(
                    "`no effective price sku=... market=... tier=...` and the product page renders "
                    "without a buy button."
                ),
                causes=(
                    "A price version published with an effective date in the future and no predecessor",
                    "A market added in catalog-service without a corresponding price list",
                    "Replica lag serving a region that has not yet received the publish",
                ),
                detection=(
                    "`bin/pricing explain --sku <sku> --market <market>` prints the version chain it "
                    "considered and why each was rejected. If the chain is empty, this is a missing "
                    "price list, not a lag problem."
                ),
                fix=(
                    "Publish a fallback price from the base market with "
                    "`bin/pricing publish --from-base --sku <sku> --market <market>`. If the cause is "
                    "replica lag, confirm with `price_version_lag_seconds` and wait rather than "
                    "publishing — a duplicate publish creates a second version to reconcile later."
                ),
                antipattern=(
                    "Never hard-code a price in the front end to unblock a launch. The price then "
                    "exists nowhere in the audit trail, and tax and promotion both resolve against "
                    "the real price, producing a basket that does not add up."
                ),
                escalation="Page Commerce immediately — this is revenue-blocking.",
                related=("CAT-4410", "PROMO-5502"),
            ),
            Fault(
                code="PRC-3320",
                title="Replica lag serving stale prices",
                severity="high",
                symptom=(
                    "The product page and the cart disagree on price for the same SKU within one "
                    "session. `price_version_lag_seconds` is elevated in one region only."
                ),
                causes=(
                    "A large bulk publish saturating the replication stream",
                    "A long-running analytics query on the replica blocking WAL apply",
                ),
                detection=(
                    "Compare `pg_last_wal_replay_lsn()` on the lagging replica against the primary's "
                    "`pg_current_wal_lsn()`. Then check `pg_stat_activity` on the replica for a query "
                    "older than the lag."
                ),
                fix=(
                    "Kill the blocking analytics query. Route pricing reads to the primary for the "
                    "affected region with `pricing.read_from_primary_markets=[<market>]` until the "
                    "replica catches up."
                ),
                antipattern=(
                    "Do not route all regions to the primary as a blanket fix. The primary is sized "
                    "for writes plus one region's reads; global read traffic will saturate it and turn "
                    "a regional inconsistency into a global outage."
                ),
                escalation="Commerce on-call. Page if two regions lag simultaneously.",
                related=("PRC-3301",),
            ),
        ),
    ),
    Service(
        name="promotion-service",
        domain="commerce",
        team="Growth",
        language="Java 21",
        purpose=(
            "Evaluates discount rules against a basket. Rules are authored by merchandisers "
            "in a DSL and compiled to a decision tree at publish time, so a bad rule is a "
            "publish-time failure rather than a runtime one — usually."
        ),
        datastore="Postgres `promotions` cluster; compiled rule trees cached in-process",
        dependencies=("pricing-service", "loyalty-service", "config-service"),
        hazards=(
            "Rule evaluation is order-dependent and the order is the merchandiser's stated "
            "priority. Two rules at equal priority evaluate in publish order, which is not "
            "stable across a rebuild.",
            "Stacking limits are enforced per basket, not per line. A rule that looks capped "
            "at 20% can compound to more across multiple lines.",
        ),
        alerts=(
            "`promo_discount_ratio > 0.6` on any single basket (page — likely a stacking bug)",
            "`rule_compile_failures > 0` (ticket)",
        ),
        slo="99.9% availability, p99 evaluation under 50ms",
        faults=(
            Fault(
                code="PROMO-5502",
                title="Discount stacking beyond the intended cap",
                severity="critical",
                symptom=(
                    "Baskets settling far below cost. `discount_ratio=0.78 rules_applied=[...]` in the "
                    "evaluation log, with more rule ids than the merchandiser expected."
                ),
                causes=(
                    "Two rules at equal priority both marked `stackable: true`",
                    "A category rule and a SKU rule overlapping where the category was recently widened in catalog-service",
                ),
                detection=(
                    "`bin/promo explain --basket <id>` replays the decision tree and prints each rule's "
                    "contribution. The rule that should have terminated evaluation but did not is the bug."
                ),
                fix=(
                    "Disable the offending rule immediately with `bin/promo disable --rule <id>` — this "
                    "takes effect within one cache TTL (30s) and does not require a deploy. Then fix the "
                    "priority or the stackable flag and republish."
                ),
                antipattern=(
                    "Do not apply a global discount ceiling in checkout as the fix. It hides the broken "
                    "rule, and the basket the shopper was shown no longer matches the basket they are "
                    "charged for, which is a consumer-protection problem in several of our markets."
                ),
                escalation=(
                    "Page Growth on-call and notify Finance. Any basket already settled below cost is a "
                    "revenue-recognition matter, not just an engineering one."
                ),
                related=("PRC-3301", "LOY-6603"),
            ),
            Fault(
                code="PROMO-5530",
                title="Rule tree cache serving a withdrawn promotion",
                severity="high",
                symptom=(
                    "A promotion continues applying after being withdrawn, on some pods only. "
                    "`rule_tree_version=` differs across replicas in the same deployment."
                ),
                causes=(
                    "A pod that missed the invalidation broadcast because it was mid-restart",
                    "config-service unavailable at the moment of withdrawal, so the broadcast never fanned out",
                ),
                detection=(
                    "`kubectl exec` into each pod and curl `localhost:9090/rules/version`. A version "
                    "mismatch across pods confirms a partial invalidation."
                ),
                fix=(
                    "Force a rolling restart of the deployment. The rule tree is rebuilt from Postgres "
                    "at boot, so a restart is always authoritative."
                ),
                antipattern=(
                    "Do not shorten the cache TTL to seconds to make invalidation 'safer'. Rule "
                    "compilation is expensive and a short TTL turns every pod into a constant load "
                    "generator against the promotions database."
                ),
                escalation="Growth on-call.",
                related=("CFG-1120",),
            ),
        ),
    ),
    Service(
        name="tax-service",
        domain="commerce",
        team="Commerce",
        language="Python 3.12",
        purpose=(
            "Calculates tax for a basket and destination by delegating to a third-party "
            "engine (Vertexa) with a local cache. It is a thin service with heavy "
            "compliance obligations: every quote is retained for seven years."
        ),
        datastore="Postgres `tax` cluster (append-only quote ledger) plus a 15-minute Redis quote cache",
        dependencies=("checkout-service", "config-service", "audit-log-service"),
        hazards=(
            "The quote ledger is append-only and legally retained. Never delete from it, "
            "including to clear disk pressure — archive instead.",
            "Vertexa rate-limits per account, not per host. Scaling out pods does not buy "
            "more tax throughput and will simply distribute the 429s.",
        ),
        alerts=(
            "`tax_provider_429_rate > 1/min` (page)",
            "`tax_ledger_write_failures > 0` (page — a lost quote is a compliance gap)",
        ),
        slo="99.9% availability, p95 quote under 400ms",
        faults=(
            Fault(
                code="TAX-8801",
                title="Tax provider rate limit exceeded",
                severity="high",
                symptom="`provider returned 429 retry_after=30` and checkout begins reporting CHK-7010.",
                causes=(
                    "Traffic above the contracted provider quota, typically during a campaign",
                    "Cache hit rate collapsing after a catalog change altered basket hashes",
                ),
                detection=(
                    "Check `tax_cache_hit_ratio` first. A normal ratio with high volume means we are "
                    "genuinely over quota; a collapsed ratio means the cache key changed and the "
                    "volume is self-inflicted."
                ),
                fix=(
                    "If the cache collapsed, that is the bug — find the catalog change that altered "
                    "the basket hash. If we are genuinely over quota, enable the estimate fallback in "
                    "checkout (CHK-7010) and request a temporary quota increase from the provider."
                ),
                antipattern=(
                    "Do not add pods. The rate limit is per account; more pods produce the same total "
                    "throughput with more failed requests and a worse cache hit rate."
                ),
                escalation="Page Commerce. Provider quota increases go through the vendor manager, not support.",
                related=("CHK-7010",),
            ),
            Fault(
                code="TAX-8830",
                title="Quote ledger write failure",
                severity="critical",
                symptom=(
                    "`ledger append failed` with the quote still served to the shopper. The quote "
                    "exists in the response but not in the retained record."
                ),
                causes=(
                    "Disk pressure on the tax cluster — the ledger is append-only and grows monotonically",
                    "A schema migration holding an ACCESS EXCLUSIVE lock on the ledger table",
                ),
                detection=(
                    "Check free space on the tax primary and `pg_locks` for a lock on `tax_quotes`. "
                    "The append-only design means growth is predictable, so sudden pressure usually "
                    "means archival stopped running."
                ),
                fix=(
                    "Restart the archival job (`bin/tax-archive --before <date>`), which moves quotes "
                    "older than the hot window to object storage. Then replay the missed appends from "
                    "the application-side write-ahead buffer with `bin/tax-replay`."
                ),
                antipattern=(
                    "Never `DELETE FROM tax_quotes` to reclaim space. The retention obligation is seven "
                    "years and deletion is not recoverable. Archive, then drop the archived partition."
                ),
                escalation="Page Commerce and notify Compliance within one hour of a confirmed gap.",
            ),
        ),
    ),
    Service(
        name="catalog-service",
        domain="commerce",
        team="Discovery",
        language="Java 21",
        purpose=(
            "System of record for products, variants, categories, and market availability. "
            "Everything downstream — pricing, search, promotion — derives from it, which "
            "makes a bad catalog publish unusually far-reaching."
        ),
        datastore="Postgres `catalog` cluster; change events published to event-bus",
        dependencies=("event-bus", "supplier-sync", "audit-log-service"),
        hazards=(
            "A category tree change fans out to pricing, promotion and search. There is no "
            "transactional boundary across those consumers, so the system is briefly "
            "inconsistent by design after every publish.",
            "Variant deletion is soft. A 'deleted' variant still resolves in older caches for "
            "up to an hour.",
        ),
        alerts=(
            "`catalog_publish_fanout_lag_seconds > 120` (page)",
            "`sellable_without_price_count > 0` (page)",
        ),
        slo="99.95% availability, publish fanout under 60s p95",
        faults=(
            Fault(
                code="CAT-4410",
                title="Sellable product published without a price",
                severity="critical",
                symptom=(
                    "`sellable_without_price sku=... market=...` and the product renders unbuyable. "
                    "pricing-service reports PRC-3301 for the same SKU."
                ),
                causes=(
                    "A market enabled on a product before the price list was published",
                    "A supplier-sync import setting `sellable=true` from feed data without checking pricing",
                ),
                detection=(
                    "`bin/catalog audit --market <market> --unpriced` lists every affected SKU. If the "
                    "list correlates with one supplier, the import is the cause."
                ),
                fix=(
                    "Set the affected SKUs non-sellable immediately (`bin/catalog set-sellable false "
                    "--from-file`), then have Commerce publish the missing price list and re-enable. "
                    "Unbuyable-but-visible is worse than absent."
                ),
                antipattern=(
                    "Do not let supplier-sync set `sellable` directly. ADR-0041 moved that decision "
                    "behind an explicit merchandiser gate for exactly this reason; if you find the "
                    "importer writing it again, that is a regression to file, not to work around."
                ),
                escalation="Page Discovery; notify Commerce for the price publish.",
                related=("PRC-3301", "SUPP-7702"),
            ),
            Fault(
                code="CAT-4455",
                title="Category tree publish fanout stalled",
                severity="high",
                symptom=(
                    "Search and promotion disagree with the catalog for longer than the usual "
                    "inconsistency window. `catalog_publish_fanout_lag_seconds` climbing without bound."
                ),
                causes=(
                    "event-bus partition backpressure (see BUS-1201)",
                    "A category tree change touching an unusually large subtree, producing millions of events",
                ),
                detection=(
                    "Check the fanout event count for the publish: `bin/catalog publish-stats --id <id>`. "
                    "Over roughly 500k events the fanout will not complete within the SLO regardless of "
                    "bus health."
                ),
                fix=(
                    "For an oversized publish, cancel and re-issue it as a staged rollout "
                    "(`--stage-by-category`). For bus backpressure, resolve BUS-1201 first — the fanout "
                    "will drain on its own once the bus recovers."
                ),
                antipattern=(
                    "Do not replay the publish while the first one is still draining. Consumers are "
                    "idempotent per event but the duplicate volume doubles the drain time."
                ),
                escalation="Discovery on-call.",
                related=("BUS-1201",),
            ),
        ),
    ),
    Service(
        name="quote-service",
        domain="commerce",
        team="Commerce",
        language="Go 1.24",
        purpose=(
            "Assembles binding B2B quotes: negotiated pricing, volume tiers, and an expiry. "
            "Unlike consumer checkout, a quote is a commitment — once issued we honour it "
            "until it expires, which makes correctness matter more than availability here."
        ),
        datastore="Postgres `quotes` cluster, append-only with a materialised current-state view",
        dependencies=("pricing-service", "tax-service", "account-service"),
        hazards=(
            "An issued quote is binding. There is no mechanism to retract one, only to "
            "decline renewal, so a pricing bug that reaches quote issuance is expensive.",
            "Volume tiers are evaluated against forecast quantity, not committed quantity. "
            "Customers can qualify for a tier they never reach.",
        ),
        alerts=(
            "`quote_issued_below_floor > 0` (page)",
            "`quote_expiry_backlog > 100` (ticket)",
        ),
        slo="99.5% availability, quote assembly under 3s",
        faults=(
            Fault(
                code="QUOTE-9101",
                title="Quote issued below the negotiated floor",
                severity="critical",
                symptom="`quote below floor account=... floor=... issued=...` after the quote is already sent.",
                causes=(
                    "A promotion rule leaking into the B2B path, which should be excluded (PROMO-5502)",
                    "A volume tier applied against forecast rather than committed quantity",
                ),
                detection=(
                    "`bin/quote explain --id <id>` prints every price component and its source. A "
                    "promotion component appearing at all in a B2B quote is the bug."
                ),
                fix=(
                    "The quote is binding and stands. Record it, notify the account manager, and "
                    "block renewal at the bad price. Then fix the leak: B2B baskets must carry "
                    "`channel=b2b`, which promotion-service excludes by rule."
                ),
                antipattern=(
                    "Do not attempt to retract or silently reprice an issued quote. It has been "
                    "relied upon by the customer, and we have lost a contract dispute on exactly "
                    "this point before."
                ),
                escalation="Page Commerce, notify the named account manager and Finance same-day.",
                related=("PROMO-5502",),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

PAYMENTS: tuple[Service, ...] = (
    Service(
        name="ledger-service",
        domain="payments",
        team="Payments",
        language="Java 21",
        purpose=(
            "Double-entry ledger of record for every movement of money. Every other "
            "payments component is eventually reconciled against it. It is append-only "
            "and never updated in place."
        ),
        datastore="Postgres `ledger` cluster, partitioned monthly, with a standby in a second region",
        dependencies=("payment-service", "refund-service", "payout-service", "audit-log-service"),
        hazards=(
            "Entries are immutable. A wrong entry is corrected by a compensating entry, "
            "never by an UPDATE — the audit trail depends on it.",
            "Month-end partition rollover is the highest-risk scheduled operation in the "
            "domain; a failed rollover blocks all writes.",
        ),
        alerts=(
            "`ledger_imbalance_cents != 0` (page — the books do not balance)",
            "`ledger_write_latency_p99 > 200ms` (page)",
        ),
        slo="99.99% availability, no tolerated data loss",
        faults=(
            Fault(
                code="LED-1001",
                title="Ledger imbalance detected",
                severity="critical",
                symptom=(
                    "The balance check job reports `imbalance_cents=<non-zero>` for a period. Debits "
                    "and credits do not sum to zero."
                ),
                causes=(
                    "A partial write during a failover — one leg of a double entry committed, the other did not",
                    "A compensating entry applied twice after a retry without an idempotency key",
                ),
                detection=(
                    "`bin/ledger bisect --period <period>` narrows the imbalance to a transaction "
                    "window by binary search. From there `bin/ledger show --txn <id>` prints both legs."
                ),
                fix=(
                    "Post a compensating entry with `bin/ledger compensate --txn <id> --reason <ref>`. "
                    "Every compensation requires a written reason referencing an incident id; this is "
                    "an audit requirement, not a formality."
                ),
                antipattern=(
                    "Never UPDATE or DELETE a ledger row to make the books balance. It destroys the "
                    "audit trail, and the imbalance will reappear at the next period close with no "
                    "way to trace what happened."
                ),
                escalation=(
                    "Page Payments immediately and notify Finance and Compliance within the hour. An "
                    "imbalance is a reportable control failure regardless of size."
                ),
                related=("PAY-5021", "REF-2201"),
            ),
            Fault(
                code="LED-1030",
                title="Monthly partition rollover failed",
                severity="critical",
                symptom=(
                    "All ledger writes failing at the start of a month with `no partition for value "
                    "(<date>)`. Every payments write path stalls behind it."
                ),
                causes=(
                    "The rollover job did not run because scheduler-service was down at the boundary",
                    "The rollover ran but could not acquire the ACCESS EXCLUSIVE lock within its timeout",
                ),
                detection=(
                    "`\\d+ ledger_entries` in psql shows the partition list. If the current month is "
                    "absent, this is the fault regardless of what else is failing."
                ),
                fix=(
                    "Create the partition manually: `bin/ledger create-partition --month <YYYY-MM>`. "
                    "It is idempotent and safe to run during the incident. Then investigate why the "
                    "scheduled job missed — see SCH-3310."
                ),
                antipattern=(
                    "Do not create a catch-all DEFAULT partition to prevent recurrence. Rows landing "
                    "in DEFAULT cannot later be moved into the correct partition without an exclusive "
                    "lock and a full rewrite, which is far worse at volume."
                ),
                escalation="Page Payments and Data Platform together.",
                related=("SCH-3310",),
            ),
        ),
    ),
    Service(
        name="refund-service",
        domain="payments",
        team="Payments",
        language="Go 1.24",
        purpose=(
            "Issues refunds against captured charges, either full or partial, and reconciles "
            "them against the processor and the ledger. It is deliberately slow and "
            "conservative: every refund is a real movement of money out."
        ),
        datastore="Postgres `refunds` cluster",
        dependencies=("payment-service", "ledger-service", "returns-service"),
        hazards=(
            "Refunds are not idempotent at the processor unless our idempotency key is "
            "supplied. A retry without one issues a second refund.",
            "Partial refunds against a partially-captured charge are rejected by the "
            "processor with a generic error, which reads as an availability problem but is not.",
        ),
        alerts=(
            "`duplicate_refund_detected > 0` (page)",
            "`refund_reconciliation_backlog > 0 for 60m` (page)",
        ),
        slo="99.5% availability, refund settled within 24h",
        faults=(
            Fault(
                code="REF-2201",
                title="Duplicate refund issued",
                severity="critical",
                symptom=(
                    "The reconciliation job reports two processor refunds for one order. "
                    "`duplicate refund order_id=... processor_refs=[...]`."
                ),
                causes=(
                    "A retry issued without reusing the original idempotency key",
                    "An operator re-running `bin/refund issue` after a timeout, where the first call had in fact succeeded",
                ),
                detection=(
                    "Query the processor ledger by order id rather than trusting our own records: "
                    "`bin/refund processor-history --order <id>`. Our side may only show one."
                ),
                fix=(
                    "Recover the duplicate through the processor's reversal API where the window "
                    "allows, otherwise raise a collection with Finance. Post a compensating ledger "
                    "entry either way so the books reflect reality."
                ),
                antipattern=(
                    "Do not retry a refund that timed out without first querying the processor. This "
                    "is the same lesson as INC-2025-1103 on the capture side: a timeout tells you that "
                    "you did not hear back, not that nothing happened."
                ),
                escalation="Page Payments; notify Finance same-day.",
                related=("LED-1001", "PAY-5021"),
            ),
            Fault(
                code="REF-2240",
                title="Partial refund rejected against a partial capture",
                severity="medium",
                symptom=(
                    "`processor rejected refund: amount exceeds captured` on an order where our "
                    "records show a sufficient captured amount."
                ),
                causes=(
                    "The original authorisation was partially captured and our record reflects the authorised amount",
                    "A prior partial refund not yet reflected in our view of remaining balance",
                ),
                detection=(
                    "Compare `bin/refund processor-history --order <id>` against our captured amount. "
                    "A mismatch confirms our view is stale rather than the processor being wrong."
                ),
                fix=(
                    "Resync the charge state (`bin/payment resync --order <id>`), then reissue the "
                    "refund for the true remaining balance."
                ),
                antipattern=(
                    "Do not refund the difference through a separate manual payout to 'make the "
                    "customer whole'. It bypasses the refund audit trail and reconciliation will flag "
                    "it as an unmatched outflow every month until someone unpicks it."
                ),
                escalation="Payments on-call during business hours.",
                related=("REF-2201",),
            ),
        ),
    ),
    Service(
        name="payout-service",
        domain="payments",
        team="Payments",
        language="Java 21",
        purpose=(
            "Pays marketplace sellers on a scheduled cycle. Batches settle nightly; a batch "
            "is all-or-nothing so a partial failure leaves no sellers half-paid."
        ),
        datastore="Postgres `payouts` cluster",
        dependencies=("ledger-service", "account-service", "scheduler-service"),
        hazards=(
            "A batch holds a long transaction while it assembles. Batch assembly during "
            "business hours competes with interactive queries on the same cluster.",
            "Seller bank details are verified asynchronously. A payout can be assembled "
            "against details that fail verification before settlement.",
        ),
        alerts=(
            "`payout_batch_failed > 0` (page)",
            "`payout_batch_duration_minutes > 90` (ticket)",
        ),
        slo="Batches settle by 06:00 UTC on the scheduled day",
        faults=(
            Fault(
                code="PAYOUT-4401",
                title="Nightly batch aborted mid-assembly",
                severity="high",
                symptom=(
                    "`batch aborted batch_id=... assembled=... of ...` and no sellers paid for the cycle."
                ),
                causes=(
                    "A statement timeout on the long assembly transaction",
                    "A seller record failing bank-detail verification mid-batch, which aborts the whole batch by design",
                ),
                detection=(
                    "`bin/payout batch-log --id <id>` shows the last seller processed. If it names a "
                    "specific seller with a verification error, the abort is working as intended and "
                    "the fix is that seller's details, not the batch."
                ),
                fix=(
                    "Exclude the failing seller (`bin/payout exclude --seller <id> --cycle <cycle>`) "
                    "and re-run the batch. The excluded seller rolls into the next cycle automatically."
                ),
                antipattern=(
                    "Do not switch the batch to best-effort so it skips failures silently. All-or-nothing "
                    "is deliberate — a partially settled batch is extremely difficult to reconcile, and "
                    "sellers who were skipped have no signal that anything went wrong."
                ),
                escalation="Payments on-call. Page if the batch will miss the 06:00 UTC commitment.",
            ),
        ),
    ),
    Service(
        name="fraud-service",
        domain="payments",
        team="Payments",
        language="Python 3.12",
        purpose=(
            "Scores transactions for fraud risk in-line with checkout and returns "
            "allow / review / deny. It is on the critical path with a hard 250ms budget, "
            "and it fails open by design."
        ),
        datastore="Feature store on Redis; model artefacts in object storage",
        dependencies=("account-service", "metrics-store", "config-service"),
        hazards=(
            "The service fails open. A fraud-service outage does not block checkout — it "
            "silently removes fraud protection, which is a business risk that looks like nothing.",
            "Model refresh is a hot-swap. A bad model artefact degrades scoring without any "
            "error being raised.",
        ),
        alerts=(
            "`fraud_fail_open_rate > 1%` (page — protection is off)",
            "`fraud_deny_rate` deviating more than 3σ from the trailing week (page)",
        ),
        slo="99.9% availability, p99 scoring under 250ms",
        faults=(
            Fault(
                code="FRAUD-6601",
                title="Scoring failing open under latency pressure",
                severity="high",
                symptom=(
                    "`scoring budget exceeded, failing open` at volume. Checkout is unaffected and "
                    "no customer-visible error occurs, which is precisely the danger."
                ),
                causes=(
                    "Feature store latency — usually a Redis hot key on a popular merchant id",
                    "A model artefact larger than the previous one pushing inference past the budget",
                ),
                detection=(
                    "Split `fraud_scoring_latency` by stage. Feature fetch versus inference tells you "
                    "immediately which half of the budget is being consumed."
                ),
                fix=(
                    "If the feature store is the cause, enable the local feature cache "
                    "(`fraud.feature_cache=true`) which trades a little staleness for latency. If the "
                    "model is the cause, roll back to the previous artefact with `bin/fraud model-rollback`."
                ),
                antipattern=(
                    "Do not raise the 250ms budget to stop the fail-opens. The budget exists because "
                    "checkout has a funnel SLO; raising it moves the failure into checkout where it is "
                    "far more expensive. Fail-open is the designed relief valve — fix the cause."
                ),
                escalation=(
                    "Page Payments. Sustained fail-open above 10 minutes should be reported to Risk, "
                    "who may choose to tighten rules elsewhere while we are unprotected."
                ),
            ),
            Fault(
                code="FRAUD-6640",
                title="Deny rate spike after model refresh",
                severity="critical",
                symptom=(
                    "Legitimate checkouts denied en masse immediately after a model hot-swap. "
                    "`deny_rate` several σ above the trailing baseline."
                ),
                causes=(
                    "A model trained on a skewed window — typically one that included a past incident's traffic",
                    "A feature schema change where the model reads a renamed feature as null",
                ),
                detection=(
                    "`bin/fraud shadow --model <artefact> --replay 1h` scores the last hour of real "
                    "traffic through the candidate model without acting on it. Compare deny rates."
                ),
                fix=(
                    "Roll back the artefact immediately (`bin/fraud model-rollback`) — this is a hot "
                    "swap and takes effect in under a minute. Investigate afterwards; do not debug in place."
                ),
                antipattern=(
                    "Do not disable fraud scoring entirely to clear the denials. Fail-open removes "
                    "protection completely, whereas a rollback restores a known-good model. Rollback is "
                    "always available and always preferable."
                ),
                escalation="Page Payments and Risk. Customer Support needs a heads-up — denied shoppers will contact them.",
                related=("FRAUD-6601",),
            ),
        ),
    ),
    Service(
        name="wallet-service",
        domain="payments",
        team="Payments",
        language="Rust",
        purpose=(
            "Stored-value balances: gift cards, store credit, and refund-to-wallet. "
            "Balances are strongly consistent and every mutation is a ledger-backed "
            "transaction, because a wallet balance is customer money."
        ),
        datastore="Postgres `wallet` cluster with serialisable isolation on balance mutations",
        dependencies=("ledger-service", "account-service", "refund-service"),
        hazards=(
            "Serialisable isolation means concurrent spends on one wallet will genuinely "
            "conflict and retry. High retry rates are expected, not a bug, up to a point.",
            "Gift card codes are bearer instruments. Anyone with the code can spend it, so "
            "code enumeration is an active attack surface.",
        ),
        alerts=(
            "`wallet_serialisation_retry_rate > 5%` (ticket)",
            "`wallet_negative_balance_count > 0` (page)",
        ),
        slo="99.99% availability, p99 balance mutation under 80ms",
        faults=(
            Fault(
                code="WAL-3301",
                title="Negative wallet balance",
                severity="critical",
                symptom="`negative balance wallet=... balance_cents=-...` from the invariant checker.",
                causes=(
                    "A spend applied outside a serialisable transaction — usually a new code path that missed the helper",
                    "A refund-to-wallet reversal applied twice",
                ),
                detection=(
                    "`bin/wallet history --id <wallet>` prints every mutation with its transaction id. "
                    "Two mutations sharing a transaction id is the duplicate-reversal case."
                ),
                fix=(
                    "Freeze the wallet (`bin/wallet freeze --id <wallet>`), reconcile against the "
                    "ledger, then post a correcting mutation. Unfreeze only after the invariant checker "
                    "passes for that wallet."
                ),
                antipattern=(
                    "Do not zero the balance to clear the alert. The difference is customer money that "
                    "either we owe them or they owe us, and zeroing loses the ability to tell which."
                ),
                escalation="Page Payments; notify Finance.",
                related=("LED-1001",),
            ),
            Fault(
                code="WAL-3350",
                title="Gift card code enumeration detected",
                severity="high",
                symptom=(
                    "A sharp rise in `code lookup miss` from a narrow set of source addresses, with "
                    "occasional hits."
                ),
                causes=(
                    "An attacker walking the code space, usually after a code-format leak",
                    "A legitimate bulk-validation integration misconfigured to retry aggressively",
                ),
                detection=(
                    "Group lookup misses by source address over five-minute windows. A genuine shopper "
                    "generates one or two lookups; enumeration generates hundreds."
                ),
                fix=(
                    "Apply a per-source rate limit through rate-limiter (`bin/ratelimit add --route "
                    "wallet.lookup --key source_ip --limit 5/min`) and rotate the code format for "
                    "unissued cards."
                ),
                antipattern=(
                    "Do not simply lengthen new codes and leave the existing ones. Codes already in "
                    "circulation remain enumerable, and those are the ones with money on them."
                ),
                escalation="Page Payments and Platform Security.",
                related=("RATE-5501",),
            ),
        ),
    ),
    Service(
        name="invoice-service",
        domain="payments",
        team="Payments",
        language="Python 3.12",
        purpose=(
            "Generates and delivers invoices for B2B orders, including PDF rendering and "
            "the statutory fields each market requires. Delivery is guaranteed; rendering "
            "is best-effort and retried."
        ),
        datastore="Postgres `invoices` cluster; rendered PDFs in object storage",
        dependencies=("quote-service", "tax-service", "email-service", "audit-log-service"),
        hazards=(
            "Invoice numbering is strictly sequential per market by law. A gap is a "
            "compliance finding, so the sequence is allocated inside the same transaction "
            "as the invoice row and never pre-allocated.",
            "PDF rendering runs a headless browser and is by far the heaviest workload in "
            "the payments domain.",
        ),
        alerts=(
            "`invoice_sequence_gap_detected > 0` (page)",
            "`invoice_render_queue_depth > 500` (ticket)",
        ),
        slo="Invoice delivered within 4h of order completion",
        faults=(
            Fault(
                code="BILL-7701",
                title="Invoice sequence gap",
                severity="critical",
                symptom="`sequence gap market=... expected=... found=...` from the nightly audit.",
                causes=(
                    "A transaction that allocated a sequence number and then rolled back",
                    "A manual invoice created outside the service",
                ),
                detection=(
                    "`bin/invoice audit --market <market> --range <from>:<to>` lists the missing "
                    "numbers. Cross-reference against the application log for rollbacks at that timestamp."
                ),
                fix=(
                    "Issue a documented void invoice for each missing number "
                    "(`bin/invoice void --number <n> --reason <ref>`). A void with a reason satisfies "
                    "the sequence requirement; a silent gap does not."
                ),
                antipattern=(
                    "Do not renumber existing invoices to close the gap. Those numbers have already "
                    "been sent to customers and filed with their own accounting."
                ),
                escalation="Page Payments; Compliance must be informed before the market's filing deadline.",
            ),
            Fault(
                code="BILL-7730",
                title="Render queue saturated by headless browser workers",
                severity="medium",
                symptom=(
                    "`render queue depth=...` climbing, invoices delivered late, and worker pods "
                    "showing high memory with no crash."
                ),
                causes=(
                    "A batch of unusually long invoices (hundreds of lines) each holding a browser instance",
                    "A leaked browser process from a render that timed out without cleanup",
                ),
                detection=(
                    "`kubectl exec` into a worker and count chromium processes. More processes than "
                    "configured concurrency means leaked instances."
                ),
                fix=(
                    "Restart the render workers to clear leaked processes, then cap per-invoice line "
                    "count for single-pass rendering (`invoice.render.max_lines=200`); longer invoices "
                    "render in paginated passes."
                ),
                antipattern=(
                    "Do not scale render workers indefinitely. Each holds a browser and several hundred "
                    "MB; the node runs out of memory before the queue drains, and then delivery stops "
                    "entirely rather than merely running late."
                ),
                escalation="Payments on-call during business hours.",
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Fulfilment
# ---------------------------------------------------------------------------

FULFILMENT: tuple[Service, ...] = (
    Service(
        name="warehouse-service",
        domain="fulfilment",
        team="Fulfilment",
        language="Java 21",
        purpose=(
            "Represents physical warehouse state: bins, pick lists, and putaway. It is the "
            "bridge between our systems and the warehouse management systems our 3PL "
            "partners run, each of which behaves slightly differently."
        ),
        datastore="Postgres `warehouse` cluster, one schema per site",
        dependencies=("inventory-service", "shipping-service", "supplier-sync"),
        hazards=(
            "Each 3PL partner exposes a different API dialect behind a common adapter. A "
            "partner-specific bug looks like a generic one until you check the site id.",
            "Pick lists are optimistically locked. A picker's device going offline mid-pick "
            "holds the lock until a 15-minute lease expires.",
        ),
        alerts=(
            "`pick_list_lease_expired_rate > 5%` (ticket)",
            "`wms_adapter_error_rate > 1%` per site (page)",
        ),
        slo="99.9% availability during site operating hours",
        faults=(
            Fault(
                code="WH-5501",
                title="3PL adapter rejecting pick confirmations",
                severity="high",
                symptom=(
                    "`wms adapter error site=... code=...` for one site only, with pick confirmations "
                    "piling up unacknowledged."
                ),
                causes=(
                    "The partner deployed an API change without notice",
                    "A certificate expiry on the partner's mutual-TLS endpoint",
                ),
                detection=(
                    "Check whether the failure is isolated to one `site=`. A single site points at the "
                    "partner; multiple sites point at us. Then `bin/wms probe --site <site>` runs a "
                    "synthetic confirmation and prints the raw partner response."
                ),
                fix=(
                    "Queue confirmations locally (`warehouse.adapter.buffer=true`) so picking continues, "
                    "and open a partner incident. Buffered confirmations replay automatically on recovery."
                ),
                antipattern=(
                    "Do not disable confirmation entirely to keep the site moving. Unconfirmed picks "
                    "diverge from physical stock and the reconciliation afterwards costs the site a full "
                    "cycle count."
                ),
                escalation="Fulfilment on-call; partner escalation path is in the site's runbook annexe.",
            ),
            Fault(
                code="WH-5530",
                title="Pick list locks held by offline devices",
                severity="medium",
                symptom=(
                    "Pickers report items showing as already assigned. `lease held device=... age=...` "
                    "with ages approaching the 15-minute ceiling."
                ),
                causes=(
                    "Warehouse wifi dead zones dropping devices mid-pick",
                    "A device battery failure leaving the lease to expire naturally",
                ),
                detection=(
                    "`bin/warehouse leases --site <site> --older-than 5m` lists held leases with their "
                    "device ids. A cluster of the same device is a hardware problem; a cluster of the "
                    "same zone is a network one."
                ),
                fix=(
                    "Force-release the specific leases (`bin/warehouse release-lease --id <id>`) after "
                    "confirming with the floor supervisor that the pick did not physically happen."
                ),
                antipattern=(
                    "Do not shorten the lease globally to clear stuck picks faster. A short lease "
                    "releases items a picker is physically holding, and two pickers then converge on "
                    "one bin."
                ),
                escalation="Site supervisor first, Fulfilment on-call if the pattern is site-wide.",
            ),
        ),
    ),
    Service(
        name="shipping-service",
        domain="fulfilment",
        team="Fulfilment",
        language="Go 1.24",
        purpose=(
            "Rates, books, and tracks shipments across a dozen carriers. Rating is on the "
            "checkout critical path; booking and tracking are asynchronous."
        ),
        datastore="Postgres `shipping` cluster; carrier rate cache in Redis",
        dependencies=("carrier APIs (12)", "warehouse-service", "label-service", "notification-service"),
        hazards=(
            "Carrier APIs vary from excellent to appalling. The rate cache exists mostly to "
            "insulate the funnel from the worst of them.",
            "Tracking webhooks arrive out of order and sometimes duplicated. Status is "
            "computed from the highest-ranked event seen, never from the latest received.",
        ),
        alerts=(
            "`carrier_rate_timeout_rate > 5%` per carrier (ticket)",
            "`shipment_stuck_in_booking > 100` (page)",
        ),
        slo="99.9% rating availability, p95 rate under 800ms",
        faults=(
            Fault(
                code="SHIP-4401",
                title="Carrier rating timeout degrading checkout options",
                severity="high",
                symptom=(
                    "`carrier rate timeout carrier=... after 800ms` and shoppers see fewer shipping "
                    "options than expected — not an error, just silently reduced choice."
                ),
                causes=(
                    "Carrier-side degradation, which is routine for two of the twelve",
                    "A rate request for an unusual destination falling outside the cached corridors",
                ),
                detection=(
                    "`shipping_rate_latency` split by carrier. One carrier's tail rising while others "
                    "are flat confirms it is theirs, not ours."
                ),
                fix=(
                    "Serve the cached corridor rate for that carrier (`shipping.rate.fallback_cache=true`) "
                    "or drop the carrier from the option set temporarily "
                    "(`bin/shipping disable-carrier --id <carrier> --ttl 1h`)."
                ),
                antipattern=(
                    "Do not raise the per-carrier timeout above 800ms. Rating is parallel across "
                    "carriers but the funnel waits for the slowest; one bad carrier then sets the "
                    "checkout latency for everyone."
                ),
                escalation="Fulfilment on-call. Carrier escalation via the partner manager.",
                related=("SHIP-4430",),
            ),
            Fault(
                code="SHIP-4430",
                title="Out-of-order tracking webhooks regressing shipment status",
                severity="medium",
                symptom=(
                    "A shipment shows 'in transit' after having shown 'delivered'. Customers receive "
                    "a contradictory notification."
                ),
                causes=(
                    "A carrier replaying webhooks after their own outage, delivering old events last",
                    "A duplicate webhook delivered to two pods concurrently, racing on the status write",
                ),
                detection=(
                    "`bin/shipping events --shipment <id>` prints every received event with both its "
                    "carrier timestamp and our receipt time. A large gap between the two is a replay."
                ),
                fix=(
                    "Recompute status from the full event set: `bin/shipping recompute --shipment <id>`. "
                    "Status is a pure function of the event set, so this is always safe."
                ),
                antipattern=(
                    "Do not suppress the notification by disabling status-change emails. The customer "
                    "then loses genuine delivery notifications too; fix the ordering, which is what the "
                    "event ranking in ADR-0037 is for."
                ),
                escalation="Fulfilment on-call during business hours.",
            ),
        ),
    ),
    Service(
        name="returns-service",
        domain="fulfilment",
        team="Fulfilment",
        language="Go 1.24",
        purpose=(
            "Manages the return lifecycle from request through receipt, inspection, and "
            "refund authorisation. It authorises refunds but never issues them — that "
            "separation is deliberate."
        ),
        datastore="Postgres `returns` cluster",
        dependencies=("order-service", "refund-service", "warehouse-service", "label-service"),
        hazards=(
            "A return can be refunded on receipt or after inspection depending on value "
            "threshold and customer tier. The threshold lives in config and changes often.",
            "Return labels are pre-paid at generation. An abandoned return still costs us "
            "the label if the customer posts it months later.",
        ),
        alerts=(
            "`return_awaiting_inspection > 2000` (ticket)",
            "`refund_authorised_without_receipt > 0` (page)",
        ),
        slo="Return processed within 5 business days of receipt",
        faults=(
            Fault(
                code="RET-8801",
                title="Refund authorised without physical receipt",
                severity="critical",
                symptom="`refund authorised return=... receipt=absent` — money out with no goods in.",
                causes=(
                    "The auto-refund threshold raised in config without the receipt check being re-evaluated",
                    "A warehouse receipt event lost, so the return was auto-approved on timeout",
                ),
                detection=(
                    "`bin/returns trace --id <return>` shows the state machine transitions. An "
                    "`auto_approved_on_timeout` transition without a receipt event is the fault."
                ),
                fix=(
                    "Hold further auto-approvals (`returns.auto_approve=false`), audit the affected "
                    "window with `bin/returns audit --since <ts>`, and pursue recovery through the "
                    "standard non-return process for genuine losses."
                ),
                antipattern=(
                    "Do not raise the auto-approve threshold to reduce support load without a "
                    "corresponding change to the receipt requirement. The two settings are coupled and "
                    "the coupling is not enforced in code — ADR-0044 proposes fixing that."
                ),
                escalation="Page Fulfilment and Payments; notify Finance.",
                related=("REF-2201",),
            ),
        ),
    ),
    Service(
        name="supplier-sync",
        domain="fulfilment",
        team="Fulfilment",
        language="Python 3.12",
        purpose=(
            "Ingests supplier catalogue and stock feeds — CSV, EDI, and three bespoke JSON "
            "dialects — and normalises them into catalog and inventory updates. It is the "
            "messiest input boundary in the platform."
        ),
        datastore="Postgres `supplier` cluster; raw feeds retained in object storage for 90 days",
        dependencies=("catalog-service", "inventory-service", "event-bus"),
        hazards=(
            "Feeds arrive on the supplier's schedule, not ours, and several suppliers send a "
            "full snapshot rather than a delta. A snapshot with missing rows reads as "
            "deletions.",
            "The bulk import holds long database transactions — this is the known trigger "
            "for INV-3007 in inventory-service.",
        ),
        alerts=(
            "`feed_row_delta_ratio > 0.3` (page — a third of a catalogue changing at once is almost always a bad feed)",
            "`feed_parse_failures > 0` (ticket)",
        ),
        slo="Feeds processed within 2h of arrival",
        faults=(
            Fault(
                code="SUPP-7702",
                title="Truncated supplier snapshot read as mass deletion",
                severity="critical",
                symptom=(
                    "`row delta ratio=0.62 supplier=...` and a large number of SKUs going non-sellable "
                    "in one import."
                ),
                causes=(
                    "The supplier's export failed partway and shipped a truncated file",
                    "A transfer interrupted, leaving a partial file that still parsed cleanly",
                ),
                detection=(
                    "Compare the row count against the trailing average: "
                    "`bin/supplier feed-stats --supplier <id> --last 10`. A truncation is obvious as a "
                    "sharp count drop with no corresponding business reason."
                ),
                fix=(
                    "The delta guard should have blocked this. If it did not, roll back the import "
                    "(`bin/supplier rollback --import <id>`) — raw feeds are retained precisely so this "
                    "is possible — and re-request the feed from the supplier."
                ),
                antipattern=(
                    "Do not lower the delta guard threshold because it 'keeps blocking legitimate "
                    "imports'. Every time we have done that, the next truncated feed went through. If "
                    "a supplier genuinely changes a third of their catalogue, approve that import "
                    "explicitly."
                ),
                escalation="Page Fulfilment; notify Discovery since catalog is affected.",
                related=("CAT-4410", "INV-3007"),
            ),
            Fault(
                code="SUPP-7740",
                title="Bulk import starving the inventory connection pool",
                severity="high",
                symptom=(
                    "inventory-service reports INV-3007 shortly after a supplier import begins outside "
                    "the scheduled window."
                ),
                causes=(
                    "An import triggered manually during business hours",
                    "A retried import from a failed overnight run spilling into the day",
                ),
                detection=(
                    "Correlate the import start time in supplier-sync with the first INV-3007. If the "
                    "import began within a few minutes prior, this is the cause rather than an "
                    "independent inventory problem."
                ),
                fix=(
                    "Pause the import (`bin/supplier pause --import <id>`); it resumes from its "
                    "checkpoint. Reschedule for the 02:00 UTC window."
                ),
                antipattern=(
                    "Do not raise the inventory pool size to accommodate imports. RB-INVENTORY-POOL "
                    "explains why at length: the database's own connection limit becomes the next "
                    "ceiling and the failure moves to every service sharing the cluster."
                ),
                escalation="Fulfilment on-call.",
                related=("INV-3007",),
            ),
        ),
    ),
    Service(
        name="label-service",
        domain="fulfilment",
        team="Fulfilment",
        language="Python 3.12",
        purpose=(
            "Generates carrier-compliant shipping and return labels as ZPL and PDF. Labels "
            "are immutable once generated because the tracking number is allocated from the "
            "carrier at generation time."
        ),
        datastore="Object storage for artefacts; Postgres `labels` for allocation records",
        dependencies=("shipping-service", "carrier APIs", "returns-service"),
        hazards=(
            "Every generated label consumes a tracking number from a finite carrier "
            "allocation. Regenerating labels burns numbers.",
            "ZPL rendering is carrier-specific and version-sensitive; a carrier firmware "
            "update at a site can invalidate previously fine templates.",
        ),
        alerts=(
            "`tracking_number_pool_remaining < 5000` per carrier (page)",
            "`label_regeneration_rate > 2%` (ticket)",
        ),
        slo="99.95% availability, label generated under 2s",
        faults=(
            Fault(
                code="LBL-2201",
                title="Tracking number pool near exhaustion",
                severity="high",
                symptom="`tracking pool low carrier=... remaining=...` trending toward zero.",
                causes=(
                    "Elevated regeneration burning numbers faster than the pool refills",
                    "A carrier failing to honour the scheduled pool top-up request",
                ),
                detection=(
                    "Compare allocation rate against shipment count. Allocations materially exceeding "
                    "shipments means regeneration, not growth."
                ),
                fix=(
                    "Request an emergency allocation from the carrier and suppress regeneration "
                    "(`label.allow_regeneration=false`) until the pool recovers. Existing labels remain valid."
                ),
                antipattern=(
                    "Do not reuse tracking numbers from cancelled shipments. Carriers treat reuse as a "
                    "contract violation and it makes tracking genuinely ambiguous for the customer."
                ),
                escalation="Page Fulfilment; carrier allocation requests go through the partner manager.",
            ),
        ),
    ),
    Service(
        name="slot-service",
        domain="fulfilment",
        team="Fulfilment",
        language="Java 21",
        purpose=(
            "Manages delivery slot capacity and reservations for scheduled delivery markets. "
            "Capacity is finite per slot per postcode area, and overselling a slot is a "
            "physical problem, not a virtual one."
        ),
        datastore="Postgres `slots` cluster with row-level locking on capacity decrements",
        dependencies=("checkout-service", "shipping-service", "warehouse-service"),
        hazards=(
            "Slot capacity is decremented at checkout and released on cancellation. A lost "
            "release leaks capacity permanently until the nightly reconciliation.",
            "Capacity is planned weekly by the operations team. A planning import error "
            "oversells before any technical failure occurs.",
        ),
        alerts=(
            "`slot_oversold_count > 0` (page)",
            "`slot_capacity_leak > 50` after nightly reconciliation (ticket)",
        ),
        slo="99.9% availability, no oversold slots",
        faults=(
            Fault(
                code="SLOT-6601",
                title="Slot oversold",
                severity="critical",
                symptom="`slot oversold slot=... capacity=... reserved=...` with reservations exceeding capacity.",
                causes=(
                    "A capacity reduction applied after reservations were already taken",
                    "Concurrent decrements that bypassed the row lock via a new code path",
                ),
                detection=(
                    "`bin/slot history --id <slot>` shows capacity changes and reservations "
                    "interleaved. A capacity change after the reservations is an operations issue, "
                    "not a concurrency bug."
                ),
                fix=(
                    "Contact the affected customers through the operations team to rebook — this is a "
                    "physical capacity problem and cannot be fixed in software. Freeze further "
                    "reservations for that slot with `bin/slot freeze --id <slot>`."
                ),
                antipattern=(
                    "Do not silently move customers to an adjacent slot. Scheduled delivery customers "
                    "have arranged to be present; an unannounced change is worse than a call asking them "
                    "to rebook."
                ),
                escalation="Page Fulfilment and notify the operations duty manager immediately.",
            ),
        ),
    ),
)
