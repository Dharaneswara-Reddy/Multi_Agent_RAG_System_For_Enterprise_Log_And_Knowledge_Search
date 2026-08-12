"""Data contracts for the expanded Meridian corpus.

The original 18 documents were written by hand as markdown literals. That does
not scale to 200, and hand-writing 200 near-identical runbooks would have
produced a corpus whose documents differ only in noise — which makes retrieval
*look* hard while actually being trivial (every chunk is equidistant from every
query).

So the expansion is structured instead: the facts live here as typed records,
one per real entity, and `render.py` turns them into markdown. The variation
that matters — a distinct failure mode, a distinct fix, a distinct
anti-pattern — is authored per entity. The variation that does not matter —
heading order, boilerplate phrasing — is templated, exactly as a real
engineering wiki's runbook template would be.

Two properties are deliberate and load-bearing for evaluation:

1. **Near-misses.** Several new services have connection-pool exhaustion,
   upstream-timeout, OOM and poison-message faults. These are genuine
   distractors for the original PAY-5021 / INV-3007 / SRCH-6001 / NOTIF-2210
   questions. If retrieval still finds the right runbook among a dozen
   plausible neighbours, that is a real result rather than an artefact of a
   corpus too small to be confusing.
2. **Cross-references.** Documents cite each other by id, so the multi-hop
   questions in the golden set have an actual path to follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fault:
    """One error code: what it looks like, why it happens, what to do."""

    code: str
    title: str
    severity: str  # low | medium | high | critical
    symptom: str
    causes: tuple[str, ...]
    detection: str
    fix: str
    antipattern: str
    escalation: str = ""
    related: tuple[str, ...] = ()


@dataclass(frozen=True)
class Service:
    name: str
    domain: str
    team: str
    language: str
    purpose: str
    datastore: str
    dependencies: tuple[str, ...]
    hazards: tuple[str, ...]
    alerts: tuple[str, ...]
    slo: str
    faults: tuple[Fault, ...] = field(default_factory=tuple)

    @property
    def doc_id(self) -> str:
        return f"SVC-{self.name}"


@dataclass(frozen=True)
class Decision:
    """An architecture decision record."""

    adr_id: str  # e.g. "ADR-0031"
    title: str
    service: str
    status: str  # Accepted (date) | Proposed (date) | Superseded by ...
    context: str
    options: tuple[tuple[str, str], ...]  # (name, assessment)
    decision: str
    consequences: tuple[str, ...]
    codes: tuple[str, ...] = ()
    slug: str = ""

    @property
    def doc_id(self) -> str:
        return f"{self.adr_id}-{self.slug}"


@dataclass(frozen=True)
class Incident:
    """A post-mortem."""

    incident_id: str  # e.g. "INC-2026-0210-03"
    title: str
    service: str
    impact: str
    timeline: tuple[tuple[str, str], ...]  # (clock, event)
    root_cause: str
    detection_gap: str
    actions: tuple[str, ...]
    lesson: str
    codes: tuple[str, ...] = ()
    slug: str = ""

    @property
    def doc_id(self) -> str:
        return f"PM-{self.incident_id.removeprefix('INC-')}-{self.slug}"


@dataclass(frozen=True)
class Guide:
    """A policy or how-to document that is not tied to one service."""

    slug: str
    title: str
    summary: str
    sections: tuple[tuple[str, str], ...]
    service: str | None = None
    codes: tuple[str, ...] = ()

    @property
    def doc_id(self) -> str:
        return f"GUIDE-{self.slug}"
