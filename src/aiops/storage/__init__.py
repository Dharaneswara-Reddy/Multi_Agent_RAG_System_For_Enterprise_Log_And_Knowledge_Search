"""Persistence backends: which one runs is a configuration decision, not a code one.

`knowledge/catalog.py` keeps its public API; this package decides where those
rows actually land. Selection is intentionally a single rule with no fallbacks:

- `AIOPS_DB_URL` set to a `postgresql://` URL  ->  PostgreSQL
- anything else                                ->  SQLite at `settings.db_path`
- ...unless `AIOPS_REQUIRE_POSTGRES=1`, in which case anything but a
  `postgresql://` URL raises `CloudPersistenceError` instead.

There is no "try Postgres, fall back to SQLite". A deployment that silently
degrades to a per-task SQLite file when its database is unreachable would answer
questions and queue escalations into a queue nobody reads, which is worse than
failing to start. If the URL says Postgres, Postgres is what it uses or what it
fails on.

That rule was already strict about a *malformed* URL, but not about a missing
one: with `AIOPS_DB_URL` unset the second line quietly selected SQLite, which in
a container is a file that dies with the task. `AIOPS_REQUIRE_POSTGRES` closes
that gap, and every cloud deployment sets it. The flag is deliberately explicit
rather than inferred, because the condition it exists to catch is the absence of
the very setting it would have to be inferred from.

An explicit `db_path=` argument means SQLite at that path, regardless of
`AIOPS_DB_URL`. That keeps `catalog.lookup_error_code(code, db_path=tmp)` doing
the obvious thing in tests and in one-off scripts — a filesystem path is an
unambiguous request for a file — at the cost of making the argument's meaning
backend-specific, which is why it is documented here rather than left implied.
Under `AIOPS_REQUIRE_POSTGRES` it raises too: an explicit path is not a silent
fallback, but a cloud deployment has no local file worth writing to, so it is
still a bug worth surfacing rather than honouring.
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
from aiops.storage.base import CloudPersistenceError, MissingDependency, StorageBackend
from aiops.storage.postgres_backend import PostgresBackend, is_postgres_url
from aiops.storage.sqlite_backend import SQLiteBackend

__all__ = [
    "INDEX_ARTIFACTS",
    "REQUIRED_ARTIFACTS",
    "CloudPersistenceError",
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
    from aiops.config import settings

    if db_path is not None:
        if settings.require_postgres:
            raise CloudPersistenceError(
                f"get_backend(db_path={str(db_path)!r}) asks for SQLite, but "
                "AIOPS_REQUIRE_POSTGRES is set. An explicit path is not a "
                "fallback, so this is a caller bug rather than a configuration "
                "one: something reached for a local file in a deployment that "
                "has none. Drop the argument, or unset AIOPS_REQUIRE_POSTGRES "
                "if this is a local run."
            )
        # Not cached: these are one-off temporary databases in practice, and a
        # cache keyed on a tmp_path would grow once per test.
        return SQLiteBackend(Path(db_path))

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
    elif settings.require_postgres:
        raise CloudPersistenceError(
            "AIOPS_REQUIRE_POSTGRES is set but AIOPS_DB_URL is empty, so there "
            "is no database to use. This is the check working: without it the "
            "process would start on a container-local SQLite file and discard "
            "every audit record and escalation when the task is replaced. "
            "Set AIOPS_DB_URL to a postgresql:// URL — the entrypoint assembles "
            "one from AIOPS_DB_HOST / _USER / _PASSWORD / _NAME."
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
