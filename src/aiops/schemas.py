"""Shared data contracts. Every module speaks these types."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    RUNBOOK = "runbook"
    ADR = "adr"
    POSTMORTEM = "postmortem"
    SERVICE_DOC = "service_doc"
    LOG = "log"


class Chunk(BaseModel):
    """The unit of retrieval. `text` is what gets embedded; `metadata` drives filtering."""

    chunk_id: str
    doc_id: str
    text: str
    source_type: SourceType
    title: str = ""
    # Metadata schema — the knowledge-engineering layer. Kept flat so it filters cheaply.
    service: str | None = None
    timestamp: datetime | None = None
    trace_id: str | None = None
    error_codes: list[str] = Field(default_factory=list)
    ordinal: int = 0  # position within the parent doc

    def citation(self) -> str:
        label = self.title or self.doc_id
        return f"{label}#{self.ordinal}"


class RetrievedChunk(BaseModel):
    chunk: Chunk
    dense_score: float = 0.0
    lexical_score: float = 0.0
    score: float = 0.0
    rank: int = 0


class LogRecord(BaseModel):
    """Structured extraction target for raw log lines."""

    timestamp: datetime
    level: Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
    service: str
    trace_id: str
    message: str
    error_code: str | None = None
    latency_ms: int | None = None
    raw: str = ""


class ErrorCodeEntry(BaseModel):
    """A row of the error-code catalog (SQL-backed knowledge base)."""

    code: str
    service: str
    title: str
    severity: Literal["low", "medium", "high", "critical"]
    description: str
    likely_causes: list[str]
    remediation: str
    runbook_ref: str | None = None


class Citation(BaseModel):
    ref: str
    source_type: SourceType
    snippet: str


class AgentStep(BaseModel):
    """One hop of the multi-agent trace, surfaced in the UI and the OTel span tree."""

    agent: str
    summary: str
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class Verdict(StrEnum):
    ANSWERED = "answered"
    ESCALATED = "escalated"
    BLOCKED = "blocked"


class CopilotAnswer(BaseModel):
    question: str
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: float = 0.0
    verdict: Verdict = Verdict.ANSWERED
    route: str = ""
    steps: list[AgentStep] = Field(default_factory=list)
    guardrail_notes: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    latency_ms: int = 0
    trace_id: str = ""
    extras: dict[str, Any] = Field(default_factory=dict)
