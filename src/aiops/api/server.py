"""FastAPI surface.

The Streamlit UI talks to this rather than importing the graph directly, so the
copilot is a service with a contract rather than a notebook. That separation is
what makes the escalation queue, the audit trail, and the metrics endpoint
usable by anything else later (a Slack bot, a CI check, a PagerDuty hook).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from aiops.config import settings
from aiops.knowledge import catalog
from aiops.observability import tracing as tr
from aiops.schemas import CopilotAnswer

_STARTED = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the index and tracer at startup so the first request is not the one
    # that pays a 4-minute embedding build.
    tr.configure_tracing()
    catalog.init_db()
    catalog.seed_error_catalog()
    from aiops.agents.graph import get_copilot

    get_copilot()
    yield


app = FastAPI(
    title="AI Ops Copilot",
    version="0.1.0",
    description="Multi-agent RAG over enterprise logs and knowledge.",
    lifespan=lifespan,
)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    record: bool = True


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=1, le=50)
    source_types: list[str] | None = None
    services: list[str] | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    from aiops.agents.graph import get_copilot
    from aiops.retrieval.index import get_index

    index = get_index()
    return {
        "status": "ok",
        "uptime_s": round(time.time() - _STARTED, 1),
        "offline_mode": get_copilot().offline,
        "index_chunks": len(index),
        "llm_provider": settings.llm_provider,
        "reasoning_model": settings.active_reasoning_model,
        "cheap_model": settings.active_cheap_model,
    }


@app.post("/ask", response_model=CopilotAnswer)
def ask(req: AskRequest = Body(...)) -> CopilotAnswer:
    from aiops.agents.graph import get_copilot

    try:
        return get_copilot().answer(req.question, record=req.record)
    except Exception as exc:  # surface the failure rather than a blank 500
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/search")
def search(req: SearchRequest = Body(...)) -> dict[str, Any]:
    """Raw retrieval, no model. Useful for debugging what the agents saw."""
    from aiops.retrieval.index import get_index

    hits = get_index().search(
        req.query,
        top_k=req.top_k,
        source_types=req.source_types,
        services=req.services,
    )
    return {
        "query": req.query,
        "hits": [
            {
                "ref": h.chunk.citation(),
                "score": round(h.score, 4),
                "dense": round(h.dense_score, 4),
                "lexical": round(h.lexical_score, 4),
                "source_type": h.chunk.source_type.value,
                "service": h.chunk.service,
                "trace_id": h.chunk.trace_id,
                "text": h.chunk.text[:500],
            }
            for h in hits
        ],
    }


@app.get("/error-codes")
def error_codes() -> dict[str, Any]:
    return {"codes": [e.model_dump() for e in catalog.all_codes()]}


@app.get("/error-codes/{code}")
def error_code(code: str) -> dict[str, Any]:
    entry = catalog.lookup_error_code(code)
    if not entry:
        raise HTTPException(status_code=404, detail=f"unknown error code {code}")
    return entry.model_dump()


@app.get("/escalations")
def escalations(status: str | None = Query(default="pending")) -> dict[str, Any]:
    return {"escalations": catalog.list_escalations(status=status)}


class ResolveRequest(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    reviewer: str
    resolution: str = ""


@app.post("/escalations/{escalation_id}/resolve")
def resolve(escalation_id: int, req: ResolveRequest) -> dict[str, str]:
    catalog.resolve_escalation(escalation_id, req.status, req.reviewer, req.resolution)
    return {"status": "ok"}


@app.get("/audit")
def audit(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    return {"entries": catalog.audit_history(limit=limit)}


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    """Operational health: volume, cost, latency, escalation rate, and drift.

    Drift here is deliberately the simplest defensible proxy — the share of
    answers falling below the confidence threshold, bucketed by day. A rising
    low-confidence rate means the questions have moved away from what the corpus
    covers, which is the drift that actually matters for a RAG system.
    """
    rows = catalog.audit_history(limit=1000)
    if not rows:
        return {"total": 0, "note": "no queries recorded yet"}

    total = len(rows)
    escalated = sum(1 for r in rows if r["verdict"] == "escalated")
    blocked = sum(1 for r in rows if r["verdict"] == "blocked")
    latencies = sorted(r["latency_ms"] or 0 for r in rows)

    by_day: dict[str, dict[str, float]] = {}
    for r in rows:
        day = (r["created_at"] or "")[:10]
        bucket = by_day.setdefault(day, {"n": 0, "low_conf": 0, "cost": 0.0, "escalated": 0})
        bucket["n"] += 1
        bucket["cost"] += r["cost_usd"] or 0.0
        if (r["confidence"] or 0) < settings.confidence_threshold:
            bucket["low_conf"] += 1
        if r["verdict"] == "escalated":
            bucket["escalated"] += 1

    return {
        "total": total,
        "escalation_rate": round(escalated / total, 4),
        "block_rate": round(blocked / total, 4),
        "mean_confidence": round(sum(r["confidence"] or 0 for r in rows) / total, 4),
        "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 6),
        "mean_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows) / total, 6),
        "latency_ms": {
            "p50": latencies[len(latencies) // 2],
            "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
            "max": latencies[-1],
        },
        "tokens": {
            "input": sum(r["input_tokens"] or 0 for r in rows),
            "output": sum(r["output_tokens"] or 0 for r in rows),
        },
        "drift_by_day": {
            day: {
                "queries": int(v["n"]),
                "low_confidence_rate": round(v["low_conf"] / v["n"], 4),
                "escalation_rate": round(v["escalated"] / v["n"], 4),
                "cost_usd": round(v["cost"], 6),
            }
            for day, v in sorted(by_day.items())
        },
    }


@app.get("/traces")
def traces(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    spans = tr.get_collector().recent(limit)
    return {
        "spans": [
            {
                "name": s.name,
                "duration_ms": round(s.duration_ms, 2),
                "status": s.status,
                "trace_id": s.trace_id,
                "span_id": s.span_id,
                "parent_id": s.parent_id,
                "attributes": {k: v for k, v in s.attributes.items()},
            }
            for s in spans
        ]
    }


@app.get("/traces/{trace_id}")
def trace_detail(trace_id: str) -> dict[str, Any]:
    spans = tr.get_collector().by_trace(trace_id)
    if not spans:
        raise HTTPException(status_code=404, detail="trace not found (in-memory buffer only)")
    return {
        "trace_id": trace_id,
        "spans": [
            {
                "name": s.name,
                "duration_ms": round(s.duration_ms, 2),
                "span_id": s.span_id,
                "parent_id": s.parent_id,
                "attributes": dict(s.attributes),
            }
            for s in sorted(spans, key=lambda x: x.start_ms)
        ],
    }
