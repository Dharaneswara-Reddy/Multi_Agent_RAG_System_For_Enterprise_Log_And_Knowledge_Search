"""Fetching index artefacts from object storage.

The index is three files — `vectors.npy`, `chunks.pkl`, `stats.json` — built by
`scripts/setup.py` and read at process start. Locally they sit in `data/index/`.
In a container they cannot: baking them into the image ties a 2 MB data artefact
to the release cadence of the code, and rebuilding them per task costs an
embedding pass over the whole corpus on every scale-out event.

So the artefacts are built once, uploaded to S3, and every task downloads the
same copy at startup. That is a deliberate choice of *object storage over a
vector database* for the same reason the index itself is numpy: at 1,560 chunks
the artefact is small enough that S3 plus a local cache is the whole solution,
and the interface below is narrow enough to swap for a real store later.

**boto3 is optional** (`pip install ".[s3]"`) and imported inside the fetch
path, so nothing about this module loads — or can fail — unless
`AIOPS_INDEX_URI` is actually set.

The cache is *not* invalidated. A task downloads the artefacts once and keeps
them for its lifetime, which is correct for immutable, versioned prefixes
(`s3://bucket/index/2026-08-13/`) and wrong for a mutable one, where a task
started before a reindex serves the old index until it is replaced. The fix is
to version the prefix rather than to poll S3 on every query, but the failure
mode is real and is stated rather than papered over: `force=True` exists for
callers that know better.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from aiops.storage.base import MissingDependency

#: The complete set of files `HybridIndex.save()` writes. `stats.json` is
#: optional at load time, so a missing one is not an error.
INDEX_ARTIFACTS: tuple[str, ...] = ("vectors.npy", "chunks.pkl", "stats.json")
REQUIRED_ARTIFACTS: tuple[str, ...] = ("vectors.npy", "chunks.pkl")


class S3Location(NamedTuple):
    bucket: str
    prefix: str  # normalised: no leading slash, trailing slash iff non-empty

    def key(self, name: str) -> str:
        return f"{self.prefix}{name}"


def is_s3_uri(uri: str | None) -> bool:
    return bool(uri) and str(uri).lower().startswith("s3://")


def parse_s3_uri(uri: str) -> S3Location:
    """Split `s3://bucket/prefix/` into its bucket and a key prefix.

    The prefix is normalised to end in `/` when non-empty so callers can
    concatenate a filename onto it without a separate join step, which is where
    a doubled or missing slash usually creeps in. `s3://bucket` and
    `s3://bucket/` both yield an empty prefix, meaning the bucket root.
    """
    if not isinstance(uri, str) or not uri.lower().startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri!r}")
    remainder = uri[len("s3://") :]
    bucket, _, prefix = remainder.partition("/")
    if not bucket:
        raise ValueError(f"s3 URI has no bucket: {uri!r}")
    prefix = prefix.strip("/")
    return S3Location(bucket=bucket, prefix=f"{prefix}/" if prefix else "")


def _s3_client(endpoint_url: str | None = None) -> Any:
    """Build a boto3 S3 client, deferring both the import and the credentials.

    Credentials are left entirely to boto3's default chain, which on Fargate is
    the task role. Reading keys out of settings and passing them explicitly
    would mean the deployment has to carry long-lived credentials in order to
    use a feature whose whole point is that it does not need them.
    """
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise MissingDependency(
            "AIOPS_INDEX_URI points at S3 but boto3 is not installed. "
            'Install it with: pip install "aiops-copilot[s3]"'
        ) from exc
    # endpoint_url is for MinIO and localstack; None means real AWS.
    return boto3.client("s3", endpoint_url=endpoint_url or None)


def fetch_index_artifacts(
    uri: str,
    cache_dir: Path,
    *,
    endpoint_url: str | None = None,
    force: bool = False,
    client: Any | None = None,
) -> Path:
    """Download the index artefacts to `cache_dir` and return it.

    Returns the cache directory whether or not anything was downloaded, so the
    caller's next step is the same local-filesystem load in both cases.

    `client` is injectable so this is testable against a fake without either a
    network or a boto3 install; production callers never pass it.
    """
    location = parse_s3_uri(uri)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not force and all((cache_dir / name).exists() for name in REQUIRED_ARTIFACTS):
        return cache_dir

    s3 = client if client is not None else _s3_client(endpoint_url)
    for name in INDEX_ARTIFACTS:
        destination = cache_dir / name
        try:
            s3.download_file(location.bucket, location.key(name), str(destination))
        except Exception:
            # stats.json is metadata for the dashboard; the index loads without
            # it. The two required files are not optional, and a failure there
            # must surface rather than degrade into "index looks empty", which
            # is indistinguishable from a corpus that was never built.
            if name in REQUIRED_ARTIFACTS:
                raise
    return cache_dir


def fetch_documents(
    uri: str,
    docs_dir: Path,
    *,
    endpoint_url: str | None = None,
    client: Any | None = None,
) -> int:
    """Sync the markdown corpus from S3 into `docs_dir`. Returns the file count.

    S3 is the source of truth for knowledge documents in a cloud deployment, so
    that re-indexing does not require an application release and a corpus
    correction does not require a rebuild. The image still ships code and
    models; it no longer ships the knowledge.

    Downloads into the local directory rather than reading from S3 lazily,
    because the ingestion path globs a directory and reuses paths as stable
    document ids. Keeping the filesystem shape means nothing downstream — the
    chunker, the reference graph, the citation resolver — has to learn about
    object storage.

    An empty prefix is an error, not an empty corpus. A deployment whose bucket
    is missing or misspelled would otherwise start, index nothing, and answer
    every question with an abstention that looks exactly like a working system
    being appropriately cautious.
    """
    location = parse_s3_uri(uri)
    docs_dir = Path(docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)

    s3 = client if client is not None else _s3_client(endpoint_url)
    paginator = s3.get_paginator("list_objects_v2")

    count = 0
    for page in paginator.paginate(Bucket=location.bucket, Prefix=location.prefix):
        for entry in page.get("Contents", ()):
            key = entry["Key"]
            if not key.lower().endswith(".md"):
                continue
            # Flattened deliberately: `load_documents` globs one level, and a
            # nested key would silently never be read.
            s3.download_file(location.bucket, key, str(docs_dir / Path(key).name))
            count += 1

    if count == 0:
        raise FileNotFoundError(
            f"no .md objects under {uri} — the corpus is the source of truth for "
            "a cloud deployment, and an empty one would start cleanly and abstain "
            "from every question. Upload the corpus, or check the prefix."
        )
    return count
