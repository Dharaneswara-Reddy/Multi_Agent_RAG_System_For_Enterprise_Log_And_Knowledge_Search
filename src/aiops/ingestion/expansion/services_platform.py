"""Meridian services: identity, growth, discovery, platform, and data domains.

The platform services here are the ones the rest of the estate depends on, so
their faults deliberately appear as *causes* in other services' runbooks. That
cross-referencing is what gives the multi-hop evaluation questions a real path
to follow rather than a single lucky chunk.
"""

from __future__ import annotations

from aiops.ingestion.expansion.specs import Fault, Service

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

IDENTITY: tuple[Service, ...] = (
    Service(
        name="session-service",
        domain="identity",
        team="Platform Security",
        language="Rust",
        purpose=(
            "Issues and tracks browser sessions, distinct from the JWTs auth-service "
            "handles. A session survives token refresh and is what we revoke when a user "
            "signs out everywhere."
        ),
        datastore="Redis cluster (sessions) with a Postgres audit trail of revocations",
        dependencies=("auth-service", "account-service", "audit-log-service"),
        hazards=(
            "Session revocation is eventually consistent across regions. A revoked session "
            "may remain usable in a remote region for up to 10 seconds.",
            "Session ids are stored in a cookie with a 30-day lifetime, far longer than the "
            "15-minute access token, so session compromise is the higher-value attack.",
        ),
        alerts=(
            "`session_revocation_propagation_seconds > 30` (page)",
            "`session_fixation_attempt_rate > 0` (page)",
        ),
        slo="99.99% availability, p99 lookup under 15ms",
        faults=(
            Fault(
                code="SESS-1101",
                title="Revocation not propagating across regions",
                severity="critical",
                symptom=(
                    "A user reports still being signed in on a device after 'sign out everywhere'. "
                    "`revocation pending region=... age=...` well beyond the 10s expectation."
                ),
                causes=(
                    "Cross-region Redis replication stalled",
                    "The revocation broadcast topic backed up on event-bus",
                ),
                detection=(
                    "`bin/session revocation-status --id <session>` prints per-region acknowledgement. "
                    "One region missing points at replication; all regions missing points at the broadcast."
                ),
                fix=(
                    "Force a direct revocation in each region "
                    "(`bin/session revoke --id <session> --all-regions --direct`), which bypasses the "
                    "broadcast. Then resolve the underlying replication or bus issue."
                ),
                antipattern=(
                    "Do not shorten the session cookie lifetime as a mitigation. It signs out every "
                    "healthy user without affecting the compromised session you were trying to revoke."
                ),
                escalation=(
                    "Page Platform Security. If the revocation was requested in response to a "
                    "suspected account compromise, treat it as a security incident."
                ),
                related=("BUS-1201",),
            ),
            Fault(
                code="SESS-1140",
                title="Session fixation attempt detected",
                severity="high",
                symptom=(
                    "`session id supplied by client did not originate here` — a session identifier "
                    "presented that we never issued."
                ),
                causes=(
                    "An attacker attempting to plant a known session id before a victim authenticates",
                    "A misbehaving integration replaying captured cookies in a test environment against production",
                ),
                detection=(
                    "Group by source address and user agent. A single source presenting many "
                    "unissued ids is an attack; a single id from many sources is a leaked cookie."
                ),
                fix=(
                    "The service already rejects unissued ids and rotates the session id on privilege "
                    "change, so the attempt fails by design. Rate-limit the source and preserve the "
                    "logs for Security review."
                ),
                antipattern=(
                    "Do not accept a client-supplied session id under any circumstance, including for "
                    "'testing convenience'. Session fixation is precisely what that enables."
                ),
                escalation="Platform Security, same day. Page if a hit rate above zero is observed.",
                related=("RATE-5501",),
            ),
        ),
    ),
    Service(
        name="account-service",
        domain="identity",
        team="Platform Security",
        language="Go 1.24",
        purpose=(
            "System of record for customer and seller accounts: profile, contact details, "
            "tier, and the relationships between them. It is the source of personal data "
            "and therefore the centre of every data-subject request."
        ),
        datastore="Postgres `accounts` cluster with column-level encryption on PII fields",
        dependencies=("auth-service", "consent-service", "audit-log-service"),
        hazards=(
            "PII columns are encrypted with keys held in secrets-broker. A key rotation "
            "failure makes existing rows unreadable rather than throwing at write time.",
            "Account deletion is a 30-day soft delete followed by hard erasure. The window "
            "is a legal requirement and cannot be shortened for convenience.",
        ),
        alerts=(
            "`pii_decrypt_failures > 0` (page)",
            "`erasure_backlog_past_due > 0` (page — a missed erasure deadline is a regulatory breach)",
        ),
        slo="99.99% availability, erasure completed within the statutory window",
        faults=(
            Fault(
                code="ACC-3301",
                title="PII decryption failures after key rotation",
                severity="critical",
                symptom=(
                    "`decrypt failed key_version=... field=email` on reads of older rows, while newly "
                    "written rows read fine."
                ),
                causes=(
                    "A rotation that provisioned the new key but retired the old one before re-encryption completed",
                    "secrets-broker unable to serve the historical key version (see SEC-9002)",
                ),
                detection=(
                    "`bin/account key-audit` reports how many rows exist per key version and whether "
                    "each version is still retrievable. A version with rows but no key is the fault."
                ),
                fix=(
                    "Restore the retired key version from the secrets-broker archive, then complete "
                    "re-encryption with `bin/account reencrypt --from-version <n>` before retiring it again."
                ),
                antipattern=(
                    "Never null out unreadable PII columns to clear the errors. That is irreversible "
                    "destruction of customer data and converts a recoverable incident into a permanent one."
                ),
                escalation="Page Platform Security. Notify the Data Protection Officer if any data proves unrecoverable.",
                related=("SEC-9002",),
            ),
            Fault(
                code="ACC-3340",
                title="Erasure backlog past the statutory deadline",
                severity="critical",
                symptom="`erasure past due account=... due=... age_days=...` from the compliance sweep.",
                causes=(
                    "The erasure worker blocked on a downstream service that will not confirm deletion",
                    "An account with an open financial obligation, which legitimately blocks erasure but must be recorded as such",
                ),
                detection=(
                    "`bin/account erasure-status --id <account>` lists each downstream system and its "
                    "confirmation state. The unconfirmed one is the blocker."
                ),
                fix=(
                    "Resolve the specific downstream confirmation. Where erasure is legitimately "
                    "blocked by a retention obligation — an open invoice, a tax record — record the "
                    "lawful basis with `bin/account erasure-hold --id <account> --basis <ref>`."
                ),
                antipattern=(
                    "Do not mark an erasure complete without downstream confirmation. The record "
                    "persists somewhere, and the next audit finds it."
                ),
                escalation="Page Platform Security; the Data Protection Officer must be informed within 24h.",
            ),
        ),
    ),
    Service(
        name="consent-service",
        domain="identity",
        team="Platform Security",
        language="Python 3.12",
        purpose=(
            "Records marketing and tracking consent per user per purpose, with full history. "
            "Every downstream that touches personal data is expected to check here first."
        ),
        datastore="Postgres `consent` cluster, append-only with a current-state view",
        dependencies=("account-service", "audit-log-service", "event-bus"),
        hazards=(
            "Consent is checked at send time, not at list-build time. A list built yesterday "
            "may contain users who withdrew consent since.",
            "Absence of a consent record means *no consent*, never implied consent. A lookup "
            "failure must therefore fail closed.",
        ),
        alerts=(
            "`consent_lookup_failure_rate > 0.1%` (page — failures mean sends are being suppressed)",
            "`send_without_consent_check > 0` (page)",
        ),
        slo="99.99% availability, p99 lookup under 20ms",
        faults=(
            Fault(
                code="CNS-4401",
                title="Consent lookup failing closed and suppressing sends",
                severity="high",
                symptom=(
                    "Campaign volumes fall sharply with no send errors. `consent lookup failed, "
                    "suppressing send` at volume in email-service."
                ),
                causes=(
                    "consent-service degraded or its database unreachable",
                    "A network policy change blocking the caller",
                ),
                detection=(
                    "Compare `consent_lookup_failure_rate` against campaign volume. Suppression is "
                    "silent by design, so the send-side metric is the one that moves visibly."
                ),
                fix=(
                    "Restore the service. Suppressed sends are not lost — campaign-service requeues "
                    "them — so there is no data recovery step, only the availability fix."
                ),
                antipattern=(
                    "Never add a fail-open path so campaigns keep flowing during an outage. Sending "
                    "marketing without a verified consent check is a regulatory breach per message; "
                    "a delayed campaign is not."
                ),
                escalation="Page Platform Security; notify Growth so campaigns can be rescheduled.",
                related=("EMAIL-2201",),
            ),
        ),
    ),
    Service(
        name="mfa-service",
        domain="identity",
        team="Platform Security",
        language="Rust",
        purpose=(
            "Second-factor challenges: TOTP, WebAuthn, and SMS fallback. SMS is offered "
            "reluctantly and only where the stronger factors are unavailable to the user."
        ),
        datastore="Postgres `mfa` cluster; TOTP secrets encrypted at rest",
        dependencies=("auth-service", "sms-gateway", "account-service"),
        hazards=(
            "TOTP validation depends on node clock accuracy — the same class of problem as "
            "AUTH-1015, with a wider default window that hides small drift until it is large.",
            "SMS fallback depends on a third-party gateway and is the weakest factor; it is "
            "also the one attackers target through SIM swap.",
        ),
        alerts=(
            "`totp_validation_failure_rate > 5%` (page)",
            "`sms_fallback_usage_ratio > 20%` (ticket — the stronger factors may be broken)",
        ),
        slo="99.99% availability, p99 challenge under 100ms",
        faults=(
            Fault(
                code="MFA-5501",
                title="TOTP rejections from node clock drift",
                severity="high",
                symptom=(
                    "Users report correct codes being rejected. `totp rejected node=... drift_s=...` "
                    "concentrated on a subset of nodes."
                ),
                causes=(
                    "chronyd stopped or degraded on a node, exactly as in AUTH-1015",
                    "A node scheduled onto a host with a drifting hypervisor clock",
                ),
                detection=(
                    "The rejection log carries `node=`. If failures name one node, compare "
                    "`chronyc tracking` there against a healthy peer. TOTP tolerates ±1 step (30s), so "
                    "drift must exceed that before users notice — by which point it is substantial."
                ),
                fix=(
                    "`systemctl restart chronyd` on the drifted node and confirm the offset settles "
                    "under 50ms. Cordon the node if it drifts again."
                ),
                antipattern=(
                    "Do not widen the TOTP acceptance window. Every extra step materially lengthens "
                    "the window in which a phished code remains usable, which is the entire threat "
                    "TOTP exists to address."
                ),
                escalation="Page Platform Security.",
                related=("AUTH-1015",),
            ),
            Fault(
                code="MFA-5540",
                title="SMS fallback surge indicating a stronger-factor outage",
                severity="medium",
                symptom="`sms_fallback_usage_ratio` jumping well above its usual few percent.",
                causes=(
                    "WebAuthn broken by a browser or platform update",
                    "TOTP failing for the reasons in MFA-5501, pushing users to fall back",
                ),
                detection=(
                    "Split challenge outcomes by factor. A collapse in WebAuthn success with a "
                    "corresponding SMS rise identifies the broken factor precisely."
                ),
                fix=(
                    "Fix the underlying factor. SMS absorbing the load is the fallback working, but it "
                    "is the weakest factor and sustained reliance on it raises account-takeover risk."
                ),
                antipattern=(
                    "Do not disable SMS fallback to force users back to stronger factors while those "
                    "factors are broken. That locks people out of their accounts entirely."
                ),
                escalation="Platform Security on-call. Page if sustained above 50%.",
                related=("MFA-5501", "SMS-3301"),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Growth
# ---------------------------------------------------------------------------

GROWTH: tuple[Service, ...] = (
    Service(
        name="email-service",
        domain="growth",
        team="Growth",
        language="Python 3.12",
        purpose=(
            "Renders and delivers transactional and marketing email through two ESPs, with "
            "automatic failover between them. Transactional mail always takes priority over "
            "marketing when capacity is constrained."
        ),
        datastore="Postgres `email` cluster; render templates in object storage",
        dependencies=("consent-service", "notification-service", "ESP providers (2)"),
        hazards=(
            "Sender reputation is shared across both streams. A bad marketing campaign "
            "degrades transactional deliverability, which is the expensive failure.",
            "Failover between ESPs is automatic but the two have different suppression "
            "lists; a recipient suppressed at one may receive from the other.",
        ),
        alerts=(
            "`email_bounce_rate > 5%` (page — reputation risk)",
            "`transactional_queue_age_seconds > 300` (page)",
        ),
        slo="Transactional mail delivered within 60s p95",
        faults=(
            Fault(
                code="EMAIL-2201",
                title="Bounce rate spike threatening sender reputation",
                severity="critical",
                symptom="`bounce rate=0.14 campaign=...` — well above the 5% threshold ESPs act on.",
                causes=(
                    "A campaign sent to a stale list with many dead addresses",
                    "A list imported without validation, typically from an acquisition or event",
                ),
                detection=(
                    "Split bounces into hard and soft. A hard-bounce spike is a list-quality problem; "
                    "a soft-bounce spike is usually the receiving side or our reputation already sliding."
                ),
                fix=(
                    "Pause the campaign immediately (`bin/email pause-campaign --id <id>`). Transactional "
                    "mail continues; that separation is the priority queue working. Then validate the "
                    "list before any resend."
                ),
                antipattern=(
                    "Do not fail over to the second ESP to escape the bounces. The reputation damage "
                    "follows the sending domain, not the provider, and you will burn the second ESP too."
                ),
                escalation="Page Growth. If transactional deliverability is affected, this becomes a platform incident.",
                related=("CNS-4401",),
            ),
            Fault(
                code="EMAIL-2240",
                title="Transactional queue starved behind a marketing burst",
                severity="high",
                symptom=(
                    "Order confirmations arriving minutes late while a large campaign sends. "
                    "`transactional queue age=...` climbing."
                ),
                causes=(
                    "A campaign scheduled without a rate cap, saturating the shared ESP connection pool",
                    "Priority inversion from a marketing batch enqueued with transactional priority by mistake",
                ),
                detection=(
                    "`bin/email queue-stats` shows depth and age per priority class. If marketing "
                    "messages appear in the transactional class, that is the inversion."
                ),
                fix=(
                    "Throttle the campaign (`bin/email throttle --campaign <id> --rate 5000/min`). The "
                    "transactional queue drains within a minute or two once capacity is freed."
                ),
                antipattern=(
                    "Do not raise the overall send rate to clear both queues. The ESP applies its own "
                    "rate limit and will begin deferring, which delays transactional mail further."
                ),
                escalation="Growth on-call; page if transactional age exceeds 10 minutes.",
                related=("EMAIL-2201",),
            ),
        ),
    ),
    Service(
        name="sms-gateway",
        domain="growth",
        team="Growth",
        language="Go 1.24",
        purpose=(
            "Sends SMS through regional aggregators, handling per-country routing, sender "
            "id rules, and delivery receipts. Carries MFA codes, which makes it "
            "security-critical despite sitting in the growth domain."
        ),
        datastore="Postgres `sms` cluster",
        dependencies=("mfa-service", "notification-service", "aggregators (4)"),
        hazards=(
            "Per-country sender id rules differ and change without notice. A message that "
            "worked last month may be silently dropped by a carrier this month.",
            "MFA traffic and marketing traffic share aggregator capacity. MFA must always win.",
        ),
        alerts=(
            "`sms_delivery_receipt_missing_rate > 10%` per country (ticket)",
            "`mfa_sms_latency_p95 > 20s` (page)",
        ),
        slo="MFA codes delivered within 20s p95",
        faults=(
            Fault(
                code="SMS-3301",
                title="Silent drops after a carrier sender-id rule change",
                severity="high",
                symptom=(
                    "Delivery receipts stop arriving for one country while the aggregator reports "
                    "acceptance. Users in that country stop receiving MFA codes."
                ),
                causes=(
                    "A carrier newly requiring a registered sender id or short code",
                    "An aggregator silently changing routes to a carrier with different rules",
                ),
                detection=(
                    "Accepted-but-undelivered is the signature: compare `sms_accepted` against "
                    "`sms_delivered` by country. A gap in one country only points at that country's rules."
                ),
                fix=(
                    "Switch that country to a secondary aggregator "
                    "(`bin/sms route --country <cc> --aggregator <id>`) while sender-id registration is "
                    "completed with the carrier."
                ),
                antipattern=(
                    "Do not retry into the same route. The messages are being accepted and discarded, "
                    "so retries produce cost and no delivery — and for MFA they produce a queue of codes "
                    "that all arrive at once if the route later recovers."
                ),
                escalation="Page Growth and Platform Security together when MFA delivery is affected.",
                related=("MFA-5540",),
            ),
        ),
    ),
    Service(
        name="recommendation-service",
        domain="growth",
        team="Growth",
        language="Python 3.12",
        purpose=(
            "Serves product recommendations from a two-stage model: candidate retrieval "
            "then ranking. It is on the page-render path with a 120ms budget and degrades "
            "to editorial fallbacks rather than failing."
        ),
        datastore="Feature store on Redis; model artefacts in object storage",
        dependencies=("catalog-service", "metrics-store", "ranking-service"),
        hazards=(
            "Degrading to editorial fallbacks is invisible to monitoring that only watches "
            "error rates. Recommendation quality can collapse with a perfect health check.",
            "The candidate index is rebuilt nightly. A failed rebuild serves yesterday's "
            "catalogue, including discontinued products.",
        ),
        alerts=(
            "`recommendation_fallback_ratio > 10%` (page)",
            "`candidate_index_age_hours > 36` (page)",
        ),
        slo="99.9% availability, p99 under 120ms",
        faults=(
            Fault(
                code="REC-4401",
                title="Serving editorial fallbacks at scale",
                severity="high",
                symptom=(
                    "`fallback served reason=budget_exceeded` at volume. No errors, no alerts on "
                    "availability, and recommendation click-through quietly halving."
                ),
                causes=(
                    "Feature store latency pushing the two-stage pipeline past 120ms",
                    "A candidate index rebuild failure leaving an oversized or corrupt index",
                ),
                detection=(
                    "Split latency by stage — retrieval versus ranking — and check "
                    "`candidate_index_age_hours`. A stale index and a slow retrieval stage usually "
                    "travel together."
                ),
                fix=(
                    "Roll back to the last good candidate index (`bin/rec index-activate previous`) "
                    "and rerun the rebuild off-peak."
                ),
                antipattern=(
                    "Do not raise the 120ms budget. Recommendations sit on the page-render path and a "
                    "longer budget delays the whole page for every shopper, including the ones who "
                    "would have been served a perfectly good fallback."
                ),
                escalation="Growth on-call. Page if the fallback ratio exceeds 50%.",
                related=("REC-4430",),
            ),
            Fault(
                code="REC-4430",
                title="Recommending discontinued products",
                severity="medium",
                symptom="Shoppers clicking recommendations and landing on unavailable products.",
                causes=(
                    "A stale candidate index built before a catalogue purge",
                    "catalog-service fanout lag (CAT-4455) meaning availability changes have not propagated",
                ),
                detection=(
                    "Sample recommended SKUs against current catalogue availability: "
                    "`bin/rec audit --sample 1000`. A high unavailable ratio confirms staleness."
                ),
                fix=(
                    "Trigger an index rebuild. If catalogue fanout is lagging, fix that first — "
                    "rebuilding against a lagging catalogue reproduces the same problem."
                ),
                antipattern=(
                    "Do not filter unavailable products at render time as the permanent fix. It hides "
                    "the staleness, and the candidate set silently shrinks until recommendations become "
                    "repetitive."
                ),
                escalation="Growth on-call during business hours.",
                related=("CAT-4455",),
            ),
        ),
    ),
    Service(
        name="review-service",
        domain="growth",
        team="Growth",
        language="Java 21",
        purpose=(
            "Collects, moderates, and serves product reviews. Moderation is a mix of "
            "automated classification and a human queue, and the automated tier is tuned to "
            "favour escalation over silent rejection."
        ),
        datastore="Postgres `reviews` cluster; full-text search delegated to search-service",
        dependencies=("account-service", "order-service", "search-service"),
        hazards=(
            "Only verified purchasers may review, which requires an order lookup. An "
            "order-service outage blocks new reviews entirely.",
            "The moderation queue is human-staffed during business hours only. A weekend "
            "spike lands on Monday.",
        ),
        alerts=(
            "`moderation_queue_depth > 5000` (ticket)",
            "`review_spam_classifier_confidence_mean < 0.6` (ticket)",
        ),
        slo="99.5% read availability; reviews published within 24h of submission",
        faults=(
            Fault(
                code="REV-5501",
                title="Coordinated review spam bypassing the classifier",
                severity="medium",
                symptom=(
                    "A burst of similar reviews across related products, each individually scoring "
                    "below the spam threshold."
                ),
                causes=(
                    "A campaign using paraphrased text specifically to stay under the per-review threshold",
                    "Compromised accounts with genuine purchase history, which defeats the verified-purchaser check",
                ),
                detection=(
                    "Per-review scoring will not catch this; cluster by submission window and account "
                    "age instead. `bin/review cluster --window 6h` surfaces coordinated bursts."
                ),
                fix=(
                    "Quarantine the cluster to the human queue (`bin/review quarantine --cluster <id>`) "
                    "rather than auto-rejecting. False positives here remove legitimate customer "
                    "feedback, which is its own harm."
                ),
                antipattern=(
                    "Do not lower the per-review spam threshold globally. It rejects legitimate short "
                    "reviews at a far higher rate than it catches coordinated campaigns, which are "
                    "specifically written to sit under whatever threshold you set."
                ),
                escalation="Growth on-call during business hours.",
            ),
        ),
    ),
    Service(
        name="loyalty-service",
        domain="growth",
        team="Growth",
        language="Java 21",
        purpose=(
            "Points accrual and redemption, tier calculation, and member benefits. Points "
            "are a liability on the balance sheet, so accrual correctness is a finance "
            "concern rather than merely a product one."
        ),
        datastore="Postgres `loyalty` cluster, append-only accrual ledger",
        dependencies=("order-service", "ledger-service", "promotion-service"),
        hazards=(
            "Points have monetary value and appear as a liability. An over-accrual is a "
            "restatement, not a bug fix.",
            "Tier calculation runs nightly on a rolling 12-month window, so a data problem "
            "silently changes people's tiers overnight.",
        ),
        alerts=(
            "`points_accrual_anomaly_sigma > 3` (page)",
            "`tier_downgrade_batch_size > 1000` (page — mass downgrade is almost always a bug)",
        ),
        slo="99.9% availability; accrual visible within 5 minutes of order completion",
        faults=(
            Fault(
                code="LOY-6603",
                title="Over-accrual from promotion interaction",
                severity="critical",
                symptom=(
                    "`accrual anomaly sigma=4.2` with individual orders accruing far more points than "
                    "their value supports."
                ),
                causes=(
                    "Points accrued on the pre-discount basket value while the customer paid the discounted price",
                    "A promotion granting bonus points stacking with a tier multiplier (see PROMO-5502)",
                ),
                detection=(
                    "`bin/loyalty explain --order <id>` prints the accrual base and every multiplier. "
                    "An accrual base above the amount actually charged is the bug."
                ),
                fix=(
                    "Suspend the offending promotion's points component, then decide with Finance "
                    "whether to claw back. Clawback of already-visible points is a customer-trust "
                    "decision, not an engineering one."
                ),
                antipattern=(
                    "Do not silently remove accrued points from member balances. Members see their "
                    "balance; a quiet reduction generates more support cost and reputational damage "
                    "than the liability is worth."
                ),
                escalation="Page Growth; notify Finance same-day — this changes a balance-sheet liability.",
                related=("PROMO-5502",),
            ),
            Fault(
                code="LOY-6640",
                title="Mass tier downgrade after a nightly recalculation",
                severity="high",
                symptom="Thousands of members downgraded in one batch, with support contacts spiking the next morning.",
                causes=(
                    "An order-history backfill gap making the rolling 12-month window look empty",
                    "A timezone error shifting the window boundary by a day at a month end",
                ),
                detection=(
                    "`bin/loyalty tier-explain --member <id>` prints the qualifying spend it computed "
                    "and the window it used. Compare against the member's actual order history."
                ),
                fix=(
                    "Restore tiers from the previous night's snapshot "
                    "(`bin/loyalty tier-restore --date <date>`) and rerun once the data gap is fixed. "
                    "Snapshots are retained for 30 days precisely for this."
                ),
                antipattern=(
                    "Do not let the downgrade stand and handle it through support. Tier benefits are "
                    "consumed immediately — free delivery, early access — and reinstating them "
                    "retroactively is far harder than restoring the snapshot."
                ),
                escalation="Page Growth; brief Customer Support before the morning contact spike.",
            ),
        ),
    ),
    Service(
        name="campaign-service",
        domain="growth",
        team="Growth",
        language="Python 3.12",
        purpose=(
            "Builds audiences, schedules campaigns, and hands messages to email-service and "
            "sms-gateway. Audience building is a heavy analytical query against the data "
            "warehouse, not an operational one."
        ),
        datastore="Postgres `campaigns` cluster; audiences materialised from warehouse-etl output",
        dependencies=("warehouse-etl", "consent-service", "email-service", "sms-gateway"),
        hazards=(
            "Audiences are materialised at build time but consent is checked at send time, "
            "so audience size and delivered volume legitimately differ.",
            "A campaign scheduled against a stale audience can target customers on facts "
            "that are no longer true — a churn-winback message to an active customer, for example.",
        ),
        alerts=(
            "`audience_staleness_hours > 48` (ticket)",
            "`campaign_send_rate` exceeding the configured cap (page)",
        ),
        slo="Campaigns start within 5 minutes of their scheduled time",
        faults=(
            Fault(
                code="CMP-8801",
                title="Campaign sent against a stale audience",
                severity="medium",
                symptom=(
                    "Customers receiving irrelevant or contradictory messages — a winback offer to "
                    "someone who ordered yesterday."
                ),
                causes=(
                    "warehouse-etl failing so the audience was never refreshed (see ETL-4401)",
                    "A campaign scheduled far in advance against an audience built at authoring time",
                ),
                detection=(
                    "`bin/campaign audience-info --id <campaign>` prints the audience build timestamp. "
                    "Compare it against the send time."
                ),
                fix=(
                    "Pause the campaign, rebuild the audience, and resume. The staleness guard should "
                    "have blocked the send at 48 hours; if it did not, check whether the campaign "
                    "carries a staleness override."
                ),
                antipattern=(
                    "Do not disable the staleness guard for 'time-sensitive' campaigns. A time-sensitive "
                    "campaign against a two-week-old audience is precisely the case that generates "
                    "complaints."
                ),
                escalation="Growth on-call during business hours.",
                related=("ETL-4401",),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

DISCOVERY: tuple[Service, ...] = (
    Service(
        name="indexer-service",
        domain="discovery",
        team="Discovery",
        language="Java 21",
        purpose=(
            "Consumes catalogue change events and applies incremental updates to the search "
            "index. Distinct from search-service, which only reads — this is the write path."
        ),
        datastore="Writes Lucene segments to shared storage; checkpoint offsets in Postgres",
        dependencies=("catalog-service", "event-bus", "search-service"),
        hazards=(
            "Checkpoint offsets are committed after segment flush. A crash between the two "
            "reprocesses events, which is safe, but a checkpoint written early loses updates silently.",
            "Segment merges are I/O heavy and compete with search reads on shared storage.",
        ),
        alerts=(
            "`index_lag_events > 50000` (page)",
            "`segment_merge_duration_minutes > 30` (ticket)",
        ),
        slo="Catalogue changes searchable within 5 minutes p95",
        faults=(
            Fault(
                code="IDX-7701",
                title="Index lag from event backlog",
                severity="high",
                symptom=(
                    "New or changed products not appearing in search. `index lag events=...` climbing "
                    "steadily."
                ),
                causes=(
                    "A large catalogue publish fanout (CAT-4455) producing more events than the indexer can absorb",
                    "Segment merge contention slowing the write path",
                ),
                detection=(
                    "Compare consumption rate against production rate on the catalogue events topic. "
                    "If production is spiking, the cause is upstream; if consumption dropped, it is here."
                ),
                fix=(
                    "Scale indexer replicas up to the partition count — beyond that adds nothing since "
                    "parallelism is bounded by partitions. Defer scheduled merges with "
                    "`indexer.merge.pause=true` until the backlog drains."
                ),
                antipattern=(
                    "Do not skip ahead by advancing the checkpoint past the backlog. Those catalogue "
                    "changes are then never applied and the index diverges permanently from the "
                    "catalogue with no error anywhere."
                ),
                escalation="Discovery on-call; page if lag exceeds 30 minutes of changes.",
                related=("CAT-4455", "BUS-1201"),
            ),
            Fault(
                code="IDX-7730",
                title="Checkpoint committed before segment flush",
                severity="critical",
                symptom=(
                    "Search results missing changes that the indexer logged as applied, with no lag "
                    "and no errors."
                ),
                causes=(
                    "A crash between checkpoint commit and flush under a code path that reordered the two",
                    "Shared storage acknowledging a write that did not durably land",
                ),
                detection=(
                    "`bin/indexer verify --since <checkpoint>` replays events against the index and "
                    "reports divergence. This is the only way to detect it — nothing else surfaces an error."
                ),
                fix=(
                    "Rewind the checkpoint to the last verified position and reprocess. Reprocessing is "
                    "idempotent, so over-rewinding is safe and under-rewinding is not — rewind further "
                    "than you think you need."
                ),
                antipattern=(
                    "Do not rely on the absence of lag as evidence of correctness. This fault presents "
                    "as a perfectly healthy indexer; only verification finds it."
                ),
                escalation="Page Discovery. A full reindex may be required if divergence is widespread.",
            ),
        ),
    ),
    Service(
        name="suggest-service",
        domain="discovery",
        team="Discovery",
        language="Go 1.24",
        purpose=(
            "Type-ahead suggestions from a prefix trie rebuilt hourly from query logs and "
            "the catalogue. Extremely latency-sensitive: it renders on every keystroke."
        ),
        datastore="In-memory trie per pod, rebuilt from object storage artefacts",
        dependencies=("search-service", "catalog-service", "metrics-store"),
        hazards=(
            "Suggestions derive from real user queries, so offensive or embarrassing queries "
            "can surface as suggestions. The blocklist is a permanent operational obligation.",
            "The trie is per-pod in memory. A rebuild is a rolling event and pods disagree during it.",
        ),
        alerts=(
            "`suggest_latency_p99 > 30ms` (page)",
            "`blocklist_bypass_detected > 0` (page)",
        ),
        slo="99.9% availability, p99 under 30ms",
        faults=(
            Fault(
                code="SUG-2201",
                title="Offensive query surfacing as a suggestion",
                severity="high",
                symptom="A blocked or offensive term appearing in type-ahead, usually reported externally.",
                causes=(
                    "A term reaching the frequency threshold before the blocklist was updated",
                    "A variant spelling or unicode homoglyph bypassing exact-match blocklist entries",
                ),
                detection=(
                    "`bin/suggest trace --term <term>` shows the frequency and which blocklist rules "
                    "were evaluated. A homoglyph bypass shows the term passing every rule."
                ),
                fix=(
                    "Add the term and its normalised form to the blocklist, then force a trie rebuild "
                    "(`bin/suggest rebuild --now`) rather than waiting for the hourly cycle."
                ),
                antipattern=(
                    "Do not raise the frequency threshold to reduce recurrence. It delays every "
                    "legitimate new suggestion — seasonal and launch terms especially — while a "
                    "coordinated push still clears any threshold you choose."
                ),
                escalation="Page Discovery. Notify Communications if the term surfaced publicly.",
            ),
        ),
    ),
    Service(
        name="ranking-service",
        domain="discovery",
        team="Discovery",
        language="Python 3.12",
        purpose=(
            "Re-ranks search and recommendation candidates with a learned model. It is a "
            "pure function of features and candidates, which makes it unusually easy to "
            "replay and debug."
        ),
        datastore="Stateless; model artefacts in object storage",
        dependencies=("search-service", "recommendation-service", "metrics-store"),
        hazards=(
            "Model rollouts are gradual by traffic percentage. A bad model affects a slice "
            "of users, which makes it harder to notice in aggregate metrics.",
            "Feature drift degrades ranking quality without any error being raised.",
        ),
        alerts=(
            "`ranking_ndcg_online < 0.72` (page)",
            "`feature_null_rate > 2%` (ticket)",
        ),
        slo="99.9% availability, p99 under 40ms",
        faults=(
            Fault(
                code="RNK-3301",
                title="Ranking quality collapse from feature nulls",
                severity="high",
                symptom=(
                    "Online NDCG dropping with no latency change and no errors. "
                    "`feature_null_rate=0.18` on features that are normally always present."
                ),
                causes=(
                    "A feature renamed upstream in metrics-store so the model reads null",
                    "A feature pipeline failure leaving values stale then absent",
                ),
                detection=(
                    "`bin/ranking feature-report` lists null rates per feature against their baseline. "
                    "A single feature at 100% null names the broken pipeline directly."
                ),
                fix=(
                    "Restore the feature pipeline. If that will take time, roll back to a model version "
                    "that does not depend on the missing feature — `bin/ranking model-activate <version>` "
                    "lists each version's feature dependencies."
                ),
                antipattern=(
                    "Do not impute missing features with zeros to keep the model serving. Zero is a "
                    "meaningful value for most of our features, so imputation produces confidently wrong "
                    "rankings rather than obviously degraded ones."
                ),
                escalation="Discovery on-call; page if NDCG falls more than 10% below baseline.",
                related=("MET-6601",),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------

PLATFORM: tuple[Service, ...] = (
    Service(
        name="config-service",
        domain="platform",
        team="Platform",
        language="Go 1.24",
        purpose=(
            "Distributes runtime configuration to every service with change broadcast and "
            "versioned rollback. It is a tier-1 dependency: when it is down, nothing can "
            "change, which is sometimes exactly the wrong state to be stuck in."
        ),
        datastore="etcd cluster (3 nodes) with a Postgres change-history mirror",
        dependencies=("etcd", "audit-log-service"),
        hazards=(
            "Clients cache the last known good configuration and continue on failure. This "
            "is correct, but means a config-service outage is invisible until someone tries "
            "to change something during an incident.",
            "Broadcast is best-effort fan-out, not guaranteed delivery. Partial application "
            "is a normal failure mode (see PROMO-5530).",
        ),
        alerts=(
            "`config_broadcast_ack_ratio < 0.98` (page)",
            "`etcd_leader_changes > 3 in 10m` (page)",
        ),
        slo="99.99% availability, change visible within 30s p99",
        faults=(
            Fault(
                code="CFG-1120",
                title="Configuration broadcast partially applied",
                severity="high",
                symptom=(
                    "`broadcast ack ratio=0.71 key=...` — some pods on the new value, some on the old, "
                    "with behaviour differing between them."
                ),
                causes=(
                    "Pods restarting during the broadcast and missing it",
                    "A network partition isolating a subset of clients from config-service",
                ),
                detection=(
                    "`bin/config ack-report --key <key>` lists which clients acknowledged. Group the "
                    "unacknowledged by node — a single node points at networking, a scattered set at restarts."
                ),
                fix=(
                    "Re-broadcast (`bin/config rebroadcast --key <key>`), which is idempotent. If "
                    "acknowledgement remains incomplete, restart the affected pods — they read "
                    "authoritative state from etcd at boot."
                ),
                antipattern=(
                    "Do not assume a config change applied because the write succeeded. The write and "
                    "the fan-out are separate; always check the ack ratio for anything safety-relevant."
                ),
                escalation="Page Platform if the affected key is safety-relevant (a circuit breaker, a rate limit).",
                related=("PROMO-5530",),
            ),
            Fault(
                code="CFG-1150",
                title="etcd leader election storm",
                severity="critical",
                symptom=(
                    "`etcd leader changed` repeatedly, config reads timing out, and every dependent "
                    "service falling back to cached configuration."
                ),
                causes=(
                    "Disk latency on an etcd node exceeding the election timeout — usually a noisy neighbour",
                    "Network latency between etcd nodes across availability zones",
                ),
                detection=(
                    "`etcdctl endpoint status --cluster` shows the current leader and each node's raft "
                    "term. A rapidly incrementing term confirms the storm. Then check `wal_fsync_duration_seconds`."
                ),
                fix=(
                    "Identify the slow node and remove it from the cluster rather than restarting it "
                    "in place — a two-node cluster with a stable leader is healthier than three nodes "
                    "with one flapping. Replace the node on healthier hardware."
                ),
                antipattern=(
                    "Do not raise the election timeout to stop the flapping. It masks the underlying "
                    "disk problem and lengthens genuine failover, so real outages last longer."
                ),
                escalation="Page Platform immediately — this is a tier-1 dependency for the whole estate.",
            ),
        ),
    ),
    Service(
        name="feature-flags",
        domain="platform",
        team="Platform",
        language="Go 1.24",
        purpose=(
            "Percentage rollouts, targeted enablement, and kill switches. Distinct from "
            "config-service: flags are evaluated per request against a context, not read as "
            "a static value."
        ),
        datastore="Backed by config-service for storage; evaluation is in-process in each client SDK",
        dependencies=("config-service", "account-service", "metrics-store"),
        hazards=(
            "Flag evaluation is in-process, so a flag change relies on config-service "
            "broadcast and inherits CFG-1120's partial-application behaviour.",
            "Stale flags accumulate. A flag left at 100% for a year is indistinguishable from "
            "one that is load-bearing, and removing it is then risky.",
        ),
        alerts=(
            "`flag_evaluation_error_rate > 0.1%` (page)",
            "`stale_flag_count > 50` (ticket)",
        ),
        slo="99.99% availability; evaluation adds under 1ms",
        faults=(
            Fault(
                code="FLAG-4401",
                title="Kill switch not taking effect on all pods",
                severity="critical",
                symptom=(
                    "A kill switch flipped during an incident but the bad behaviour continues on some "
                    "pods — the worst possible moment for CFG-1120 to bite."
                ),
                causes=(
                    "Partial config broadcast (CFG-1120)",
                    "A client SDK with an evaluation cache longer than the broadcast interval",
                ),
                detection=(
                    "`bin/flags ack --name <flag>` reports per-pod flag state. Any pod on the old value "
                    "is still executing the behaviour you are trying to stop."
                ),
                fix=(
                    "Re-broadcast, then restart pods that have not acknowledged within 30 seconds. "
                    "During an incident, restart rather than wait — a kill switch that is 90% applied "
                    "is not a kill switch."
                ),
                antipattern=(
                    "Never treat a kill switch as applied on the strength of the write succeeding. "
                    "Always verify per-pod acknowledgement; this is the whole reason the ack report exists."
                ),
                escalation="Page Platform alongside whichever team owns the incident.",
                related=("CFG-1120",),
            ),
        ),
    ),
    Service(
        name="scheduler-service",
        domain="platform",
        team="Platform",
        language="Java 21",
        purpose=(
            "Runs cron-style and event-triggered jobs across the estate with leader election, "
            "so exactly one instance fires each schedule. Many financial and compliance "
            "processes depend on it firing reliably."
        ),
        datastore="Postgres `scheduler` cluster; leader lease held via advisory lock",
        dependencies=("Postgres", "config-service", "metrics-store"),
        hazards=(
            "A missed schedule is silent unless something downstream notices. The ledger "
            "partition rollover (LED-1030) is the canonical example of a silent miss with "
            "loud consequences.",
            "Jobs are at-most-once by design. A job that fails does not automatically retry, "
            "because several of them are not idempotent.",
        ),
        alerts=(
            "`schedule_missed_count > 0` (page)",
            "`leader_lease_churn > 5 in 10m` (ticket)",
        ),
        slo="Schedules fire within 60s of their nominal time, 99.9%",
        faults=(
            Fault(
                code="SCH-3310",
                title="Schedule missed entirely",
                severity="critical",
                symptom=(
                    "`schedule missed name=... nominal=...` — or, more often, no log at all and a "
                    "downstream failure such as LED-1030 the next morning."
                ),
                causes=(
                    "Leader lease lost and not reacquired before the schedule window passed",
                    "The scheduler pod evicted during a node drain with no replacement scheduled in time",
                ),
                detection=(
                    "`bin/scheduler history --name <job> --last 10` shows nominal versus actual fire "
                    "times. A gap with no failure record means the schedule never fired at all."
                ),
                fix=(
                    "Fire the job manually (`bin/scheduler run --name <job> --as-of <nominal>`), "
                    "supplying the nominal time so the job computes the right window. Confirm "
                    "idempotency for that specific job before running it twice."
                ),
                antipattern=(
                    "Do not add blanket automatic retries. Several scheduled jobs are not idempotent — "
                    "payout batches and ledger rollovers among them — and a retry would do real damage. "
                    "Alert on the miss instead."
                ),
                escalation="Page Platform and the owning team for the missed job.",
                related=("LED-1030", "PAYOUT-4401"),
            ),
        ),
    ),
    Service(
        name="audit-log-service",
        domain="platform",
        team="Platform",
        language="Java 21",
        purpose=(
            "Tamper-evident append-only record of privileged actions across the estate. "
            "Entries are hash-chained so a deletion or modification is detectable even by "
            "someone with database access."
        ),
        datastore="Postgres `audit` cluster with a periodic hash anchor written to immutable object storage",
        dependencies=("Postgres", "object storage"),
        hazards=(
            "Write failures must never be silently dropped. Callers buffer locally and retry, "
            "which means an audit outage becomes memory pressure in every caller.",
            "The hash chain must be verified periodically; an unverified chain provides no "
            "assurance at all.",
        ),
        alerts=(
            "`audit_chain_verification_failed > 0` (page)",
            "`audit_write_buffer_depth > 10000` in any caller (page)",
        ),
        slo="99.99% write availability; zero tolerated loss",
        faults=(
            Fault(
                code="AUD-9901",
                title="Hash chain verification failure",
                severity="critical",
                symptom="`chain verification failed at seq=...` from the periodic verifier.",
                causes=(
                    "A row modified or deleted directly in the database",
                    "A restore from backup that reintroduced an older chain state",
                ),
                detection=(
                    "`bin/audit verify --from <seq> --to <seq>` narrows the break to a specific entry. "
                    "Compare the surrounding entries against the last anchor in object storage, which "
                    "cannot be modified."
                ),
                fix=(
                    "Do not repair the chain. Preserve it exactly as-is, record the break, and start a "
                    "new chain segment anchored to the last verified hash. The break itself is the "
                    "evidence and must survive."
                ),
                antipattern=(
                    "Never recompute the hash chain to make verification pass. That is precisely the "
                    "tampering the design exists to detect, and doing it destroys the only record that "
                    "something happened."
                ),
                escalation=(
                    "Page Platform Security immediately and treat as a potential security incident until "
                    "shown otherwise. Compliance must be notified."
                ),
            ),
        ),
    ),
    Service(
        name="secrets-broker",
        domain="platform",
        team="Platform Security",
        language="Rust",
        purpose=(
            "Issues short-lived credentials and serves versioned secrets to workloads "
            "authenticated by workload identity. No static long-lived credentials are "
            "intended to exist in the estate."
        ),
        datastore="HashiCorp Vault backend; version metadata mirrored to Postgres",
        dependencies=("Vault", "workload identity provider"),
        hazards=(
            "Clients cache secrets at boot. A rotation therefore does not take effect until "
            "restart, which surprises people during incidents (see CHK-7044).",
            "Retiring an old secret version breaks anything still holding data encrypted "
            "under it (see ACC-3301).",
        ),
        alerts=(
            "`secret_fetch_failure_rate > 0.1%` (page)",
            "`secret_version_retired_with_live_references > 0` (page)",
        ),
        slo="99.99% availability, p99 fetch under 50ms",
        faults=(
            Fault(
                code="SEC-9002",
                title="Retired secret version still referenced",
                severity="critical",
                symptom=(
                    "`version retired but referenced version=... references=...` — or downstream "
                    "symptoms such as ACC-3301 decryption failures."
                ),
                causes=(
                    "A rotation completing its retirement step before all consumers re-encrypted or restarted",
                    "A consumer that pins a version explicitly and was missed in the rotation plan",
                ),
                detection=(
                    "`bin/secrets references --path <path> --version <n>` lists live references. Any "
                    "non-empty result means retirement was premature."
                ),
                fix=(
                    "Restore the retired version from the archive (`bin/secrets restore-version`), let "
                    "consumers re-encrypt or restart, verify references reach zero, then retire again."
                ),
                antipattern=(
                    "Do not retire a version on a schedule without checking references. The reference "
                    "check exists because retirement is effectively irreversible for anything already "
                    "encrypted under the key."
                ),
                escalation="Page Platform Security; involve the owning team for each live reference.",
                related=("ACC-3301", "CHK-7044"),
            ),
        ),
    ),
    Service(
        name="rate-limiter",
        domain="platform",
        team="Platform",
        language="Rust",
        purpose=(
            "Distributed rate limiting at the edge and between services, using a sliding "
            "window counter in Redis. It is the primary defence against both abuse and "
            "accidental self-inflicted load."
        ),
        datastore="Redis cluster, one counter shard per limit key",
        dependencies=("Redis", "config-service", "api-gateway"),
        hazards=(
            "The limiter fails open when Redis is unavailable. That is the right default for "
            "availability and the wrong one during an attack.",
            "Limit keys with very high cardinality (per-user limits on a hot endpoint) create "
            "Redis hot shards, so the limiter itself becomes the bottleneck.",
        ),
        alerts=(
            "`rate_limiter_fail_open_rate > 0.5%` (page)",
            "`redis_hot_shard_ops_ratio > 0.4` (ticket)",
        ),
        slo="99.99% availability; adds under 3ms p99",
        faults=(
            Fault(
                code="RATE-5501",
                title="Limiter failing open under Redis pressure",
                severity="high",
                symptom=(
                    "`failing open, redis unavailable` while request volume is elevated — the limiter "
                    "stops limiting exactly when it is most needed."
                ),
                causes=(
                    "A hot shard from a high-cardinality limit key",
                    "Redis cluster degradation independent of the limiter",
                ),
                detection=(
                    "`bin/ratelimit shard-report` shows operations per shard. One shard far above the "
                    "others identifies the offending limit key."
                ),
                fix=(
                    "Re-key the hot limit to a coarser dimension (per account rather than per session, "
                    "for example) with `bin/ratelimit rekey`. If Redis itself is degraded, fix that "
                    "first — the limiter has no independent state to repair."
                ),
                antipattern=(
                    "Do not switch to fail-closed as a blanket policy. Under a Redis outage that turns "
                    "a degraded defence into a total site outage. Fail-closed is appropriate only for "
                    "specific abuse-sensitive routes, configured individually."
                ),
                escalation="Page Platform. If an attack is in progress, page Platform Security too.",
                related=("WAL-3350", "SESS-1140"),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Data platform
# ---------------------------------------------------------------------------

DATA: tuple[Service, ...] = (
    Service(
        name="event-bus",
        domain="data",
        team="Data Platform",
        language="Java 21 (Kafka)",
        purpose=(
            "The estate's asynchronous backbone. Every domain event flows through it, which "
            "makes its failure modes everyone's failure modes."
        ),
        datastore="Kafka cluster, 3 brokers per region, replication factor 3",
        dependencies=("Kafka", "schema registry"),
        hazards=(
            "Consumer lag is per partition. A single hot partition can starve one consumer "
            "while aggregate lag looks healthy.",
            "Topic retention is 7 days. A consumer down longer than that loses events "
            "permanently with no error at restart.",
        ),
        alerts=(
            "`consumer_lag_max_partition > 100000` (page)",
            "`under_replicated_partitions > 0` (page)",
        ),
        slo="99.99% availability; end-to-end publish-to-consume under 5s p99",
        faults=(
            Fault(
                code="BUS-1201",
                title="Partition backpressure from an uneven key distribution",
                severity="high",
                symptom=(
                    "One partition's lag growing without bound while the rest are flat. Consumers "
                    "assigned to that partition fall behind; others are fine."
                ),
                causes=(
                    "A partition key with a skewed distribution — an event keyed by category where one category dominates",
                    "A single very large publish fanout landing on one key (see CAT-4455)",
                ),
                detection=(
                    "`kafka-consumer-groups --describe` per partition rather than in aggregate. "
                    "Aggregate lag hides this completely, which is why the alert is on max-partition lag."
                ),
                fix=(
                    "Repartition the topic with a better key, or for a one-off fanout let it drain "
                    "while temporarily scaling the affected consumer group. Repartitioning requires a "
                    "consumer-side migration — see ADR-0052."
                ),
                antipattern=(
                    "Do not add partitions to an existing topic to fix skew. Existing keys keep hashing "
                    "to their current partitions, so the hot one stays hot, and ordering guarantees "
                    "break for keys that do move."
                ),
                escalation="Page Data Platform; notify the owning team of the affected topic.",
                related=("CAT-4455", "IDX-7701"),
            ),
            Fault(
                code="BUS-1240",
                title="Consumer offline past the retention window",
                severity="critical",
                symptom=(
                    "A consumer restarts after a long outage and begins from the earliest available "
                    "offset, silently having skipped everything older than 7 days."
                ),
                causes=(
                    "A consumer deployment left disabled longer than retention during an extended incident",
                    "A consumer group whose offsets expired after prolonged inactivity",
                ),
                detection=(
                    "Compare the consumer's committed offset against the topic's earliest offset before "
                    "restarting it. If committed is below earliest, data loss has already occurred."
                ),
                fix=(
                    "Backfill from the source of truth rather than the bus — each producing service can "
                    "replay its own state. Do not expect the bus to be the recovery mechanism; it is a "
                    "transport with finite retention."
                ),
                antipattern=(
                    "Do not restart a long-offline consumer without checking offsets first. Once it "
                    "commits a new offset the evidence of what was skipped is gone."
                ),
                escalation="Page Data Platform and the consumer's owning team.",
            ),
        ),
    ),
    Service(
        name="stream-processor",
        domain="data",
        team="Data Platform",
        language="Java 21 (Flink)",
        purpose=(
            "Stateful stream processing for real-time aggregates: live inventory positions, "
            "fraud features, and operational metrics. State is checkpointed to object storage."
        ),
        datastore="Flink state backend on object storage; checkpoints every 60s",
        dependencies=("event-bus", "object storage", "metrics-store"),
        hazards=(
            "Recovery replays from the last checkpoint, so up to 60 seconds of processing is "
            "repeated. Downstream sinks must be idempotent and not all of them are.",
            "Watermarks drive windowing. A producer with a skewed clock stalls windows for "
            "every other producer on that stream.",
        ),
        alerts=(
            "`checkpoint_failure_count > 2 consecutive` (page)",
            "`watermark_lag_seconds > 300` (page)",
        ),
        slo="99.9% availability; aggregates current within 30s p95",
        faults=(
            Fault(
                code="STRM-2201",
                title="Checkpoint failures preventing recovery",
                severity="critical",
                symptom=(
                    "`checkpoint failed attempt=3` repeatedly. The job runs but cannot recover from "
                    "failure without replaying from a very old checkpoint."
                ),
                causes=(
                    "Object storage throttling under checkpoint write load",
                    "State size growing past what fits in the checkpoint interval",
                ),
                detection=(
                    "Compare checkpoint duration against the 60s interval. A duration approaching the "
                    "interval means the next checkpoint starts before the last finished."
                ),
                fix=(
                    "Lengthen the checkpoint interval to buy headroom, then address state growth — "
                    "usually an unbounded key space needing a TTL. `bin/stream state-report` shows state "
                    "size by operator."
                ),
                antipattern=(
                    "Do not disable checkpointing to stop the errors. The job then cannot recover at "
                    "all, and the next restart reprocesses from the beginning of retention."
                ),
                escalation="Page Data Platform.",
            ),
            Fault(
                code="STRM-2240",
                title="Watermark stalled by a skewed producer clock",
                severity="high",
                symptom=(
                    "Windowed aggregates stop advancing. `watermark lag=...` growing while event "
                    "throughput is normal."
                ),
                causes=(
                    "One producer emitting timestamps far in the past, holding the watermark back",
                    "An idle partition with no events, which without an idleness timeout stalls the watermark",
                ),
                detection=(
                    "`bin/stream watermark-report` shows the per-partition watermark. The lowest one is "
                    "holding everything back; identify its producer."
                ),
                fix=(
                    "Fix the producer's clock. As an immediate mitigation, enable idleness detection "
                    "(`stream.source.idle_timeout=60s`) so an idle or lagging partition stops blocking "
                    "the global watermark."
                ),
                antipattern=(
                    "Do not switch to processing time to make the stall go away. Every aggregate then "
                    "becomes non-deterministic on replay, and the fraud features that depend on this "
                    "stream become impossible to reproduce during an investigation."
                ),
                escalation="Data Platform on-call; page if fraud features are affected.",
                related=("FRAUD-6601",),
            ),
        ),
    ),
    Service(
        name="warehouse-etl",
        domain="data",
        team="Data Platform",
        language="Python 3.12",
        purpose=(
            "Batch pipelines loading operational data into the analytical warehouse. "
            "Campaign audiences, finance reporting, and business metrics all derive from it."
        ),
        datastore="Snowflake-compatible warehouse; job state in Postgres",
        dependencies=("operational databases (read replicas)", "event-bus", "scheduler-service"),
        hazards=(
            "Pipelines read from operational read replicas. A replica lag problem produces "
            "*quietly incomplete* loads rather than failures.",
            "Downstream consumers rarely check freshness, so a failed load surfaces as a "
            "business decision made on stale data.",
        ),
        alerts=(
            "`etl_job_failed > 0` (ticket)",
            "`warehouse_table_staleness_hours > 26` (page)",
        ),
        slo="Daily loads complete by 05:00 UTC",
        faults=(
            Fault(
                code="ETL-4401",
                title="Silent partial load from replica lag",
                severity="high",
                symptom=(
                    "A pipeline reports success but row counts are materially below expectation. "
                    "Downstream audiences and reports are quietly incomplete."
                ),
                causes=(
                    "The source read replica lagging past the extraction watermark",
                    "An extraction window computed from the replica's clock rather than the primary's",
                ),
                detection=(
                    "`bin/etl row-count-check --job <job> --last 7` compares against the trailing "
                    "average. A drop with a success status is the signature of this fault."
                ),
                fix=(
                    "Re-run the extraction against the primary for the affected window "
                    "(`bin/etl rerun --job <job> --window <from>:<to> --source primary`). Loads are "
                    "idempotent per window."
                ),
                antipattern=(
                    "Do not treat job success as load success. The row-count check exists because "
                    "success only means no exception was raised, not that the data is complete."
                ),
                escalation="Data Platform on-call; notify Growth if campaign audiences are affected.",
                related=("CMP-8801",),
            ),
        ),
    ),
    Service(
        name="metrics-store",
        domain="data",
        team="Data Platform",
        language="Go 1.24",
        purpose=(
            "Time-series storage for operational and product metrics, and the feature source "
            "for several models. Ingest is high-cardinality and that is its main hazard."
        ),
        datastore="Prometheus-compatible TSDB with long-term blocks in object storage",
        dependencies=("object storage", "stream-processor"),
        hazards=(
            "Cardinality explosions from a label containing an unbounded value (a request id, "
            "an email address) can take the whole store down.",
            "Models read features from here. A metric rename is therefore a breaking change "
            "for ranking and fraud (see RNK-3301).",
        ),
        alerts=(
            "`active_series > 8000000` (page)",
            "`ingest_rejected_samples > 0` (ticket)",
        ),
        slo="99.9% query availability; ingest never rejects valid samples",
        faults=(
            Fault(
                code="MET-6601",
                title="Cardinality explosion from an unbounded label",
                severity="critical",
                symptom=(
                    "`active series=11.2M` climbing fast, query latency degrading, and eventually "
                    "ingest rejections across every team's metrics."
                ),
                causes=(
                    "A newly deployed service emitting a label with unbounded values — request id, user id, or a raw URL path",
                    "An error metric labelled with the full exception message",
                ),
                detection=(
                    "`bin/metrics cardinality-top --limit 20` ranks metrics by series count. The "
                    "offender is usually one metric an order of magnitude above the rest, deployed within the hour."
                ),
                fix=(
                    "Drop the offending series at ingest with a relabel rule "
                    "(`bin/metrics drop-rule --metric <name>`), which takes effect immediately, then "
                    "have the owning team remove the label."
                ),
                antipattern=(
                    "Do not scale the store to absorb the cardinality. It grows without bound by "
                    "definition, and the cost lands on every team while the offending metric remains "
                    "useless at that cardinality anyway."
                ),
                escalation="Page Data Platform; contact the owning team directly — this affects everyone's observability.",
                related=("RNK-3301",),
            ),
        ),
    ),
)


ALL_PLATFORM_SERVICES: tuple[Service, ...] = IDENTITY + GROWTH + DISCOVERY + PLATFORM + DATA
