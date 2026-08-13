"""Persistence backends: which one runs is a configuration decision, not a code one.

`knowledge/catalog.py` keeps its public API; this package decides where those
rows actually land. Selection is intentionally a single rule with no fallbacks:

- `AIOPS_DB_URL` set to a `postgresql://` URL  ->  PostgreSQL
- anything else                                ->  SQLite at `settings.db_path`

There is no "try Postgres, fall back to SQLite". A deployment that silently
degrades to a per-task SQLite file when its database is unreachable would answer
questions and queue escalations into a queue nobody reads, which is worse than
failing to start. If the URL says Postgres, Postgres is what it uses or what it
fails on.

An explicit `db_path=` argument always means SQLite at that path, regardless of
`AIOPS_DB_URL`. That keeps `catalog.lookup_error_code(code, db_path=tmp)` doing
the obvious thing in tests and in one-off scripts — a filesystem path is an
unambiguous request for a file — at the cost of making the argument's meaning
backend-specific, which is why it is documented here rather than left implied.
"""

from __future__ import annotations

from pathlib import Path

from aiops.storage.artifacts import (
    INDEX_ARTIFACTS,
    REQUIRED_ARTIFACTS,
    S3Location,
    fetch_index_artifacts,
    is_s3_uri,
    parse_s3_uri,
)
from aiops.storage.base import MissingDependency, StorageBackend
from aiops.storage.postgres_backend import PostgresBackend, is_postgres_url
from aiops.storage.sqlite_backend import SQLiteBackend

__all__ = [
    "INDEX_ARTIFACTS",
    "REQUIRED_ARTIFACTS",
    "MissingDependency",
    "PostgresBackend",
    "S3Location",
    "SQLiteBackend",
    "StorageBackend",
    "fetch_index_artifacts",
    "get_backend",
    "is_postgres_url",
    "is_s3_uri",
    "parse_s3_uri",
    "reset_backend",
]

# Cached per target rather than as a single global, so that flipping AIOPS_DB_URL
# mid-process (which only tests do) picks up a matching backend instead of the
# first one anything happened to ask for.
_BACKENDS: dict[str, StorageBackend] = {}


def get_backend(db_path: Path | str | None = None) -> StorageBackend:
    """The backend for this call. See the module docstring for the selection rule."""
    if db_path is not None:
        # Not cached: these are one-off temporary databases in practice, and a
        # cache keyed on a tmp_path would grow once per test.
        return SQLiteBackend(Path(db_path))

    from aiops.config import settings

    url = settings.db_url
    if is_postgres_url(url):
        assert url is not None  # narrowed by is_postgres_url
        key = f"postgres:{url}"
        backend: StorageBackend = _BACKENDS.get(key) or PostgresBackend(url)
    elif url:
        raise ValueError(
            f"AIOPS_DB_URL scheme is not supported: {url.partition('://')[0]!r}. "
            "Use a postgresql:// URL, or unset it to use the default SQLite file."
        )
    else:
        # No path captured: SQLiteBackend re-reads settings.db_path per call, so
        # one cached instance stays correct when the tests repoint it.
        key = "sqlite:default"
        backend = _BACKENDS.get(key) or SQLiteBackend()

    _BACKENDS[key] = backend
    return backend


def reset_backend() -> None:
    """Drop cached backends. For tests that change `AIOPS_DB_URL` mid-process."""
    _BACKENDS.clear()
