"""SQL-backed knowledge base: error-code catalog, audit log, escalation queue.

Error-code lookup is a *tool*, not an agent. It is a deterministic key-value
join against a curated table — routing it through an LLM would add latency and a
hallucination surface to a query that SQL answers exactly. Agents exist for
reasoning steps; lookups stay lookups.

SQLite keeps the whole system single-binary and local. The schema is plain
enough to move to Postgres by changing the connection string.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiops.config import settings
from aiops.schemas import ErrorCodeEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS error_codes (
    code           TEXT PRIMARY KEY,
    service        TEXT NOT NULL,
    title          TEXT NOT NULL,
    severity       TEXT NOT NULL,
    description    TEXT NOT NULL,
    likely_causes  TEXT NOT NULL,   -- JSON array
    remediation    TEXT NOT NULL,
    runbook_ref    TEXT
);
CREATE INDEX IF NOT EXISTS idx_error_codes_service ON error_codes(service);

-- Every answer the copilot produces, for audit and drift analysis.
CREATE TABLE IF NOT EXISTS query_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    trace_id     TEXT,
    question     TEXT NOT NULL,
    route        TEXT,
    verdict      TEXT NOT NULL,
    confidence   REAL NOT NULL,
    answer       TEXT,
    citations    TEXT,             -- JSON array
    guardrails   TEXT,             -- JSON array of notes
    latency_ms   INTEGER,
    cost_usd     REAL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON query_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_verdict ON query_audit(verdict);

-- Human-in-the-loop: low-confidence or guardrail-flagged answers land here
-- instead of being served.
CREATE TABLE IF NOT EXISTS escalations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    audit_id    INTEGER,
    question    TEXT NOT NULL,
    draft       TEXT,
    reason      TEXT NOT NULL,
    confidence  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    reviewer    TEXT,
    resolution  TEXT,
    resolved_at TEXT,
    FOREIGN KEY (audit_id) REFERENCES query_audit(id)
);
CREATE INDEX IF NOT EXISTS idx_escalations_status ON escalations(status);
"""


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def seed_error_catalog(entries: list[ErrorCodeEntry] | None = None, db_path: Path | None = None) -> int:
    if entries is None:
        # full_catalog(), not ERROR_CATALOG: the latter is only the 7 canonical
        # codes, and seeding from it leaves the lookup tool unable to resolve
        # any of the 66 expansion codes whose runbooks are in the index.
        from aiops.ingestion.corpus import full_catalog

        entries = full_catalog()
    init_db(db_path)
    with connect(db_path) as conn:
        for e in entries:
            conn.execute(
                """INSERT INTO error_codes
                   (code, service, title, severity, description, likely_causes, remediation, runbook_ref)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET
                     service=excluded.service, title=excluded.title, severity=excluded.severity,
                     description=excluded.description, likely_causes=excluded.likely_causes,
                     remediation=excluded.remediation, runbook_ref=excluded.runbook_ref""",
                (
                    e.code,
                    e.service,
                    e.title,
                    e.severity,
                    e.description,
                    json.dumps(e.likely_causes),
                    e.remediation,
                    e.runbook_ref,
                ),
            )
    return len(entries)


def _row_to_entry(row: sqlite3.Row) -> ErrorCodeEntry:
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


def lookup_error_code(code: str, db_path: Path | None = None) -> ErrorCodeEntry | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM error_codes WHERE code = ?", (code.upper().strip(),)).fetchone()
    return _row_to_entry(row) if row else None


def lookup_many(codes: list[str], db_path: Path | None = None) -> list[ErrorCodeEntry]:
    if not codes:
        return []
    codes = [c.upper().strip() for c in codes]
    placeholders = ",".join("?" for _ in codes)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM error_codes WHERE code IN ({placeholders})", codes
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


def codes_for_service(service: str, db_path: Path | None = None) -> list[ErrorCodeEntry]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM error_codes WHERE service = ?", (service,)).fetchall()
    return [_row_to_entry(r) for r in rows]


def all_codes(db_path: Path | None = None) -> list[ErrorCodeEntry]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM error_codes ORDER BY code").fetchall()
    return [_row_to_entry(r) for r in rows]


# --------------------------------------------------------------------------
# Audit + escalation
# --------------------------------------------------------------------------


def record_audit(answer, db_path: Path | None = None) -> int:
    """Persist one answer. Returns the audit row id."""
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO query_audit
               (created_at, trace_id, question, route, verdict, confidence, answer,
                citations, guardrails, latency_ms, cost_usd, input_tokens, output_tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                answer.trace_id,
                answer.question,
                answer.route,
                answer.verdict.value if hasattr(answer.verdict, "value") else str(answer.verdict),
                answer.confidence,
                answer.answer,
                json.dumps([c.model_dump(mode="json") for c in answer.citations]),
                json.dumps(answer.guardrail_notes),
                answer.latency_ms,
                answer.cost_usd,
                sum(s.input_tokens for s in answer.steps),
                sum(s.output_tokens for s in answer.steps),
            ),
        )
        return int(cur.lastrowid or 0)


def create_escalation(
    question: str, draft: str, reason: str, confidence: float, audit_id: int | None = None,
    db_path: Path | None = None,
) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO escalations (created_at, audit_id, question, draft, reason, confidence)
               VALUES (?,?,?,?,?,?)""",
            (
                datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                audit_id,
                question,
                draft,
                reason,
                confidence,
            ),
        )
        return int(cur.lastrowid or 0)


def list_escalations(status: str | None = "pending", limit: int = 100, db_path: Path | None = None) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM escalations WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM escalations ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def resolve_escalation(
    escalation_id: int, status: str, reviewer: str, resolution: str = "", db_path: Path | None = None
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """UPDATE escalations
               SET status=?, reviewer=?, resolution=?, resolved_at=?
               WHERE id=?""",
            (
                status,
                reviewer,
                resolution,
                datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                escalation_id,
            ),
        )


def audit_history(limit: int = 500, db_path: Path | None = None) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM query_audit ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    n = seed_error_catalog()
    print(f"seeded {n} error codes into {settings.db_path}")
