# ---------------------------------------------------------------------------
# Identity and placement
# ---------------------------------------------------------------------------

variable "project" {
  description = "Short name prefixed onto every resource. Lower-case, no spaces."
  type        = string
  default     = "aiops"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project))
    error_message = "project must be 2-21 chars, lower-case alphanumeric or hyphen, starting with a letter."
  }
}

variable "environment" {
  description = <<-EOT
    Environment name. Every resource name carries it, so dev and prod can share
    an account without colliding, and cost allocation can split them apart.
    Some defaults key off it (see locals.tf): `prod` gets deletion protection
    and a per-AZ NAT gateway, everything else gets the cheap configuration.
  EOT
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,12}$", var.environment))
    error_message = "environment must be 2-13 chars, lower-case alphanumeric or hyphen, starting with a letter."
  }
}

variable "region" {
  description = <<-EOT
    AWS region. us-east-1 is the default because it is the cheapest region for
    Fargate and ALB, which are the two largest line items here. If the corpus
    or the users are elsewhere, change it — none of this stack is region-locked.
  EOT
  type        = string
  default     = "us-east-1"
}

variable "extra_tags" {
  description = "Additional tags merged into default_tags on every resource."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR for the VPC. /16 leaves plenty of room; nothing here needs it."
  type        = string
  default     = "10.42.0.0/16"
}

variable "az_count" {
  description = <<-EOT
    Availability zones to spread across. Two is the ALB minimum and the point
    of diminishing returns for a service that runs 1-4 tasks; a third AZ adds a
    third NAT gateway and a third copy of every interface endpoint for no extra
    resilience at this size.
  EOT
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3 (an ALB requires at least two subnets in distinct AZs)."
  }
}

variable "nat_gateway_mode" {
  description = <<-EOT
    How private subnets reach the internet. This is the single largest cost
    decision in the stack, so it is a first-class variable rather than a
    hard-coded choice. See modules/network/main.tf for the full arithmetic.

      "none"    - no NAT at all. Only viable together with VPC endpoints, and
                  it disables answer synthesis: api.anthropic.com is not an AWS
                  service and has no endpoint. The copilot degrades to its
                  offline extractive mode, which is a real deployment (retrieval,
                  guardrails, tracing and escalation all still work) but not the
                  full one. $0/month.
      "single"  - one NAT gateway in the first AZ, shared by every private
                  subnet. ~$32.85/month. If that AZ fails, tasks in the other AZ
                  keep serving from the ALB but lose outbound internet, so
                  synthesis fails while retrieval continues.
      "per_az"  - one NAT gateway per AZ. ~$65.70/month for two. No shared
                  failure domain, and no cross-AZ data charge on egress.

    Default follows `environment`: per_az for prod, single otherwise.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.nat_gateway_mode == null || contains(["none", "single", "per_az"], coalesce(var.nat_gateway_mode, "single"))
    error_message = "nat_gateway_mode must be one of: none, single, per_az (or null to follow the environment default)."
  }
}

variable "enable_interface_endpoints" {
  description = <<-EOT
    Create interface (PrivateLink) endpoints for ECR API, ECR Docker, CloudWatch
    Logs and Secrets Manager.

    **This does not save money at this scale and the code does not pretend it
    does.** Four interface endpoints x 2 AZs x $0.01/hour is $58.40/month, which
    is more than the $32.85 single NAT gateway they would replace — and they
    cannot replace it anyway, because the workload has to reach api.anthropic.com.

    What they actually buy is posture: AWS control-plane traffic never leaves the
    VPC, so a compromised task cannot exfiltrate to an attacker-controlled host
    over the same path it uses to pull an image, and endpoint policies can scope
    what the VPC may call. Turn it on when that is worth $58/month; leave it off
    for a demo.

    The S3 *gateway* endpoint is separate and always on: it is free, and it is
    where the bytes are — ECR image layers are served from S3, so the 1-2 GB
    pull of a model-laden image bypasses the NAT gateway's $0.045/GB charge
    whether or not the interface endpoints exist.
  EOT
  type        = bool
  default     = false
}

variable "alb_ingress_cidrs" {
  description = <<-EOT
    Who may reach the load balancer. Defaults to the whole internet because the
    API is the public surface; narrow it to an office or VPN range if the whole
    deployment is internal.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "ui_allowed_cidrs" {
  description = <<-EOT
    Who may reach the Streamlit console, enforced as a source-IP condition on
    the ALB listener rule.

    Empty (the default) means the UI is not routed at all — no listener rule is
    created and /ui returns the API's 404. That is deliberate: the console is an
    operator tool with no authentication of its own, and it imports the graph
    in-process, so anyone who can reach it can spend Anthropic tokens.

    Source-IP conditions are the weakest useful control here. They read the
    client IP as the ALB sees it (X-Forwarded-For is honoured), so they are
    spoofable in ways a real authenticator is not. For anything beyond a demo,
    put the UI behind an OIDC listener rule or a private ALB instead.
  EOT
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# TLS / DNS
# ---------------------------------------------------------------------------

variable "acm_certificate_arn" {
  description = <<-EOT
    ARN of an existing ACM certificate in `region`. Optional so the stack stands
    up with no domain: when empty, only an HTTP:80 listener is created and the
    service is reachable on the ALB's own dns name.

    The certificate is not created here on purpose — issuing one requires DNS or
    email validation against a zone this stack does not own, and a half-created
    certificate blocks `apply` for as long as validation takes.
  EOT
  type        = string
  default     = ""
}

variable "ssl_policy" {
  description = "ALB security policy for the HTTPS listener. TLS 1.2 floor, TLS 1.3 preferred."
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-2021-06"
}

# ---------------------------------------------------------------------------
# Container image
# ---------------------------------------------------------------------------

variable "image_tag" {
  description = <<-EOT
    Tag of the image in ECR to run. There is no `latest` default on purpose: a
    service pinned to a mutable tag cannot answer "which commit is running", and
    the ECR repository is created with IMMUTABLE tags to enforce that.

    The intended flow is that CI pushes `<git-sha>` and the deploy step runs
    `terraform apply -var image_tag=$GITHUB_SHA`. Terraform therefore owns the
    task definition; nothing should call `aws ecs update-service` behind it, or
    the next plan will revert the rollout.
  EOT
  type        = string
}

variable "cpu_architecture" {
  description = <<-EOT
    Must match what CI builds. ARM64 (Graviton) Fargate is roughly 20% cheaper
    per vCPU-hour and onnxruntime ships aarch64 wheels, so it is a real option
    for this workload — but the image has to be built for it, and the Dockerfile
    is owned elsewhere. Default X86_64 until that is confirmed.
  EOT
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

variable "ecr_untagged_expiry_days" {
  description = "Days before an untagged image layer is expired from ECR."
  type        = number
  default     = 7
}

variable "ecr_keep_tagged_images" {
  description = <<-EOT
    How many tagged images to retain. Worth setting deliberately here: ~250 MB
    of ONNX models are baked into the image, so a build is on the order of
    1-2 GB and ECR charges $0.10/GB-month. Thirty of them is a $3-6/month line
    item for images nobody will ever roll back to.
  EOT
  type        = number
  default     = 20
}

# ---------------------------------------------------------------------------
# API service sizing
# ---------------------------------------------------------------------------

variable "api_cpu" {
  description = <<-EOT
    Fargate CPU units for the API task. 1024 = 1 vCPU.

    **1 vCPU is the requested starting point and I think it is one size too
    small.** A query is ~2.4s of CPU-bound work: the cross-encoder reranker runs
    16 forward passes over the candidate pool and onnxruntime will happily use
    every core it is given. On 1 vCPU those passes serialise, so p95 under any
    concurrency at all is roughly N x 2.4s — the task does not degrade
    gracefully, it queues. 2 vCPU roughly halves rerank latency for +$29/month
    per task, which is the cheapest latency in this entire stack.

    Left at 1024 because that is what was asked for, and because for a demo
    serving one user at a time it is genuinely fine. Raise it before anyone
    else uses it.
  EOT
  type        = number
  default     = 1024
}

variable "api_memory" {
  description = <<-EOT
    Fargate memory (MiB) for the API task. Fargate only accepts 2048/3072/4096
    with 1024 CPU units.

    2 GB is tight but workable: Python plus onnxruntime with two models resident
    (bge-small ~130 MB, ms-marco-MiniLM-L-12 ~120 MB) plus the 1,560 x 384
    float32 matrix plus pandas/plotly in the same image lands around 1.2-1.5 GB
    resident. There is not much headroom for a request spike, and Fargate OOM is
    a hard task kill rather than a slowdown. 3072 is the safer first move if
    tasks start dying without a log line explaining why.
  EOT
  type        = number
  default     = 2048
}

variable "api_desired_count" {
  description = "Baseline task count. Also the autoscaling floor."
  type        = number
  default     = 2
}

variable "api_min_capacity" {
  description = <<-EOT
    Autoscaling floor. Two, not one: the index build at startup takes minutes
    (see api_health_check_grace_period), so a single task means every deploy and
    every task replacement is an outage.
  EOT
  type        = number
  default     = 2
}

variable "api_max_capacity" {
  description = "Autoscaling ceiling. Also the cost ceiling — see infra/README.md."
  type        = number
  default     = 6
}

variable "api_cpu_target" {
  description = <<-EOT
    Target-tracking threshold for average service CPU.

    CPU is an unusually honest load signal for this service, which is why it is
    the scaling metric: a query is CPU-bound almost end to end, so utilisation
    tracks demand rather than tracking how long something is blocked on IO. The
    caveat is reaction time — scale-out has to pull a multi-gigabyte image and
    then build the index, so 60% leaves headroom for the several minutes before
    a new task is useful. ALBRequestCountPerTarget would react sooner and is the
    obvious second policy if that proves too slow.
  EOT
  type        = number
  default     = 60
}

variable "api_container_port" {
  description = "Port uvicorn listens on inside the container."
  type        = number
  default     = 8000
}

variable "api_health_check_path" {
  description = <<-EOT
    Read from src/aiops/api/server.py rather than guessed: the service defines
    GET /health, returning status, uptime, offline_mode and index_chunks.

    Worth knowing what this actually proves. /health calls get_index() and
    get_copilot(), so a 200 means the index is loaded and the graph is
    constructed — not merely that the process is up. That is the right depth for
    a target-group check on this service, and it is also why the grace period
    below is minutes rather than seconds.
  EOT
  type        = string
  default     = "/health"
}

variable "api_health_check_grace_period" {
  description = <<-EOT
    Seconds ECS ignores load-balancer health before it will kill a task.

    300s is not padding. server.py's lifespan warms the index and the tracer at
    startup precisely so "the first request is not the one that pays a 4-minute
    embedding build" — which means the *task* pays it instead. Set this too low
    and ECS kills every task mid-warm-up, forever, and the service never
    stabilises. If the index is fetched from S3 as prebuilt artefacts (the
    AIOPS_INDEX_URI path) this is closer to 60s, but the safe number is the one
    that also survives a cold rebuild.
  EOT
  type        = number
  default     = 300
}

# ---------------------------------------------------------------------------
# UI service sizing
# ---------------------------------------------------------------------------

variable "enable_ui" {
  description = "Whether to run the Streamlit console as a second ECS service."
  type        = bool
  default     = true
}

variable "ui_cpu" {
  description = <<-EOT
    The console is not a thin client. src/aiops/ui/app.py calls the graph
    in-process ("The UI calls the graph in-process rather than over HTTP so a
    single `streamlit run` is enough to demo everything"), so a UI task loads
    the same models, builds the same index and needs the same database and S3
    access as the API. It is sized the same for that reason, not by symmetry.
  EOT
  type        = number
  default     = 1024
}

variable "ui_memory" {
  description = "Fargate memory (MiB) for the UI task."
  type        = number
  default     = 2048
}

variable "ui_desired_count" {
  description = "UI task count. One is enough for an internal console; there is no autoscaling policy on it."
  type        = number
  default     = 1
}

variable "ui_container_port" {
  description = "Port Streamlit listens on inside the container."
  type        = number
  default     = 8501
}

variable "ui_base_url_path" {
  description = <<-EOT
    Path prefix the console is served under, so one ALB serves both services
    without a second hostname.

    This is the most fragile piece of the routing and it is a shared contract
    with the Dockerfile: Streamlit must be started with
    `--server.baseUrlPath=<this>` or every asset and the /_stcore/stream
    websocket will 404 behind the prefix. If it turns out simpler to give the
    console its own hostname, delete the rule and point a second listener at the
    same target group.
  EOT
  type        = string
  default     = "ui"
}

variable "ui_health_check_path" {
  description = <<-EOT
    Streamlit's own health endpoint, prefixed by ui_base_url_path. `/_stcore/health`
    has been the path since Streamlit 1.19 and pyproject pins >=1.45, so it
    should hold — but unlike the API's /health this was not read out of this
    repository's source, and it is the one health check here I would verify by
    hand before trusting a green target group.
  EOT
  type        = string
  default     = null
}

variable "ui_use_fargate_spot" {
  description = <<-EOT
    Run the console on Spot capacity, which is roughly 70% cheaper.

    The trade is real and it is why the API does not do this: a Spot reclaim
    gives two minutes' notice and kills the task. For Streamlit that drops
    server-side session state and every open websocket, so an operator loses
    whatever they were looking at. For an internal console that is an
    irritation; for the API it would be a dropped request mid-synthesis, and a
    replacement task is minutes away because of the warm-up.
  EOT
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

variable "db_instance_class" {
  description = <<-EOT
    db.t4g.micro is the smallest sensible Postgres instance: 2 vCPU burstable,
    1 GB RAM, Graviton, ~$0.016/hour. The workload is an audit insert per query,
    an escalation queue and a ~70-row error-code catalog — this is not a
    database under load, and a larger class would be paying for idle.

    Burstable means CPU credits. At this write rate the instance will never
    exhaust them; if it ever does, the symptom is a cliff rather than a slope,
    so CPUCreditBalance is worth an alarm before moving to t4g.small.
  EOT
  type        = string
  default     = "db.t4g.micro"
}

variable "db_engine_version" {
  description = <<-EOT
    Major version only, paired with auto_minor_version_upgrade, so RDS applies
    security patches in the maintenance window without a Terraform diff.
    Pinned to 17 because that is a version I can state is available on RDS;
    newer majors may well be by now, and moving up is a one-line change plus a
    maintenance window.
  EOT
  type        = string
  default     = "17"
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "aiops"
}

variable "db_username" {
  description = "Master username. Not `postgres` and not `admin`, both of which are the first two guesses in any credential-stuffing attempt."
  type        = string
  default     = "aiops_app"
}

variable "db_allocated_storage" {
  description = "Initial gp3 storage in GB. 20 is the RDS minimum."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = <<-EOT
    Storage autoscaling ceiling. An audit trail only grows, and a full disk on
    RDS means the instance stops accepting writes — which for this system means
    it stops recording what it answered. Cheap insurance; storage is billed on
    what is allocated, so the ceiling itself costs nothing.
  EOT
  type        = number
  default     = 100
}

variable "db_multi_az" {
  description = <<-EOT
    Synchronous standby in a second AZ. Doubles the instance cost (~+$11.70/month
    here) and is off by default: losing the audit database degrades the copilot
    to an unrecorded but still-answering state rather than taking it down, so
    for dev the failover is not worth the money. For prod it is, and locals.tf
    turns it on there.
  EOT
  type        = bool
  default     = null
}

variable "db_backup_retention_days" {
  description = "Automated backup retention. 7 days is the free-tier-equivalent default and enough to unwind a bad migration."
  type        = number
  default     = 7
}

variable "db_url_scheme" {
  description = <<-EOT
    URL scheme written into the AIOPS_DB_URL secret. `postgresql` matches what
    src/aiops/storage/postgres_backend.py accepts (`is_postgres_url` allows
    postgres/postgresql, with or without a +driver suffix) and what the
    pyproject extra documents.

    Change to `postgresql+psycopg` if the backend is ever routed through
    SQLAlchemy, where a bare `postgresql://` selects the psycopg2 dialect that
    this project does not install.
  EOT
  type        = string
  default     = "postgresql"
}

# ---------------------------------------------------------------------------
# Storage, secrets, logs
# ---------------------------------------------------------------------------

variable "index_prefix" {
  description = <<-EOT
    Key prefix under the artefact bucket holding vectors.npy / chunks.pkl /
    stats.json. Becomes AIOPS_INDEX_URI on both tasks.

    storage/artifacts.py is explicit that the download cache is never
    invalidated, so a task started before a reindex serves the old artefacts for
    its whole life. The fix it names is a versioned prefix — set this to
    `index/2026-08-13/` or `index/<sha>/` per publish and the task definition
    revision becomes the thing that pins the index, which is auditable. A
    mutable `index/` is the default only because it is the one that works
    without a publishing pipeline.
  EOT
  type        = string
  default     = "index/"
}

variable "s3_force_destroy" {
  description = "Allow `terraform destroy` to empty the artefact bucket. Off outside dev."
  type        = bool
  default     = null
}

variable "secret_recovery_window_days" {
  description = <<-EOT
    Days a deleted secret stays recoverable. 0 deletes immediately, which is the
    only way to `destroy` and re-`apply` a dev stack with the same secret names
    inside the recovery window — a scheduled-for-deletion secret still holds the
    name.
  EOT
  type        = number
  default     = null
}

variable "log_retention_days" {
  description = <<-EOT
    CloudWatch Logs retention. Never leave this unset: the default is "never
    expire", which is a bill that only grows and the most common avoidable
    CloudWatch cost. 30 days is enough to debug an incident; the audit trail
    that actually needs to be durable lives in Postgres.
  EOT
  type        = number
  default     = 30
}

variable "enable_container_insights" {
  description = <<-EOT
    ECS Container Insights. Off by default because it is billed as custom
    CloudWatch metrics per task and, at 1-6 tasks, the ALB and ECS service
    metrics that are free already answer the questions worth asking. Turn it on
    when per-container memory pressure becomes the thing being debugged — which
    for a 2 GB task holding two ONNX models is plausible.
  EOT
  type        = bool
  default     = false
}

variable "enable_execute_command" {
  description = <<-EOT
    ECS Exec (`aws ecs execute-command`) for shelling into a running task.

    Off by default and worth being honest about why: turning it on grants the
    task role ssmmessages:* on `*` — those actions do not support resource-level
    permissions — and gives anyone with ecs:ExecuteCommand a shell next to the
    Anthropic key. It is the right tool for debugging a task that will not warm
    up, and it should be switched on to debug and off afterwards.
  EOT
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Application configuration
# ---------------------------------------------------------------------------

variable "otlp_endpoint" {
  description = <<-EOT
    AIOPS_OTLP_ENDPOINT. Empty means spans stay in the in-process ring buffer
    that backs /traces, which works but is per-task and lost on restart — with
    two or more tasks behind an ALB, /traces shows you whichever task the
    request happened to land on.

    No collector is deployed here. Running one properly (a sidecar or an ECS
    service, plus a backend to ship to) is its own decision with its own cost,
    and inventing one would be worse than pointing at whatever already exists.
  EOT
  type        = string
  default     = ""
}

variable "extra_environment" {
  description = <<-EOT
    Additional AIOPS_* environment variables for both tasks. Anything in
    src/aiops/config.py is settable here — AIOPS_TOP_K, AIOPS_DENSE_WEIGHT,
    AIOPS_CONFIDENCE_THRESHOLD — so retrieval can be retuned without a rebuild.

    Not for secrets: these land in the task definition in plaintext and are
    readable by anyone with ecs:DescribeTaskDefinition.
  EOT
  type        = map(string)
  default     = {}
}

variable "force_offline" {
  description = <<-EOT
    Sets AIOPS_FORCE_OFFLINE=1, which pins the deterministic extractive path
    even when the Anthropic key is present. Useful for a cost-free demo, and the
    honest setting when nat_gateway_mode = "none" makes api.anthropic.com
    unreachable anyway.
  EOT
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------

variable "alarm_email" {
  description = <<-EOT
    Address subscribed to the alarm SNS topic. Empty creates the topic with no
    subscriber, which is a topic that alarms into the void — set it or wire the
    topic ARN output into whatever already pages.

    Email subscriptions require the recipient to click a confirmation link.
    Terraform reports the subscription as created before that happens, so a
    green apply does not mean anyone is being notified.
  EOT
  type        = string
  default     = ""
}

variable "alarm_5xx_rate_threshold" {
  description = "Percent of requests returning 5xx (target-generated plus ALB-generated) that trips the alarm."
  type        = number
  default     = 5
}

variable "alarm_p95_latency_seconds" {
  description = <<-EOT
    p95 target response time that trips the latency alarm.

    10s, not 3s, because ~2.4s is *normal* here — the cross-encoder rerank
    dominates every query and that is a measured property of the pipeline, not a
    regression. An alarm set near the healthy value fires constantly and gets
    muted, and a muted alarm protects nothing. 10s catches queueing on a
    saturated 1-vCPU task, which is the failure this is actually for.
  EOT
  type        = number
  default     = 10
}
