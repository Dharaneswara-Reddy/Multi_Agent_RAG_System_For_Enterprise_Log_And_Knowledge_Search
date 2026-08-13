# syntax=docker/dockerfile:1.7
#
# AI Ops Copilot — production image for ECS Fargate.
#
# The shape of this file is driven by one fact: the retrieval stack is two ONNX
# models (~194 MB on disk) plus an index that costs a full embedding pass over
# the corpus to build. Both are *startup* costs, and on Fargate a task that pays
# them is a task that fails its ALB health check before it serves anything. So
# both are moved into the image build, where they are paid once per release
# rather than once per task.
#
# Stages:
#   base          runtime floor — interpreter, env, non-root user
#   builder       uv + dependency install into /opt/venv
#   models        pre-downloads the two ONNX models into the image
#   assets-baked  runs scripts/setup.py to produce data/ (corpus, DB, index)
#   assets-none   the same directories, empty — for the S3-supplied index path
#   runtime       the shipped image: no uv, no curl, no build toolchain
#
# One image serves both processes. The Streamlit UI imports the graph in
# process rather than calling the API over HTTP (see ui/app.py's docstring), so
# it needs the same index, the same models and the same dependency tree as the
# API. Two images would differ only in their entrypoint argument, which is not
# a difference worth a second build and a second thing to keep in step.

# Global build arguments. Declared before the first FROM so they can be used in
# FROM lines; re-declared inside a stage when a RUN or ENV needs them.
ARG PYTHON_VERSION=3.11-slim-bookworm
ARG UV_VERSION=0.8.11

# `baked` ships a ready-to-serve index in the image; `none` ships empty data
# directories and expects AIOPS_INDEX_URI to point at S3 (or a mounted volume)
# at runtime. See docs/docker.md for which to pick.
ARG INDEX_MODE=baked

# Kept in step with src/aiops/config.py by an assertion in the assets stage —
# they are duplicated here rather than read from config so that the expensive
# model-download layer does not invalidate every time application source
# changes.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ARG RERANKER_MODEL=Xenova/ms-marco-MiniLM-L-12-v2

# Optional dependency extras to install: `postgres` pulls psycopg 3, `s3` pulls
# boto3, `cloud` pulls both.
#
# `cloud` by default, because every consumer of this image is a cloud one — ECS
# reads its credentials from Secrets Manager and its index from S3, and compose
# runs against Postgres. An image built without these starts happily and then
# fails on the first database call, which is the worst place to discover a
# missing driver. Build with --build-arg AIOPS_EXTRAS= for a SQLite-only image.
ARG AIOPS_EXTRAS=cloud

# Bytecode-compiling site-packages costs roughly 80 MB of image and saves the
# compile pass on every process start. Worth it for a long-lived service whose
# layers are cached on the host after the first pull; set to 0 if pull time
# matters more than start time.
ARG UV_COMPILE_BYTECODE=1


# ---------------------------------------------------------------------------
# base — everything every later stage and the final image agree on
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS base

# The official python images are built against libsqlite3, which the README
# calls out as a hard requirement (some pyenv builds omit the sqlite3 module and
# the SQLite backend then fails at import). Using this image rather than
# compiling an interpreter is what makes that a non-issue rather than a
# documented footgun.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # The venv is on PATH rather than activated: there is no shell to activate
    # it in when the entrypoint execs directly into uvicorn.
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    # config.ROOT is derived from the location of config.py on disk
    # (parents[2]), so the package must be imported from the source tree. An
    # installed copy in site-packages would resolve data_dir to somewhere inside
    # the venv. This is why the project itself is never pip-installed here.
    PYTHONPATH=/app/src \
    # FastEmbed otherwise caches to $TMPDIR/fastembed_cache, which in a
    # container is a fresh ~194 MB download on every cold start. Note the path
    # deliberately contains no "tmp" component: fastembed's tar.gz fallback
    # rmtree()s a directory when "tmp" appears in its cache path.
    FASTEMBED_CACHE_PATH=/opt/aiops/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    # The tokenizers Rust extension warns and disables itself after a fork;
    # saying so up front keeps the log clean.
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# uid/gid are fixed so a bind-mounted volume has predictable ownership, and
# high enough not to collide with anything Debian ships.
RUN groupadd --system --gid 10001 aiops \
 && useradd --system --uid 10001 --gid aiops --create-home --home-dir /home/aiops aiops


# ---------------------------------------------------------------------------
# builder — dependencies only, resolved from uv.lock
# ---------------------------------------------------------------------------
FROM base AS builder

ARG UV_VERSION
ARG AIOPS_EXTRAS
ARG UV_COMPILE_BYTECODE

# Copying the uv binary from its published image rather than curl|sh: it pins an
# exact version and adds no installer script to audit.
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=${UV_COMPILE_BYTECODE} \
    # Hardlinks fail across the cache mount's filesystem boundary; copying is
    # the documented fix and costs a little build I/O, not image size.
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Only the lockfile and the manifest, so this layer is invalidated by a
# dependency change and not by an application change.
COPY pyproject.toml uv.lock ./

# --no-install-project: only the dependencies. The project is not installed at
# all — see PYTHONPATH above for why. --frozen uses the lock exactly as written
# and never re-resolves, so the image cannot drift from what CI tested.
#
# Every one of the 105 locked packages publishes a manylinux wheel for cp311,
# so no compiler is needed here. That is checked rather than assumed; if it ever
# stops being true this stage fails loudly instead of silently needing gcc.
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    extras=""; \
    for extra in $(printf '%s' "${AIOPS_EXTRAS}" | tr ',' ' '); do \
        extras="${extras} --extra ${extra}"; \
    done; \
    uv sync --frozen --no-install-project --no-dev ${extras}


# ---------------------------------------------------------------------------
# models — bake the ONNX models into the image
# ---------------------------------------------------------------------------
# This is the layer the whole file exists for. ~194 MB downloaded once at build
# time instead of on every container's first query.
FROM builder AS models

ARG EMBEDDING_MODEL
ARG RERANKER_MODEL

ENV AIOPS_EMBEDDING_MODEL=${EMBEDDING_MODEL} \
    AIOPS_RERANKER_MODEL=${RERANKER_MODEL}

# The models are fetched by constructing the same classes the application
# constructs, rather than by pulling known URLs: FastEmbed maps a model name
# onto a repository (bge-small-en-v1.5 actually resolves to a quantised Qdrant
# mirror), and hardcoding the resolved repository would break silently the day
# that mapping changes. Each model is then *used* once, because a download that
# lands a corrupt or incomplete ONNX graph should fail this build rather than
# the first production query.
RUN --mount=type=cache,target=/root/.cache/uv \
    python <<'PY'
import os

from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

embedding_model = os.environ["AIOPS_EMBEDDING_MODEL"]
reranker_model = os.environ["AIOPS_RERANKER_MODEL"]

print(f"warming {embedding_model}", flush=True)
vectors = list(TextEmbedding(model_name=embedding_model).embed(["warm the onnx session"]))
assert len(vectors) == 1 and vectors[0].shape[0] > 0, "embedding model produced no vector"

print(f"warming {reranker_model}", flush=True)
scores = list(TextCrossEncoder(model_name=reranker_model).rerank("query", ["candidate"]))
assert len(scores) == 1, "reranker produced no score"

print(f"models cached in {os.environ['FASTEMBED_CACHE_PATH']}", flush=True)
PY


# ---------------------------------------------------------------------------
# assets-baked — corpus, seeded catalog and prebuilt index
# ---------------------------------------------------------------------------
FROM models AS assets-baked

# curl for scripts/fetch_loghub.sh. Installed here, in a stage that is
# discarded, so it never reaches the runtime image's attack surface.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl

COPY src ./src
COPY scripts ./scripts
COPY data/eval ./data/eval

# Fail the build if the models baked above are not the ones config.py asks for.
# Without this the drift is invisible: setup.py would simply download the real
# model during the build, the image would ship both, and the size regression
# would be the only symptom.
RUN python <<'PY'
import os
import sys

from aiops.config import settings

expected = {
    "embedding_model": os.environ["AIOPS_EMBEDDING_MODEL"],
    "reranker_model": os.environ["AIOPS_RERANKER_MODEL"],
}
drift = {
    name: (getattr(settings, name), baked)
    for name, baked in expected.items()
    if getattr(settings, name) != baked
}
if drift:
    for name, (configured, baked) in drift.items():
        print(f"  {name}: config.py says {configured!r}, image baked {baked!r}", file=sys.stderr)
    sys.exit(
        "Model drift: the Dockerfile's EMBEDDING_MODEL/RERANKER_MODEL build args "
        "no longer match src/aiops/config.py. Update the build args."
    )
print("baked models match config.py", flush=True)
PY

# AIOPS_FORCE_OFFLINE is what makes this step buildable at all: setup.py ends in
# a smoke query, and there is no API key at build time. Offline mode makes that
# query deterministic and free.
#
# The LogHub fetch inside setup.py is allowed to fail — it is an overlay on a
# corpus that stands on its own, and a build that breaks because a raw
# githubusercontent URL had a bad minute is a worse trade than a slightly
# thinner log set. setup.py already treats it that way.
RUN --mount=type=cache,target=/root/.cache/uv \
    AIOPS_FORCE_OFFLINE=1 python scripts/setup.py

# The index build wrote a SQLite database at data/aiops.db as a side effect of
# seeding the catalog. It is kept (86 KB) because it makes the no-Postgres path
# work out of the box; a deployment with AIOPS_DB_URL set never reads it, and
# the API re-seeds the catalog on startup either way.


# ---------------------------------------------------------------------------
# assets-none — the same directory shape, no contents
# ---------------------------------------------------------------------------
# Selected with --build-arg INDEX_MODE=none. BuildKit only builds the stages the
# target actually depends on, so choosing this genuinely skips the embedding
# pass rather than branching inside a RUN.
FROM base AS assets-none

RUN mkdir -p /app/data/docs /app/data/logs /app/data/index /app/data/eval /app/data/index-cache

COPY data/eval ./data/eval


# ---------------------------------------------------------------------------
# assets — whichever of the two the build selected
# ---------------------------------------------------------------------------
ARG INDEX_MODE
FROM assets-${INDEX_MODE} AS assets


# ---------------------------------------------------------------------------
# runtime — what actually ships
# ---------------------------------------------------------------------------
FROM base AS runtime

ARG INDEX_MODE

LABEL org.opencontainers.image.title="AI Ops Copilot" \
      org.opencontainers.image.description="Multi-agent RAG over enterprise logs and knowledge" \
      org.opencontainers.image.source="https://github.com/GojoV339/Multi_Agent_RAG_System_For_Enterprise_Log_And_Knowledge_Search" \
      org.opencontainers.image.licenses="MIT"

# Root-owned and read-only to the application. A missing model here means the
# image was built wrong, and the entrypoint's preflight says so in one line
# rather than letting FastEmbed try a 194 MB download from a container that may
# have no egress.
COPY --from=builder /opt/venv /opt/venv
COPY --from=models /opt/aiops/models /opt/aiops/models

# Source last among the root-owned copies: it changes most often, so everything
# above it stays cached across ordinary code changes.
COPY src ./src
COPY scripts ./scripts
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# data/ is the one tree the application writes to — the SQLite database, the
# audit trail, and a runtime index rebuild all land here.
COPY --from=assets --chown=aiops:aiops /app/data ./data

RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /app/data/index-cache \
 && chown aiops:aiops /app/data /app/data/index-cache

ENV AIOPS_INDEX_MODE=${INDEX_MODE} \
    AIOPS_API_PORT=8000 \
    AIOPS_UI_PORT=8501 \
    # One worker. Each uvicorn worker is a separate process with its own copy of
    # the index and its own ONNX sessions, so N workers cost N times the memory
    # to serve a workload whose bottleneck is CPU-bound reranking anyway. Scale
    # with Fargate tasks behind the ALB, not with workers inside one task.
    AIOPS_API_WORKERS=1 \
    # ONNX Runtime and OpenMP size their thread pools from the host's core
    # count, which on Fargate is the *instance's*, not the task's. Left
    # unbounded a 0.5 vCPU task spawns dozens of threads and thrashes. Set this
    # to the task's vCPU allocation.
    OMP_NUM_THREADS=2 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    HOME=/home/aiops

USER aiops

EXPOSE 8000 8501

# Targets the API, which is this image's primary role; the compose file
# overrides it for the UI service, whose equivalent is /_stcore/health. Written
# against urllib rather than curl so the runtime image needs no extra package.
#
# start-period covers loading the index and both ONNX sessions on a cold task.
# Raise it when AIOPS_BUILD_INDEX_ON_START=1, where the entrypoint runs a full
# embedding pass before the server binds at all.
#
# Note for ECS: this instruction is ignored unless the task definition repeats
# it as a `healthCheck` block. The ALB target group's health check is what
# actually decides whether a task receives traffic.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('AIOPS_API_PORT','8000')}/health\", timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
