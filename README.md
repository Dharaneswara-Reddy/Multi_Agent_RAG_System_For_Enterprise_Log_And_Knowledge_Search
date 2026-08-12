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
uv run pytest -q                                   # 109 tests
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
| Evaluation | `evaluation/harness.py` | 95-case golden set, 3 metric tiers |
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

**1a. Hand-written backbone — "Meridian", a fictional e-commerce platform.**
Eighteen documents and five correlated incident scenarios, hand-written to be
*internally consistent*: `PAY-5021` appears in the logs, in the SQL catalog, in
a runbook, and in a post-mortem, and the payment → order → gateway failure
cascade shares trace IDs across services. That consistency is what makes
evaluation possible — a golden question like *"which service failed first?"* has
a verifiable answer (`payment-service`; the gateway 504s are downstream
symptoms). These eighteen remain the canonical labelled answers.

**1b. Generated expansion — 201 further documents over 42 more services.**
Eighteen documents is not enough to make retrieval *hard*, and a corpus that
small flatters every metric measured on it. The expansion (`ingestion/expansion/`)
adds 42 services, 66 error codes, 40 ADRs, 33 post-mortems, and 20 cross-cutting
guides — 219 documents and ~44,000 words in total.

It is generated from typed records rather than written as markdown literals,
because 200 hand-written near-identical runbooks differ only in noise, which
makes retrieval *look* hard while actually being trivial. The facts that matter
— a distinct failure mode, fix, and anti-pattern per fault — are authored per
entity; only the heading scaffolding is templated, exactly as a real
engineering wiki's runbook template would be.

Two properties are deliberate and load-bearing:

- **Near-misses.** The expansion adds a dozen connection-pool faults, several
  upstream-timeout faults, two clock-skew faults, and three fail-open decisions.
  These are genuine distractors for the original `INV-3007`, `PAY-5021`, and
  `AUTH-1015` questions. Retrieval now has to *rank*, not merely *find*.
- **Cross-references.** Documents cite each other by id, so multi-hop questions
  have an actual path to follow. `test_expansion.py` asserts that every
  referenced code, ADR, and incident resolves — a generated corpus does not
  crash when it is wrong, it renders fine and is quietly inconsistent.

The expansion never redefines one of the original seven error codes, which is
asserted by a test: if it did, the labelled answer for the original questions
would become ambiguous and recall would move for reasons unrelated to retrieval
quality.

**2. Real-log overlay — nine production systems from [LogHub](https://github.com/logpai/loghub).**
HDFS, OpenStack, Spark, Zookeeper, Hadoop, Apache, Linux, Mac, and Thunderbird
captures are interleaved into the index and drive a parser stress-test suite.
This is what stops the parser from being validated only against data its own
author shaped. The parser reaches **100% structured-field coverage across all
18,000 real lines** — and the last 8% came from a genuine bug the real data
exposed (Zookeeper thread names nest brackets: `QuorumPeer[myid=1]/0:0:...`,
which broke a non-greedy regex that synthetic logs would never have caught).

**Limitations I'd state in an interview before being asked:**
- I authored both the corpus and the golden questions, which is a mild form of
  teaching to the test. Mitigated by writing questions from *what an SRE would
  ask* and including out-of-scope negatives, but not eliminated.
- Synthetic logs are cleaner than production. The LogHub overlay covers format
  diversity; it does not cover truncated multi-line stack traces or partially
  corrupted lines.
- **The 201 expansion documents share a house style**, because one author wrote
  the underlying records and one template renders them. A real wiki has a dozen
  authors, inconsistent structure, stale pages that contradict current ones, and
  documents that simply do not answer the question they appear to. My corpus is
  more uniform and more correct than any real one, which almost certainly still
  flatters retrieval — just by less than 18 documents did.
- **The logs cover only the original seven services.** The 42 expansion services
  have documentation but no log lines, so `log_evidence` questions are still
  scored against the original incident set. Retrieval got harder for document
  questions and did not get harder for log questions.

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

**No vector database.** The corpus is ~1,560 chunks × 384 dims ≈ 1.8 MB of
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
on-corpus questions   (n=24)   min 0.602   mean 0.772   max 0.863
off-corpus questions  (n=8)    min 0.458   mean 0.502   max 0.554
                                                  separation 0.048
```

`RELEVANCE_FLOOR = 0.58` sits in that gap. Below it, confidence collapses and
the answer escalates regardless of how fluent or well-cited it is — which is
what makes *"Who won the 1998 world cup?"* escalate while *"How do I fix pool
exhaustion?"* answers confidently.

These constants are properties of (embedding model × corpus), not universal
truths — re-running the calibration is **mandatory** after changing either.

**The separation narrowed when the corpus grew, and that is the honest result.**
On the 18-document corpus this gap was 0.153. It is now 0.048, and the floor had
to come *down* from 0.64 to 0.58 — at 0.64 it would sit above the on-corpus
minimum of 0.602 and over-escalate legitimate questions about thinly-covered
services.

Two things changed together, and I would not separate them in an interview:
the corpus went from 18 to 219 documents, and the probe set went from 12
questions covering seven services to 24 covering all 42. The old probes only
ever asked about the corpus's densest, best-covered region, so the old 0.153
was partly a measurement artefact.

The underlying effect is real, though: the more a corpus contains, the less a
single cosine score separates "covered" from "not covered", because there is
almost always *something* moderately similar. That is the argument for
confidence being a composite — corroboration across distinct sources and
citation grounding carry weight precisely because the retrieval term alone
degrades with scale.

---

## Evaluation

Three tiers, deliberately separated by cost and trust level:

| Tier | Metrics | Needs a model? | Runs in CI |
|---|---|---|---|
| Retrieval | recall@k, precision@k, MRR, hit rate | no | ✅ every commit |
| Behaviour | routing accuracy, injection blocked, out-of-scope escalated | no | ✅ every commit |
| Answer quality | faithfulness, coverage, citation validity | **yes** | on demand |

Current results (95 golden cases over 219 documents, offline mode, tuned
configuration):

```
RETRIEVAL                      BEHAVIOUR
  recall@k        0.949          injection blocked        100%
  precision@k     0.238          out-of-scope escalated   100%
  MRR             0.830          routing accuracy         (see note)
  hit rate        0.989
```

**Read `precision@k` carefully — its denominator is the number of distinct
documents surfaced, not `k`.** With 219 documents competing and a mean of 1.41
labelled documents per question, the top-8 chunks now come from 5.9 distinct
documents on average rather than clustering inside one or two. More competing
documents surface, so the denominator grows and precision falls even where
recall does not. It fell from 0.414 to 0.238 for that reason, not because
ranking got worse — MRR is essentially flat (0.836 → 0.830).

### Scaling the corpus 12× — what actually changed

The corpus deliberately grew from 18 to 219 documents to test whether the
original numbers were measuring retrieval quality or just a corpus too small to
be confusing. Same 43 original questions, same configuration, only the corpus
changed:

| | 18 documents | 219 documents |
|---|---|---|
| recall@k | 0.949 | 0.847 |
| MRR | 0.836 | 0.773 |
| hit rate | 1.000 | 0.944 |

**Some of the original score was corpus size, not retrieval quality.** Adding
plausible neighbours cost 10 points of recall on questions that had not changed
at all. Retuning on the larger corpus recovers recall to 0.949 across the full
95-case set, but the honest statement is that the first number flattered the
system.

### The sweep, re-run at scale

`scripts/evaluate.py --sweep` rebuilds the index at three chunk sizes and scores
five dense/BM25 blends — 15 configurations. **The optimum moved when the corpus
grew**, which is the single most useful thing the expansion produced:

| chunk tokens | dense weight | recall@k | MRR | |
|---|---|---|---|---|
| 320 | **0.65** | **0.949** | 0.830 | new optimum |
| 640 | 0.50 | 0.939 | 0.845 | |
| 320 | 0.50 | 0.932 | 0.830 | |
| 640 | 0.80 | 0.924 | 0.861 | best MRR |
| 160 | 0.80 | 0.915 | 0.863 | *old optimum* |
| 640 | 1.00 *(pure vector)* | 0.892 | 0.815 | |
| 320 | 1.00 *(pure vector)* | 0.886 | 0.823 | |
| 160 | 1.00 *(pure vector)* | 0.875 | 0.827 | |

Three findings:

**Pure dense retrieval still loses at every chunk size, and by a wider margin.**
`dense_weight = 1.0` is the worst row in every block. BM25 carries exact-match
tokens — `PAY-5021`, `INV-3007`, `idx_reservations_sku_warehouse` — that the
embedding blurs into "some error code". With 73 error codes across 42 services
that blurring costs more than it did with 7, so the argument for hybrid over
pure vector got *stronger* with scale.

**The optimal chunk size doubled, 160 → 320.** On the small corpus I concluded
that smaller chunks win because a runbook section is a complete retrieval unit
and larger chunks average two topics into one vector. That reasoning was right
about chunks and wrong about the problem: with 219 documents a query has
hundreds of plausible neighbours, and fine-grained chunks fragment a document
into many weak candidates that split its evidence between them. Larger chunks
carry enough surrounding context to win the comparison. **The conclusion I drew
from the first sweep did not survive contact with a realistic corpus.**

**BM25's optimal share grew, 0.20 → 0.35.** Same cause, from the other
direction: as near-duplicate documents multiply, exact tokens discriminate and
embeddings converge.

160 tokens still wins on MRR (0.863 against 0.830). The tie is broken on recall
because `top_k=8` chunks reach the synthesiser either way — having the right
document in the context matters more than its exact rank within it.

The defaults in `config.py` are the sweep winners, with the measurement and the
superseded reasoning recorded in the comment next to each.

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
  (currently 0.830) more than further chunk tuning — `log_evidence` is the
  weakest category at 0.366 MRR and the obvious first target. It got worse with
  the expansion (0.559 → 0.366): log chunks now compete with 416 document chunks
  for the same eight slots, and a hybrid blend tuned for document retrieval is
  not tuned for them. A per-route weight is probably the cheaper fix.
- **Log coverage for the expansion services.** 42 services have runbooks and no
  logs, which caps what the log analyst can be evaluated on. Generating
  correlated incidents for a subset would let `log_evidence` face the same
  distractor pressure the document questions now do.
- **A real triage evaluation.** Routing accuracy is currently only measurable
  against the offline keyword stub; the triage model's actual routing quality is
  unmeasured until the harness runs with a key.
- **Streaming responses** in the UI — synthesis currently blocks to completion.

## Licence and attribution

LogHub samples are redistributed under their original CC-BY licence
(`logpai/loghub`). The Meridian corpus is synthetic and authored for this
project; any resemblance to a real platform is coincidental.
