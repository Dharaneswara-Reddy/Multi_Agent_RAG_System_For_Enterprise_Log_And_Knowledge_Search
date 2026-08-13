# Container image

One image serves both processes. The Streamlit console imports the graph in
process rather than calling the API over HTTP, so it needs the same index, the
same models and the same dependency tree — two images would differ only in an
entrypoint argument, which is not worth a second build and a second thing to
keep in step.

> **Never built.** This was written on a CPU-only machine with ~5GB RAM free,
> where a build would download ~250MB of models and run a full embedding pass.
> The Dockerfile is reviewed and `docker compose config` validates, but no image
> has been produced from it. Expect to fix something on the first build.

## Build

```bash
docker build -t aiops-copilot:local .           # baked index (default)
docker build -t aiops-copilot:s3 \
  --build-arg INDEX_MODE=none .                 # index supplied at runtime
```

Roughly 10 minutes and ~1.2GB, most of it the ONNX runtime, torch-free
FastEmbed wheels and the two models.

### Stages

| Stage | Purpose |
|---|---|
| `base` | interpreter, env, non-root user |
| `builder` | `uv sync --frozen` into `/opt/venv` |
| `models` | pre-downloads both ONNX models |
| `assets-baked` | runs `scripts/setup.py` → corpus, DB, index |
| `assets-none` | the same directories, empty |
| `runtime` | shipped image: no uv, no curl, no build toolchain |

**Why models are baked in.** They are ~250MB and FastEmbed downloads them on
first use. A Fargate task that pays that at startup fails its ALB health check
before it serves anything, and pays it again on every scale-out. Baking moves
the cost to once per release.

**Why the index is baked in by default, and how to opt out.** Building it costs
a full embedding pass over the corpus. Baking makes a task start ready; the
trade is that a reindex requires an application release. `INDEX_MODE=none` plus
`AIOPS_INDEX_URI=s3://…` decouples them, which is the right choice once the
corpus changes more often than the code.

## Run

```bash
docker run -p 8000:8000 -e AIOPS_FORCE_OFFLINE=1 aiops-copilot:local
docker run -p 8501:8501 -e AIOPS_FORCE_OFFLINE=1 aiops-copilot:local ui
```

The entrypoint takes a command: `api` (default), `ui`, `setup`, `evaluate`,
`shell`, or anything else run verbatim.

`evaluate` runs the quality gate inside the same image CI builds, so there is no
second environment to keep in step with the one that actually serves traffic.

## Local stack

```bash
docker compose up
```

API on `:8000`, console on `:8501`, Postgres on `:5432`. This exists to
exercise the **cloud** configuration — the Postgres backend and the container
entrypoint — not as a nicer way to develop. For that, `uv run uvicorn` against
SQLite is faster and needs no build.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `AIOPS_DB_URL` | — | Postgres DSN. Unset means SQLite. |
| `AIOPS_DB_HOST` / `_USER` / `_PASSWORD` / `_NAME` / `_PORT` | — | Assembled into `AIOPS_DB_URL` by the entrypoint |
| `AIOPS_INDEX_URI` | — | `s3://bucket/prefix/` for runtime index fetch |
| `AIOPS_FORCE_OFFLINE` | — | `1` = deterministic extractive answers, no API key |
| `ANTHROPIC_API_KEY` | — | Enables synthesis |
| `AIOPS_API_PORT` / `AIOPS_UI_PORT` | `8000` / `8501` | |
| `OMP_NUM_THREADS` | `2` | **Set this to the task's vCPU count** |
| `AIOPS_BUILD_INDEX_ON_START` | `0` | Build the index at startup (slow; local only) |
| `FASTEMBED_CACHE_PATH` | baked path | Where models live |

**`OMP_NUM_THREADS` matters more than it looks.** ONNX Runtime and OpenMP size
their thread pools from the host's core count, which on Fargate is the
*instance's*, not the task's. Left unbounded, a 0.5 vCPU task spawns dozens of
threads and thrashes.

## Why the entrypoint does what it does

**It `exec`s.** The final process must replace PID 1 or SIGTERM from ECS reaches
bash instead of uvicorn — the task then ignores graceful shutdown and is
SIGKILLed after the stop timeout, dropping in-flight requests on every deploy.

**It waits for Postgres, bounded.** ECS starts tasks before RDS is necessarily
reachable. Unbounded waiting is worse than exiting: a task stuck forever never
gets replaced by the scheduler.

**It warms the index before binding a port.** With `AIOPS_INDEX_URI`, a lazy
first fetch would happen inside the first request, which then takes tens of
seconds and may time out at the ALB.

**It fails loudly.** A task that starts, fails health checks for two minutes and
is replaced tells you nothing. Each preflight check prints one line naming the
actual problem and exits non-zero.

## Notes

- Runs as non-root (`aiops`).
- `HEALTHCHECK` targets `/health`. **ECS ignores it** unless the task definition
  repeats it as a `healthCheck` block — the Terraform does. The ALB target group
  is what actually decides whether a task receives traffic.
- Built for `linux/arm64` in CI to match the Fargate `runtime_platform`. Pass
  `--platform` to match your machine when building locally.
