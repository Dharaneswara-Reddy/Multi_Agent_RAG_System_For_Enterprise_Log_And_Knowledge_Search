"""In-memory hybrid index: dense vectors + BM25, hand-written on numpy.

Why no vector database: this corpus is ~1.2k chunks × 384 dims — about 1.8 MB of
float32. A brute-force matmul over that is well under a millisecond, so an ANN
index would add operational surface (a server, a schema, a sync job) to optimise
something that is not the bottleneck. The interface below is deliberately the
same shape a Qdrant/pgvector client would expose, so swapping in a real store
when the corpus outgrows RAM is a change to this file only.

The honest limit: this rebuilds from scratch on load and holds everything in one
process. At roughly a million chunks the memory and the rebuild time both stop
being free, and that is the point to move to a real store.
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from aiops.config import settings
from aiops.embedding.embedder import Embedder, get_embedder
from aiops.schemas import Chunk, RetrievedChunk


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, keeping error-code shapes intact.

    `PAY-5021` must survive as one token — splitting it on the hyphen is how a
    lexical search stops being able to find the single most searchable string in
    the corpus.
    """
    import re

    text = text.lower()
    return re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text)


@dataclass
class IndexStats:
    chunks: int
    dim: int
    built_at: str
    source_counts: dict[str, int]


class HybridIndex:
    """Dense cosine + BM25 lexical search over the same chunk set."""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder = embedder or get_embedder()
        self.chunks: list[Chunk] = []
        self.matrix: np.ndarray = np.zeros((0, self.embedder.dim), dtype=np.float32)
        self._bm25 = None
        self._stats: IndexStats | None = None

    # -- build -------------------------------------------------------------

    def build(self, chunks: list[Chunk]) -> IndexStats:
        from datetime import UTC, datetime

        from rank_bm25 import BM25Okapi

        self.chunks = list(chunks)
        texts = [c.text for c in self.chunks]
        self.matrix = self.embedder.embed_documents(texts)
        self._bm25 = BM25Okapi([_tokenize(t) for t in texts]) if texts else None

        counts: dict[str, int] = {}
        for c in self.chunks:
            counts[c.source_type.value] = counts.get(c.source_type.value, 0) + 1
        self._stats = IndexStats(
            chunks=len(self.chunks),
            dim=self.embedder.dim,
            built_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            source_counts=counts,
        )
        return self._stats

    @property
    def stats(self) -> IndexStats | None:
        return self._stats

    def __len__(self) -> int:
        return len(self.chunks)

    # -- search ------------------------------------------------------------

    def _candidate_mask(
        self,
        services: list[str] | None,
        source_types: list[str] | None,
        error_codes: list[str] | None,
    ) -> np.ndarray | None:
        """Metadata pre-filter. Returns None when no filter applies (fast path)."""
        if not (services or source_types or error_codes):
            return None
        mask = np.ones(len(self.chunks), dtype=bool)
        for i, c in enumerate(self.chunks):
            # A chunk spanning several services has service=None; keep it if
            # any of its text mentions a requested service.
            if (
                services
                and (c.service or "") not in services
                and not (c.service is None and any(s in c.text for s in services))
            ):
                mask[i] = False
                continue
            if source_types and c.source_type.value not in source_types:
                mask[i] = False
                continue
            if error_codes and not set(error_codes) & set(c.error_codes):
                mask[i] = False
        return mask

    def search(
        self,
        query: str,
        top_k: int | None = None,
        *,
        services: list[str] | None = None,
        source_types: list[str] | None = None,
        error_codes: list[str] | None = None,
        dense_weight: float | None = None,
        fusion: str | None = None,
    ) -> list[RetrievedChunk]:
        """First-stage hybrid search. Returns `top_k` fused candidates.

        Two fusion modes, both swept rather than assumed:

        - `blend` min-max normalises dense and lexical scores *within this
          query's candidate set* before a weighted sum. Raw BM25 scores are
          unbounded and corpus-dependent, so blending them against cosine
          without normalising makes the weight meaningless.
        - `rrf` fuses ranks instead of scores, which sidesteps normalisation
          entirely and is robust to the two scorers disagreeing about scale.

        This is stage one only. `retrieve()` is the full pipeline and is what
        production paths should call.
        """
        if not self.chunks:
            return []
        top_k = top_k or settings.top_k
        dense_weight = settings.dense_weight if dense_weight is None else dense_weight

        qv = self.embedder.embed_query(query)
        dense = self.matrix @ qv  # cosine: both sides are unit vectors

        if self._bm25 is not None:
            lexical = np.asarray(self._bm25.get_scores(_tokenize(query)), dtype=np.float32)
        else:
            lexical = np.zeros_like(dense)

        mask = self._candidate_mask(services, source_types, error_codes)
        if mask is not None:
            if not mask.any():
                return []
            dense = np.where(mask, dense, -np.inf)
            lexical = np.where(mask, lexical, -np.inf)

        fusion = fusion or settings.fusion
        if fusion == "rrf":
            # RRF fuses *ranks*, not scores, so it needs no normalisation and is
            # unaffected by the two scorers' wildly different score
            # distributions. Its scores are ~1/k and tiny by construction, so
            # min_score (a blend-scale threshold) does not apply.
            blended = _rrf(dense, lexical, dense_weight, settings.rrf_k)
            floor = -np.inf
        else:
            blended = _minmax(dense) * dense_weight + _minmax(lexical) * (1.0 - dense_weight)
            floor = settings.min_score

        # Partition over a pool at least as large as the requested cut.
        k = min(max(top_k, 1), len(self.chunks))
        idx = np.argpartition(-blended, k - 1)[:k]
        idx = idx[np.argsort(-blended[idx])]

        out: list[RetrievedChunk] = []
        for rank, i in enumerate(idx[:top_k]):
            score = float(blended[i])
            if score < floor:
                continue
            out.append(
                RetrievedChunk(
                    chunk=self.chunks[i],
                    dense_score=float(dense[i]) if np.isfinite(dense[i]) else 0.0,
                    lexical_score=float(lexical[i]) if np.isfinite(lexical[i]) else 0.0,
                    score=score,
                    rank=rank,
                )
            )
        return out

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        *,
        services: list[str] | None = None,
        source_types: list[str] | None = None,
        error_codes: list[str] | None = None,
        dense_weight: float | None = None,
        fusion: str | None = None,
        rerank: bool | None = None,
    ) -> list[RetrievedChunk]:
        """The full two-stage pipeline: fuse to `candidate_k`, rerank to `top_k`.

        This is what every production path should call. `search()` remains
        available as stage one alone, which is what makes it possible to
        measure the reranker's contribution rather than assume it.
        """
        from aiops.retrieval.rerank import maybe_rerank

        top_k = top_k or settings.top_k
        use_rerank = settings.rerank_enabled if rerank is None else rerank
        pool = max(top_k, settings.candidate_k) if use_rerank else top_k

        candidates = self.search(
            query,
            top_k=pool,
            services=services,
            source_types=source_types,
            error_codes=error_codes,
            dense_weight=dense_weight,
            fusion=fusion,
        )
        if not use_rerank:
            return candidates[:top_k]
        return maybe_rerank(query, candidates, top_k=top_k)

    def get(self, chunk_id: str) -> Chunk | None:
        for c in self.chunks:
            if c.chunk_id == chunk_id:
                return c
        return None

    def by_trace(self, trace_id: str) -> list[Chunk]:
        return [c for c in self.chunks if c.trace_id == trace_id]

    # -- persistence -------------------------------------------------------

    def save(self, path: Path | None = None) -> Path:
        """Write the three artefacts to a local directory.

        Local only, deliberately: building the index is an offline job, and
        uploading its output belongs to whatever runs that job (a CI step, an
        `aws s3 sync`), not to the class. Giving `save()` a write path to S3
        would mean any process that rebuilt an index in memory could overwrite
        the artefacts every running task loads from.
        """
        path = Path(path or settings.index_dir)
        path.mkdir(parents=True, exist_ok=True)
        np.save(path / "vectors.npy", self.matrix)
        with open(path / "chunks.pkl", "wb") as fh:
            pickle.dump(self.chunks, fh)
        if self._stats:
            (path / "stats.json").write_text(json.dumps(self._stats.__dict__, indent=2))
        return path

    def load(self, path: Path | None = None) -> bool:
        from rank_bm25 import BM25Okapi

        path = _resolve_artifact_dir(path)
        vec, chk = path / "vectors.npy", path / "chunks.pkl"
        if not (vec.exists() and chk.exists()):
            return False
        self.matrix = np.load(vec)
        with open(chk, "rb") as fh:
            self.chunks = pickle.load(fh)
        self._bm25 = BM25Okapi([_tokenize(c.text) for c in self.chunks]) if self.chunks else None
        stats_file = path / "stats.json"
        if stats_file.exists():
            self._stats = IndexStats(**json.loads(stats_file.read_text()))
        return True


def _resolve_artifact_dir(path: Path | None) -> Path:
    """Where `load()` should read the three artefacts from.

    An explicit `path` always wins and is always local — callers that pass one
    (the sweep, the ablation harness, the tests) are pointing at a directory
    they just built, and reaching for S3 there would be wrong and slow.

    Otherwise `AIOPS_INDEX_URI`, if it names an S3 prefix, is downloaded once
    into `index_cache_dir` and read from there. Everything after this function
    is the same local-filesystem load in both cases, which is the point: the
    remote path adds a fetch, not a second code path through the loader.
    """
    if path is not None:
        return Path(path)

    from aiops.storage import fetch_index_artifacts, is_s3_uri

    uri = settings.index_uri
    if is_s3_uri(uri):
        assert uri is not None  # narrowed by is_s3_uri
        return fetch_index_artifacts(
            uri, settings.index_cache_dir, endpoint_url=settings.s3_endpoint_url
        )
    if uri:
        # A non-S3 value is treated as a local directory rather than rejected:
        # it is the natural way to point a container at a mounted volume, and
        # the only other sensible reading of the setting.
        return Path(uri)
    return Path(settings.index_dir)


def _minmax(scores: np.ndarray) -> np.ndarray:
    finite = scores[np.isfinite(scores)]
    if finite.size == 0:
        return np.zeros_like(scores)
    lo, hi = float(finite.min()), float(finite.max())
    if hi - lo < 1e-9:
        return np.where(np.isfinite(scores), 1.0, 0.0).astype(np.float32)
    out = (scores - lo) / (hi - lo)
    return np.where(np.isfinite(out), out, 0.0).astype(np.float32)


def _rrf(
    dense: np.ndarray, lexical: np.ndarray, dense_weight: float, rrf_k: int
) -> np.ndarray:
    """Reciprocal rank fusion: score = sum over rankers of w / (rrf_k + rank).

    RRF discards the magnitudes entirely and keeps only the ordering each
    retriever produced. That is the whole point — cosine similarity and BM25
    live on incomparable scales, and any weighted sum of their *scores*
    silently depends on how those scales happen to sit for a given query.
    Ranks have no such problem.

    `rrf_k` damps the contribution of top ranks; 60 is the constant from
    Cormack et al. (2009) and is left configurable rather than inlined because
    it is swept.

    The weighting is a deviation from textbook RRF, which treats every retriever
    equally. It is kept because the sweep found the optimal dense/lexical split
    is genuinely not 50/50 on this corpus, and discarding that finding to match
    the textbook would cost measurable recall.
    """
    scores = np.zeros_like(dense, dtype=np.float32)
    for arr, weight in ((dense, dense_weight), (lexical, 1.0 - dense_weight)):
        if weight <= 0:
            continue
        finite = np.isfinite(arr)
        if not finite.any():
            continue
        # argsort of -arr gives the index order; ranking that gives each
        # element's 0-based rank in one pass.
        order = np.argsort(-np.where(finite, arr, -np.inf))
        ranks = np.empty(len(arr), dtype=np.int64)
        ranks[order] = np.arange(len(arr))
        contribution = weight / (rrf_k + ranks + 1)
        scores += np.where(finite, contribution, 0.0).astype(np.float32)
    return scores


_INDEX: HybridIndex | None = None


def get_index(rebuild: bool = False) -> HybridIndex:
    """Process-wide index singleton. Loads from disk, builds only if missing."""
    global _INDEX
    if _INDEX is not None and not rebuild:
        return _INDEX
    idx = HybridIndex()
    if rebuild or not idx.load():
        from aiops.ingestion.documents import build_all_chunks

        chunks, _ = build_all_chunks()
        idx.build(chunks)
        idx.save()
    _INDEX = idx
    return idx
