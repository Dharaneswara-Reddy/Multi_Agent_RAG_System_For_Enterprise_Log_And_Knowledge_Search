"""Central configuration. Everything tunable lives here so experiments are one edit away."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AIOPS_", extra="ignore")

    # --- paths ---
    data_dir: Path = ROOT / "data"
    docs_dir: Path = ROOT / "data" / "docs"
    logs_dir: Path = ROOT / "data" / "logs"
    eval_dir: Path = ROOT / "data" / "eval"
    index_dir: Path = ROOT / "data" / "index"
    db_path: Path = ROOT / "data" / "aiops.db"

    # --- models ---
    # Synthesis / reasoning model. Routing and extraction use the cheap model.
    reasoning_model: str = "claude-opus-5"
    cheap_model: str = "claude-haiku-4-5"
    max_tokens: int = 8000
    effort: str = "medium"

    # --- embedding ---
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # --- chunking (the main context-engineering lever) ---
    # 320 is the sweep optimum on the 219-document corpus (scripts/evaluate.py
    # --sweep): recall@k 0.949 / MRR 0.830, against 0.915 / 0.863 at 160.
    #
    # This value *changed* when the corpus grew. On the original 18-document
    # corpus 160 won outright (0.949 / 0.836 against 0.921 / 0.762 at 320), on
    # the reasoning that a runbook section is a complete retrieval unit and
    # larger chunks average two topics into one vector. That reasoning was
    # right about chunks and wrong about the ranking problem: with 219 documents
    # a query has hundreds of plausible neighbours, and fine-grained chunks
    # fragment each document into many weak candidates that split its evidence.
    # Larger chunks carry enough surrounding context to win the comparison.
    #
    # 160 still wins on MRR. The tie is broken on recall@k because top_k=8
    # chunks go to the synthesiser either way — having the right document in
    # the context matters more than its exact rank within it.
    doc_chunk_tokens: int = 320  # ceiling per chunk, not a target — see documents._pack
    doc_chunk_overlap: int = 64
    log_window_size: int = 12  # log lines per untraced chunk
    max_log_chunk_chars: int = 3200  # hard cap so a hot trace can't blow the embed window

    # --- retrieval ---
    top_k: int = 8
    # First-stage pool handed to the reranker. Reranking cost is linear in this
    # number — every candidate is a cross-encoder forward pass — and on CPU that
    # dominates the whole pipeline.
    #
    # 16 is chosen on the accuracy/latency curve (scripts/sweep_candidates.py),
    # not for accuracy alone: 30 scores recall 0.964 at 4.42s/query, 16 scores
    # 0.958 at 2.36s. That is 47% of the latency for 0.006 recall — half a case
    # out of 88 — and MRR is marginally *better* at 16. A demo that answers in
    # two seconds is worth more than half a case of recall.
    candidate_k: int = 16

    # --- reranking (stage 2) ---
    # A bi-encoder scores query and chunk independently; a cross-encoder reads
    # them together and is far more accurate at ranking. Too slow for the whole
    # corpus, which is why it runs over candidate_k only.
    rerank_enabled: bool = True
    # L-12 over L-6 is measured, not assumed (scripts/sweep_rerank.py): on blend
    # fusion L-12 reaches recall 0.964 / MRR 0.859 against L-6's 0.930 / 0.844.
    # It costs roughly 2x the latency, which is the trade recorded in the README.
    # Both are small ONNX models (120MB / 80MB) — deliberately sized for a
    # CPU-only machine rather than reaching for bge-reranker-base at 1GB.
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-12-v2"  # 120MB, ONNX/CPU
    # 1.0 = the reranker decides outright, which won on recall (0.964 vs 0.958
    # at 0.25). The two are within half a case of each other on 88 queries, so
    # this follows the project's stated tie-break — recall first — rather than
    # claiming a meaningful gap.
    rerank_blend: float = 1.0

    # --- fusion ---
    # "blend" = min-max normalised weighted sum of dense and BM25 scores.
    # "rrf"   = reciprocal rank fusion, which ignores scores and uses ranks.
    #
    # RRF is the textbook choice and it *loses* here, which is why both are
    # implemented and swept. With reranking on top, blend reaches recall 0.964 /
    # MRR 0.859 against RRF's 0.947 / 0.866. RRF discards score magnitudes, and
    # on this corpus the magnitudes carry real signal: a BM25 spike on an exact
    # error code is evidence, and rank fusion flattens it to "first place".
    fusion: str = "blend"
    rrf_k: int = 60  # RRF damping constant; 60 is the value from the original paper

    # --- multi-hop retrieval ---
    # Follows explicit cross-references (error codes, ADR and incident ids) out
    # of retrieved chunks. Hops are corroboration and are damped below every
    # direct hit, so they cannot change what an answer is about.
    multihop_enabled: bool = True
    multihop_max_hops: int = 1
    # 3, not 5. Raising it recovers only +0.006 recall (0.962 -> 0.968) of the
    # 0.017 that preferring defining documents cost, and gives up precision
    # (0.187 -> 0.174) plus context budget to do it. The conflict between
    # traversal quality and recall is real rather than a budget artefact, so it
    # is recorded as a trade instead of tuned away.
    multihop_per_hop_cap: int = 3
    multihop_min_similarity: float = 0.45  # a cited but off-topic document is not evidence
    # Log chunks carry error codes as metadata, so a hop on a code can land on
    # raw log lines rather than the runbook explaining it. Excluding them made
    # traversal cleaner and cost golden-set recall, because logs are legitimate
    # evidence for log questions. Kept switchable and measured — see README.
    multihop_allow_logs: bool = True
    # Whether a document may hop on a code it already owns. Service docs list
    # every code of their service, so allowing this turns them into hubs that
    # reach a service's unrelated faults. Switchable because "reaches more
    # documents" and "traverses the right chain" are different goals and the
    # trade between them has to be measured, not assumed.
    multihop_follow_own_codes: bool = False

    # --- multi-query rewriting ---
    # Several formulations of the question, fused by RRF across variants.
    #
    # **Off by default, because it was measured and it does not work.** The
    # cumulative ablation showed no movement at all on the golden set (recall
    # 0.958 -> 0.958, MRR 0.861 -> 0.860) for +0.3s/query. Suspecting the
    # golden set was biased — I wrote both the questions and the corpus, so the
    # questions already use the corpus's vocabulary — it was re-tested on 20
    # deliberately conversational rephrasings (scripts/eval_paraphrase.py).
    # Identical result: recall +0.000, MRR +0.000, +0.18s.
    #
    # The reason is that the deterministic variants are too *close* to the
    # original. Stripping stopwords from "carts keep emptying themselves when
    # the site gets busy" yields nearly the same embedding, so fusing the
    # variants fuses near-duplicate ranked lists and changes nothing. Genuine
    # query diversity needs a model that can produce "Redis eviction under
    # memory pressure" from that sentence, which is `multiquery_use_llm` and
    # cannot be measured in CI without a key.
    #
    # The code is kept rather than deleted because the LLM path is a real and
    # untested hypothesis, not because the deterministic path deserves defence.
    multiquery_enabled: bool = False
    multiquery_max_variants: int = 4
    multiquery_use_llm: bool = False  # deterministic variants only unless enabled
    # 0.65 is the sweep optimum, down from 0.80 on the 18-document corpus —
    # BM25's optimal share *grew* as the corpus grew. With 73 error codes across
    # 42 services, an exact token like PAY-5021 discriminates far better than an
    # embedding that places every timeout runbook in roughly the same region.
    #
    # Pure dense (1.0) remains the worst setting at every chunk size (0.875 /
    # 0.886 / 0.892 recall), and the margin over hybrid widened with scale.
    # That is the whole argument for hybrid over pure vector, and the expansion
    # strengthened it rather than weakening it.
    dense_weight: float = 0.65  # hybrid blend; bm25 gets (1 - dense_weight)
    min_score: float = 0.15

    # --- self-correction ---
    # Attempts *after* the first. 1 is deliberate: the recoverable failure this
    # targets is triage over-narrowing, which a single broadened retry fixes or
    # does not. Further attempts repeat the same broad query and only spend
    # money to reach the same escalation.
    max_retries: int = 1

    # --- guardrails / escalation ---
    confidence_threshold: float = 0.55
    max_context_chars: int = 24_000

    # --- observability ---
    otlp_endpoint: str | None = None  # e.g. http://localhost:4318/v1/traces
    service_name: str = "aiops-copilot"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.docs_dir, self.logs_dir, self.eval_dir, self.index_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
