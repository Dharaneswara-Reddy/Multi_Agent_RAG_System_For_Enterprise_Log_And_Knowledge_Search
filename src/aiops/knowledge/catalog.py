"""SQL-backed knowledge base: error-code catalog, audit log, escalation queue.

Error-code lookup is a *tool*, not an agent. It is a deterministic key-value
join against a curated table — routing it through an LLM would add latency and a
hallucination surface to a query that SQL answers exactly. Agents exist for
reasoning steps; lookups stay lookups.

This module is the stable API — `graph.py`, the FastAPI server, the Streamlit
console, `scripts/setup.py`, and the tests all call these functions and none of
them knows which database is underneath. *Where* the rows live is now a
configuration decision handled by `aiops.storage`: SQLite by default, PostgreSQL
when `AIOPS_DB_URL` is set, because a file-backed database cannot be shared
between ECS tasks that serve the same escalation queue.

The split is worth stating precisely, since "add an abstraction layer" is easy
to do badly. Domain rules stay here — codes are upper-cased before lookup,
`likely_causes` is JSON that becomes a list, an escalation is created with
status `pending`. Only dialect and connection concerns moved down. The storage
backends know nothing about error codes and this module knows nothing about
placeholders.

`db_path=` still means "this SQLite file", on every function that takes it. That
is what the tests and one-off scripts want from it, and it is unambiguous in a
way that reinterpreting it under a Postgres configuration would not be.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiops.config import settings
from aiops.schemas import ErrorCodeEntry
from aiops.storage import get_backend


def _utcnow() -> str:
    """Second-resolution ISO-8601 with a `Z` suffix.

    The same string format on both backends: it sorts chronologically under a
    plain lexicographic `ORDER BY`, and every consumer already slices or parses
    it as text (`/metrics` takes `created_at[:10]`, the console hands it to
    pandas).
    """
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def init_db(db_path: Path | None = None) -> None:
    """Create the tables if they are missing.

    Still called at start-up by the API, the console, and `scripts/setup.py`.
    The backend now remembers that a given target is prepared, so the repeated
    calls from `record_audit` and friends cost nothing after the first — which
    matters on Postgres, where a `CREATE TABLE IF NOT EXISTS` per audit write
    takes a catalog lock inside every transaction.
    """
    get_backend(db_path).ensure_schema(force=True)


def seed_error_catalog(entries: list[ErrorCodeEntry] | None = None, db_path: Path | None = None) -> int:
    if entries is None:
        # full_catalog(), not ERROR_CATALOG: the latter is only the 7 canonical
        # codes, and seeding from it leaves the lookup tool unable to resolve
        # any of the 66 expansion codes whose runbooks are in the index.
        from aiops.ingestion.corpus import full_catalog

        entries = full_catalog()
    return get_backend(db_path).upsert_error_codes(entries)


def _row_to_entry(row: dict[str, Any]) -> ErrorCodeEntry:
    return ErrorCodeEntry(
        code=row["code"],
        service=row["service"],
        title=row["title"],
        severity=row["severity"],
        description=row["description"],
        likely_causes=json.loads(row["likely_causes"]),
        remediation=row["remediation"],
        runbook_ref=row["runbook_ref"],
    )


def _normalise(code: str) -> str:
    return code.upper().strip()


def lookup_error_code(code: str, db_path: Path | None = None) -> ErrorCodeEntry | None:
    row = get_backend(db_path).fetch_error_code(_normalise(code))
    return _row_to_entry(row) if row else None


def lookup_many(codes: list[str], db_path: Path | None = None) -> list[ErrorCodeEntry]:
    if not codes:
        return []
    rows = get_backend(db_path).fetch_error_codes([_normalise(c) for c in codes])
    return [_row_to_entry(r) for r in rows]


def codes_for_service(service: str, db_path: Path | None = None) -> list[ErrorCodeEntry]:
    rows = get_backend(db_path).fetch_error_codes_for_service(service)
    return [_row_to_entry(r) for r in rows]


def all_codes(db_path: Path | None = None) -> list[ErrorCodeEntry]:
    return [_row_to_entry(r) for r in get_backend(db_path).fetch_all_error_codes()]


# --------------------------------------------------------------------------
# Audit + escalation
# --------------------------------------------------------------------------


def record_audit(answer, db_path: Path | None = None) -> int:
    """Persist one answer. Returns the audit row id."""
    return get_backend(db_path).insert_audit(
        {
            "created_at": _utcnow(),
            "trace_id": answer.trace_id,
            "question": answer.question,
            "route": answer.route,
            "verdict": answer.verdict.value if hasattr(answer.verdict, "value") else str(answer.verdict),
            "confidence": answer.confidence,
            "answer": answer.answer,
            "citations": json.dumps([c.model_dump(mode="json") for c in answer.citations]),
            "guardrails": json.dumps(answer.guardrail_notes),
            "latency_ms": answer.latency_ms,
            "cost_usd": answer.cost_usd,
            "input_tokens": sum(s.input_tokens for s in answer.steps),
            "output_tokens": sum(s.output_tokens for s in answer.steps),
        }
    )


def create_escalation(
    question: str, draft: str, reason: str, confidence: float, audit_id: int | None = None,
    db_path: Path | None = None,
) -> int:
    return get_backend(db_path).insert_escalation(
        {
            "created_at": _utcnow(),
            "audit_id": audit_id,
            "question": question,
            "draft": draft,
            "reason": reason,
            "confidence": confidence,
        }
    )


def list_escalations(status: str | None = "pending", limit: int = 100, db_path: Path | None = None) -> list[dict[str, Any]]:
    return get_backend(db_path).fetch_escalations(status, limit)


def resolve_escalation(
    escalation_id: int, status: str, reviewer: str, resolution: str = "", db_path: Path | None = None
) -> None:
    get_backend(db_path).update_escalation(escalation_id, status, reviewer, resolution, _utcnow())


def audit_history(limit: int = 500, db_path: Path | None = None) -> list[dict[str, Any]]:
    return get_backend(db_path).fetch_audit(limit)


if __name__ == "__main__":
    init_db()
    n = seed_error_catalog()
    print(f"seeded {n} error codes into {settings.db_url or settings.db_path}")
