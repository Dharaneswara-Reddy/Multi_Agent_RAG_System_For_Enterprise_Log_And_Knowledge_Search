#!/usr/bin/env python
"""One-command bootstrap: corpus -> database -> index -> smoke test.

    uv run python scripts/setup.py

Safe to re-run. The index build is the slow step (it embeds every chunk on CPU);
everything else is fast.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def step(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")
    print("-" * (len(title) + 8))


def main() -> int:
    total = 5
    started = time.perf_counter()

    step(1, total, "Fetching real log samples (LogHub)")
    script = ROOT / "scripts" / "fetch_loghub.sh"
    try:
        subprocess.run(["bash", str(script)], check=True)
    except subprocess.CalledProcessError:
        print("  continuing without the external overlay — the synthetic corpus is enough to run")

    step(2, total, "Generating the synthetic Meridian corpus")
    from aiops.ingestion.corpus import generate_corpus

    manifest = generate_corpus()
    for k, v in manifest.items():
        print(f"  {k:<14} {v}")

    step(3, total, "Seeding the SQL knowledge base")
    from aiops.knowledge import catalog

    catalog.init_db()
    n = catalog.seed_error_catalog()
    print(f"  {n} error codes, audit + escalation tables ready")

    step(4, total, "Building the hybrid index (embeds every chunk — this is the slow part)")
    from aiops.retrieval.index import get_index

    index = get_index(rebuild=True)
    stats = index.stats
    print(f"  {stats.chunks:,} chunks · dim {stats.dim}")
    for source, count in sorted(stats.source_counts.items(), key=lambda kv: -kv[1]):
        print(f"    {source:<13} {count:>6,}")

    step(5, total, "Smoke test")
    from aiops.agents.graph import Copilot

    copilot = Copilot(index=index)
    answer = copilot.answer("Why did checkout start failing on 14 July?", record=False)
    print(f"  mode       {'offline (extractive)' if copilot.offline else 'online'}")
    print(f"  route      {answer.route}")
    print(f"  verdict    {answer.verdict.value}")
    print(f"  confidence {answer.confidence:.2f}")
    print(f"  citations  {len(answer.citations)}")

    print(f"\nDone in {time.perf_counter() - started:.1f}s.\n")
    print("Next:")
    print("  uv run streamlit run src/aiops/ui/app.py      # console")
    print("  uv run uvicorn aiops.api.server:app --reload  # API")
    print("  uv run python scripts/evaluate.py --gate      # evaluation")
    if copilot.offline:
        print("\n  No ANTHROPIC_API_KEY found — answers are extractive and answer-quality")
        print("  metrics report n/a. Everything else (retrieval, guardrails, tracing,")
        print("  audit, escalation) is fully live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
