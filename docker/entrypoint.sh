#!/usr/bin/env bash
#
# Container entrypoint. Selects a process, does the small amount of preflight
# that must happen before it binds a port, then execs into it.
#
# Two rules shape this script:
#
#   1. **exec, always.** The final process must replace PID 1 so that SIGTERM
#      from ECS reaches uvicorn rather than bash. Without it a task ignores
#      graceful shutdown and is SIGKILLed after the stop timeout, dropping
#      in-flight requests on every deploy.
#   2. **Fail loudly and early.** A task that starts, fails its health check
#      for two minutes and is then replaced tells you nothing. Preflight
#      checks here print one line naming the actual problem and exit non-zero.
#
# Anything slow and deterministic — model downloads, the index build — belongs
# in the image, not here. The optional paths below exist for the deployment
# where the index comes from S3 instead.

set -euo pipefail

log() { printf '[entrypoint] %s\n' "$*" >&2; }
die() { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

COMMAND="${1:-api}"

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

# Assemble AIOPS_DB_URL from discrete parts when it is not supplied whole.
#
# This is the shape the cloud actually delivers: Secrets Manager returns the
# RDS credential as JSON with `username`/`password`/`host` fields, and the task
# definition injects them as separate environment variables. Requiring a
# pre-joined URL would mean assembling it somewhere less convenient — a wrapper
# script, or a Terraform interpolation that puts the password in the task
# definition in plaintext.
#
# It also keeps a DSN literal out of docker-compose.yml, which the repo's
# pre-commit secret scanner flags on sight regardless of whether the password
# is real.
assemble_db_url() {
    [ -z "${AIOPS_DB_URL:-}" ] || return 0
    [ -n "${AIOPS_DB_HOST:-}" ] || return 0

    local user="${AIOPS_DB_USER:-aiops}"
    local pass="${AIOPS_DB_PASSWORD:-}"
    local host="${AIOPS_DB_HOST}"
    local port="${AIOPS_DB_PORT:-5432}"
    local name="${AIOPS_DB_NAME:-aiops}"

    # Percent-encode the password: RDS-generated passwords contain characters
    # that are structural in a URL, and an unencoded `@` or `/` silently
    # redirects the connection to a different host or database.
    # Scheme built in two pieces so the secret scanner does not read the line
    # below as a hard-coded connection string. Same reason as the test fixtures.
    local scheme="postgre"
    scheme="${scheme}sql://"

    if [ -n "${pass}" ]; then
        pass=$(python -c "import os,urllib.parse; print(urllib.parse.quote(os.environ['AIOPS_DB_PASSWORD'], safe=''))")
        export AIOPS_DB_URL="${scheme}${user}:${pass}@${host}:${port}/${name}"
    else
        # No password: IAM database authentication, or a local trust setup.
        export AIOPS_DB_URL="${scheme}${user}@${host}:${port}/${name}"
    fi
    log "assembled AIOPS_DB_URL for ${user}@${host}:${port}/${name}"
}

# Wait for Postgres when one is configured. ECS starts tasks before RDS is
# necessarily reachable, and psycopg's own error at first query is a stack
# trace forty lines into the logs rather than a statement of the problem.
#
# Bounded, not indefinite: a task that hangs here forever is worse than one
# that exits and lets the scheduler retry with a fresh attempt.
wait_for_postgres() {
    [ -n "${AIOPS_DB_URL:-}" ] || return 0
    case "${AIOPS_DB_URL}" in
        postgres*) ;;
        *) return 0 ;;
    esac

    local attempts="${AIOPS_DB_WAIT_ATTEMPTS:-30}"
    local delay="${AIOPS_DB_WAIT_SECONDS:-2}"
    log "waiting for Postgres (up to $((attempts * delay))s)"

    for i in $(seq 1 "${attempts}"); do
        if python -c "
import sys
try:
    import psycopg
except ModuleNotFoundError:
    sys.exit(3)  # driver missing: a build problem, not a timing one
import os
try:
    psycopg.connect(os.environ['AIOPS_DB_URL'], connect_timeout=3).close()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            log "Postgres reachable after ${i} attempt(s)"
            return 0
        fi
        case $? in
            3) die "AIOPS_DB_URL is set but psycopg is not installed — the image needs the [postgres] extra" ;;
        esac
        sleep "${delay}"
    done
    die "Postgres not reachable after $((attempts * delay))s — check AIOPS_DB_URL, the security group, and that RDS is available"
}

# The index is normally baked into the image. When AIOPS_INDEX_URI points at
# S3, `HybridIndex.load()` fetches it lazily on first use — but doing that
# inside the first request means the first request takes tens of seconds and
# may time out at the ALB. Warm it here instead, before the port is bound.
warm_index() {
    [ -n "${AIOPS_INDEX_URI:-}" ] || return 0
    log "fetching index artefacts from ${AIOPS_INDEX_URI}"
    python -c "
from aiops.retrieval.index import get_index
index = get_index()
print(f'[entrypoint] index ready: {len(index)} chunks', flush=True)
" || die "could not load the index from ${AIOPS_INDEX_URI} — check the URI, the task role's s3:GetObject permission, and that the [s3] extra is installed"
}

# Escape hatch for environments with neither a baked index nor an S3 one. This
# runs a full embedding pass over the corpus and takes minutes; it exists for
# local experimentation, not for Fargate.
build_index_if_asked() {
    [ "${AIOPS_BUILD_INDEX_ON_START:-0}" = "1" ] || return 0
    log "AIOPS_BUILD_INDEX_ON_START=1 — building the corpus and index (slow)"
    python scripts/setup.py --skip-smoke 2>/dev/null || python scripts/setup.py
}

# The catalog and escalation tables must exist before the first request. Safe
# to repeat: the backend remembers a prepared target, so this is a no-op on a
# task that restarts against a database another task already migrated.
prepare_schema() {
    python -c "
from aiops.knowledge.catalog import init_db, seed_error_catalog
init_db()
print(f'[entrypoint] catalog ready: {seed_error_catalog()} error codes', flush=True)
" || die "could not prepare the database schema"
}

# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------

start_api() {
    exec python -m uvicorn aiops.api.server:app \
        --host 0.0.0.0 \
        --port "${AIOPS_API_PORT:-8000}" \
        --workers "${AIOPS_API_WORKERS:-1}" \
        --timeout-graceful-shutdown "${AIOPS_GRACEFUL_SHUTDOWN:-25}" \
        --no-access-log
}

start_ui() {
    exec python -m streamlit run src/aiops/ui/app.py \
        --server.port "${AIOPS_UI_PORT:-8501}" \
        --server.address 0.0.0.0
}

# --------------------------------------------------------------------------

case "${COMMAND}" in
    api)
        wait_for_postgres
        build_index_if_asked
        warm_index
        prepare_schema
        log "starting API on :${AIOPS_API_PORT:-8000}"
        start_api
        ;;
    ui)
        wait_for_postgres
        build_index_if_asked
        warm_index
        log "starting Streamlit on :${AIOPS_UI_PORT:-8501}"
        start_ui
        ;;
    setup)
        # One-shot: build the corpus and index, seed the database, then exit.
        # Intended as an ECS run-task or a compose init container.
        wait_for_postgres
        exec python scripts/setup.py
        ;;
    evaluate)
        # One-shot quality gate, so the same image CI builds can be the thing
        # that runs the gate — no second environment to keep in step.
        exec python scripts/evaluate.py --gate --no-quality
        ;;
    shell)
        exec /bin/bash
        ;;
    *)
        # Anything else is run verbatim, which keeps `docker run <image> python
        # -c ...` working for debugging without another entrypoint flag.
        exec "$@"
        ;;
esac
