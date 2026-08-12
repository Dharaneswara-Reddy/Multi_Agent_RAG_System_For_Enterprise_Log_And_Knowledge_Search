"""The multi-agent graph: a LangGraph supervisor over two specialist agents.

Topology (supervisor pattern):

    triage ──┬── knowledge_agent ──┐
             ├── log_analyst ──────┼── synthesize ── guardrail_gate ── END
             └── (both, hybrid)  ──┘

Three deliberate choices:

- **Two agents, not five.** Error-code mapping is a SQL join and lives as a tool
  called inside the graph, not as an agent. An agent earns its place by having a
  distinct *reasoning* job; a lookup does not, and paying a model round-trip for
  a deterministic join is cost with no accuracy.
- **The supervisor routes; it does not answer.** Triage runs on the cheap model
  and emits structured output. Synthesis runs on the reasoning model. That split
  is where most of the cost/quality trade-off in this system lives.
- **Guardrails are a graph node, not a wrapper.** The gate can see everything the
  agents saw, so escalation decisions are made with the retrieval evidence in
  hand rather than on the answer text alone.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from aiops.agents import prompts
from aiops.config import settings
from aiops.guardrails import rules as gr
from aiops.knowledge import catalog
from aiops.llm import LLMClient, UsageLedger
from aiops.observability import tracing as tr
from aiops.retrieval.context import AssembledContext, assemble, format_error_entries
from aiops.retrieval.index import HybridIndex, get_index
from aiops.schemas import AgentStep, Citation, CopilotAnswer, Verdict


def _merge_steps(left: list[AgentStep], right: list[AgentStep]) -> list[AgentStep]:
    return (left or []) + (right or [])


def _merge_notes(left: list[str], right: list[str]) -> list[str]:
    return (left or []) + (right or [])


class CopilotState(TypedDict, total=False):
    # inputs
    question: str
    raw_question: str
    # triage output
    route: str
    error_codes: list[str]
    services: list[str]
    search_query: str
    needs_human: bool
    # agent outputs
    log_findings: str
    doc_findings: str
    catalog_text: str
    context: AssembledContext
    citations: list[Citation]
    # final
    answer: str
    confidence: float
    verdict: str
    # self-correction: which attempt this is, and why the previous one failed
    attempt: int
    retry_reason: str
    # accumulated
    steps: Annotated[list[AgentStep], _merge_steps]
    guardrail_notes: Annotated[list[str], _merge_notes]


class Copilot:
    """Owns the compiled graph plus its dependencies (index, llm, catalog)."""

    def __init__(self, index: HybridIndex | None = None, llm: LLMClient | None = None) -> None:
        from aiops.offline import resolve_llm

        self.index = index or get_index()
        # Falls back to a deterministic extractive stand-in when no credential is
        # reachable, so the graph, guardrails, and audit trail stay testable in CI.
        self.llm = llm or resolve_llm()
        self.offline = getattr(self.llm, "offline", False)
        self.graph = self._build()

    # -- nodes -------------------------------------------------------------

    def _triage(self, state: CopilotState) -> dict[str, Any]:
        question = state["question"]
        with tr.span("agent.triage", **{tr.GEN_AI_AGENT_NAME: "triage"}):
            try:
                data, result = self.llm.complete_json(
                    f"Question: {question}",
                    prompts.TRIAGE_SCHEMA,
                    system=prompts.TRIAGE_SYSTEM,
                    route="cheap",
                    agent="triage",
                )
            except Exception as exc:
                # Routing must degrade, not fail. Hybrid is the safe default: it
                # costs more but never starves the synthesiser of evidence.
                return {
                    "route": "hybrid",
                    "error_codes": _extract_codes(question),
                    "services": [],
                    "search_query": question,
                    "needs_human": False,
                    "guardrail_notes": [f"[info] triage_fallback: {type(exc).__name__}"],
                    "steps": [AgentStep(agent="triage", summary=f"fallback to hybrid ({exc})")],
                }

        route = data.get("route") or "hybrid"
        codes = [c.upper() for c in (data.get("error_codes") or [])] or _extract_codes(question)
        step = AgentStep(
            agent="triage",
            summary=f"route={route} codes={codes or '-'} · {data.get('reasoning', '')}",
            **result.as_step_fields(),
        )
        self._ledger.add(result)
        return {
            "route": route,
            "error_codes": codes,
            "services": data.get("services") or [],
            "search_query": data.get("search_query") or question,
            "needs_human": bool(data.get("needs_human")),
            "steps": [step],
        }

    def _retrieve(self, state: CopilotState, source_types: list[str] | None) -> AssembledContext:
        """Full pipeline: multi-query -> fuse -> rerank -> hop -> assemble.

        On a retry attempt the query is deliberately broadened: the service and
        source-type filters are dropped and the raw question is used instead of
        triage's rewrite. The most common reason a first attempt finds nothing
        is that triage guessed the wrong service or over-narrowed the query, so
        repeating the same constrained search would fail the same way.
        """
        from aiops.retrieval.pipeline import retrieve as run_pipeline

        attempt = state.get("attempt", 0)
        broadened = attempt > 0

        hits, rtrace = run_pipeline(
            state["question"] if broadened else (state.get("search_query") or state["question"]),
            self.index,
            top_k=settings.top_k,
            services=None if broadened else (state.get("services") or None),
            source_types=None if broadened else source_types,
        )
        ctx = assemble(hits, self.index)
        ctx.retrieval_stages = rtrace.stages
        ctx.query_variants = rtrace.variants
        ctx.reference_hops = rtrace.hops
        return ctx

    def _knowledge_agent(self, state: CopilotState) -> dict[str, Any]:
        with tr.span("agent.knowledge", **{tr.GEN_AI_AGENT_NAME: "knowledge"}) as sp:
            ctx = self._retrieve(state, ["runbook", "adr", "postmortem", "service_doc"])
            sp.set_attribute(tr.AIOPS_RETRIEVED_K, len(ctx.used))
            sp.set_attribute(tr.AIOPS_RETRIEVAL_TOP_SCORE, round(ctx.top_score, 4))
            if ctx.is_empty:
                return {
                    "doc_findings": "No documentation matched this question.",
                    "citations": [],
                    "context": ctx,
                    "steps": [AgentStep(agent="knowledge", summary="no documentation matched")],
                }
            prompt = (
                f"<question>{state['question']}</question>\n\n"
                f"<documentation>\n{ctx.text}\n</documentation>"
            )
            result = self.llm.complete(
                prompt, system=prompts.KNOWLEDGE_SYSTEM, route="reasoning",
                effort="low", agent="knowledge", max_tokens=2500,
            )
            self._ledger.add(result)
            return {
                "doc_findings": result.text,
                "citations": ctx.citations,
                "context": ctx,
                "steps": [
                    AgentStep(
                        agent="knowledge",
                        summary=f"{len(ctx.used)} sources, {ctx.distinct_sources} distinct docs",
                        **result.as_step_fields(),
                    )
                ],
            }

    def _log_analyst(self, state: CopilotState) -> dict[str, Any]:
        with tr.span("agent.log_analyst", **{tr.GEN_AI_AGENT_NAME: "log_analyst"}) as sp:
            ctx = self._retrieve(state, ["log"])
            sp.set_attribute(tr.AIOPS_RETRIEVED_K, len(ctx.used))
            sp.set_attribute(tr.AIOPS_RETRIEVAL_TOP_SCORE, round(ctx.top_score, 4))
            if ctx.is_empty:
                return {
                    "log_findings": "No log evidence matched this question.",
                    "context": ctx,
                    "steps": [AgentStep(agent="log_analyst", summary="no log evidence matched")],
                }
            prompt = (
                f"<question>{state['question']}</question>\n\n"
                f"<logs>\n{ctx.text}\n</logs>"
            )
            result = self.llm.complete(
                prompt, system=prompts.LOG_ANALYST_SYSTEM, route="reasoning",
                effort="medium", agent="log_analyst", max_tokens=2500,
            )
            self._ledger.add(result)
            existing = list(state.get("citations") or [])
            return {
                "log_findings": result.text,
                "citations": existing + ctx.citations,
                "context": ctx if not state.get("context") else state["context"],
                "steps": [
                    AgentStep(
                        agent="log_analyst",
                        summary=(
                            f"{len(ctx.used)} log windows, "
                            f"{ctx.trace_expansions} pulled in by trace correlation"
                        ),
                        **result.as_step_fields(),
                    )
                ],
            }

    def _both(self, state: CopilotState) -> dict[str, Any]:
        """Hybrid route: documentation and logs, then merge citations."""
        docs = self._knowledge_agent(state)
        merged_state = dict(state)
        merged_state["citations"] = docs.get("citations", [])
        merged_state["context"] = None  # let the log agent report its own retrieval
        logs = self._log_analyst(merged_state)  # type: ignore[arg-type]

        # Confidence reads `context.top_dense`, so carry forward whichever side
        # actually found the stronger evidence rather than defaulting to docs.
        doc_ctx, log_ctx = docs.get("context"), logs.get("context")
        best = max(
            [c for c in (doc_ctx, log_ctx) if c is not None],
            key=lambda c: c.top_dense,
            default=None,
        )
        return {
            "doc_findings": docs.get("doc_findings", ""),
            "log_findings": logs.get("log_findings", ""),
            "citations": logs.get("citations", docs.get("citations", [])),
            "context": best,
            "steps": docs.get("steps", []) + logs.get("steps", []),
        }

    def _lookup_codes(self, state: CopilotState) -> dict[str, Any]:
        """Deterministic tool call — no model involved."""
        codes = state.get("error_codes") or []
        if not codes:
            return {"catalog_text": ""}
        with tr.span("tool.error_catalog", codes=",".join(codes)):
            entries = catalog.lookup_many(codes)
        if not entries:
            return {
                "catalog_text": "",
                "steps": [AgentStep(agent="error_catalog", summary=f"no rows for {codes}")],
            }
        return {
            "catalog_text": format_error_entries(entries),
            "steps": [
                AgentStep(
                    agent="error_catalog",
                    summary=f"resolved {len(entries)}/{len(codes)}: {', '.join(e.code for e in entries)}",
                )
            ],
        }

    def _synthesize(self, state: CopilotState) -> dict[str, Any]:
        parts = [f"<question>{state['question']}</question>"]
        if state.get("log_findings"):
            parts.append(f"<log_analysis>\n{state['log_findings']}\n</log_analysis>")
        if state.get("doc_findings"):
            parts.append(f"<documentation_findings>\n{state['doc_findings']}\n</documentation_findings>")
        if state.get("catalog_text"):
            parts.append(f"<error_catalog>\n{state['catalog_text']}\n</error_catalog>")

        with tr.span("agent.synthesize", **{tr.GEN_AI_AGENT_NAME: "synthesizer"}):
            result = self.llm.complete(
                "\n\n".join(parts),
                system=prompts.SYNTHESIS_SYSTEM,
                route="reasoning",
                effort=settings.effort,
                agent="synthesizer",
            )
            self._ledger.add(result)
        if result.refused:
            return {
                "answer": "The request was declined by the model's safety policy.",
                "verdict": Verdict.BLOCKED.value,
                "guardrail_notes": ["[block] model_refusal: safety policy declined the request"],
                "steps": [AgentStep(agent="synthesizer", summary="refused", **result.as_step_fields())],
            }
        return {
            "answer": result.text,
            "steps": [
                AgentStep(agent="synthesizer", summary="final answer drafted", **result.as_step_fields())
            ],
        }

    @staticmethod
    def _route_after_gate(state: CopilotState) -> str:
        """Loop back to triage on a retry, otherwise finish.

        The gate signals a retry by returning no verdict. Any terminal verdict
        ends the run, so a state that somehow reaches here without one still
        terminates via the attempt ceiling rather than spinning.
        """
        if state.get("verdict"):
            return END
        return "triage" if state.get("attempt", 0) <= settings.max_retries else END

    @staticmethod
    def _retry_would_help(state: CopilotState, out: gr.OutputCheck) -> bool:
        """Is this failure the kind a broader query could fix?

        Retrying costs a full synthesis, so it is gated on the failure having a
        plausible retrieval cause. Two signals qualify:

        - the context was thin or empty, which a narrowed filter commonly causes;
        - the question named services or codes that triage filtered on, so
          dropping those filters materially changes the search.

        A well-retrieved question that simply is not covered by the corpus does
        not qualify — the honest response there is to escalate, and retrying
        would only spend money to reach the same conclusion.
        """
        ctx = state.get("context")
        if ctx is None or ctx.is_empty:
            return True
        # independent_sources, not distinct_sources: hop and trace expansions are
        # derived from what was retrieved, so counting them here would let the
        # pipeline's own expansion talk it out of retrying a thin result.
        if ctx.independent_sources < 2:
            return True
        if state.get("services") or state.get("error_codes"):
            return True
        # Answer cited nothing usable despite a populated context: worth one
        # broader attempt.
        return not out.cited_refs

    def _guardrail_gate(self, state: CopilotState) -> dict[str, Any]:
        if state.get("verdict") == Verdict.BLOCKED.value:
            return {"confidence": 0.0}

        answer = state.get("answer", "")
        citations = state.get("citations") or []
        allowed = [c.ref for c in citations]
        ctx = state.get("context")

        with tr.span("guardrail.output") as sp:
            # Passing the context text switches on per-claim verification: not
            # just "are these citations real" but "does the context actually
            # contain the specifics this answer asserts".
            out = gr.check_output(answer, allowed, context_text=ctx.text if ctx else None)
            # Only retrieval-reached documents count as corroboration; hop and
            # trace expansions are derived from those and are not independent.
            independent = ctx.independent_sources if ctx else 0
            confidence = gr.score_confidence(
                top_dense=ctx.top_dense if ctx else 0.0,
                distinct_sources=independent,
                output=out,
                context_empty=not citations,
            )
            decision = gr.decide_escalation(confidence, out, distinct_sources=independent)
            if state.get("needs_human") and not decision.escalate:
                decision = gr.EscalationDecision(
                    True, "question requests a destructive action", confidence
                )
            sp.set_attribute(tr.AIOPS_CONFIDENCE, confidence)
            sp.set_attribute(tr.AIOPS_VERDICT, Verdict.ESCALATED.value if decision.escalate else Verdict.ANSWERED.value)

        notes = [str(f) for f in out.findings]
        attempt = state.get("attempt", 0)

        # Self-correction. A weak answer is retried once with a broadened query
        # before it is handed to a human, because the most common cause of a
        # weak first attempt is triage over-narrowing (wrong service filter, an
        # over-specific rewritten query) rather than the corpus lacking the
        # answer.
        #
        # Two things keep this from being a loop that papers over real failure:
        # the attempt budget is hard, and a retry is only allowed when the
        # failure looks *recoverable*. A blocked output or an injection attempt
        # is not retried — retrying a blocked answer is how a guardrail gets
        # worn down by repetition.
        if (
            decision.escalate
            and attempt < settings.max_retries
            and not out.blocked
            and not state.get("needs_human")
            and self._retry_would_help(state, out)
        ):
            sp.set_attribute(tr.AIOPS_VERDICT, "retry")
            return {
                "attempt": attempt + 1,
                "retry_reason": decision.reason,
                "guardrail_notes": [
                    f"[info] attempt {attempt + 1}: retrying with a broadened query "
                    f"({decision.reason})"
                ],
                "steps": [
                    AgentStep(
                        agent="self-correction",
                        summary=f"attempt {attempt} weak ({decision.reason}); broadening query",
                    )
                ],
            }

        if decision.escalate:
            notes.append(f"[warn] escalated: {decision.reason}")
            if attempt:
                notes.append(f"[info] escalated after {attempt + 1} attempts")

        # Keep only citations the answer actually used, so the UI shows evidence
        # rather than everything retrieved.
        used_refs = set(out.cited_refs)
        shown = [c for c in citations if c.ref in used_refs] or citations[:5]

        return {
            "confidence": confidence,
            "verdict": (Verdict.ESCALATED if decision.escalate else Verdict.ANSWERED).value,
            "citations": shown,
            "guardrail_notes": notes,
            "steps": [
                AgentStep(
                    agent="guardrails",
                    summary=(
                        f"confidence={confidence:.2f} grounded={out.grounded} "
                        f"cited={len(out.cited_refs)} findings={len(out.findings)}"
                    ),
                )
            ],
        }

    # -- graph -------------------------------------------------------------

    def _route_edge(self, state: CopilotState) -> Literal["knowledge", "logs", "both"]:
        route = state.get("route", "hybrid")
        if route == "knowledge":
            return "knowledge"
        if route == "logs":
            return "logs"
        return "both"

    def _build(self):
        g = StateGraph(CopilotState)
        g.add_node("triage", self._triage)
        g.add_node("knowledge", self._knowledge_agent)
        g.add_node("logs", self._log_analyst)
        g.add_node("both", self._both)
        g.add_node("catalog", self._lookup_codes)
        g.add_node("synthesize", self._synthesize)
        g.add_node("gate", self._guardrail_gate)

        g.add_edge(START, "triage")
        g.add_conditional_edges(
            "triage",
            self._route_edge,
            {"knowledge": "knowledge", "logs": "logs", "both": "both"},
        )
        for node in ("knowledge", "logs", "both"):
            g.add_edge(node, "catalog")
        g.add_edge("catalog", "synthesize")
        g.add_edge("synthesize", "gate")
        # The one cycle in the graph. `_route_after_gate` returns "triage" only
        # while the attempt budget allows, so termination is guaranteed by the
        # counter rather than by hoping the model stops asking.
        g.add_conditional_edges(
            "gate",
            self._route_after_gate,
            {"triage": "triage", END: END},
        )
        return g.compile()

    # -- entry point -------------------------------------------------------

    def answer(self, question: str, *, record: bool = True) -> CopilotAnswer:
        self._ledger = UsageLedger()
        started = time.perf_counter()

        with tr.span("copilot.answer", question_chars=len(question)) as root:
            trace_id = tr.current_trace_id()

            check = gr.check_input(question)
            if check.blocked:
                reasons = [str(f) for f in check.findings]
                root.set_attribute(tr.AIOPS_VERDICT, Verdict.BLOCKED.value)
                ans = CopilotAnswer(
                    question=question,
                    answer=(
                        "This request was blocked before reaching the model. "
                        + "; ".join(f.detail for f in check.findings if f.severity is gr.Severity.BLOCK)
                    ),
                    verdict=Verdict.BLOCKED,
                    guardrail_notes=reasons,
                    trace_id=trace_id,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
                if record:
                    catalog.record_audit(ans)
                return ans

            initial: CopilotState = {
                "question": check.text,
                "raw_question": question,
                "steps": [],
                "guardrail_notes": [str(f) for f in check.findings],
                "citations": [],
            }
            final = self.graph.invoke(initial)

            latency = int((time.perf_counter() - started) * 1000)
            root.set_attribute(tr.AIOPS_ROUTE, final.get("route", ""))
            root.set_attribute(tr.AIOPS_CONFIDENCE, final.get("confidence", 0.0))
            root.set_attribute(tr.AIOPS_VERDICT, final.get("verdict", ""))
            root.set_attribute(tr.AIOPS_COST_USD, round(self._ledger.cost_usd, 6))

            ans = CopilotAnswer(
                question=question,
                answer=final.get("answer", ""),
                citations=final.get("citations", []),
                confidence=final.get("confidence", 0.0),
                verdict=Verdict(final.get("verdict", Verdict.ANSWERED.value)),
                route=final.get("route", ""),
                steps=final.get("steps", []),
                guardrail_notes=final.get("guardrail_notes", []),
                cost_usd=round(self._ledger.cost_usd, 6),
                latency_ms=latency,
                trace_id=trace_id,
                extras={
                    "llm_calls": self._ledger.calls,
                    "cost_by_route": {k: round(v, 6) for k, v in self._ledger.by_route.items()},
                    "error_codes": final.get("error_codes", []),
                    "services": final.get("services", []),
                },
            )

        if record:
            audit_id = catalog.record_audit(ans)
            if ans.verdict is Verdict.ESCALATED:
                reason = next(
                    (n for n in ans.guardrail_notes if "escalated:" in n), "low confidence"
                )
                catalog.create_escalation(
                    question=question,
                    draft=ans.answer,
                    reason=reason,
                    confidence=ans.confidence,
                    audit_id=audit_id,
                )
        return ans


def _extract_codes(text: str) -> list[str]:
    import re

    return sorted(set(re.findall(r"\b[A-Z]{2,6}-\d{3,5}\b", text.upper())))


_COPILOT: Copilot | None = None


def get_copilot() -> Copilot:
    global _COPILOT
    if _COPILOT is None:
        _COPILOT = Copilot()
    return _COPILOT
