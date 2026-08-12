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
    candidate_k: int = 30  # pulled before reranking
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
