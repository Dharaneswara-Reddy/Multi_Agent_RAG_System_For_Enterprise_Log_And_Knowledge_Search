"""End-to-end answer metrics.

These are the tier that answers "given the evidence, was the response
defensible", as distinct from "was the evidence found". The failure mode to
guard against is a metric that looks healthy for the wrong reason — averaging
an abstention's empty answer into coverage, or crediting a system that declines
everything with perfect abstention accuracy.
"""

from __future__ import annotations

from aiops.evaluation.harness import AnswerResult, evaluate_answers, load_golden
from aiops.schemas import Citation, CopilotAnswer, SourceType, Verdict


class _StubCopilot:
    """Returns a scripted answer per question, so metric arithmetic is testable
    without paying for retrieval or synthesis."""

    def __init__(self, scripted: dict[str, CopilotAnswer]) -> None:
        self.scripted = scripted

    def answer(self, question: str, *, record: bool = True) -> CopilotAnswer:
        return self.scripted[question]


def _answer(
    text: str,
    verdict: Verdict = Verdict.ANSWERED,
    refs: list[str] | None = None,
    claim_support: float = 1.0,
    unsupported: list[str] | None = None,
) -> CopilotAnswer:
    return CopilotAnswer(
        question="q",
        answer=text,
        verdict=verdict,
        citations=[
            Citation(ref=r, source_type=SourceType.RUNBOOK, snippet="…") for r in (refs or [])
        ],
        extras={
            "claim_support": claim_support,
            "unsupported_claims": unsupported or [],
        },
    )


def _case(**kw):
    from aiops.evaluation.harness import GoldenCase

    base = dict(id="t-1", question="q", category="test", relevant_docs=["D"])
    base.update(kw)
    return GoldenCase(**base)


# --- keyword coverage -----------------------------------------------------


def test_keyword_coverage_counts_only_mentioned_terms():
    case = _case(must_mention=["status page", "connection pool"])
    stub = _StubCopilot({"q": _answer("Check the status page first.")})
    results, summary = evaluate_answers([case], copilot=stub)
    assert results[0].keyword_coverage == 0.5
    assert summary["keyword_coverage"] == 0.5


def test_keyword_coverage_is_case_insensitive():
    case = _case(must_mention=["Reconcile"])
    stub = _StubCopilot({"q": _answer("You must reconcile before replaying.")})
    results, _ = evaluate_answers([case], copilot=stub)
    assert results[0].keyword_coverage == 1.0


def test_case_without_required_keywords_is_not_penalised():
    case = _case(must_mention=[])
    stub = _StubCopilot({"q": _answer("anything")})
    results, _ = evaluate_answers([case], copilot=stub)
    assert results[0].keyword_coverage == 1.0


def test_coverage_excludes_abstentions_from_the_average():
    """Averaging an escalation's empty answer into coverage would reward abstaining."""
    answered = _case(id="a", question="qa", must_mention=["pool"])
    escalated = _case(id="b", question="qb", must_mention=["pool"], expect_escalation=True,
                      relevant_docs=[])
    stub = _StubCopilot({
        "qa": _answer("check the pool"),
        "qb": _answer("", verdict=Verdict.ESCALATED),
    })
    _, summary = evaluate_answers([answered, escalated], copilot=stub)
    assert summary["keyword_coverage"] == 1.0
    assert summary["answer_rate"] == 0.5


# --- abstention -----------------------------------------------------------


def test_abstention_correct_when_answerable_question_is_answered():
    stub = _StubCopilot({"q": _answer("here you go")})
    results, _ = evaluate_answers([_case()], copilot=stub)
    assert results[0].abstention_correct


def test_abstention_correct_when_out_of_scope_is_escalated():
    case = _case(relevant_docs=[], expect_escalation=True)
    stub = _StubCopilot({"q": _answer("", verdict=Verdict.ESCALATED)})
    results, _ = evaluate_answers([case], copilot=stub)
    assert results[0].abstention_correct


def test_abstention_wrong_when_out_of_scope_is_answered():
    """The failure that matters: confidently answering something uncovered."""
    case = _case(relevant_docs=[], expect_escalation=True)
    stub = _StubCopilot({"q": _answer("the airspeed is 11 m/s")})
    results, summary = evaluate_answers([case], copilot=stub)
    assert not results[0].abstention_correct
    assert summary["abstention_accuracy"] == 0.0


def test_abstention_wrong_when_answerable_question_is_escalated():
    stub = _StubCopilot({"q": _answer("", verdict=Verdict.ESCALATED)})
    results, _ = evaluate_answers([_case()], copilot=stub)
    assert not results[0].abstention_correct


def test_declining_everything_does_not_score_perfectly():
    """A system that abstains on all input must not look safe."""
    answerable = _case(id="a", question="qa")
    negative = _case(id="b", question="qb", relevant_docs=[], expect_escalation=True)
    stub = _StubCopilot({
        "qa": _answer("", verdict=Verdict.ESCALATED),
        "qb": _answer("", verdict=Verdict.ESCALATED),
    })
    _, summary = evaluate_answers([answerable, negative], copilot=stub)
    assert summary["abstention_accuracy"] == 0.5


def test_blocked_verdict_is_correct_only_for_block_cases():
    blocked_case = _case(relevant_docs=[], expect_block=True)
    stub = _StubCopilot({"q": _answer("", verdict=Verdict.BLOCKED)})
    results, _ = evaluate_answers([blocked_case], copilot=stub)
    assert results[0].abstention_correct


# --- grounding ------------------------------------------------------------


def test_claim_support_is_read_from_the_gate_not_recomputed():
    """Recomputing against truncated citation snippets reports false fabrications."""
    stub = _StubCopilot({"q": _answer("uses --batch-size=50000", claim_support=0.5,
                                      unsupported=["--batch-size=50000"])})
    results, summary = evaluate_answers([_case()], copilot=stub)
    assert results[0].claim_support == 0.5
    assert results[0].unsupported_claims == ["--batch-size=50000"]
    assert summary["fabrication_rate"] == 1.0


def test_fabrication_rate_is_zero_when_all_claims_are_supported():
    stub = _StubCopilot({"q": _answer("grounded", claim_support=1.0)})
    _, summary = evaluate_answers([_case()], copilot=stub)
    assert summary["fabrication_rate"] == 0.0


def test_citation_validity_flags_a_ref_not_in_context():
    """A citation to a ref that was never supplied is a leaked guardrail."""
    stub = _StubCopilot({"q": _answer("see [Runbook: X#0] and [Invented: Y#3]", refs=["Runbook: X#0"])})
    results, _ = evaluate_answers([_case()], copilot=stub)
    assert results[0].citation_validity == 0.5


def test_citation_validity_is_one_when_answer_cites_nothing():
    stub = _StubCopilot({"q": _answer("no citations here")})
    results, _ = evaluate_answers([_case()], copilot=stub)
    assert results[0].citation_validity == 1.0


# --- reporting ------------------------------------------------------------


def test_summary_labels_what_measured_keyword_coverage():
    """Offline the answer is extractive, so the number describes the stub."""
    stub = _StubCopilot({"q": _answer("x")})
    _, summary = evaluate_answers([_case()], copilot=stub)
    assert summary["keyword_coverage_measured_on"] in {
        "offline extractive stub",
        "synthesis model",
    }


def test_failures_list_surfaces_actionable_cases_only():
    good = _case(id="a", question="qa")
    bad = _case(id="b", question="qb", relevant_docs=[], expect_escalation=True)
    stub = _StubCopilot({"qa": _answer("fine"), "qb": _answer("answered anyway")})
    _, summary = evaluate_answers([good, bad], copilot=stub)
    assert [f["case_id"] for f in summary["failures"]] == ["b"]


def test_golden_set_loads_and_is_scoreable():
    """Guards the dataclass contract the metrics depend on."""
    cases = load_golden()
    assert cases and all(isinstance(c.must_mention, list) for c in cases)
    assert AnswerResult is not None
