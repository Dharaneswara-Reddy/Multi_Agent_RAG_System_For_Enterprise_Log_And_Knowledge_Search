# Demo deployment

The architecture that actually runs 24/7. `../` is the production architecture
and is unchanged by anything here — different root, different state key, no
shared resources.

> **Validated, never applied.** `terraform validate` and `fmt` pass, the
> dependency graph builds with no cycles (64 nodes, 83 edges), and the
> PostgreSQL version and CloudFront prefix list were checked against the live
> API. No `plan` has run, because a plan needs credentials. Expect to fix
> something on the first one.

```
                        Internet
                           │  HTTPS + WebSocket
                           ▼
                    CloudFront distribution
                     *.cloudfront.net cert
                           │  HTTP :8501
                           │  (origin = EC2 public DNS of the Elastic IP)
                           ▼
   ┌───────────────── VPC 10.20.0.0/16 ─────────────────┐
   │                                                     │
   │  public subnet (1 AZ)                               │
   │   ┌───────────────────────────────────────┐         │
   │   │ EC2 t4g.medium · ARM64 · Elastic IP    │         │
   │   │  ECS agent → cluster                   │         │
   │   │   └─ 1 task: Streamlit + RAG pipeline  │         │
   │   │      models baked into the image       │         │
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
| Compute | 1 × EC2 t4g.medium, ECS on EC2 | ECS Fargate ARM64, 2–6 tasks |
| **Availability** | **Single instance, single AZ. Not HA.** ASG replaces a failed instance in minutes | 2+ tasks across 2 AZs, ALB health checks, circuit breaker |
| AZs | 1 for compute; 2 private subnets exist only because an RDS subnet group demands two | 2, public and private tiers |
| Load balancing | **None.** CloudFront is a CDN with one origin | ALB, path routing, 2 target groups |
| **Autoscaling** | **None.** ASG is min=max=1; that is replacement, not scaling | Target tracking on ECS CPU, 2→6 tasks |
| Database | RDS PostgreSQL Single-AZ, 20 GB, 7-day backups | RDS PostgreSQL Multi-AZ, Performance Insights, log exports |
| Networking | Public subnet + IGW, **no NAT**; DB private with no route out | Public/private tiers, NAT or interface endpoints |
| TLS | CloudFront default certificate | ACM certificate on the ALB |
| Observability | 1 log group, 7-day retention, 3 optional alarms | Log group + 4 alarms + SNS + Container Insights |
| Deploys | Stop-then-start, ~60–90 s downtime | Rolling, zero downtime |
| **Cost** | **~$45/month** | **~$76–131/month** |

**Why the demo is cheaper**, line by line: no ALB (−$16.43), no NAT gateway
(−$32.85), one task instead of two (−$14.42), Single-AZ instead of Multi-AZ
(−$16.28), SSM Parameter Store instead of Secrets Manager (−$0.80), no
Container Insights, no Performance Insights, 7-day log retention.

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
| EC2 t4g.medium | $0.0336/hr | $24.53 |
| Public IPv4 | $0.005/hr | $3.65 |
| EBS gp3, 12 GB | $0.08/GB-mo | $0.96 |
| RDS db.t4g.micro Single-AZ | $0.016/hr | $11.68 |
| RDS gp3, 20 GB | $0.115/GB-mo | $2.30 |
| S3, ECR, CloudWatch | | ~$0.55 |
| CloudFront, SSM, ECS control plane | free tier / free | $0.00 |
| **Total** | | **~$43.67** |

| 1 hour | 1 day | 1 week | 1 month |
|---|---|---|---|
| $0.060 | $1.44 | $10.05 | $43.67 |

**$120 of credits ≈ 2.7 months.**

### Why t4g.medium and not t4g.small

Measured, not assumed. Resident set of the application alone:

```
after 1 query    1024 MB
after 2 queries  1221 MB
after 4 queries  2114 MB
after 8 queries  2120 MB   <- plateau
```

That is the ONNX Runtime arena reaching steady state, not a leak, and it is
insensitive to thread count (2114 MB at `OMP_NUM_THREADS=1`, 2126 MB at 2).
Adding the ECS agent (~150 MB), the container runtime (~100 MB) and the OS
(~300 MB) gives **~2.66 GB**, which does not fit t4g.small's 2 GB.

Swap was considered and rejected: paging ONNX inference is pathological, and it
would hide the shortfall rather than fix it. t4g.medium costs $12.27/month more
and leaves ~1.3 GB of headroom.

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

- **`cpu_credit_specification` is `standard`, deliberately.** Under `unlimited`
  a sustained CPU spike bills surcharge credits with no ceiling.
- **CloudFront's free tier is 1 TB/month.** Demo traffic will not approach it,
  but a scraper could.
- **CloudWatch Logs ingestion is $0.50/GB.** The RDS parameter group logs only
  statements over 1 s for this reason.
- **A `terraform destroy` leaves the Elastic IP billed if it is released from
  the instance but not deallocated** — Terraform handles this, manual
  intervention may not.
