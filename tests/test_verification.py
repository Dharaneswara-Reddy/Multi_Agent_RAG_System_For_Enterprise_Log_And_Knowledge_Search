"""Per-claim citation verification.

Both directions are tested. A verifier that flags everything would pass a
one-sided suite and would be switched off within a week of anyone using it, so
the false-positive cases below carry as much weight as the detection cases.
"""

from __future__ import annotations

from aiops.guardrails.rules import check_output, score_confidence
from aiops.guardrails.verify import extract_atoms, verify_claims

CONTEXT = """
<source ref="Runbook: Search rebuild OOM#1" type="runbook">
Run the rebuild with --batch-size=50000 and raise the container limit to 8GiB.
The pod is OOMKilled past the 4GiB limit. Error code SRCH-6001. See ADR-0009.
Run bin/reconcile-charges before replaying any order.
</source>
"""


# --- atom extraction ------------------------------------------------------


def test_extracts_each_atom_kind():
    kinds = dict(
        (atom, kind)
        for kind, atom in extract_atoms(
            "SRCH-6001 needs --batch-size=50000 and bin/reconcile-charges at 8GiB"
        )
    )
    assert kinds["SRCH-6001"] == "identifier"
    assert kinds["--batch-size=50000"] == "flag"
    assert kinds["bin/reconcile-charges"] == "command"
    assert kinds["8GiB"] == "quantity"


def test_atom_extraction_deduplicates():
    atoms = extract_atoms("SRCH-6001 and again SRCH-6001")
    assert [a for _k, a in atoms].count("SRCH-6001") == 1


# --- detection ------------------------------------------------------------


def test_fabricated_flag_value_is_caught():
    """The canonical failure: correct citation, invented specific."""
    check = verify_claims("Run the rebuild with --batch-size=99999.", CONTEXT)
    assert check.has_fabrication
    assert any(atom == "--batch-size=99999" for _kind, atom in check.unsupported)


def test_fabricated_error_code_is_caught():
    check = verify_claims("This is caused by SRCH-9999.", CONTEXT)
    assert check.has_fabrication


def test_fabricated_quantity_is_caught():
    check = verify_claims("Raise the container limit to 64GiB.", CONTEXT)
    assert check.has_fabrication
    assert check.support_ratio < 1.0


def test_unsupported_sentence_is_reported_for_review():
    check = verify_claims("Set --batch-size=12345 immediately.", CONTEXT)
    assert check.unsupported_sentences
    assert "12345" in check.unsupported_sentences[0]


# --- no false positives ---------------------------------------------------


def test_supported_specifics_pass():
    check = verify_claims(
        "Run with --batch-size=50000 and raise the limit to 8GiB. See SRCH-6001.", CONTEXT
    )
    assert not check.has_fabrication
    assert check.support_ratio == 1.0


def test_prose_without_specifics_is_not_penalised():
    """An answer can be entirely correct and contain nothing exact."""
    check = verify_claims(
        "Stop the rebuild job so it does not crash-loop, then page the Discovery on-call.",
        CONTEXT,
    )
    assert check.total_atoms == 0
    assert check.support_ratio == 1.0
    assert not check.has_fabrication


def test_paraphrase_is_not_flagged():
    check = verify_claims(
        "The container runs out of memory and the pod is killed during the merge.", CONTEXT
    )
    assert not check.has_fabrication


def test_hedged_statements_are_not_treated_as_claims():
    """'It might be around 64GiB' is not an assertion about the corpus."""
    check = verify_claims("The limit might be 64GiB on some clusters.", CONTEXT)
    assert not check.has_fabrication


def test_config_key_compares_on_the_key_not_the_value():
    """Recommending a different value for a documented setting is judgement, not fabrication."""
    context = "<source ref='x#0'>Set cart.anon_ttl_hours=72 normally.</source>"
    check = verify_claims("Reduce cart.anon_ttl_hours=24 during the incident.", context)
    assert not check.has_fabrication


def test_empty_answer_is_neutral():
    check = verify_claims("", CONTEXT)
    assert check.total_atoms == 0 and check.support_ratio == 1.0


# --- integration with the gate -------------------------------------------


def test_check_output_reports_unsupported_claims():
    out = check_output(
        "Run --batch-size=99999 [Runbook: Search rebuild OOM#1].",
        ["Runbook: Search rebuild OOM#1"],
        context_text=CONTEXT,
    )
    assert out.claim_support < 1.0
    assert any(f.rule == "unsupported_claim" for f in out.findings)
    assert not out.blocked, "a WARN must not block; it should cost confidence instead"


def test_check_output_without_context_stays_backward_compatible():
    out = check_output("Anything [X#0].", ["X#0"])
    assert out.claim_support == 1.0
    assert not out.unsupported_claims


def test_unsupported_claims_reduce_confidence():
    """The point of the whole mechanism: fabrication must move the number."""
    grounded = check_output(
        "Run --batch-size=50000 [R#1].", ["R#1"], context_text=CONTEXT
    )
    fabricated = check_output(
        "Run --batch-size=99999 [R#1].", ["R#1"], context_text=CONTEXT
    )
    kwargs = dict(top_dense=0.85, distinct_sources=3, context_empty=False)
    assert score_confidence(output=fabricated, **kwargs) < score_confidence(
        output=grounded, **kwargs
    )
