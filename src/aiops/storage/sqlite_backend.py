"""SQLite backend — the default, and the one that must never need configuring.

This is what `scripts/setup.py`, `pytest`, CI, and the credential-free offline
mode all run against. It keeps the system single-binary: no server to start, no
connection string to get right, no fixture to tear down. Everything the cloud
backend gains, it gains by being configured; this one is what happens when
nothing is.

The path is resolved on every operation rather than captured at construction.
That looks wasteful and is deliberate: `settings.db_path` is monkeypatched to a
temporary file by the test suite, and a backend that cached the path at import
time would keep writing to `data/aiops.db` throughout the tests.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aiops.storage.base import StorageBackend

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS error_codes (
        code           TEXT PRIMARY KEY,
        service        TEXT NOT NULL,
        title          TEXT NOT NULL,
        severity       TEXT NOT NULL,
        description    TEXT NOT NULL,
        likely_causes  TEXT NOT NULL,   -- JSON array
        remediation    TEXT NOT NULL,
        runbook_ref    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_error_codes_service ON error_codes(service)",
    # Every answer the copilot produces, for audit and drift analysis.
    """
    CREATE TABLE IF NOT EXISTS query_audit (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    TEXT NOT NULL,
        trace_id      TEXT,
        question      TEXT NOT NULL,
        route         TEXT,
        verdict       TEXT NOT NULL,
        confidence    REAL NOT NULL,
        answer        TEXT,
        citations     TEXT,             -- JSON array
        guardrails    TEXT,             -- JSON array of notes
        latency_ms    INTEGER,
        cost_usd      REAL,
        input_tokens  INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_created ON query_audit(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_verdict ON query_audit(verdict)",
    # Human-in-the-loop: low-confidence or guardrail-flagged answers land here
    # instead of being served.
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_escalations_status ON escalations(status)",
)


class SQLiteBackend(StorageBackend):
    """File-backed storage. `path=None` means "whatever settings says right now"."""

    placeholder = "?"
    returning_clause = ""  # sqlite3 reports the key via cursor.lastrowid

    def __init__(self, path: Path | None = None) -> None:
        super().__init__()
        self._path = Path(path) if path is not None else None

    def resolve_path(self) -> Path:
        if self._path is not None:
            return self._path
        from aiops.config import settings

        return Path(settings.db_path)

    @property
    def _target(self) -> str:
        return str(self.resolve_path())

    @property
    def _schema_statements(self) -> tuple[str, ...]:
        return SCHEMA_STATEMENTS

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        path = self.resolve_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _rows(self, cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
        return [dict(r) for r in cursor.fetchall()]

    def _generated_id(self, cursor: sqlite3.Cursor) -> int:
        return int(cursor.lastrowid or 0)

    def __repr__(self) -> str:  # shows up in error messages and test failures
        return f"SQLiteBackend({self._path or '<settings.db_path>'})"
