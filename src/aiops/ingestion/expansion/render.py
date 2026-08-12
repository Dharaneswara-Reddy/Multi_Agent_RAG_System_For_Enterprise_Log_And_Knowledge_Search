"""Render the expansion specs into markdown in the existing house style.

The frontmatter schema and heading structure match the original hand-written
documents exactly, because `documents.py` chunks on heading boundaries and
prefixes each chunk with its heading path. A different heading shape would
produce differently-shaped chunks and make the expansion incomparable to the
original corpus in the eval harness.

One deliberate choice: runbooks render one fault per document rather than
grouping a service's faults together. A runbook is the unit an on-call engineer
opens, and it is also the unit the golden set labels as relevant — grouping two
faults into one document would make "which document answers this" ambiguous.
"""

from __future__ import annotations

from aiops.ingestion.expansion.specs import Decision, Fault, Guide, Incident, Service


def _fm(title: str, source_type: str, service: str | None, codes: tuple[str, ...]) -> str:
    """Frontmatter block. Matches the original documents' minimal YAML subset."""
    code_list = ", ".join(codes)
    return (
        "---\n"
        f'title: "{title}"\n'
        f"source_type: {source_type}\n"
        f"service: {service or 'null'}\n"
        f"error_codes: [{code_list}]\n"
        "---\n"
    )


def _bullets(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _numbered(items: tuple[str, ...]) -> str:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))


# ---------------------------------------------------------------------------
# Runbooks
# ---------------------------------------------------------------------------


def render_runbook(service: Service, fault: Fault) -> tuple[str, str]:
    """One runbook per fault. Returns (doc_id, markdown)."""
    doc_id = f"RB-{fault.code}"
    title = f"Runbook: {fault.title} ({fault.code})"
    related = ""
    if fault.related:
        related = (
            "\n## Related\n"
            f"{', '.join(fault.related)}. A shared trace id across these codes usually means one "
            "incident, not several — see the incident triage guide.\n"
        )
    escalation = f"\n## Escalation\n{fault.escalation}\n" if fault.escalation else ""

    body = f"""
# {fault.title}

`{fault.code}` — {service.name}, severity **{fault.severity}**.

## When this fires
{fault.symptom}

## Likely causes
{_bullets(fault.causes)}

## Diagnosis
{fault.detection}

## Remediation
{fault.fix}

## What not to do
{fault.antipattern}
{escalation}{related}"""
    return doc_id, _fm(title, "runbook", service.name, (fault.code,)) + body


# ---------------------------------------------------------------------------
# Service documents
# ---------------------------------------------------------------------------


def render_service(service: Service) -> tuple[str, str]:
    codes = tuple(f.code for f in service.faults)
    title = f"Service: {service.name}"
    fault_lines = tuple(f"`{f.code}` — {f.title} ({f.severity})" for f in service.faults)
    faults_section = ""
    if fault_lines:
        faults_section = f"""
## Error codes
{_bullets(fault_lines)}

Each has its own runbook, named `RB-<code>`.
"""

    body = f"""
# {service.name}

{service.purpose}

Written in {service.language}. Owned by the {service.team} team, in the
{service.domain} domain.

## Storage
{service.datastore}

## Dependencies
{_bullets(service.dependencies)}

## Operational hazards
{_bullets(service.hazards)}

## Alerts
{_bullets(service.alerts)}

## Service level objective
{service.slo}
{faults_section}"""
    return service.doc_id, _fm(title, "service_doc", service.name, codes) + body


# ---------------------------------------------------------------------------
# ADRs
# ---------------------------------------------------------------------------


def render_decision(decision: Decision) -> tuple[str, str]:
    title = f"{decision.adr_id}: {decision.title}"
    options = "\n\n".join(
        f"{i}. **{name}**\n   {assessment}"
        for i, (name, assessment) in enumerate(decision.options, 1)
    )
    body = f"""
# {decision.adr_id}: {decision.title}

**Status:** {decision.status}
**Service:** {decision.service}

## Context
{decision.context}

## Options considered
{options}

## Decision
{decision.decision}

## Consequences
{_bullets(decision.consequences)}
"""
    return decision.doc_id, _fm(title, "adr", decision.service, decision.codes) + body


# ---------------------------------------------------------------------------
# Post-mortems
# ---------------------------------------------------------------------------


def render_incident(incident: Incident) -> tuple[str, str]:
    title = f"Post-mortem: {incident.title} ({incident.incident_id})"
    timeline = "\n".join(f"- **{clock}** {event}" for clock, event in incident.timeline)
    body = f"""
# Post-mortem: {incident.title}

**Incident:** {incident.incident_id}
**Service:** {incident.service}

## Impact
{incident.impact}

## Timeline
{timeline}

## Root cause
{incident.root_cause}

## Detection gap
{incident.detection_gap}

## What we changed
{_bullets(incident.actions)}

## Lesson
{incident.lesson}
"""
    return incident.doc_id, _fm(title, "postmortem", incident.service, incident.codes) + body


# ---------------------------------------------------------------------------
# Guides
# ---------------------------------------------------------------------------


def render_guide(guide: Guide) -> tuple[str, str]:
    title = f"Guide: {guide.title}"
    sections = "\n\n".join(f"## {heading}\n{text}" for heading, text in guide.sections)
    body = f"""
# {guide.title}

{guide.summary}

{sections}
"""
    return guide.doc_id, _fm(title, "runbook", guide.service, guide.codes) + body


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_all() -> dict[str, str]:
    """Every expansion document as {filename: markdown}."""
    from aiops.ingestion.expansion.decisions import DECISIONS
    from aiops.ingestion.expansion.incidents import GUIDES, INCIDENTS
    from aiops.ingestion.expansion.services_core import COMMERCE, FULFILMENT, PAYMENTS
    from aiops.ingestion.expansion.services_platform import ALL_PLATFORM_SERVICES

    services = COMMERCE + PAYMENTS + FULFILMENT + ALL_PLATFORM_SERVICES
    out: dict[str, str] = {}

    for service in services:
        doc_id, markdown = render_service(service)
        out[f"{doc_id}.md"] = markdown
        for fault in service.faults:
            fault_id, fault_md = render_runbook(service, fault)
            out[f"{fault_id}.md"] = fault_md

    for decision in DECISIONS:
        doc_id, markdown = render_decision(decision)
        out[f"{doc_id}.md"] = markdown

    for incident in INCIDENTS:
        doc_id, markdown = render_incident(incident)
        out[f"{doc_id}.md"] = markdown

    for guide in GUIDES:
        doc_id, markdown = render_guide(guide)
        out[f"{doc_id}.md"] = markdown

    return out


def all_services() -> tuple[Service, ...]:
    from aiops.ingestion.expansion.services_core import COMMERCE, FULFILMENT, PAYMENTS
    from aiops.ingestion.expansion.services_platform import ALL_PLATFORM_SERVICES

    return COMMERCE + PAYMENTS + FULFILMENT + ALL_PLATFORM_SERVICES
