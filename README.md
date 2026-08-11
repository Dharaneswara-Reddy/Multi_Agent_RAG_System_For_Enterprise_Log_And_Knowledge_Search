# AI Ops Copilot

A multi-agent RAG system that answers SRE questions over a mixed corpus of
**runbooks, ADRs, post-mortems, service docs, and platform logs** — with
calibrated confidence, deterministic guardrails, OpenTelemetry tracing, a human
escalation queue, and an evaluation harness that gates CI.

Built as a portfolio project for the Cognizant Ace Frontier Engineer program.

```
                 ┌─────────── triage (supervisor, cheap model) ───────────┐
   question ────▶│  route · extract error codes · rewrite search query    │
                 └───┬──────────────────┬───────────────────┬─────────────┘
                     │                  │                   │
              knowledge agent      log analyst          both (hybrid)
              (docs retrieval)   (log correlation)
                     └──────────────────┴───────────────────┘
                                        │
                          error-code catalog  (SQL tool, no model)
                                        │
                              synthesizer (reasoning model)
                                        │
                            guardrail gate → answer | escalate | block
```

---

## Quick start

```bash
uv venv --python 3.11 --python-preference only-managed
uv pip install -e ".[dev]"
uv run python scripts/setup.py          # corpus → DB → index → smoke test

uv run streamlit run src/aiops/ui/app.py           # console
uv run uvicorn aiops.api.server:app --reload       # API on :8000
uv run python scripts/evaluate.py --gate           # evaluation + CI gate
uv run pytest -q                                   # 90 tests
```

**No API key required to run.** Without `ANTHROPIC_API_KEY` the system runs in
*offline mode*: retrieval, guardrails, tracing, audit, escalation, and every
retrieval/safety metric are fully live; answers are extractive rather than
generated, and answer-quality metrics report `n/a` rather than a fabricated
number. Set the key to enable synthesis and LLM-judge grading.

> The environment needs a Python built **with** `sqlite3`. Some pyenv builds
> omit it; `--python-preference only-managed` avoids that.

---

## What's actually here

| Capability | Where | Note |
|---|---|---|
| Multi-agent orchestration | `agents/graph.py` | LangGraph supervisor, 2 agents + 1 SQL tool |
| RAG pipeline | `ingestion/`, `embedding/`, `retrieval/` | FastEmbed + hand-written hybrid index |
| Context engineering | `retrieval/context.py` | per-doc cap, trace expansion, char budget |
| Knowledge engineering | `ingestion/corpus.py`, `knowledge/catalog.py` | metadata schema + SQL error catalog |
| Guardrails | `guardrails/rules.py` | PII, injection, citation grounding, destructive-action |
| Human-in-the-loop | `knowledge/catalog.py`, UI tab | confidence-gated escalation queue |
| Observability | `observability/tracing.py` | OTel `gen_ai.*` spans, cost + latency per call |
| Cost/latency routing | `llm.py` | Haiku for routing/extraction, Opus for synthesis |
| Evaluation | `evaluation/harness.py` | 46-case golden set, 3 metric tiers |
| CI quality gate | `.github/workflows/ci.yml` | build fails on retrieval or safety regression |
| API | `api/server.py` | FastAPI: ask, search, metrics, escalations, traces |
| UI | `ui/app.py` | Streamlit console, 5 surfaces |

---

## The data question

**I generated the corpus. Here is exactly what that means.**

Real enterprise runbooks and production logs are the most confidential artifacts
a company owns, and no public dataset pairs *logs* with the *documentation that
explains them* — which is the entire point of this system. So the corpus has two
layers:

**1. Synthetic backbone — "Meridian", a fictional e-commerce platform.**
Eighteen documents and five correlated incident scenarios, hand-written to be
*internally consistent*: `PAY-5021` appears in the logs, in the SQL catalog, in
a runbook, and in a post-mortem, and the payment → order → gateway failure
cascade shares trace IDs across services. That consistency is what makes
evaluation possible — a golden question like *"which service failed first?"* has
a verifiable answer (`payment-service`; the gateway 504s are downstream
symptoms).

**2. Real-log overlay — nine production systems from [LogHub](https://github.com/logpai/loghub).**
HDFS, OpenStack, Spark, Zookeeper, Hadoop, Apache, Linux, Mac, and Thunderbird
captures are interleaved into the index and drive a parser stress-test suite.
This is what stops the parser from being validated only against data its own
author shaped. The parser reaches **100% structured-field coverage across all
18,000 real lines** — and the last 8% came from a genuine bug the real data
exposed (Zookeeper thread names nest brackets: `QuorumPeer[myid=1]/0:0:...`,
which broke a non-greedy regex that synthetic logs would never have caught).

**Two limitations I'd state in an interview before being asked:**
- I authored both the corpus and the golden questions, which is a mild form of
  teaching to the test. Mitigated by writing questions from *what an SRE would
  ask* and including out-of-scope negatives, but not eliminated.
- Synthetic logs are cleaner than production. The LogHub overlay covers format
  diversity; it does not cover truncated multi-line stack traces or partially
  corrupted lines.

---

## Design decisions worth defending

**Two agents, not five.** Error-code mapping is a SQL join against a curated
table. Routing it through an LLM would add latency, cost, and a hallucination
surface to a query that SQL answers exactly. An agent earns its place by having
a distinct *reasoning* job; a lookup does not. The triage supervisor routes but
never answers.

**Cost routing is measured, not asserted.** Triage and structured extraction run
on Haiku; synthesis runs on Opus with adaptive thinking. Every call records
tokens and cost on its span, so `/metrics` reports actual `$/query` rather than
a claim about it.

**No vector database.** The corpus is ~1,200 chunks × 384 dims ≈ 1.8 MB of
float32; a brute-force matmul is sub-millisecond. An ANN index would add a
server, a schema, and a sync job to optimise something that isn't the
bottleneck. `retrieval/index.py` exposes the same interface a Qdrant client
would, so the swap is one file when the corpus outgrows RAM — which is around a
million chunks, and I'd say so rather than pretend the current design scales
forever.

**Chunking respects section boundaries.** A markdown section in a runbook is a
unit a human deliberately wrote; merging "## Remediation" with "## Escalation"
to fill a token budget dilutes the embedding toward the average of two topics.
`doc_chunk_tokens` is a *ceiling*, not a target — only very small sections merge.

**Guardrails are deterministic.** Every rule is a regex or a set operation, no
model call. Guardrails therefore cannot hallucinate, cost nothing, add no
latency, and behave identically in tests and production. An LLM judge is the
right tool for *quality* scoring and the wrong tool for an auditable safety gate.

**Confidence is composed, not self-reported.** Models are badly calibrated when
asked "how confident are you", and a self-reported number can't be audited. The
score combines retrieval strength, corroboration across distinct documents, and
whether the answer actually grounded itself — each independently checkable.

---

## Calibration: the number that makes escalation real

Confidence reads the **raw cosine** of the top hit, not the blended rank score.
That distinction matters and cost me a bug: blended scores are min-max
normalised per query, so the top hit is *always exactly 1.0* and carries no
information about whether retrieval found anything.

The relevance floor is measured, not guessed (`scripts/calibrate.py`):

```
on-corpus questions   (n=12)   min 0.719   mean 0.787   max 0.872
off-corpus questions  (n=8)    min 0.476   mean 0.509   max 0.566
                                                  separation 0.153
```

`RELEVANCE_FLOOR = 0.64` sits in that gap. Below it, confidence collapses and
the answer escalates regardless of how fluent or well-cited it is — which is
what makes *"Who won the 1998 world cup?"* escalate while *"How do I fix pool
exhaustion?"* answers confidently.

These constants are properties of (embedding model × corpus), not universal
truths — re-running the calibration is **mandatory** after changing either. That
turned out to matter: retuning the chunk size from 320 to 160 tokens more than
doubled the separation (0.070 → 0.153), so the same change that improved recall
also made the escalation boundary substantially more robust. I would not have
predicted that; the calibration script is what surfaced it.

---

## Evaluation

Three tiers, deliberately separated by cost and trust level:

| Tier | Metrics | Needs a model? | Runs in CI |
|---|---|---|---|
| Retrieval | recall@k, precision@k, MRR, hit rate | no | ✅ every commit |
| Behaviour | routing accuracy, injection blocked, out-of-scope escalated | no | ✅ every commit |
| Answer quality | faithfulness, coverage, citation validity | **yes** | on demand |

Current results (46 golden cases, offline mode, tuned configuration):

```
RETRIEVAL                      BEHAVIOUR
  recall@k        0.949          injection blocked        100%
  precision@k     0.414          out-of-scope escalated   100%
  MRR             0.836          routing accuracy         (see note)
  hit rate        1.000
```

### The sweep that produced those numbers

`scripts/evaluate.py --sweep` rebuilds the index at three chunk sizes and scores
five dense/BM25 blends against the golden set — 15 configurations:

| chunk tokens | dense weight | recall@k | MRR |
|---|---|---|---|
| 160 | **0.80** | **0.949** | **0.836** |
| 320 | 0.80 | 0.935 | 0.826 |
| 640 | 0.65 | 0.949 | 0.787 |
| 320 | 0.65 *(old default)* | 0.921 | 0.762 |
| 160 | 1.00 *(pure vector)* | 0.857 | 0.822 |
| 320 | 1.00 *(pure vector)* | 0.782 | 0.775 |

Two findings worth stating:

**Pure dense retrieval loses at every chunk size.** `dense_weight = 1.0` is the
worst or near-worst row in each block. BM25 is carrying exact-match tokens —
`PAY-5021`, `INV-3007`, `idx_reservations_sku_warehouse` — that the embedding
blurs into "some error code". That is the concrete argument for hybrid over
pure vector search, measured rather than asserted.

**Smaller chunks won.** 160 tokens beat 320 and 640. This corpus is dense
reference prose where a runbook step is a complete retrieval unit; larger chunks
average two topics into one vector. I would not assume this generalises to a
corpus of long narrative documents — which is the point of having the sweep.

The defaults in `config.py` are the sweep winners, with the measurement recorded
in the comment next to each.

**Answer quality reports `n/a` offline rather than a number.** Reporting a
metric you cannot actually measure is worse than reporting none — the stub's
output says nothing about a model's faithfulness.

**Routing accuracy is labelled by what measured it.** In offline mode the router
is a keyword heuristic, so the number (0.47) describes the stub, not the system.
The report prints `measured on: offline keyword stub` so the figure is never
mistaken for the triage model's performance.

The CI gate fails the build below `recall@k 0.70`, `MRR 0.60`, `injection
blocked 1.00`, `out-of-scope escalated 0.75`. Thresholds are deliberately
conservative: a gate that fails on noise gets disabled, and a disabled gate
protects nothing.

Parameter sweeps live in `scripts/evaluate.py --sweep` (chunk size × dense/BM25
blend), so retrieval tuning is an experiment with a record rather than a guess.

---

## Observability

Every model call emits an OpenTelemetry span using the **GenAI semantic
conventions** — `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.response.finish_reasons` — plus project attributes for cost, route,
confidence, and verdict.

Those conventions are still in **Development** status (not GA) as of mid-2026,
so the attribute keys are centralised in `observability/tracing.py` and the OTel
dependency is pinned. With no collector configured, spans are buffered in-process
so the dashboard works out of the box; set `AIOPS_OTLP_ENDPOINT` to export.

`/metrics` reports volume, escalation rate, p50/p95 latency, cost per query, and
a **drift proxy**: the share of low-confidence answers per day. For a RAG system
that is the drift that matters — rising low-confidence means incoming questions
have moved away from what the corpus covers, and the fix is corpus coverage, not
retraining.

---

## Project layout

```
src/aiops/
  config.py            all tunables in one place
  schemas.py           shared pydantic contracts
  llm.py               Anthropic client: routing, cost, tracing
  offline.py           deterministic stand-in for credential-free runs
  ingestion/
    corpus.py          synthetic Meridian corpus generator
    parsers.py         10 log formats + graceful fallback
    documents.py       frontmatter, section-aware chunking, trace windowing
  embedding/embedder.py    FastEmbed (ONNX, CPU)
  retrieval/
    index.py           hybrid dense + BM25, numpy, in-memory
    context.py         context engineering / prompt assembly
  knowledge/catalog.py     SQL error catalog, audit, escalation queue
  guardrails/rules.py      input, output, and escalation policy
  observability/tracing.py OTel GenAI spans
  agents/
    graph.py           LangGraph supervisor + agents
    prompts.py         all prompts, reviewable as a unit
  evaluation/harness.py    golden set + 3-tier metrics + CI gate
  api/server.py        FastAPI
  ui/app.py            Streamlit console
```

---

## Configuration

Environment variables use the `AIOPS_` prefix (see `config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | enables synthesis + quality grading |
| `AIOPS_FORCE_OFFLINE` | `0` | force the deterministic path (CI uses this) |
| `AIOPS_REASONING_MODEL` | `claude-opus-5` | synthesis |
| `AIOPS_CHEAP_MODEL` | `claude-haiku-4-5` | routing, extraction |
| `AIOPS_TOP_K` | `8` | retrieved chunks |
| `AIOPS_DENSE_WEIGHT` | `0.80` | dense vs BM25 blend (sweep optimum) |
| `AIOPS_DOC_CHUNK_TOKENS` | `160` | chunk ceiling (sweep optimum) |
| `AIOPS_CONFIDENCE_THRESHOLD` | `0.55` | escalation trigger |
| `AIOPS_OTLP_ENDPOINT` | — | OTel collector |

---

## What I'd do next

- **Deploy to Azure AI Foundry.** Deliberately deferred until the local system
  was complete; the orchestrator endpoint is the natural first piece.
- **Reranking.** A cross-encoder over the top ~30 candidates should lift MRR
  (currently 0.836) more than further chunk tuning — `log_evidence` is the
  weakest category at 0.559 MRR and the obvious first target.
- **A real triage evaluation.** Routing accuracy is currently only measurable
  against the offline keyword stub; the triage model's actual routing quality is
  unmeasured until the harness runs with a key.
- **Streaming responses** in the UI — synthesis currently blocks to completion.

## Licence and attribution

LogHub samples are redistributed under their original CC-BY licence
(`logpai/loghub`). The Meridian corpus is synthetic and authored for this
project; any resemblance to a real platform is coincidental.
