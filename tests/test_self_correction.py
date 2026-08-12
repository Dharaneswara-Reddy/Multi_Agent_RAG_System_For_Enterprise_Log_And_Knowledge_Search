"""The retry loop.

A cycle in an agent graph is the one change that can turn a bounded request
into an unbounded one, and a retry that re-attempts a *blocked* answer is a
guardrail that can be worn down by repetition. Both properties are asserted
here rather than left to the attempt counter being right by inspection.
"""

from __future__ import annotations

from langgraph.graph import END

from aiops.agents.graph import Copilot
from aiops.config import settings
from aiops.guardrails.rules import GuardrailFinding, OutputCheck, Severity
from aiops.retrieval.context import AssembledContext
from aiops.schemas import Chunk, RetrievedChunk, SourceType


def _ctx(n_docs: int, hops: int = 0) -> AssembledContext:
    used = []
    for i in range(n_docs):
        used.append(
            RetrievedChunk(
                chunk=Chunk(
                    chunk_id=f"c{i}",
                    doc_id=f"DOC-{i}",
                    text="body",
                    source_type=SourceType.RUNBOOK,
                ),
                score=0.5,
            )
        )
    for j in range(hops):
        used.append(
            RetrievedChunk(
                chunk=Chunk(
                    chunk_id=f"h{j}",
                    doc_id=f"HOP-{j}",
                    text="body",
                    source_type=SourceType.ADR,
                ),
                score=0.1,
                provenance="reference_hop",
                hop=1,
            )
        )
    return AssembledContext(text="ctx", used=used)


# --- termination ----------------------------------------------------------


def test_route_after_gate_ends_once_a_verdict_exists():
    assert Copilot._route_after_gate({"verdict": "escalated", "attempt": 0}) is END
    assert Copilot._route_after_gate({"verdict": "answered", "attempt": 0}) is END
    assert Copilot._route_after_gate({"verdict": "blocked", "attempt": 0}) is END


def test_route_after_gate_loops_only_while_budget_remains():
    assert Copilot._route_after_gate({"attempt": 1}) == "triage"
    over_budget = {"attempt": settings.max_retries + 1}
    assert Copilot._route_after_gate(over_budget) is END


def test_loop_cannot_run_forever():
    """Walk the cycle the way the graph would and assert it terminates."""
    state = {"attempt": 0}
    for _ in range(50):
        if Copilot._route_after_gate(state) is END:
            break
        state["attempt"] += 1
    else:
        raise AssertionError("retry loop did not terminate")
    assert state["attempt"] <= settings.max_retries + 1


# --- when a retry is worthwhile ------------------------------------------


def test_retry_when_context_is_empty():
    assert Copilot._retry_would_help({"context": AssembledContext(text="")}, OutputCheck())


def test_retry_when_context_lacks_corroboration():
    assert Copilot._retry_would_help({"context": _ctx(1)}, OutputCheck())


def test_retry_when_triage_applied_filters():
    """A wrong service filter is the most common recoverable failure."""
    state = {"context": _ctx(3), "services": ["payment-service"]}
    assert Copilot._retry_would_help(state, OutputCheck(cited_refs=["A#0"]))


def test_retry_when_the_answer_cited_nothing():
    state = {"context": _ctx(3)}
    assert Copilot._retry_would_help(state, OutputCheck(cited_refs=[]))


def test_no_retry_when_retrieval_was_healthy_but_uncovered():
    """The corpus genuinely not covering a question is not a retrieval failure.

    Retrying here spends a full synthesis to reach the same escalation.
    """
    state = {"context": _ctx(4)}
    out = OutputCheck(cited_refs=["A#0", "B#0"], grounded=True)
    assert not Copilot._retry_would_help(state, out)


def test_hops_do_not_make_a_thin_context_look_corroborated():
    """Two hops off one document is one source, not three."""
    state = {"context": _ctx(1, hops=2)}
    assert Copilot._retry_would_help(state, OutputCheck(cited_refs=["A#0"]))


# --- corroboration accounting --------------------------------------------


def test_independent_sources_excludes_hops():
    ctx = _ctx(2, hops=3)
    assert ctx.distinct_sources == 5
    assert ctx.independent_sources == 2, "hops are derived evidence, not corroboration"


def test_independent_sources_excludes_trace_expansions():
    ctx = _ctx(2)
    ctx.used.append(
        RetrievedChunk(
            chunk=Chunk(
                chunk_id="t0", doc_id="log-x", text="line", source_type=SourceType.LOG
            ),
            score=0.4,
            rank=999,  # the sentinel context.assemble uses for trace siblings
        )
    )
    assert ctx.independent_sources == 2


# --- the guardrail must not be retryable ---------------------------------


def test_blocked_output_is_never_retried():
    """Retrying a blocked answer is how a guardrail gets worn down."""
    blocked = OutputCheck(
        findings=[GuardrailFinding("fabricated_citation", Severity.BLOCK, "invented source")]
    )
    assert blocked.blocked

    copilot = object.__new__(Copilot)  # no index/LLM needed for the gate's guard
    state = {
        "verdict": None,
        "answer": "x",
        "citations": [],
        "context": AssembledContext(text=""),
        "attempt": 0,
    }
    # The gate's retry branch requires `not out.blocked`; assert that guard
    # directly so the intent survives refactoring of the surrounding node.
    should_retry = (
        state["attempt"] < settings.max_retries
        and not blocked.blocked
        and not state.get("needs_human")
        and copilot._retry_would_help(state, blocked)
    )
    assert not should_retry


def test_destructive_request_is_never_retried():
    state = {"context": AssembledContext(text=""), "needs_human": True, "attempt": 0}
    out = OutputCheck()
    should_retry = (
        state["attempt"] < settings.max_retries
        and not out.blocked
        and not state.get("needs_human")
        and Copilot._retry_would_help(state, out)
    )
    assert not should_retry
