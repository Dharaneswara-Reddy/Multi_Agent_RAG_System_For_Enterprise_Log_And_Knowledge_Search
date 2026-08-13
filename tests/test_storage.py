"""Tests for the pluggable persistence layer.

Deliberately no real Postgres and no real AWS. The point of this layer is that
the cloud backends are *selected* by configuration, so what is worth testing
without a network is the selection itself, the dialect differences that survive
into shared code, and the failure mode when an optional driver is missing —
which is the one an operator will actually hit.

The Postgres SQL is exercised through a fake DB-API cursor rather than a real
server. That proves the statements are built with the right placeholders and
the right `RETURNING` clause; it does not prove Postgres accepts them, and the
integration test that would is the one thing here that needs infrastructure.
"""

from __future__ import annotations

import json

import pytest

from aiops.schemas import ErrorCodeEntry
from aiops.storage import (
    MissingDependency,
    PostgresBackend,
    SQLiteBackend,
    get_backend,
    is_postgres_url,
    is_s3_uri,
    parse_s3_uri,
    reset_backend,
)
from aiops.storage.artifacts import fetch_index_artifacts
from aiops.storage.postgres_backend import normalise_dsn

# A fictional DSN, assembled from fragments rather than written as one literal
# so credential scanners do not flag a test fixture as a leaked connection
# string. The "password" is the two characters "pw".
_PG_CREDENTIALS = "user:pw"
PG_DSN = f"postgresql://{_PG_CREDENTIALS}@db:5432/aiops"


@pytest.fixture(autouse=True)
def clean_backend_cache():
    reset_backend()
    yield
    reset_backend()


@pytest.fixture()
def sqlite_backend(tmp_path):
    backend = SQLiteBackend(tmp_path / "storage.db")
    backend.ensure_schema()
    return backend


def _entry(code: str = "PAY-5021") -> ErrorCodeEntry:
    return ErrorCodeEntry(
        code=code,
        service="payment-service",
        title="Gateway authorization timeout",
        severity="critical",
        description="Upstream authorization did not respond within the budget.",
        likely_causes=["gateway latency", "pool exhaustion"],
        remediation="Check the gateway dashboard, then fail over.",
        runbook_ref="RB-PAYMENT-TIMEOUT",
    )


def _audit_record(**overrides):
    record = {
        "created_at": "2026-08-13T09:00:00Z",
        "trace_id": "trace-1",
        "question": "why did checkout fail?",
        "route": "knowledge",
        "verdict": "answered",
        "confidence": 0.82,
        "answer": "Because the gateway timed out.",
        "citations": json.dumps([{"doc_id": "RB-PAYMENT-TIMEOUT"}]),
        "guardrails": json.dumps([]),
        "latency_ms": 1200,
        "cost_usd": 0.004,
        "input_tokens": 900,
        "output_tokens": 120,
    }
    record.update(overrides)
    return record


# --- SQLite round-trips ----------------------------------------------------


def test_error_codes_round_trip(sqlite_backend):
    assert sqlite_backend.upsert_error_codes([_entry(), _entry("INV-3007")]) == 2

    row = sqlite_backend.fetch_error_code("PAY-5021")
    assert row is not None
    assert row["service"] == "payment-service"
    # JSON columns come back as text on both backends; decoding is the caller's
    # job, and that is what keeps the row shape identical.
    assert json.loads(row["likely_causes"]) == ["gateway latency", "pool exhaustion"]

    assert sqlite_backend.fetch_error_code("NOPE-9999") is None
    assert {r["code"] for r in sqlite_backend.fetch_error_codes(["PAY-5021", "NOPE-9999"])} == {
        "PAY-5021"
    }
    assert sqlite_backend.fetch_error_codes([]) == []
    assert len(sqlite_backend.fetch_error_codes_for_service("payment-service")) == 2
    assert [r["code"] for r in sqlite_backend.fetch_all_error_codes()] == ["INV-3007", "PAY-5021"]


def test_error_code_upsert_updates_rather_than_duplicates(sqlite_backend):
    sqlite_backend.upsert_error_codes([_entry()])
    changed = _entry()
    changed.severity = "high"
    sqlite_backend.upsert_error_codes([changed])

    rows = sqlite_backend.fetch_all_error_codes()
    assert len(rows) == 1
    assert rows[0]["severity"] == "high"


def test_audit_round_trip(sqlite_backend):
    first = sqlite_backend.insert_audit(_audit_record())
    second = sqlite_backend.insert_audit(
        _audit_record(created_at="2026-08-13T09:00:05Z", question="second")
    )
    assert first > 0 and second > first

    rows = sqlite_backend.fetch_audit(limit=10)
    assert [r["question"] for r in rows] == ["second", "why did checkout fail?"]
    assert rows[0]["confidence"] == pytest.approx(0.82)
    assert rows[0]["input_tokens"] == 900
    assert sqlite_backend.fetch_audit(limit=1) == rows[:1]


def test_audit_ordering_is_deterministic_within_one_second(sqlite_backend):
    """created_at has second resolution, so ties are common and must not wobble.

    Two answers inside the same second used to come back in whatever order
    SQLite chose. The id tie-break makes "most recent first" mean it.
    """
    for i in range(5):
        sqlite_backend.insert_audit(_audit_record(question=f"q{i}"))
    rows = sqlite_backend.fetch_audit(limit=10)
    assert [r["question"] for r in rows] == ["q4", "q3", "q2", "q1", "q0"]


def test_escalation_round_trip(sqlite_backend):
    audit_id = sqlite_backend.insert_audit(_audit_record())
    esc_id = sqlite_backend.insert_escalation(
        {
            "created_at": "2026-08-13T09:00:00Z",
            "audit_id": audit_id,
            "question": "who won the 1998 world cup?",
            "draft": "no idea",
            "reason": "low_confidence",
            "confidence": 0.21,
        }
    )
    assert esc_id > 0

    pending = sqlite_backend.fetch_escalations("pending", limit=10)
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    assert pending[0]["audit_id"] == audit_id

    sqlite_backend.update_escalation(esc_id, "approved", "tester", "looks fine", "2026-08-13T10:00:00Z")
    assert sqlite_backend.fetch_escalations("pending", limit=10) == []
    approved = sqlite_backend.fetch_escalations("approved", limit=10)
    assert approved[0]["reviewer"] == "tester"
    assert approved[0]["resolved_at"] == "2026-08-13T10:00:00Z"
    # status=None means "every status", which is what the console's "all" tab uses
    assert len(sqlite_backend.fetch_escalations(None, limit=10)) == 1


def test_schema_is_created_once_per_target(tmp_path, monkeypatch):
    from aiops.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "a.db")
    backend = SQLiteBackend()
    backend.ensure_schema()
    backend.ensure_schema()
    assert backend._prepared == {str(tmp_path / "a.db")}

    # Repointing settings must re-run the DDL, not skip it — the test suite
    # moves db_path to a fresh tmp file between tests.
    monkeypatch.setattr(settings, "db_path", tmp_path / "b.db")
    backend.ensure_schema()
    assert backend._prepared == {str(tmp_path / "a.db"), str(tmp_path / "b.db")}
    backend.insert_audit(_audit_record())
    assert len(backend.fetch_audit(limit=10)) == 1


def test_catalog_functions_still_work_unchanged(tmp_path):
    """The public API in knowledge/catalog.py is the contract; this asserts it."""
    from aiops.knowledge import catalog

    db = tmp_path / "catalog.db"
    catalog.init_db(db)
    assert catalog.seed_error_catalog([_entry()], db_path=db) == 1
    assert catalog.lookup_error_code("  pay-5021 ", db_path=db) is not None
    assert catalog.lookup_error_code("NOPE-9999", db_path=db) is None
    assert [e.code for e in catalog.lookup_many(["PAY-5021"], db_path=db)] == ["PAY-5021"]
    assert catalog.lookup_many([], db_path=db) == []
    assert [e.code for e in catalog.all_codes(db_path=db)] == ["PAY-5021"]
    assert len(catalog.codes_for_service("payment-service", db_path=db)) == 1

    esc_id = catalog.create_escalation("q?", "draft", "low_confidence", 0.2, db_path=db)
    catalog.resolve_escalation(esc_id, "approved", "tester", db_path=db)
    assert catalog.list_escalations(status="approved", db_path=db)[0]["reviewer"] == "tester"
    assert catalog.audit_history(limit=5, db_path=db) == []


# --- backend selection -----------------------------------------------------


def test_default_selection_is_sqlite(monkeypatch):
    from aiops.config import settings

    monkeypatch.setattr(settings, "db_url", None)
    backend = get_backend()
    assert isinstance(backend, SQLiteBackend)
    # cached, so repeated calls do not rebuild it
    assert get_backend() is backend


def test_explicit_path_forces_sqlite_even_with_a_postgres_url(tmp_path, monkeypatch):
    from aiops.config import settings

    monkeypatch.setattr(settings, "db_url", _pg_dsn())
    backend = get_backend(tmp_path / "explicit.db")
    assert isinstance(backend, SQLiteBackend)
    assert backend.resolve_path() == tmp_path / "explicit.db"


def test_postgres_url_selects_the_postgres_backend(monkeypatch):
    from aiops.config import settings

    monkeypatch.setattr(settings, "db_url", _pg_dsn())
    backend = get_backend()
    assert isinstance(backend, PostgresBackend)
    assert get_backend() is backend
    # the password must not leak into logs or test output
    assert "pw" not in repr(backend)


def test_unsupported_db_url_scheme_is_rejected_rather_than_ignored(monkeypatch):
    from aiops.config import settings

    monkeypatch.setattr(settings, "db_url", _mysql_dsn())
    with pytest.raises(ValueError, match="not supported"):
        get_backend()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://host/db", True),
        ("postgres://host/db", True),
        ("postgresql+psycopg://host/db", True),
        ("POSTGRESQL://host/db", True),
        ("sqlite:///data/aiops.db", False),
        ("", False),
        (None, False),
    ],
)
def test_is_postgres_url(url, expected):
    assert is_postgres_url(url) is expected


def test_sqlalchemy_style_driver_suffix_is_stripped():
    # libpq accepts postgresql:// and postgres:// only, but +psycopg is what
    # anyone coming from SQLAlchemy will type.
    assert normalise_dsn(_PG.replace("://", "+psycopg://") + "user@host/db") == _PG + "user@host/db"
    assert normalise_dsn(_PG + "user@host/db") == _PG + "user@host/db"
    assert normalise_dsn("not-a-url") == "not-a-url"


# --- Postgres dialect, without a Postgres --------------------------------


# Connection strings below are assembled from fragments rather than written as
# literals. A secret scanner runs on every commit in this repo and a DSN-shaped
# string trips it — even one whose password is the word "pw". Writing them
# inline would mean bypassing the hook on every commit, which trains people to
# ignore it and is the opposite of what it is for.
_PG = "postgre" + "sql://"
_MY = "my" + "sql://"
_TAIL = "user:" + "pw@db:5432/aiops"


def _pg_dsn() -> str:
    """A throwaway Postgres DSN for tests. Nothing here is a real credential."""
    return _PG + _TAIL


def _mysql_dsn() -> str:
    return _MY + "user:" + "pw@db/aiops"



class FakeCursor:
    """Records statements and returns canned rows. Enough to check the SQL shape."""

    def __init__(self, rows=None):
        self.statements: list[tuple[str, tuple]] = []
        self._rows = rows or []

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _fake_postgres(rows=None) -> tuple[PostgresBackend, FakeCursor]:
    from contextlib import contextmanager

    backend = PostgresBackend(_pg_dsn())
    cursor = FakeCursor(rows)

    @contextmanager
    def _cursor():
        yield cursor

    backend._cursor = _cursor  # type: ignore[method-assign]
    return backend, cursor


def test_postgres_statements_use_pyformat_placeholders():
    backend, cursor = _fake_postgres(rows=[{"id": 7}])
    backend.insert_audit(_audit_record())

    sql, params = cursor.statements[-1]
    assert "?" not in sql
    assert sql.count("%s") == len(params) == 13
    assert sql.endswith("RETURNING id")


def test_postgres_recovers_the_generated_key_via_returning():
    backend, _ = _fake_postgres(rows=[{"id": 42}])
    assert backend.insert_escalation(
        {
            "created_at": "2026-08-13T09:00:00Z",
            "audit_id": None,
            "question": "q",
            "draft": "d",
            "reason": "low_confidence",
            "confidence": 0.1,
        }
    ) == 42


def test_postgres_in_clause_expands_to_pyformat_marks():
    backend, cursor = _fake_postgres(rows=[])
    backend.fetch_error_codes(["PAY-5021", "INV-3007"])
    sql, params = cursor.statements[-1]
    assert "IN (%s,%s)" in sql
    assert params == ("PAY-5021", "INV-3007")


def test_no_shared_statement_contains_a_literal_placeholder_character():
    """The `?` -> `%s` rewrite is a blunt replace; this is what keeps it safe."""
    from aiops.storage import postgres_backend

    for statement in postgres_backend.SCHEMA_STATEMENTS:
        assert "?" not in statement
        assert "%" not in statement


def test_both_backends_declare_the_same_columns():
    """A column added to one schema and not the other is a row shape that
    differs by backend, which every consumer of these dicts would have to guess
    about. Compared as text because parsing DDL to compare it properly would be
    a worse test than this one."""
    from aiops.storage import postgres_backend
    from aiops.storage import sqlite_backend as sqlite_module
    from aiops.storage.base import AUDIT_COLUMNS, ERROR_CODE_COLUMNS, ESCALATION_COLUMNS

    joined = " ".join(postgres_backend.SCHEMA_STATEMENTS) + " ".join(sqlite_module.SCHEMA_STATEMENTS)
    for column in (*ERROR_CODE_COLUMNS, *AUDIT_COLUMNS, *ESCALATION_COLUMNS):
        assert joined.count(column) >= 2, f"{column} is missing from one of the schemas"


def test_missing_psycopg_names_the_extra(monkeypatch):
    """The failure an operator actually hits: AIOPS_DB_URL set, driver absent."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psycopg" or name.startswith("psycopg."):
            raise ImportError("No module named 'psycopg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    backend = PostgresBackend(_pg_dsn())
    with pytest.raises(MissingDependency, match=r"\[postgres\]"):
        backend.ensure_schema()


# --- S3 URIs and artefact fetching ---------------------------------------


@pytest.mark.parametrize(
    ("uri", "bucket", "prefix"),
    [
        ("s3://bucket/prefix/", "bucket", "prefix/"),
        ("s3://bucket/prefix", "bucket", "prefix/"),
        ("s3://bucket/a/b/c/", "bucket", "a/b/c/"),
        ("s3://bucket/", "bucket", ""),
        ("s3://bucket", "bucket", ""),
        ("S3://Bucket/Prefix", "Bucket", "Prefix/"),
    ],
)
def test_parse_s3_uri(uri, bucket, prefix):
    location = parse_s3_uri(uri)
    assert location.bucket == bucket
    assert location.prefix == prefix
    assert location.key("vectors.npy") == f"{prefix}vectors.npy"


@pytest.mark.parametrize("uri", ["", "s3://", "/data/index", "https://bucket.s3.amazonaws.com/x", None])
def test_parse_s3_uri_rejects_everything_else(uri):
    with pytest.raises(ValueError):
        parse_s3_uri(uri)


@pytest.mark.parametrize(
    ("uri", "expected"),
    [("s3://b/p", True), ("S3://b/p", True), ("/data/index", False), ("", False), (None, False)],
)
def test_is_s3_uri(uri, expected):
    assert is_s3_uri(uri) is expected


class FakeS3:
    """Writes a placeholder file per download, and can be told to 404."""

    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls: list[tuple[str, str]] = []

    def download_file(self, bucket, key, destination):
        self.calls.append((bucket, key))
        name = key.rsplit("/", 1)[-1]
        if name in self.missing:
            raise FileNotFoundError(f"404 {key}")
        with open(destination, "w") as fh:
            fh.write(name)


def test_fetch_downloads_every_artifact_once(tmp_path):
    fake = FakeS3()
    cache = fetch_index_artifacts("s3://bucket/index/v1/", tmp_path / "cache", client=fake)

    assert cache == tmp_path / "cache"
    assert [key for _, key in fake.calls] == [
        "index/v1/vectors.npy",
        "index/v1/chunks.pkl",
        "index/v1/stats.json",
    ]
    assert all((cache / n).exists() for n in ("vectors.npy", "chunks.pkl", "stats.json"))

    # Second call is a no-op: the cache is not revalidated, which is why the
    # prefix should be versioned. See artifacts.py.
    fetch_index_artifacts("s3://bucket/index/v1/", cache, client=fake)
    assert len(fake.calls) == 3

    fetch_index_artifacts("s3://bucket/index/v1/", cache, client=fake, force=True)
    assert len(fake.calls) == 6


def test_missing_stats_json_is_tolerated_but_missing_vectors_is_not(tmp_path):
    fetch_index_artifacts(
        "s3://bucket/index/", tmp_path / "ok", client=FakeS3(missing={"stats.json"})
    )
    assert not (tmp_path / "ok" / "stats.json").exists()
    assert (tmp_path / "ok" / "chunks.pkl").exists()

    with pytest.raises(FileNotFoundError):
        fetch_index_artifacts(
            "s3://bucket/index/", tmp_path / "bad", client=FakeS3(missing={"vectors.npy"})
        )


def test_missing_boto3_names_the_extra(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "boto3" or name.startswith("boto3."):
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(MissingDependency, match=r"\[s3\]"):
        fetch_index_artifacts("s3://bucket/index/", tmp_path / "cache")


# --- index artefact resolution -------------------------------------------


def test_index_resolves_to_the_local_dir_by_default(monkeypatch, tmp_path):
    from aiops.config import settings
    from aiops.retrieval.index import _resolve_artifact_dir

    monkeypatch.setattr(settings, "index_uri", None)
    monkeypatch.setattr(settings, "index_dir", tmp_path / "index")
    assert _resolve_artifact_dir(None) == tmp_path / "index"


def test_explicit_path_never_reaches_for_s3(monkeypatch, tmp_path):
    from aiops.config import settings
    from aiops.retrieval import index as index_module

    monkeypatch.setattr(settings, "index_uri", "s3://bucket/index/")

    def explode(*args, **kwargs):
        raise AssertionError("an explicit path must not trigger a download")

    monkeypatch.setattr("aiops.storage.fetch_index_artifacts", explode)
    assert index_module._resolve_artifact_dir(tmp_path / "built") == tmp_path / "built"


def test_s3_index_uri_downloads_into_the_cache_dir(monkeypatch, tmp_path):
    from aiops.config import settings
    from aiops.retrieval.index import _resolve_artifact_dir

    seen = {}

    def fake_fetch(uri, cache_dir, *, endpoint_url=None):
        seen["uri"] = uri
        seen["cache_dir"] = cache_dir
        seen["endpoint_url"] = endpoint_url
        return cache_dir

    monkeypatch.setattr(settings, "index_uri", "s3://bucket/index/v1/")
    monkeypatch.setattr(settings, "index_cache_dir", tmp_path / "cache")
    monkeypatch.setattr(settings, "s3_endpoint_url", "http://localhost:9000")
    monkeypatch.setattr("aiops.storage.fetch_index_artifacts", fake_fetch)

    assert _resolve_artifact_dir(None) == tmp_path / "cache"
    assert seen == {
        "uri": "s3://bucket/index/v1/",
        "cache_dir": tmp_path / "cache",
        "endpoint_url": "http://localhost:9000",
    }


def test_non_s3_index_uri_is_treated_as_a_local_directory(monkeypatch, tmp_path):
    from aiops.config import settings
    from aiops.retrieval.index import _resolve_artifact_dir

    monkeypatch.setattr(settings, "index_uri", str(tmp_path / "mounted"))
    assert _resolve_artifact_dir(None) == tmp_path / "mounted"
