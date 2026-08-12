"""Synthetic enterprise corpus for a fictional e-commerce platform, "Meridian".

The point of this module is realism, not volume. Every incident below produces
log lines that are *correlated* — a single trace id threads through several
services, upstream failures arrive before downstream ones, and the error codes
match rows in the SQL catalog. A retrieval system evaluated on uncorrelated
random logs looks much better than it is.

The 18 documents defined here as literals are the *canonical* corpus: they are
hand-written, and they are the labelled answers for the original golden-set
questions. `aiops.ingestion.expansion` adds a further 201 generated documents
covering 42 more services. That expansion exists to supply realistic distractor
pressure — a dozen plausible timeout runbooks that a query for PAY-5021 must
now be ranked against — so retrieval metrics reflect a genuine ranking problem
rather than a corpus too small to be confusing.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from aiops.config import settings
from aiops.schemas import ErrorCodeEntry

SERVICES = [
    "api-gateway",
    "auth-service",
    "order-service",
    "payment-service",
    "inventory-service",
    "notification-service",
    "search-service",
]

BASE_TIME = datetime(2026, 7, 14, 9, 0, 0)


# --------------------------------------------------------------------------
# Error-code catalog — the structured knowledge base behind the mapping tool
# --------------------------------------------------------------------------

ERROR_CATALOG: list[ErrorCodeEntry] = [
    ErrorCodeEntry(
        code="PAY-5021",
        service="payment-service",
        title="Upstream payment processor timeout",
        severity="critical",
        description=(
            "The call to the external processor (Stripewise) exceeded the 3s socket "
            "timeout. The charge may or may not have been captured, so the order must "
            "not be marked paid without reconciliation."
        ),
        likely_causes=[
            "Processor-side degradation or maintenance window",
            "Connection pool exhaustion in payment-service starving new requests",
            "Egress NAT gateway saturation in the payments VPC",
        ],
        remediation=(
            "Check the Stripewise status page. If the processor is healthy, inspect "
            "the payment-service connection pool gauge; restart pods only after "
            "capturing a heap dump. Run the reconciliation job before replaying orders."
        ),
        runbook_ref="RB-PAYMENT-TIMEOUT",
    ),
    ErrorCodeEntry(
        code="ORD-4102",
        service="order-service",
        title="Order stuck in PENDING_PAYMENT",
        severity="high",
        description=(
            "order-service received no terminal response from payment-service within "
            "the saga timeout and left the order in PENDING_PAYMENT."
        ),
        likely_causes=[
            "Downstream PAY-5021 timeout cascade",
            "Saga coordinator lost the compensation event",
        ],
        remediation=(
            "Do not retry blindly — reconcile against the processor first, then drive "
            "the saga forward with the compensate-or-complete job."
        ),
        runbook_ref="RB-ORDER-SAGA",
    ),
    ErrorCodeEntry(
        code="GW-5030",
        service="api-gateway",
        title="Upstream 504 returned to client",
        severity="high",
        description="The gateway's upstream deadline elapsed before the backend responded.",
        likely_causes=[
            "A slow downstream service (usually the true root cause)",
            "Gateway worker saturation under a traffic spike",
        ],
        remediation=(
            "Treat as a symptom. Follow the trace id into the backend before touching "
            "gateway configuration."
        ),
        runbook_ref="RB-GATEWAY-504",
    ),
    ErrorCodeEntry(
        code="INV-3007",
        service="inventory-service",
        title="Connection pool exhausted",
        severity="critical",
        description=(
            "All 40 HikariCP connections were checked out and the 10s acquisition "
            "timeout elapsed."
        ),
        likely_causes=[
            "A long-running reservation query holding connections",
            "Missing index causing full scans on inventory_reservations",
            "Leaked connections from an un-closed transaction in the bulk import path",
        ],
        remediation=(
            "Query pg_stat_activity for long transactions, kill offenders, then apply "
            "the idx_reservations_sku_warehouse index. Raising pool size masks the leak."
        ),
        runbook_ref="RB-INVENTORY-POOL",
    ),
    ErrorCodeEntry(
        code="NOTIF-2210",
        service="notification-service",
        title="Poison message redelivery loop",
        severity="medium",
        description=(
            "A malformed event failed deserialization and was redelivered until the "
            "max-attempts ceiling, blocking the partition behind it."
        ),
        likely_causes=[
            "Producer published a schema version the consumer cannot parse",
            "Dead-letter queue not configured for the topic",
        ],
        remediation=(
            "Route the offender to the DLQ, then fix the schema-registry compatibility "
            "setting. Never skip the offset without capturing the payload."
        ),
        runbook_ref="RB-NOTIF-POISON",
    ),
    ErrorCodeEntry(
        code="AUTH-1015",
        service="auth-service",
        title="JWT validation failed: token not yet valid",
        severity="high",
        description=(
            "The nbf claim is in the future relative to the validating node's clock — "
            "almost always clock skew, not a forged token."
        ),
        likely_causes=[
            "chronyd stopped on one or more nodes",
            "A node scheduled onto a host with a drifting hypervisor clock",
        ],
        remediation=(
            "Compare `chronyc tracking` across nodes. Restart chronyd on the drifted "
            "node; do not widen the leeway window as a permanent fix."
        ),
        runbook_ref="RB-AUTH-CLOCKSKEW",
    ),
    ErrorCodeEntry(
        code="SRCH-6001",
        service="search-service",
        title="Heap exhausted during index rebuild",
        severity="high",
        description=(
            "The rebuild loaded the full catalog into memory and exceeded the 4GiB "
            "container limit, triggering an OOMKill."
        ),
        likely_causes=[
            "Rebuild not batched after the catalog grew past ~2M SKUs",
            "Container memory limit unchanged since the 2025 sizing",
        ],
        remediation=(
            "Run the rebuild with --batch-size=50000 and raise the limit to 8GiB. "
            "The streaming rebuild path is tracked in ADR-0009."
        ),
        runbook_ref="RB-SEARCH-OOM",
    ),
]


# --------------------------------------------------------------------------
# Incident scenarios
# --------------------------------------------------------------------------


@dataclass
class LogLine:
    ts: datetime
    level: str
    service: str
    trace_id: str
    message: str
    error_code: str | None = None
    latency_ms: int | None = None

    def render(self) -> str:
        parts = [
            self.ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            self.level,
            self.service,
            f"trace_id={self.trace_id}",
        ]
        if self.error_code:
            parts.append(f"error_code={self.error_code}")
        if self.latency_ms is not None:
            parts.append(f"latency_ms={self.latency_ms}")
        parts.append(self.message)
        return " ".join(parts)


@dataclass
class Incident:
    incident_id: str
    title: str
    start: datetime
    lines: list[LogLine] = field(default_factory=list)


def _trace(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(16))


def _payment_cascade(rng: random.Random, start: datetime) -> Incident:
    """The flagship scenario: one slow dependency, three services reporting errors.

    The diagnostic lesson is that api-gateway and order-service are *symptoms*;
    a naive log search that ranks by error count blames the gateway.
    """
    inc = Incident("INC-2026-0714-01", "Checkout failures during payment processor degradation", start)
    for i in range(14):
        t = start + timedelta(seconds=i * 7 + rng.randint(0, 4))
        tr = _trace(rng)
        order_id = f"ord_{rng.randint(700000, 799999)}"
        inc.lines += [
            LogLine(t, "INFO", "api-gateway", tr, f"POST /v1/checkout order_id={order_id}"),
            LogLine(
                t + timedelta(milliseconds=40),
                "INFO",
                "order-service",
                tr,
                f"saga started order_id={order_id} step=authorize_payment",
            ),
            LogLine(
                t + timedelta(milliseconds=90),
                "INFO",
                "payment-service",
                tr,
                f"calling processor=stripewise amount_cents={rng.randint(1200, 48000)}",
            ),
            LogLine(
                t + timedelta(milliseconds=3120),
                "ERROR",
                "payment-service",
                tr,
                "processor call failed: read timed out after 3000ms host=api.stripewise.io",
                error_code="PAY-5021",
                latency_ms=3120,
            ),
            LogLine(
                t + timedelta(milliseconds=3180),
                "ERROR",
                "order-service",
                tr,
                f"no terminal payment response; order_id={order_id} left in PENDING_PAYMENT",
                error_code="ORD-4102",
                latency_ms=3180,
            ),
            LogLine(
                t + timedelta(milliseconds=3210),
                "ERROR",
                "api-gateway",
                tr,
                "upstream deadline exceeded upstream=order-service returning 504",
                error_code="GW-5030",
                latency_ms=3210,
            ),
        ]
    # A pool gauge line that explains *why* the timeouts clustered.
    inc.lines.append(
        LogLine(
            start + timedelta(seconds=30),
            "WARN",
            "payment-service",
            _trace(rng),
            "http connection pool utilization=0.98 active=49 idle=1 max=50",
        )
    )
    return inc


def _inventory_pool(rng: random.Random, start: datetime) -> Incident:
    inc = Incident("INC-2026-0714-02", "Inventory reservation latency and pool exhaustion", start)
    inc.lines.append(
        LogLine(
            start,
            "INFO",
            "inventory-service",
            _trace(rng),
            "bulk import started source=supplier_feed rows=184320",
        )
    )
    for i in range(10):
        t = start + timedelta(seconds=20 + i * 11)
        tr = _trace(rng)
        latency = 900 + i * 950
        inc.lines += [
            LogLine(
                t,
                "WARN",
                "inventory-service",
                tr,
                f"slow query reserve_stock sku=SKU-{rng.randint(10000,99999)} rows_scanned={rng.randint(80000,240000)}",
                latency_ms=latency,
            ),
        ]
        if i >= 6:
            inc.lines.append(
                LogLine(
                    t + timedelta(milliseconds=10000),
                    "ERROR",
                    "inventory-service",
                    tr,
                    "HikariPool-1 timeout acquiring connection after 10000ms active=40 idle=0",
                    error_code="INV-3007",
                    latency_ms=10000,
                )
            )
            inc.lines.append(
                LogLine(
                    t + timedelta(milliseconds=10050),
                    "ERROR",
                    "order-service",
                    tr,
                    "reservation call failed upstream=inventory-service status=503",
                )
            )
    return inc


def _notif_poison(rng: random.Random, start: datetime) -> Incident:
    inc = Incident("INC-2026-0714-03", "Notification consumer stalled on poison message", start)
    tr = _trace(rng)
    offset = 88421
    for attempt in range(1, 9):
        inc.lines.append(
            LogLine(
                start + timedelta(seconds=attempt * 6),
                "ERROR",
                "notification-service",
                tr,
                (
                    f"deserialization failed topic=order.events partition=3 offset={offset} "
                    f"attempt={attempt}/8 schema_version=4 supported_max=3"
                ),
                error_code="NOTIF-2210",
            )
        )
    inc.lines.append(
        LogLine(
            start + timedelta(seconds=60),
            "WARN",
            "notification-service",
            _trace(rng),
            "consumer lag topic=order.events partition=3 lag=41208 and growing",
        )
    )
    return inc


def _auth_skew(rng: random.Random, start: datetime) -> Incident:
    inc = Incident("INC-2026-0714-04", "Intermittent 401s from a single auth node", start)
    for i in range(12):
        t = start + timedelta(seconds=i * 9)
        tr = _trace(rng)
        node = "auth-7f4c" if i % 3 == 0 else rng.choice(["auth-2a19", "auth-9b03"])
        if node == "auth-7f4c":
            inc.lines += [
                LogLine(
                    t,
                    "ERROR",
                    "auth-service",
                    tr,
                    f"jwt rejected node={node} reason=nbf_in_future skew_ms={rng.randint(41000, 52000)}",
                    error_code="AUTH-1015",
                ),
                LogLine(
                    t + timedelta(milliseconds=15),
                    "WARN",
                    "api-gateway",
                    tr,
                    "auth rejected request returning 401 path=/v1/orders",
                ),
            ]
        else:
            inc.lines.append(
                LogLine(t, "INFO", "auth-service", tr, f"jwt validated node={node} sub=usr_{rng.randint(1000,9999)}")
            )
    return inc


def _search_oom(rng: random.Random, start: datetime) -> Incident:
    inc = Incident("INC-2026-0714-05", "Search index rebuild OOMKilled", start)
    tr = _trace(rng)
    inc.lines.append(LogLine(start, "INFO", "search-service", tr, "index rebuild started catalog_size=2417883"))
    for i, pct in enumerate([12, 28, 44, 61]):
        inc.lines.append(
            LogLine(
                start + timedelta(seconds=25 * (i + 1)),
                "INFO",
                "search-service",
                tr,
                f"rebuild progress={pct}% heap_used_mb={1100 + i * 780}",
            )
        )
    inc.lines += [
        LogLine(
            start + timedelta(seconds=130),
            "ERROR",
            "search-service",
            tr,
            "java.lang.OutOfMemoryError: Java heap space during segment merge",
            error_code="SRCH-6001",
        ),
        LogLine(
            start + timedelta(seconds=131),
            "FATAL",
            "search-service",
            tr,
            "container OOMKilled limit_mb=4096 restarting pod",
            error_code="SRCH-6001",
        ),
        LogLine(
            start + timedelta(seconds=145),
            "WARN",
            "api-gateway",
            _trace(rng),
            "search upstream unhealthy, serving degraded results from cache",
        ),
    ]
    return inc


def _background_noise(rng: random.Random, start: datetime, count: int = 260) -> list[LogLine]:
    """Healthy traffic. Without it, every retrieval query trivially hits an error line."""
    templates = [
        ("INFO", "api-gateway", "GET /v1/products/{sku} status=200"),
        ("INFO", "api-gateway", "GET /v1/cart status=200"),
        ("INFO", "order-service", "order created order_id=ord_{n} items={i}"),
        ("INFO", "inventory-service", "stock reserved sku=SKU-{sku} qty={i}"),
        ("INFO", "payment-service", "charge captured amount_cents={n}"),
        ("INFO", "notification-service", "email queued template=order_confirmation"),
        ("INFO", "search-service", "query served hits={i} q=\"running shoes\""),
        ("DEBUG", "auth-service", "token refreshed sub=usr_{i}"),
        ("WARN", "api-gateway", "client retry detected idempotency_key=idem_{n}"),
    ]
    out: list[LogLine] = []
    for _ in range(count):
        level, svc, tmpl = rng.choice(templates)
        msg = tmpl.format(
            sku=rng.randint(10000, 99999),
            n=rng.randint(100000, 999999),
            i=rng.randint(1, 400),
        )
        out.append(
            LogLine(
                start + timedelta(seconds=rng.randint(0, 5400)),
                level,
                svc,
                _trace(rng),
                msg,
                latency_ms=rng.randint(4, 180),
            )
        )
    return out


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

DOCUMENTS: dict[str, str] = {
    "RB-PAYMENT-TIMEOUT.md": """---
title: "Runbook: Payment processor timeouts (PAY-5021)"
source_type: runbook
service: payment-service
error_codes: [PAY-5021, ORD-4102]
---

# Runbook: Payment processor timeouts

## When this fires
payment-service emits `PAY-5021` when a call to Stripewise exceeds the 3000ms
socket timeout. Under sustained processor degradation you will also see
`ORD-4102` from order-service and `GW-5030` at the gateway *for the same trace
id*. Those two are downstream symptoms — do not open separate incidents for them.

## First five minutes
1. Check https://status.stripewise.io. If the processor reports degradation,
   declare a dependency incident and skip to Containment.
2. If the processor is healthy, pull the connection-pool gauge:
   `kubectl exec deploy/payment-service -- curl -s localhost:9090/metrics | grep http_pool`.
   Utilization above 0.95 means we are starving ourselves, not being starved.
3. Confirm the blast radius with the checkout success-rate dashboard.

## Containment
Enable the checkout circuit breaker (`payments.breaker.enabled=true`). This
fails checkout fast with a retryable error instead of holding gateway workers
for three seconds per request, which is what turns a payment problem into a
site-wide outage.

## Reconciliation — do not skip
A timeout does **not** mean the charge failed. The processor may have captured
it. Run `bin/reconcile-charges --window=2h` before replaying any order. Marking
orders failed without reconciling is how we double-charged customers in
INC-2025-1103.

## Escalation
Page the Payments on-call. If reconciliation shows captured-but-unrecorded
charges, also notify Finance within one hour — this is a regulatory reporting
obligation.
""",
    "RB-ORDER-SAGA.md": """---
title: "Runbook: Order stuck in PENDING_PAYMENT (ORD-4102)"
source_type: runbook
service: order-service
error_codes: [ORD-4102, PAY-5021]
---

# Runbook: Orders stuck in PENDING_PAYMENT

`ORD-4102` means the checkout saga asked payment-service to authorize and got
no terminal answer within 3.2s. The order is left in `PENDING_PAYMENT` — not
`FAILED` — because at that moment we genuinely do not know whether the customer
was charged.

## When this fires
Almost always downstream of `PAY-5021`. Take the trace id and check
payment-service first; if you see PAY-5021 on the same trace, this is a symptom
and the payment incident is the one to work.

## First five minutes
1. Count the affected orders: `bin/saga-list --state PENDING_PAYMENT --since 1h`.
2. Check whether payment-service is still timing out. If it is, do nothing to
   the orders yet — they will keep accumulating and resolving them individually
   wastes the window.
3. Confirm the saga coordinator is healthy: a stuck coordinator produces the
   same symptom with no PAY-5021 at all, and that is a different problem.

## Resolution — reconcile before you drive
Run `bin/reconcile-charges --window=2h` first. Only once reconciliation has
established the true payment state should you run
`bin/saga-drive --state PENDING_PAYMENT`, which completes orders that were paid
and compensates those that were not.

## What not to do
Never mark these orders `FAILED` in bulk and re-run the checkout replay. The
processor may have captured the charge, and replaying captures it a second
time — this is exactly how INC-2025-1103 double-charged 1,842 customers.

A timeout is not evidence that nothing happened. It is evidence that we did not
hear back.

## Escalation
Page Commerce on-call. If reconciliation finds captured-but-unrecorded charges,
Payments and Finance both need to know within the hour.
""",
    "RB-INVENTORY-POOL.md": """---
title: "Runbook: Inventory connection pool exhaustion (INV-3007)"
source_type: runbook
service: inventory-service
error_codes: [INV-3007]
---

# Runbook: Inventory connection pool exhaustion

`INV-3007` means all 40 HikariCP connections were checked out and the 10s
acquisition timeout elapsed. It is nearly always a *symptom of slow queries*,
not of an undersized pool.

## Diagnosis
1. `SELECT pid, now() - query_start AS dur, query FROM pg_stat_activity
   WHERE state = 'active' ORDER BY dur DESC LIMIT 20;`
2. Look for `reserve_stock` queries with high `rows_scanned` in the service
   logs immediately preceding the first INV-3007. A bulk import running
   concurrently is the usual trigger.

## Remediation
- Kill the long transactions: `SELECT pg_terminate_backend(pid)`.
- Apply `idx_reservations_sku_warehouse` if it is missing (see ADR-0012).
- Throttle the supplier bulk import to off-peak hours.

## What not to do
Raising `maximumPoolSize` makes the graph look better for twenty minutes and
then exhausts the database's own connection limit, which takes down every
service sharing the cluster. Fix the query.
""",
    "RB-NOTIF-POISON.md": """---
title: "Runbook: Poison message in notification consumer (NOTIF-2210)"
source_type: runbook
service: notification-service
error_codes: [NOTIF-2210]
---

# Runbook: Poison message redelivery

A message that fails deserialization is redelivered up to 8 times and then
blocks its partition. Symptom: `NOTIF-2210` repeating at a fixed offset with an
increasing `attempt` counter, followed by growing consumer lag on that partition
only.

## Steps
1. Capture the payload before doing anything:
   `bin/kafka-peek --topic order.events --partition 3 --offset <offset>`.
2. Route it to the dead-letter topic with `bin/dlq-route --offset <offset>`.
3. Confirm lag drains.
4. File a producer bug. `schema_version=4 supported_max=3` in the log line means
   a producer shipped ahead of the consumer — the schema-registry compatibility
   mode should have prevented it. See ADR-0007.

## What not to do
Never advance the offset without capturing the payload. We lose the evidence and
the same bug recurs on the next deploy.
""",
    "RB-AUTH-CLOCKSKEW.md": """---
title: "Runbook: JWT nbf rejections from clock skew (AUTH-1015)"
source_type: runbook
service: auth-service
error_codes: [AUTH-1015]
---

# Runbook: JWT "token not yet valid"

`AUTH-1015` with `reason=nbf_in_future` means the validating node's clock is
behind the issuing node's. Users see intermittent 401s — intermittent because
only requests that land on the drifted node fail, so the failure rate is
roughly 1/N for N auth nodes.

## Diagnosis
The log line includes `node=`. If the failures all name one node, you have your
answer. Then: `chronyc tracking` on that node and compare `System time` offset
against a healthy peer.

## Remediation
`systemctl restart chronyd` on the drifted node, then verify the offset settles
under 50ms. Cordon the node if it drifts again — that usually means a sick
hypervisor host.

## What not to do
Widening `jwt.leeway` clears the alert and leaves a node with a broken clock
serving traffic. It is acceptable as a 30-minute mitigation, never as the fix.
""",
    "RB-SEARCH-OOM.md": """---
title: "Runbook: Search index rebuild OOM (SRCH-6001)"
source_type: runbook
service: search-service
error_codes: [SRCH-6001]
---

# Runbook: Search rebuild out-of-memory

The full rebuild loads the catalog into heap. Past roughly 2M SKUs it exceeds
the 4GiB container limit and the pod is OOMKilled mid-merge, leaving a partial
index and degraded search results served from cache.

## Immediate
1. Stop the rebuild job so it does not crash-loop.
2. Serve from the last good index: `bin/search-index --activate previous`.

## Fix
Run with `--batch-size=50000` and raise the container limit to 8GiB. The
streaming rebuild that removes the heap ceiling entirely is designed in
ADR-0009 and not yet shipped.
""",
    "RB-GATEWAY-504.md": """---
title: "Runbook: Gateway 504s (GW-5030)"
source_type: runbook
service: api-gateway
error_codes: [GW-5030]
---

# Runbook: Gateway upstream deadline exceeded

`GW-5030` is almost never a gateway problem. It means a backend did not answer
within the upstream deadline.

## Procedure
1. Take the `trace_id` from any GW-5030 line.
2. Search all services for that trace id. The *earliest* ERROR in the trace is
   your root cause; the gateway line is the last domino.
3. Only if no backend error exists for the trace should you look at gateway
   worker saturation.

This ordering matters: during INC-2026-0714-01 the gateway produced the largest
error volume of any service and had nothing wrong with it.
""",
    "ADR-0007-schema-registry.md": """---
title: "ADR-0007: Schema registry compatibility mode"
source_type: adr
service: notification-service
error_codes: [NOTIF-2210]
---

# ADR-0007: Enforce BACKWARD compatibility in the schema registry

**Status:** Accepted (2026-02-11)

## Context
Producers and consumers deploy independently. A producer that ships a new event
schema before consumers support it causes deserialization failures that block
partitions (see NOTIF-2210).

## Decision
The registry runs in `BACKWARD` compatibility mode, and CI blocks any schema
change that fails the compatibility check against the current production schema.

## Consequences
- Adding a required field is a two-step migration: add as optional, backfill,
  then require.
- The registry becomes a deploy-time dependency; its availability is now
  tier-1.
- Producers can no longer ship a breaking change accidentally, which is the
  point. Incidents caused by this class of bug should trend to zero; if one
  occurs anyway, the registry configuration itself is suspect.
""",
    "ADR-0009-search-rebuild.md": """---
title: "ADR-0009: Streaming search index rebuild"
source_type: adr
service: search-service
error_codes: [SRCH-6001]
---

# ADR-0009: Move to a streaming index rebuild

**Status:** Proposed (2026-05-30)

## Context
The current rebuild materializes the full catalog in heap. Memory use grows
linearly with catalog size, and we crossed the 4GiB container limit in
mid-2026 (SRCH-6001).

## Options considered
1. **Raise the memory limit.** Cheapest, buys perhaps a year, does not change
   the shape of the curve.
2. **Batch the existing rebuild.** A one-line change (`--batch-size`), reduces
   peak heap by roughly 70%. Still loads whole batches.
3. **Streaming rebuild with on-disk merge.** Constant memory regardless of
   catalog size. Roughly three engineer-weeks.

## Decision
Ship option 2 immediately as mitigation and schedule option 3 for Q4. Option 1
alone is rejected: it converts a predictable failure into a surprising one.
""",
    "ADR-0012-inventory-indexes.md": """---
title: "ADR-0012: Composite index on inventory reservations"
source_type: adr
service: inventory-service
error_codes: [INV-3007]
---

# ADR-0012: Add idx_reservations_sku_warehouse

**Status:** Accepted (2026-03-22)

## Context
`reserve_stock` filters on `(sku, warehouse_id)` and previously had an index on
`sku` alone. With multi-warehouse rollout the selectivity collapsed and the
planner began choosing sequential scans, holding connections long enough to
exhaust the pool (INV-3007).

## Decision
Create `idx_reservations_sku_warehouse ON inventory_reservations (sku, warehouse_id)`
and keep the single-column index for the legacy single-warehouse query path
until that path is retired.

## Consequences
Write amplification on a high-write table is acceptable here: reservation writes
are ~400/s while the scan was costing seconds of connection hold time per read.
""",
    "PM-2025-1103-double-charge.md": """---
title: "Post-mortem: INC-2025-1103 duplicate customer charges"
source_type: postmortem
service: payment-service
error_codes: [PAY-5021]
---

# Post-mortem: Duplicate charges after processor timeouts (INC-2025-1103)

**Impact:** 1,842 customers charged twice. Total refunded: $71,400. Regulatory
report filed 6 days late.

## What happened
The processor degraded and payment-service logged PAY-5021 timeouts. The
on-call engineer marked the affected orders failed and re-ran the checkout
replay job. The processor had in fact captured most of those charges, so the
replay captured them a second time.

## Root cause
Two contributing factors. First, a socket timeout was treated as evidence the
charge did not occur; it is evidence only that we did not hear back. Second,
the replay job had no idempotency key check against the processor's own ledger.

## What we changed
- The runbook now requires `bin/reconcile-charges` before any replay, in bold.
- The replay job queries the processor ledger by idempotency key and skips
  already-captured charges.
- Finance notification was added to the incident checklist with a one-hour SLA.

## Lesson
An ambiguous failure is not a failure. Design the recovery path around not
knowing, because at timeout time you genuinely do not know.
""",
    "PM-2026-0714-checkout.md": """---
title: "Post-mortem: INC-2026-0714-01 checkout outage"
source_type: postmortem
service: payment-service
error_codes: [PAY-5021, ORD-4102, GW-5030]
---

# Post-mortem: Checkout failures, 2026-07-14

**Impact:** 34 minutes of degraded checkout, 412 orders left in
PENDING_PAYMENT, no duplicate charges (reconciliation worked).

## Timeline
- 09:00 payment-service connection pool utilization reaches 0.98.
- 09:02 first PAY-5021 timeouts to the processor.
- 09:02 order-service saga timeouts (ORD-4102); gateway begins returning 504s
  (GW-5030).
- 09:11 on-call opens an incident against **api-gateway**, because the gateway
  was producing the highest error volume.
- 09:24 a second responder follows a trace id backwards and identifies
  payment-service as the origin.
- 09:31 circuit breaker enabled; gateway recovers immediately.
- 09:36 processor latency normalizes.

## Root cause
The processor's p99 latency rose above our 3s timeout. Our own connection pool
was already near saturation, so requests queued behind slow calls and the
effective failure rate was far higher than the processor's.

## Why detection was slow
Nine minutes were lost investigating the wrong service. Error *volume* pointed
at the gateway; error *causality* pointed at payments. Our dashboards ranked by
volume.

## Actions
- Alerting now ranks by earliest-error-in-trace, not error count.
- Pool utilization above 0.9 pages before it becomes timeouts.
- RB-GATEWAY-504 rewritten to say plainly that GW-5030 is a symptom.
""",
    "SVC-payment-service.md": """---
title: "Service: payment-service"
source_type: service_doc
service: payment-service
error_codes: [PAY-5021]
---

# payment-service

Owns authorization, capture, and refund against the external processor
(Stripewise). Java 21 / Spring Boot. Owned by the Payments team.

## Dependencies
- Stripewise API (`api.stripewise.io`) — 3000ms socket timeout, no retry at
  this layer by design (retries live in the saga, which is idempotent).
- Postgres `payments` cluster — HikariCP, max pool 50.

## Key operational properties
- The service is **not** safe to restart blindly during a processor incident:
  in-flight captures are tracked in memory until the write-ahead entry commits.
  Capture a heap dump first.
- Idempotency keys are the client's responsibility. The saga generates one per
  order attempt and reuses it across retries.

## Alerts
`payment_pool_utilization > 0.9` (page), `pay_5021_rate > 5/min` (page),
`reconciliation_backlog > 0 for 30m` (ticket).
""",
    "SVC-order-service.md": """---
title: "Service: order-service"
source_type: service_doc
service: order-service
error_codes: [ORD-4102]
---

# order-service

Orchestrates the checkout saga: reserve inventory, authorize payment, confirm
order, notify. Go 1.24. Owned by the Commerce team.

## Saga semantics
Each step has a compensating action. If `authorize_payment` returns no terminal
response within 3.2s the order stays `PENDING_PAYMENT` and emits ORD-4102 —
deliberately *not* failed, because the payment state is unknown at that point.

The compensate-or-complete job (`bin/saga-drive`) resolves stuck orders after
reconciliation confirms the true payment state.

## Dependencies
inventory-service (reservation), payment-service (authorize/capture),
notification-service (async, non-blocking).
""",
    "SVC-inventory-service.md": """---
title: "Service: inventory-service"
source_type: service_doc
service: inventory-service
error_codes: [INV-3007]
---

# inventory-service

Stock levels and reservations across warehouses. Java 21. Owned by Fulfilment.

## Database
Postgres `inventory` cluster. HikariCP `maximumPoolSize=40`,
`connectionTimeout=10000`. The `inventory_reservations` table is the hot path;
see ADR-0012 for its indexing.

## Known operational hazards
- The supplier bulk import holds long transactions. It is scheduled for 02:00
  UTC; running it during business hours reliably causes INV-3007.
- Reservation reads and writes share one pool. There is no read replica yet.
""",
    "SVC-notification-service.md": """---
title: "Service: notification-service"
source_type: service_doc
service: notification-service
error_codes: [NOTIF-2210]
---

# notification-service

Consumes `order.events` and sends email/SMS. Python 3.12. Owned by Growth.

## Consumer configuration
8 max delivery attempts, then the message should route to
`order.events.dlq`. Partition count 6. A blocked partition affects only the
orders hashed to it, so the customer-visible symptom is "some customers got no
confirmation email", not a total outage.

## Schema handling
Events are Avro, validated against the registry (ADR-0007). `supported_max`
in the failure log is the highest schema version this consumer build can parse.
""",
    "SVC-auth-service.md": """---
title: "Service: auth-service"
source_type: service_doc
service: auth-service
error_codes: [AUTH-1015]
---

# auth-service

Issues and validates JWTs. Rust. Owned by Platform Security. Three nodes behind
the internal load balancer: `auth-2a19`, `auth-7f4c`, `auth-9b03`.

## Token policy
RS256, 15-minute access tokens, `nbf` set to issue time with no leeway.
Validation happens on any node, so issuing and validating nodes usually differ —
which is why node clock drift produces user-visible auth failures.

## Time synchronization
All nodes run chronyd against the internal NTP pool. Offsets above 50ms alert;
above 30s the node should be cordoned automatically (this automation is not yet
deployed, tracked as PLAT-4417).
""",
    "SVC-search-service.md": """---
title: "Service: search-service"
source_type: service_doc
service: search-service
error_codes: [SRCH-6001]
---

# search-service

Product search over a Lucene index. Java 21, 4GiB container limit. Owned by
Discovery.

## Index lifecycle
Incremental updates stream continuously. A full rebuild runs weekly and is the
only memory-intensive operation; see RB-SEARCH-OOM and ADR-0009.

## Degraded mode
If the index is unavailable the gateway serves cached popular queries. Long-tail
queries return empty results rather than an error, so search failures are easy
to miss in error-rate dashboards — watch `search_zero_result_rate` instead.
""",
    "GUIDE-triage.md": """---
title: "Guide: Incident triage at Meridian"
source_type: runbook
service: null
error_codes: []
---

# Incident triage

## Rank by causality, not volume
The service emitting the most errors is usually the furthest downstream. Take a
trace id from any error and find the earliest ERROR across all services for that
trace. That is your starting point. INC-2026-0714-01 cost nine minutes to this
mistake.

## Severity
- **critical** — revenue-affecting or data-integrity risk. Page immediately.
- **high** — customer-visible degradation with a workaround.
- **medium** — partial or delayed functionality, no data risk.
- **low** — internal only.

## Ambiguous failures
Timeouts tell you that you did not hear back, not that nothing happened. Any
recovery path built on a timeout must reconcile against the source of truth
before taking a destructive action. This is the single most expensive lesson in
our post-mortem history (INC-2025-1103).

## Escalation to a human
An automated assistant should escalate rather than answer when: the remediation
is destructive and irreversible, the evidence spans fewer than two independent
sources, or the question touches customer financial state.
""",
}


# --------------------------------------------------------------------------
# Generation entry point
# --------------------------------------------------------------------------


def expansion_catalog() -> list[ErrorCodeEntry]:
    """The 66 expansion faults as catalog rows.

    The catalog is the SQL-backed lookup the error-code tool queries, so it has
    to cover every code that appears in a document — otherwise a question about
    a new service resolves in retrieval but not in the deterministic lookup,
    which is a confusing half-answer.
    """
    from aiops.ingestion.expansion import all_services

    rows: list[ErrorCodeEntry] = []
    for service in all_services():
        for fault in service.faults:
            rows.append(
                ErrorCodeEntry(
                    code=fault.code,
                    service=service.name,
                    title=fault.title,
                    severity=fault.severity,  # type: ignore[arg-type]
                    description=fault.symptom,
                    likely_causes=list(fault.causes),
                    remediation=fault.fix,
                    runbook_ref=f"RB-{fault.code}",
                )
            )
    return rows


def full_catalog() -> list[ErrorCodeEntry]:
    """Every error code in the corpus: the 7 canonical plus the 66 expansion rows.

    This is what callers should use. `ERROR_CATALOG` alone is the canonical
    subset and seeding the database from it leaves the SQL lookup tool unable to
    resolve any expansion code — the runbook is retrievable but the deterministic
    lookup says "unknown code", which is worse than either answer alone.
    """
    return ERROR_CATALOG + expansion_catalog()


def generate_corpus(seed: int = 7) -> dict[str, int]:
    """Write docs, logs, and the error catalog to disk. Idempotent for a given seed."""
    from aiops.ingestion.expansion import render_all

    settings.ensure_dirs()
    rng = random.Random(seed)

    for name, body in DOCUMENTS.items():
        (settings.docs_dir / name).write_text(body, encoding="utf-8")

    expansion_docs = render_all()
    for name, body in expansion_docs.items():
        (settings.docs_dir / name).write_text(body, encoding="utf-8")

    incidents = [
        _payment_cascade(rng, BASE_TIME),
        _inventory_pool(rng, BASE_TIME + timedelta(minutes=42)),
        _notif_poison(rng, BASE_TIME + timedelta(minutes=75)),
        _auth_skew(rng, BASE_TIME + timedelta(minutes=95)),
        _search_oom(rng, BASE_TIME + timedelta(minutes=120)),
    ]

    lines: list[LogLine] = _background_noise(rng, BASE_TIME)
    for inc in incidents:
        lines.extend(inc.lines)
    lines.sort(key=lambda ln: ln.ts)

    log_path = settings.logs_dir / "meridian-platform.log"
    log_path.write_text("\n".join(ln.render() for ln in lines) + "\n", encoding="utf-8")

    catalog = full_catalog()
    catalog_path = settings.data_dir / "error_catalog.json"
    catalog_path.write_text(
        json.dumps([e.model_dump() for e in catalog], indent=2), encoding="utf-8"
    )

    manifest = {
        "documents": len(DOCUMENTS) + len(expansion_docs),
        "canonical_documents": len(DOCUMENTS),
        "expansion_documents": len(expansion_docs),
        "log_lines": len(lines),
        "incidents": len(incidents),
        "error_codes": len(catalog),
    }
    (settings.data_dir / "corpus_manifest.json").write_text(
        json.dumps({**manifest, "incidents_detail": [
            {"id": i.incident_id, "title": i.title, "lines": len(i.lines)} for i in incidents
        ]}, indent=2),
        encoding="utf-8",
    )
    return manifest


def log_path() -> Path:
    return settings.logs_dir / "meridian-platform.log"


if __name__ == "__main__":
    print(json.dumps(generate_corpus(), indent=2))
