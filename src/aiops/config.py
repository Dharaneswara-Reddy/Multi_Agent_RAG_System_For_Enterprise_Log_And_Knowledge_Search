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
    # 160 is the sweep optimum (scripts/evaluate.py --sweep): recall@k 0.949 /
    # MRR 0.836, against 0.921 / 0.762 at 320. Smaller chunks win here because
    # the corpus is dense reference prose — a runbook step is a complete
    # retrieval unit, and larger chunks average two topics into one vector.
    doc_chunk_tokens: int = 160  # ceiling per chunk, not a target — see documents._pack
    doc_chunk_overlap: int = 64
    log_window_size: int = 12  # log lines per untraced chunk
    max_log_chunk_chars: int = 3200  # hard cap so a hot trace can't blow the embed window

    # --- retrieval ---
    top_k: int = 8
    candidate_k: int = 30  # pulled before reranking
    # 0.80 is the sweep optimum. Pure dense (1.0) scores *worse* at every chunk
    # size — BM25 is carrying exact-match tokens like PAY-5021 that the
    # embedding blurs, which is the whole argument for hybrid over pure vector.
    dense_weight: float = 0.80  # hybrid blend; bm25 gets (1 - dense_weight)
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
