"""Streamlit console for the AI Ops Copilot.

Five surfaces, each answering a different question an engineer actually has:

  Ask          — what is the answer, and why should I believe it?
  Retrieval    — what did the system actually see? (no model, pure retrieval)
  Observability— where did the time and money go?
  Escalations  — what is waiting on a human?
  Evaluation   — is it getting better or worse?

The UI calls the graph in-process rather than over HTTP so a single
`streamlit run` is enough to demo everything; the FastAPI service exposes the
same operations for anything else that needs them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from aiops.config import settings
from aiops.knowledge import catalog
from aiops.observability import tracing as tr
from aiops.ui.theme import CSS, PALETTE, SOURCE_ICON, cards, pill

st.set_page_config(
    page_title="AI Ops Copilot",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Cached resources
# --------------------------------------------------------------------------


@st.cache_resource(show_spinner="Loading index and agents…")
def bootstrap():
    tr.configure_tracing()
    catalog.init_db()
    catalog.seed_error_catalog()
    from aiops.agents.graph import get_copilot

    copilot = get_copilot()
    return copilot


copilot = bootstrap()
index = copilot.index

SAMPLE_QUESTIONS = [
    "Why did checkout start failing on 14 July and what should I do about it?",
    "The gateway is returning 504s. Is the gateway the problem?",
    "Users are getting intermittent 401s. What is wrong and how do I fix it?",
    "Orders are stuck in PENDING_PAYMENT. Can I just retry them?",
    "How do I fix inventory connection pool exhaustion?",
    "Some customers report missing confirmation emails. Diagnose and fix.",
]


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### 🛰️ AI Ops Copilot")
    st.caption("Multi-agent RAG over enterprise logs and knowledge")

    mode = "Offline (extractive)" if copilot.offline else "Online"
    badge = "🟠" if copilot.offline else "🟢"
    st.markdown(f"**Mode** {badge} {mode}")
    if copilot.offline:
        st.markdown(
            '<div class="note">No <code>ANTHROPIC_API_KEY</code> found. Retrieval, '
            "guardrails, tracing and the audit trail are fully live; answers are "
            "extractive rather than generated, and answer-quality metrics are "
            "reported as n/a rather than faked.</div>",
            unsafe_allow_html=True,
        )

    st.divider()
    st.markdown("**Corpus**")
    stats = index.stats
    if stats:
        counts = stats.source_counts
        st.markdown(
            f"<span class='mono'>{len(index):,} chunks &nbsp;·&nbsp; "
            f"{sum(v for k, v in counts.items() if k != 'log')} doc "
            f"/ {counts.get('log', 0):,} log</span>",
            unsafe_allow_html=True,
        )
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            st.markdown(
                f"<span class='mono'>{SOURCE_ICON.get(k,'•')} {k:<13} {v:>5}</span>",
                unsafe_allow_html=True,
            )

    st.divider()
    st.markdown("**Retrieval settings**")
    top_k = st.slider("top_k", 3, 20, settings.top_k)
    dense_weight = st.slider("dense weight (vs BM25)", 0.0, 1.0, settings.dense_weight, 0.05)
    settings.top_k = top_k
    settings.dense_weight = dense_weight

    st.divider()
    st.caption(
        f"models · {settings.reasoning_model} (synthesis) / "
        f"{settings.cheap_model} (routing)"
    )


st.markdown(
    '<div class="aiops-head"><h1>AI Ops Copilot</h1>'
    '<span class="sub">multi-agent retrieval over runbooks, ADRs, post-mortems and platform logs</span></div>',
    unsafe_allow_html=True,
)

tab_ask, tab_retrieval, tab_obs, tab_esc, tab_eval = st.tabs(
    ["  Ask  ", "  Retrieval  ", "  Observability  ", "  Escalations  ", "  Evaluation  "]
)


# --------------------------------------------------------------------------
# Ask
# --------------------------------------------------------------------------

with tab_ask:
    if "question" not in st.session_state:
        st.session_state.question = SAMPLE_QUESTIONS[0]

    st.markdown("##### Try one")
    cols = st.columns(3)
    for i, q in enumerate(SAMPLE_QUESTIONS):
        if cols[i % 3].button(q[:52] + ("…" if len(q) > 52 else ""), key=f"s{i}", width='stretch'):
            st.session_state.question = q

    question = st.text_area("Question", key="question", height=80, label_visibility="collapsed")
    run = st.button("Ask the copilot", type="primary")

    if run and question.strip():
        with st.spinner("Routing → retrieving → analysing → synthesising…"):
            answer = copilot.answer(question)
        st.session_state.answer = answer

    answer = st.session_state.get("answer")
    if answer:
        conf_colour = (
            "ok" if answer.confidence >= 0.75 else "warn" if answer.confidence >= settings.confidence_threshold else "bad"
        )
        st.markdown(
            cards(
                [
                    ("Verdict", pill(answer.verdict.value), ""),
                    ("Confidence", f"{answer.confidence:.2f}", f"threshold {settings.confidence_threshold:.2f}"),
                    ("Route", answer.route or "—", f"{answer.extras.get('llm_calls', 0)} model calls"),
                    ("Latency", f"{answer.latency_ms} ms", ""),
                    ("Cost", f"${answer.cost_usd:.4f}", "this query"),
                    ("Sources", str(len(answer.citations)), "cited"),
                ]
            ),
            unsafe_allow_html=True,
        )
        st.write("")

        if answer.verdict.value == "escalated":
            st.warning(
                "**Routed to human review.** "
                + next(
                    (n.split("escalated:")[-1].strip() for n in answer.guardrail_notes if "escalated:" in n),
                    "confidence below threshold",
                )
                + "  \nThe draft below is queued in the Escalations tab rather than served as a final answer."
            )
        elif answer.verdict.value == "blocked":
            st.error("**Blocked by input guardrails.** The request never reached retrieval or a model.")

        left, right = st.columns([3, 2])
        with left:
            st.markdown("#### Answer")
            st.markdown(f'<div class="aiops-answer">{answer.answer}</div>', unsafe_allow_html=True)

            if answer.citations:
                st.markdown("#### Evidence")
                for c in answer.citations:
                    icon = SOURCE_ICON.get(c.source_type.value, "•")
                    st.markdown(
                        f'<div class="cite"><span class="ref">{icon} {c.ref}</span>'
                        f'<div class="snip">{c.snippet}…</div></div>',
                        unsafe_allow_html=True,
                    )

        with right:
            st.markdown("#### Agent trace")
            for s in answer.steps:
                meta = f"{s.latency_ms} ms" if s.latency_ms else ""
                if s.input_tokens or s.output_tokens:
                    meta += f" · {s.input_tokens}→{s.output_tokens} tok"
                st.markdown(
                    f'<div class="trace-step"><span class="trace-agent">{s.agent}</span>'
                    f'<span class="trace-summary">{s.summary}</span>'
                    f'<span class="trace-meta">{meta}</span></div>',
                    unsafe_allow_html=True,
                )

            # Retrieval provenance. Which stages ran and what each contributed is
            # the difference between "the system found this" and "the system
            # found this because a runbook cited it" — a responder judging
            # whether to trust an answer needs the second.
            pipeline = answer.extras.get("retrieval_pipeline") or {}
            if pipeline:
                st.markdown("#### Retrieval pipeline")
                stages = " → ".join(pipeline.get("stages") or []) or "single-pass"
                st.markdown(f"<span class='mono'>{stages}</span>", unsafe_allow_html=True)
                variants = pipeline.get("variants") or []
                if len(variants) > 1:
                    with st.expander(f"{len(variants)} query formulations"):
                        for v in variants:
                            st.markdown(f"<span class='mono'>· {v}</span>", unsafe_allow_html=True)
                if pipeline.get("hops"):
                    st.markdown(
                        f"<span class='mono'>🔗 {pipeline['hops']} cross-reference hop(s)"
                        "</span>",
                        unsafe_allow_html=True,
                    )

            if answer.guardrail_notes:
                st.markdown("#### Guardrails")
                for n in answer.guardrail_notes:
                    sev = "🔴" if "[block]" in n else "🟠" if "[warn]" in n else "🔵"
                    st.markdown(f"<span class='mono'>{sev} {n}</span>", unsafe_allow_html=True)
            else:
                st.markdown("#### Guardrails")
                st.markdown("<span class='mono'>🔵 no findings</span>", unsafe_allow_html=True)

            if answer.extras.get("cost_by_route"):
                st.markdown("#### Cost by route")
                for route, cost in answer.extras["cost_by_route"].items():
                    st.markdown(f"<span class='mono'>{route:<10} ${cost:.5f}</span>", unsafe_allow_html=True)

            if answer.trace_id:
                st.caption(f"trace_id `{answer.trace_id}`")


# --------------------------------------------------------------------------
# Retrieval explorer
# --------------------------------------------------------------------------

with tab_retrieval:
    st.markdown("##### Retrieval explorer")
    st.caption(
        "Pure retrieval — no model involved. This is what the agents are given, "
        "which is where most answer-quality problems actually originate."
    )

    q = st.text_input("Query", value="why did checkout fail", key="rq")
    c1, c2 = st.columns(2)
    types = c1.multiselect(
        "Source types", ["runbook", "adr", "postmortem", "service_doc", "log"], default=[]
    )
    svc = c2.multiselect(
        "Services",
        ["api-gateway", "auth-service", "order-service", "payment-service",
         "inventory-service", "notification-service", "search-service"],
        default=[],
    )

    if q.strip():
        hits = index.search(q, top_k=top_k, source_types=types or None, services=svc or None)
        if not hits:
            st.info("No chunks passed the score floor for this query and filter set.")
        else:
            df = pd.DataFrame(
                [
                    {
                        "rank": h.rank + 1,
                        "score": round(h.score, 3),
                        "cosine": round(h.dense_score, 3),
                        "bm25": round(h.lexical_score, 2),
                        "type": h.chunk.source_type.value,
                        "ref": h.chunk.citation(),
                        "service": h.chunk.service or "—",
                    }
                    for h in hits
                ]
            )
            fig = px.bar(
                df, x="score", y="ref", orientation="h", color="type",
                color_discrete_map=PALETTE, height=max(260, 42 * len(df)),
                hover_data=["cosine", "bm25", "service"],
            )
            fig.update_layout(
                yaxis={"categoryorder": "total ascending", "title": ""},
                xaxis_title="blended score (min-max normalised per query)",
                margin=dict(l=0, r=0, t=10, b=0), legend_title="",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, width='stretch')

            st.caption(
                f"top raw cosine **{hits[0].dense_score:.3f}** — the confidence heuristic "
                f"reads this, not the blended score (relevance floor 0.55)."
            )
            st.dataframe(df, width='stretch', hide_index=True)

            with st.expander("Chunk contents"):
                for h in hits:
                    st.markdown(f"**{h.chunk.citation()}** · `{h.chunk.source_type.value}`")
                    st.code(h.chunk.text[:1200], language="text")


# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------

with tab_obs:
    st.markdown("##### Operational health")
    rows = catalog.audit_history(limit=1000)

    if not rows:
        st.info("No queries recorded yet. Ask something on the first tab.")
    else:
        df = pd.DataFrame(rows)
        df["created_at"] = pd.to_datetime(df["created_at"], format="mixed", utc=True)
        df["day"] = df["created_at"].dt.date.astype(str)
        total = len(df)
        esc = (df["verdict"] == "escalated").sum()
        blk = (df["verdict"] == "blocked").sum()

        st.markdown(
            cards(
                [
                    ("Queries", f"{total:,}", ""),
                    ("Escalation rate", f"{esc / total:.1%}", f"{esc} queries"),
                    ("Block rate", f"{blk / total:.1%}", f"{blk} queries"),
                    ("Mean confidence", f"{df['confidence'].mean():.2f}", ""),
                    ("p95 latency", f"{int(df['latency_ms'].quantile(0.95))} ms", ""),
                    ("Total cost", f"${df['cost_usd'].sum():.4f}", f"${df['cost_usd'].mean():.5f} / query"),
                ]
            ),
            unsafe_allow_html=True,
        )
        st.write("")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Verdict mix**")
            counts = df["verdict"].value_counts().reset_index()
            counts.columns = ["verdict", "n"]
            fig = px.pie(
                counts, names="verdict", values="n", hole=0.58,
                color="verdict", color_discrete_map=PALETTE,
            )
            fig.update_layout(
                margin=dict(l=0, r=0, t=6, b=0), height=270,
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, width='stretch')

        with c2:
            st.markdown("**Confidence distribution**")
            fig = px.histogram(df, x="confidence", nbins=20)
            fig.update_traces(marker_color=PALETTE["accent"])
            fig.add_vline(
                x=settings.confidence_threshold, line_dash="dash",
                line_color=PALETTE["escalated"], annotation_text="escalation threshold",
            )
            fig.update_layout(
                margin=dict(l=0, r=0, t=6, b=0), height=270, bargap=0.06,
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                xaxis_title="", yaxis_title="queries",
            )
            st.plotly_chart(fig, width='stretch')

        st.markdown("**Drift proxy — share of low-confidence answers per day**")
        st.caption(
            "A rising low-confidence rate means incoming questions are drifting away "
            "from what the corpus covers. It is the cheapest drift signal that is "
            "actually actionable for a RAG system: the fix is corpus coverage, not retraining."
        )
        drift = (
            df.assign(low=(df["confidence"] < settings.confidence_threshold).astype(int))
            .groupby("day")
            .agg(queries=("id", "count"), low_conf_rate=("low", "mean"), cost=("cost_usd", "sum"))
            .reset_index()
        )
        fig = go.Figure()
        fig.add_bar(x=drift["day"], y=drift["queries"], name="queries",
                    marker_color=PALETTE["accent_soft"], yaxis="y")
        fig.add_scatter(x=drift["day"], y=drift["low_conf_rate"], name="low-confidence rate",
                        mode="lines+markers", line=dict(color=PALETTE["escalated"], width=3), yaxis="y2")
        fig.update_layout(
            height=300, margin=dict(l=0, r=0, t=6, b=0),
            yaxis=dict(title="queries"),
            yaxis2=dict(title="low-conf rate", overlaying="y", side="right", range=[0, 1], tickformat=".0%"),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", y=1.14),
        )
        st.plotly_chart(fig, width='stretch')

        st.markdown("**Recent spans** (OpenTelemetry, `gen_ai.*` semantic conventions)")
        spans = tr.get_collector().recent(300)
        if spans:
            sdf = pd.DataFrame(
                [
                    {
                        "span": s.name,
                        "ms": round(s.duration_ms, 1),
                        "model": s.attributes.get("gen_ai.request.model", ""),
                        "in_tok": s.attributes.get("gen_ai.usage.input_tokens", ""),
                        "out_tok": s.attributes.get("gen_ai.usage.output_tokens", ""),
                        "cost": s.attributes.get("aiops.cost_usd", ""),
                        "trace": s.trace_id[:12],
                    }
                    for s in spans
                ]
            )
            agg = (
                sdf.groupby("span")["ms"].agg(["count", "mean", "max"]).reset_index()
                .sort_values("mean", ascending=False)
            )
            fig = px.bar(agg, x="mean", y="span", orientation="h",
                         hover_data=["count", "max"], height=max(240, 34 * len(agg)))
            fig.update_traces(marker_color=PALETTE["accent"])
            fig.update_layout(
                yaxis={"categoryorder": "total ascending", "title": ""},
                xaxis_title="mean duration (ms)", margin=dict(l=0, r=0, t=8, b=0),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, width='stretch')
            with st.expander("Raw spans"):
                st.dataframe(sdf.tail(120), width='stretch', hide_index=True)
        else:
            st.caption("No spans buffered yet.")

        with st.expander("Audit log"):
            st.dataframe(
                df[["created_at", "question", "route", "verdict", "confidence", "latency_ms", "cost_usd"]]
                .sort_values("created_at", ascending=False),
                width='stretch', hide_index=True,
            )


# --------------------------------------------------------------------------
# Escalations
# --------------------------------------------------------------------------

with tab_esc:
    st.markdown("##### Human review queue")
    st.caption(
        "Answers the system declined to serve on its own: low retrieval confidence, "
        "a failed grounding check, or a destructive recommendation with thin support. "
        "This is the human-in-the-loop boundary."
    )

    status = st.radio("Show", ["pending", "approved", "rejected", "all"], horizontal=True)
    items = catalog.list_escalations(status=None if status == "all" else status, limit=200)

    if not items:
        st.success(f"Nothing {status}." if status != "all" else "The queue is empty.")
    else:
        for item in items:
            with st.container(border=True):
                head, meta = st.columns([4, 1])
                head.markdown(f"**{item['question']}**")
                meta.markdown(pill(item["status"]), unsafe_allow_html=True)
                st.caption(f"{item['created_at']} · confidence {item['confidence']:.2f} · {item['reason']}")
                with st.expander("Draft answer"):
                    st.markdown(item["draft"] or "_no draft_")

                if item["status"] == "pending":
                    a, b, c = st.columns([1, 1, 3])
                    reviewer = c.text_input("Reviewer", value="on-call", key=f"rv{item['id']}")
                    if a.button("Approve", key=f"ap{item['id']}", type="primary"):
                        catalog.resolve_escalation(item["id"], "approved", reviewer, "approved as drafted")
                        st.rerun()
                    if b.button("Reject", key=f"rj{item['id']}"):
                        catalog.resolve_escalation(item["id"], "rejected", reviewer, "rejected")
                        st.rerun()
                elif item["resolution"]:
                    st.caption(f"{item['status']} by {item['reviewer']} — {item['resolution']}")


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

with tab_eval:
    st.markdown("##### Evaluation")
    st.caption(
        "The golden set is the contract. Retrieval and safety metrics run without "
        "a model; answer quality needs one and is reported as n/a otherwise."
    )

    report_path = settings.eval_dir / "latest_report.json"
    c1, c2 = st.columns([1, 3])
    if c1.button("Run evaluation", type="primary"):
        from aiops.evaluation.harness import run_all

        with st.spinner("Scoring golden set…"):
            run_all(include_quality=not copilot.offline)
        st.rerun()

    if not report_path.exists():
        st.info("No report yet — run the evaluation.")
    else:
        report = json.loads(report_path.read_text())
        r, b, qual = report["retrieval"], report["behaviour"], report["quality"]
        c2.caption(f"last run {report['generated_at']} · {report['duration_s']}s · "
                   f"{'offline' if report['offline_mode'] else 'online'}")

        st.markdown(
            cards(
                [
                    ("Recall@k", f"{r['recall@k']:.3f}", f"n={r['n']}, k={r['k']}"),
                    ("MRR", f"{r['mrr']:.3f}", ""),
                    ("Hit rate", f"{r['hit_rate']:.3f}", ""),
                    ("Injection blocked", f"{b['injection_blocked']:.0%}", "must be 100%"),
                    ("Out-of-scope escalated", f"{b['out_of_scope_escalated']:.0%}", ""),
                    (
                        "Faithfulness",
                        f"{qual['faithfulness']:.2f}" if qual.get("faithfulness") is not None else "n/a",
                        "needs API key" if not qual.get("available") else "",
                    ),
                ]
            ),
            unsafe_allow_html=True,
        )
        st.write("")

        cat = pd.DataFrame(
            [{"category": k, **v} for k, v in r["by_category"].items()]
        )
        fig = px.bar(
            cat.melt(id_vars=["category", "n"], value_vars=["recall@k", "mrr"]),
            x="value", y="category", color="variable", barmode="group",
            orientation="h", height=max(280, 44 * len(cat)),
            color_discrete_sequence=[PALETTE["accent"], PALETTE["accent_soft"]],
        )
        fig.update_layout(
            yaxis={"categoryorder": "total ascending", "title": ""},
            xaxis_title="score", xaxis_range=[0, 1], legend_title="",
            margin=dict(l=0, r=0, t=8, b=0),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, width='stretch')

        sweep_path = settings.eval_dir / "sweep.json"
        if sweep_path.exists():
            st.markdown("**Parameter sweep** — chunk size × dense/BM25 blend")
            sw = pd.DataFrame(json.loads(sweep_path.read_text()))
            fig = px.line(
                sw, x="dense_weight", y="recall", color="chunk_tokens",
                markers=True, height=320,
            )
            fig.update_layout(
                xaxis_title="dense weight (1.0 = pure vector, 0.0 = pure BM25)",
                yaxis_title="recall@k", legend_title="chunk tokens",
                margin=dict(l=0, r=0, t=8, b=0),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, width='stretch')
            best = sw.sort_values(["recall", "mrr"], ascending=False).iloc[0]
            st.caption(
                f"best configuration: chunk_tokens={int(best['chunk_tokens'])}, "
                f"dense_weight={best['dense_weight']}, recall={best['recall']:.3f}"
            )

        if b["failures"]:
            with st.expander(f"{len(b['failures'])} behaviour failure(s)"):
                st.dataframe(pd.DataFrame(b["failures"]), width='stretch', hide_index=True)
