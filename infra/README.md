# Infrastructure

Terraform for deploying the AI Ops Copilot to AWS: ECS Fargate behind an ALB,
RDS PostgreSQL for the audit trail and escalation queue, S3 for index
artefacts, Secrets Manager for credentials.

> **Validated, never applied.** `terraform validate` and `terraform fmt` pass
> on this module and on `bootstrap/`, against Terraform 1.14.3 and aws provider
> 6.59.0, and `.terraform.lock.hcl` is committed for both.
>
> No `plan` has ever run, because a plan requires credentials. Validate checks
> syntax, types and references — it does not talk to AWS, so it cannot catch a
> wrong ARN, an IAM policy that denies what it should permit, an unavailable
> instance class in a region, or a service quota. Expect to fix at least one
> thing on the first plan. That is the normal experience, and pretending
> otherwise would be the dishonest part.

## Architecture

```
Internet ──► ALB (public subnets, 2 AZ)
               ├─ /ui/*  ──► ECS service: ui   (Fargate Spot, 1 task)
               └─ /*     ──► ECS service: api  (Fargate, autoscaled on CPU)
                                │
              private subnets ──┼──► RDS PostgreSQL   (audit, escalations, catalog)
                                ├──► S3 gateway endpoint ──► index artefacts
                                └──► NAT or interface endpoints ──► api.anthropic.com
```

Both services run the **same image** with a different entrypoint argument. The
console imports the graph in process rather than calling the API over HTTP, so
it needs the same models and index anyway.

## First deploy

**Set a budget alert before step 1, not after.** Nothing in this stack stops
spending on its own, and neither do promotional credits — when they are
exhausted AWS bills the payment method on file. Billing → Budgets → zero-spend
budget. Do it as root: IAM access to billing data is off by default, so a
deploy user cannot create one.

```bash
# 0. Credentials. An IAM user, never root — root cannot be scoped away from
#    billing, which is the one thing worth protecting here.
export AWS_PROFILE=aiops-deploy
aws sts get-caller-identity          # confirm *which* principal before creating anything

# 1. State backend (once per account — uses local state by design)
cd infra/bootstrap
terraform init && terraform apply
terraform output -raw backend_config > ../backend.hcl

# 2. Main stack
cd ..
cp freetier.tfvars.example terraform.tfvars    # or terraform.tfvars.example
                                               # edit: image_tag, region
terraform init -backend-config=backend.hcl
terraform plan      # read this properly the first time
terraform apply
```

`freetier.tfvars.example` is the ~$49/month configuration;
`terraform.tfvars.example` is the ~$101/month one. Neither is free — Fargate
and NAT Gateway have no free-tier allowance at any size.

`image_tag` has no default and the ECR repository starts empty, so the first
apply creates a service with nothing to pull. Two options:

- set `api_desired_count = 0`, apply, push an image via CI, then raise it; or
- build and push one image by hand first, then apply with that tag.

The second is simpler for a first run.

### After the first apply

```bash
# The API key. Terraform creates the secret empty and never sees the value —
# putting it in a variable would write it to state in plaintext.
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw anthropic_secret_arn)" \
  --secret-string 'sk-ant-...'

# Index artefacts. Build locally (scripts/setup.py) or in CI, then upload.
aws s3 cp data/index/ "s3://$(terraform output -raw artifacts_bucket)/index/" --recursive
```

Set the GitHub repository variables the deploy workflow needs from the outputs:
`ECR_REPOSITORY`, `ECS_CLUSTER`, `ECS_SERVICE`, `AWS_REGION`.

## Cost

At low traffic, `environment = "dev"` with the example tfvars:

| Item | Configuration | $/month |
|---|---|---|
| Fargate — API | 1 task, 1 vCPU / 2 GB, ARM64 | ~30 |
| Fargate — UI | 1 task on Spot | ~9 |
| ALB | 1, minimal LCU | ~17 |
| RDS | `db.t4g.micro`, single-AZ, 20 GB gp3 | ~14 |
| NAT gateway | `single` | ~33 |
| S3 + ECR + logs | small | ~2 |
| **Total** | | **~$105** |

**NAT is the biggest single line and the easiest to remove.** Three options:

- `nat_gateway_mode = "none"` with `force_offline = true` — no egress at all,
  deterministic extractive answers, **saves ~$33**. Requires
  `enable_interface_endpoints = true` or tasks cannot even pull their image.
- `nat_gateway_mode = "single"` — one gateway, shared fate across AZs.
- `nat_gateway_mode = "per_az"` — the prod default, ~$66.

Interface endpoints are ~$7.20/month each per AZ (four of them: `ecr.api`,
`ecr.dkr`, `logs`, `secretsmanager`), so with two AZs they cost roughly what a
single NAT gateway does. They are a saving only when they let you drop NAT
entirely; otherwise they buy network privacy.

Production defaults (`environment = "prod"`) roughly double this: multi-AZ RDS,
per-AZ NAT, two API tasks minimum.

## Decisions worth knowing about

**S3-native state locking, no DynamoDB.** `use_lockfile = true`, stable since
Terraform 1.11. Older guides still create a DynamoDB table; it is no longer
needed.

**`ignore_changes = [task_definition, desired_count]` on the services.** CI
deploys by registering a new task definition revision. Without this, the next
`terraform apply` would roll the image back to whatever `image_tag` says and
undo the deployment.

**Immutable ECR tags.** A rollback to `sha-abc123` must fetch the same bytes it
fetched the first time. Mutable tags make a rollback a guess.

**Two IAM roles, not one.** The execution role pulls images and resolves
secrets *before* the container starts; the task role is what the application
itself holds. Merging them would give a process that executes model output the
ability to read every secret the agent can.

**The RDS password is in Terraform state.** Unavoidable when Terraform
generates it — hence the encrypted, TLS-only, versioned state bucket. Moving to
`manage_master_user_password` hands rotation to RDS and takes the value out of
state entirely; worth doing before this holds anything real.

**Deletion protection and final snapshots follow `environment`.** A `prod`
stack will refuse `terraform destroy` on the database until you turn that off,
which is the intent.

## What is not here

- **No WAF.** Add one before exposing this publicly with a real key attached.
- **No authentication on the console.** `ui_allowed_cidrs` restricts by source
  address, which is a lock on the door rather than a login. ALB + Cognito is
  the natural next step.
- **No Route 53 or ACM management.** Pass an existing certificate ARN.
- **No blue/green.** ECS rolling deploys with a circuit breaker that rolls back
  on failure. CodeDeploy blue/green is the upgrade if you need instant rollback.
- **No CI role.** The GitHub OIDC provider and deploy role are not created here
  — see `docs/cicd.md`.
