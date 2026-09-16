# Demo deployment

The architecture that actually runs 24/7. `../` is the production architecture
and is unchanged by anything here — different root, different state key, no
shared resources.

> **Deployed and serving.** Applied in `us-east-1` and live at
> <https://d269rj5uf8ejau.cloudfront.net> — CloudFront distribution
> `E3PV2IJ33JC61M` in front of an ALB, one Fargate task, and an RDS PostgreSQL
> 17 instance. The production root in `../` remains validated but never applied,
> which is the distinction the two roots exist to make.

```
                        Internet
                           │  HTTPS + WebSocket
                           ▼
                    CloudFront distribution
                     *.cloudfront.net cert
                           │  HTTP :80
                           │  (origin = ALB; its SG admits CloudFront only)
                           ▼
   ┌───────────────── VPC 10.20.0.0/16 ─────────────────┐
   │                                                     │
   │  public subnets (2 AZ for the ALB, 1 for the task)  │
   │   ┌───────────────────────────────────────┐         │
   │   │ ALB · 1 listener · 1 target group      │         │
   │   └────────────────┬──────────────────────┘         │
   │                    ▼                                │
   │   ┌───────────────────────────────────────┐         │
   │   │ ECS Fargate · ARM64 · 0.5 vCPU / 2 GB  │         │
   │   │  1 task: Streamlit + RAG pipeline      │         │
   │   │  public IP in place of a NAT gateway   │         │
   │   │  models baked into the image           │         │
   │   └───────────────────────────────────────┘         │
   │        │ SG: PostgreSQL 5432                        │
   │        ▼                                            │
   │  private subnets (2 AZ, no NAT, no IGW route)       │
   │   ┌───────────────────────────────────────┐         │
   │   │ RDS PostgreSQL db.t4g.micro Single-AZ │         │
   │   └───────────────────────────────────────┘         │
   │                                                     │
   │  S3 gateway endpoint (free) ──► S3: docs/ index/    │
   └─────────────────────────────────────────────────────┘
```

## Demo vs production

| | **Demo (this root)** | **Production (`../`)** |
|---|---|---|
| Compute | 1 × ECS Fargate task, ARM64, 0.5 vCPU / 2 GB | ECS Fargate ARM64, 2–6 tasks |
| **Availability** | **Single task, single AZ. Not HA.** ECS replaces a failed task in minutes | 2+ tasks across 2 AZs, ALB health checks, circuit breaker |
| AZs | 1 for the task; the ALB spans 2 public subnets, and 2 private subnets exist because an RDS subnet group demands two | 2, public and private tiers |
| Load balancing | ALB, 1 listener, 1 target group — **one task behind it, so nothing to balance across** | ALB, path routing, 2 target groups |
| **Autoscaling** | **None.** `desired_count = 1` and no scaling policy; ECS replaces a task, it does not add one | Target tracking on ECS CPU, 2→6 tasks |
| Database | RDS PostgreSQL Single-AZ, 20 GB, 7-day backups | RDS PostgreSQL Multi-AZ, Performance Insights, log exports |
| Networking | Task in a public subnet with a public IP, **no NAT**; DB private with no route out | Public/private tiers, NAT or interface endpoints |
| TLS | CloudFront default certificate | ACM certificate on the ALB |
| Observability | 1 log group, 7-day retention, 3 optional alarms | Log group + 4 alarms + SNS + Container Insights |
| Deploys | Stop-then-start, ~60–90 s downtime | Rolling, zero downtime |
| **Cost** | **~$52/month** | **~$76–131/month** |

**Why the demo is cheaper**, line by line: no NAT gateway (−$32.85), one task
instead of two (−$14.42), Single-AZ instead of Multi-AZ (−$16.28), SSM Parameter
Store instead of Secrets Manager (−$0.80), no Container Insights, no Performance
Insights, 7-day log retention.

Every one of those is a capability removed, not an efficiency found.

**What this deployment may be described as:** containerised, orchestrated by
ECS, PostgreSQL-backed with durable S3 object storage, encrypted at rest,
database isolated in private subnets with no internet route, least-privilege
IAM, HTTPS, infrastructure as code.

**What it may not:** highly available, autoscaling, or load balanced. It is
none of those things. The production root is, and it is real, implementable
Terraform — but it is not what is running.

## Cost

Fixed, from the AWS Pricing API for us-east-1. Usage-based items are excluded
because at 10–20 visitors a month they round to zero.

| | Rate | Monthly |
|---|---|---|
| Fargate ARM64, 0.5 vCPU / 2 GB | $0.03238/vCPU-hr + $0.00356/GB-hr | $17.02 |
| ALB | $0.0225/hr | $16.43 |
| Public IPv4, task ENI | $0.005/hr | $3.65 |
| RDS db.t4g.micro Single-AZ | $0.016/hr | $11.68 |
| RDS gp3, 20 GB | $0.115/GB-mo | $2.30 |
| S3, ECR, CloudWatch | | ~$0.55 |
| CloudFront, SSM, ECS control plane | free tier / free | $0.00 |
| **Total** | | **~$51.63** |

| 1 hour | 1 day | 1 week | 1 month |
|---|---|---|---|
| $0.071 | $1.70 | $11.88 | $51.63 |

**$120 of credits ≈ 2.3 months.** ALB LCU charges are excluded — at 10–20
visitors a month they round to zero — as are the ALB's own public IPv4
address-hours.

### Why a 2 GB task, and the one setting that made it fit

Measured, not assumed. Resident set of the application alone, with ONNX
Runtime's CPU arena at its default:

```
after 1 query    1024 MB
after 2 queries  1221 MB
after 4 queries  2114 MB
after 8 queries  2120 MB   <- plateau
```

That is the arena reaching steady state, not a leak, and it is insensitive to
thread count (2114 MB at `OMP_NUM_THREADS=1`, 2126 MB at 2). It does not fit a
2 GB Fargate task, whose memory limit applies to the container alone.

Disabling the arena ([`src/aiops/onnx_tuning.py`](../../src/aiops/onnx_tuning.py))
drops peak RSS to **1563 MB** — 29.6% lower — for 24.5% more mean query latency
(6.73s → 8.38s) and bit-identical retrieval: identical ordering and identical
scores across 240 reranked candidates. That is what makes 0.5 vCPU / 2 GB
viable; at the default the task needs twice the memory.

Swap was considered and rejected: paging ONNX inference is pathological, and it
would hide the shortfall rather than fix it.

## First deploy

**Set a zero-spend budget first.** Nothing here stops spending on its own.

```bash
export AWS_PROFILE=aiops-deploy
aws sts get-caller-identity          # confirm the principal before creating anything

# Same state bucket as the production root, different key.
terraform init \
  -backend-config=../backend.hcl \
  -backend-config="key=aiops/demo/terraform.tfstate"

terraform plan     # read it properly
terraform apply
```

The ECR repository starts empty, so the first apply creates a service whose
image does not exist. The task will fail and retry — that is expected. Build and
push, then upload the corpus and index:

```bash
aws ecr get-login-password | docker login --username AWS --password-stdin \
  "$(terraform output -raw ecr_repository_url | cut -d/ -f1)"
docker build --platform linux/arm64 -t "$(terraform output -raw ecr_repository_url):sha-$(git rev-parse --short=12 HEAD)" .
docker push "$(terraform output -raw ecr_repository_url):sha-$(git rev-parse --short=12 HEAD)"

uv run python scripts/setup.py                       # builds data/docs and data/index
BUCKET=$(terraform output -raw data_bucket)
aws s3 cp data/docs/  "s3://$BUCKET/docs/"  --recursive --exclude '*' --include '*.md'
aws s3 cp data/index/ "s3://$BUCKET/index/" --recursive

terraform apply -var="image_tag=sha-$(git rev-parse --short=12 HEAD)"
```

Enable synthesis by writing the key out of band — it never passes through
Terraform, so it never lands in state:

```bash
aws ssm put-parameter --overwrite --type SecureString \
  --name "$(terraform output -raw anthropic_key_parameter)" --value "sk-..."
terraform apply -var="force_offline=0"
```

## Administration

There is no SSH key and no inbound port 22. Use Session Manager:

```bash
aws ssm start-session --target "$(aws ec2 describe-instances \
  --filters 'Name=tag:Name,Values=aiops-demo' 'Name=instance-state-name,Values=running' \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)"
```

## Security notes

- **The instance holds a public IPv4 address**, which is what removes the need
  for a $32.85/month NAT gateway. Its security group admits inbound traffic
  from the CloudFront origin-facing managed prefix list only, so the origin is
  not reachable directly. The prefix list is looked up by name, never
  hardcoded — AWS rotates its contents.
- **CloudFront reaches the origin over HTTP.** This is the weakest link in the
  demo and is called out rather than buried: the instance has no certificate,
  and a self-signed one would only be accepted by disabling validation. The
  exposure is one hop inside the AWS network, to an origin that accepts nothing
  else. The production root terminates TLS at an ALB instead.
- **IMDSv2 is required**, closing the SSRF-to-credentials path.
- **The database has no route to the internet** — no NAT, no gateway route on
  its route table — and its security group references the application's
  security group rather than a CIDR.
- **The task role holds no `rds:*` permissions at all**, so a compromised
  container cannot snapshot or delete the database through the control plane.
  It can read `docs/` and `index/` and write only under `backups/`.
- **The master password is generated by Terraform and lands in state.** State
  lives in the encrypted, TLS-only, versioned bucket from `bootstrap/`. The
  Anthropic key does not: its parameter is created with a placeholder and
  `ignore_changes`, so the real value is only ever written by `put-parameter`.

## Watch for unexpected charges

- **Fargate bills for the task whether or not anyone visits.** There is no
  scale-to-zero here; `desired_count = 0` is the manual off switch.
- **CloudFront's free tier is 1 TB/month.** Demo traffic will not approach it,
  but a scraper could.
- **CloudWatch Logs ingestion is $0.50/GB.** The RDS parameter group logs only
  statements over 1 s for this reason.
- **Public IPv4 addresses bill per address-hour**, for the task ENI and for the
  ALB's own addresses, whether or not traffic flows through them.
