"""Integrity tests for the generated expansion corpus.

The expansion is 201 documents produced from typed records, so the failure mode
is not a crash — it is a corpus that renders fine and is quietly inconsistent:
a runbook referencing an error code that no longer exists, two faults sharing a
code, a document whose frontmatter says one service while its body describes
another. Every one of those silently degrades retrieval and none of them raise.

These tests are therefore assertions about the corpus as a body of knowledge,
not about the rendering code.
"""

from __future__ import annotations

import re

import pytest

from aiops.ingestion.corpus import (
    DOCUMENTS,
    ERROR_CATALOG,
    expansion_catalog,
    full_catalog,
)
from aiops.ingestion.expansion import all_services, render_all
from aiops.ingestion.expansion.decisions import DECISIONS
from aiops.ingestion.expansion.incidents import GUIDES, INCIDENTS

CODE_RE = re.compile(r"\b[A-Z]{2,6}-\d{4}\b")
ADR_RE = re.compile(r"\bADR-\d{4}\b")
INC_RE = re.compile(r"\bINC-\d{4}-\d{4}(?:-\d{2})?\b")

# The seven codes from the original hand-written corpus. The expansion is
# allowed to reference these — that cross-linking is deliberate — but must not
# redefine them.
ORIGINAL_CODES = {
    "PAY-5021",
    "ORD-4102",
    "GW-5030",
    "INV-3007",
    "NOTIF-2210",
    "AUTH-1015",
    "SRCH-6001",
}
ORIGINAL_ADRS = {"ADR-0007", "ADR-0009", "ADR-0012"}
ORIGINAL_INCIDENTS = {
    "INC-2025-1103",
    "INC-2026-0714-01",
    "INC-2026-0714-02",
    "INC-2026-0714-03",
    "INC-2026-0714-04",
    "INC-2026-0714-05",
}


@pytest.fixture(scope="module")
def services():
    return all_services()


@pytest.fixture(scope="module")
def faults(services):
    return [f for s in services for f in s.faults]


@pytest.fixture(scope="module")
def docs():
    return render_all()


@pytest.fixture(scope="module")
def blob(docs):
    return "\n".join(docs.values())


def test_error_codes_are_unique(faults):
    codes = [f.code for f in faults]
    duplicates = {c for c in codes if codes.count(c) > 1}
    assert not duplicates, f"duplicate error codes: {sorted(duplicates)}"


def test_expansion_does_not_redefine_original_codes(faults):
    """The original seven codes stay owned by the hand-written runbooks.

    If the expansion defined its own PAY-5021, the golden set's labelled answer
    for the payment-timeout question would become ambiguous and recall would
    drop for reasons that have nothing to do with retrieval quality.
    """
    clashes = {f.code for f in faults} & ORIGINAL_CODES
    assert not clashes, f"expansion redefines original codes: {sorted(clashes)}"


def test_every_fault_has_exactly_one_runbook(faults, docs):
    for fault in faults:
        assert f"RB-{fault.code}.md" in docs, f"no runbook for {fault.code}"


def test_every_document_has_a_unique_filename(docs, services):
    expected = len(services) + sum(len(s.faults) for s in services)
    expected += len(DECISIONS) + len(INCIDENTS) + len(GUIDES)
    assert len(docs) == expected, "a filename collision silently dropped a document"


def test_all_referenced_codes_exist(blob, faults):
    """No document may mention an error code that is defined nowhere."""
    known = {f.code for f in faults} | ORIGINAL_CODES
    mentioned = set(CODE_RE.findall(blob))
    # ADR and incident identifiers match the code shape; exclude them.
    mentioned -= {m for m in mentioned if m.startswith(("ADR-", "INC-"))}
    unknown = mentioned - known
    assert not unknown, f"documents reference undefined codes: {sorted(unknown)}"


def test_all_referenced_adrs_exist(blob):
    known = {d.adr_id for d in DECISIONS} | ORIGINAL_ADRS
    unknown = set(ADR_RE.findall(blob)) - known
    assert not unknown, f"documents reference undefined ADRs: {sorted(unknown)}"


def test_all_referenced_incidents_exist(blob):
    known = {i.incident_id for i in INCIDENTS} | ORIGINAL_INCIDENTS
    unknown = set(INC_RE.findall(blob)) - known
    assert not unknown, f"documents reference undefined incidents: {sorted(unknown)}"


def test_related_codes_resolve(services):
    known = {f.code for s in services for f in s.faults} | ORIGINAL_CODES
    for service in services:
        for fault in service.faults:
            unknown = set(fault.related) - known
            assert not unknown, f"{fault.code} relates to undefined {sorted(unknown)}"


def test_every_document_has_frontmatter(docs):
    for name, body in docs.items():
        assert body.startswith("---\n"), f"{name} missing frontmatter"
        assert "source_type:" in body, f"{name} missing source_type"
        assert "error_codes:" in body, f"{name} missing error_codes"


def test_source_types_are_valid(docs):
    from aiops.schemas import SourceType

    valid = {s.value for s in SourceType}
    for name, body in docs.items():
        declared = re.search(r"^source_type:\s*(\S+)$", body, re.M)
        assert declared, f"{name} has no parseable source_type"
        assert declared.group(1) in valid, f"{name} declares unknown type {declared.group(1)}"


def test_runbook_frontmatter_matches_its_service(services, docs):
    for service in services:
        for fault in service.faults:
            body = docs[f"RB-{fault.code}.md"]
            assert f"service: {service.name}\n" in body
            assert f"error_codes: [{fault.code}]" in body


def test_catalog_covers_every_expansion_code(faults):
    """A code documented in markdown but absent from the SQL catalog produces a
    confusing half-answer: retrieval finds the runbook, the deterministic
    lookup tool reports the code as unknown."""
    catalog_codes = {e.code for e in expansion_catalog()}
    assert catalog_codes == {f.code for f in faults}


def test_catalog_has_no_duplicates_across_original_and_expansion():
    codes = [e.code for e in full_catalog()]
    assert len(codes) == len(set(codes))


def test_seeded_database_contains_every_code(tmp_path):
    """Regression: `seed_error_catalog` defaulted to the 7 canonical entries.

    Every expansion runbook was retrievable while the deterministic lookup tool
    reported its code as unknown — the exact half-answer the catalog exists to
    prevent. Asserting the builder was not enough; this asserts what actually
    reaches the database.
    """
    from aiops.knowledge.catalog import lookup_error_code, seed_error_catalog

    db = tmp_path / "catalog.db"
    seeded = seed_error_catalog(db_path=db)
    assert seeded == len(full_catalog())
    assert seeded > len(ERROR_CATALOG), "seeding fell back to the canonical subset"

    # spot-check one code from each layer resolves through the real lookup path
    for code in ("PAY-5021", "MET-6601", "LED-1001"):
        assert lookup_error_code(code, db_path=db) is not None, f"{code} not resolvable"


def test_catalog_runbook_refs_point_at_real_documents(docs):
    for entry in expansion_catalog():
        assert f"{entry.runbook_ref}.md" in docs


def test_every_catalog_runbook_ref_resolves_including_canonical(docs):
    """No catalog row may cite a runbook that was never written.

    Regression: ORD-4102 pointed at RB-ORDER-SAGA, which did not exist. The SQL
    lookup tool would answer a question about stuck orders by telling an
    engineer to consult a document that is not there — worse than saying
    nothing, because it reads as authoritative.
    """
    available = set(docs) | set(DOCUMENTS)
    dangling = [
        (e.code, e.runbook_ref)
        for e in full_catalog()
        if e.runbook_ref and f"{e.runbook_ref}.md" not in available
    ]
    assert not dangling, f"catalog cites missing runbooks: {dangling}"


def test_expansion_does_not_collide_with_canonical_documents(docs):
    """The 18 hand-written documents must survive corpus generation intact."""
    assert not set(docs) & set(DOCUMENTS)


def test_every_runbook_states_what_not_to_do(services, docs):
    """House standard from GUIDE-runbook-standards: the intuitive wrong answer
    is the highest-value part of a runbook, so it is structurally required."""
    for service in services:
        for fault in service.faults:
            body = docs[f"RB-{fault.code}.md"]
            assert "## What not to do" in body, f"RB-{fault.code} has no anti-pattern section"
            assert len(fault.antipattern) > 60, f"{fault.code} anti-pattern is too thin to be useful"


def test_documents_carry_substantive_content(docs):
    for name, body in docs.items():
        _, _, content = body.partition("---\n\n")
        assert len(content.split()) > 60, f"{name} is too short to be a useful retrieval unit"


def test_incidents_record_a_detection_gap():
    """The detection gap is the section that generalises across services, so an
    empty one makes the post-mortem far less useful for causal questions."""
    for incident in INCIDENTS:
        assert len(incident.detection_gap.split()) > 15, f"{incident.incident_id} detection gap too thin"
        assert incident.timeline, f"{incident.incident_id} has no timeline"


def test_decisions_consider_a_real_alternative():
    """An ADR whose options are all straw men teaches nothing about 'why'.

    The word floor is deliberately low. It exists to catch a dismissal with no
    reasoning attached ("Rejected.", "Too slow."), not to tax terse writing —
    "The status quo, which failed exactly as described" is a complete
    assessment in eight words.
    """
    for decision in DECISIONS:
        assert len(decision.options) >= 2, f"{decision.adr_id} has fewer than two options"
        for name, assessment in decision.options:
            assert len(assessment.split()) > 6, f"{decision.adr_id} option {name!r} is not assessed"
