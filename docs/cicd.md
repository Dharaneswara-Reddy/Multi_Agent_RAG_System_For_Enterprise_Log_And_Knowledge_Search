# CI/CD

Two workflows. `ci.yml` decides whether a commit is good; `deploy.yml` puts a
good commit into production. They are separate because the second waits on the
first — a commit that fails the retrieval or safety gate is never built.

> **Neither has run against a real AWS account.** The deploy workflow is
> written and syntactically valid; it has never authenticated, pushed an image
> or rolled a service. Expect to fix something on the first run.

## `ci.yml` — quality gate

Runs on push to `main`, every PR, and manual dispatch.

| Job | What it does |
|---|---|
| `quality` | lint → build corpus and index → tests → `evaluate.py --gate` → upload report |
| `supply-chain` | dependency audit (`pip-audit`) and secret scan, both non-blocking |

The gate is the part worth having: unit tests catch code regressions, but a RAG
system degrades silently through retrieval and safety regressions that ordinary
tests never see.

### Defects this fixed

Three things were wrong and had been for a while:

1. **The model cache cached nothing.** It pointed at `~/.cache/fastembed`, which
   does not exist — FastEmbed defaults to `/tmp/fastembed_cache`. Both ONNX
   models (~250MB) were re-downloaded on every run. Fixed by setting
   `FASTEMBED_CACHE_PATH` into the workspace and caching that.
2. **The cache key was a fixed string.** GitHub saves a cache on miss and never
   again on hit, so a key that never changes is saved once and never updated —
   the reranker added later would never have been cached even with a correct
   path. Now keyed on `hashFiles('src/aiops/config.py')`, which is where the
   model names live.
3. **`uv sync --all-extras || uv pip install -e ".[dev]"`.** The `||` fell back
   to an unpinned install when `uv sync` failed, and the build still went green
   — a lockfile that guarantees nothing. Now `uv sync --extra dev --frozen`,
   with no fallback.

The timeout also went 30 → 45 minutes. Measured locally the job is ~20 minutes
before any download, so 30 was close enough to the edge that a slow runner
failed the build on timeout rather than on quality.

## `deploy.yml` — build and roll

Triggered by `workflow_run` on a **successful** CI run against `main`, or
manually. `workflow_run` fires on completion regardless of outcome, so the job
checks `conclusion == 'success'` explicitly — without that, a red build would
still deploy.

```
CI passes ──► build image (ARM64) ──► push to ECR ──► render task definition
                                                          │
                                            environment: production (reviewer)
                                                          │
                                       ECS rolling deploy ──► smoke test ──► done
                                                          └── on failure ──► roll back
```

Notes on specific choices:

- **Checks out `workflow_run.head_sha`, not the branch tip.** `workflow_run`
  checks out the default branch by default, which may have moved past the
  commit that actually passed the gate.
- **Skips the build when the tag already exists.** ECR tags are immutable, so
  re-pushing is an error rather than a no-op — and it makes redeploying an
  unchanged commit fast, which is what a rollback is.
- **Rollback is belt-and-braces.** Terraform configures an ECS deployment
  circuit breaker that rolls back automatically when new tasks never become
  healthy. The explicit rollback step covers the other case: tasks healthy,
  smoke test failing.

## What you must configure

Terraform outputs most of these — run `terraform output` in `infra/`.

### Repository **variables** (Settings → Secrets and variables → Actions → Variables)

| Name | From | Example |
|---|---|---|
| `AWS_REGION` | your choice | `us-east-1` |
| `ECR_REPOSITORY` | `terraform output ecr_repository_url` (name part only) | `aiops-prod` |
| `ECS_CLUSTER` | `terraform output ecs_cluster_name` | `aiops-prod` |
| `ECS_SERVICE` | `terraform output ecs_api_service_name` | `aiops-prod-api` |
| `ECS_TASK_FAMILY` | `terraform output api_task_family` | `aiops-prod-api` |
| `ECS_UI_SERVICE` | `terraform output ecs_ui_service_name` | `aiops-prod-ui` |
| `ECS_UI_TASK_FAMILY` | `terraform output ui_task_family` | `aiops-prod-ui` |
| `APP_URL` | `terraform output api_url` | `https://…elb.amazonaws.com` |

**The two `ECS_UI_*` variables are how the console gets deployed at all.**
Terraform sets `ignore_changes = [task_definition]` on both services so that a
`terraform apply` does not revert whatever the pipeline last rolled out. The
consequence is that nothing except this workflow ever moves a service to a new
image — so a pipeline that rolled only the API would leave the console running
the image `terraform apply` first pinned, indefinitely, while the API moved on.
The symptom is a console that keeps answering from a stale index and a build
that reports success.

Leave both unset when running with `enable_ui = false`; the console steps are
skipped on an empty `ECS_UI_SERVICE`.

### Repository **secret**

| Name | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | ARN of the role GitHub assumes (below) |

### Environment

Create an environment named `production` and add required reviewers. The
workflow references it, but it cannot enforce the reviewer requirement itself —
that is a repository setting.

## AWS side: the OIDC trust

No long-lived access keys. A leaked static key is valid until someone notices;
a leaked OIDC token is valid for an hour and only for this repository.

This is **not** in `infra/` on purpose — it is account-level trust that usually
predates any one stack, and putting it in the same state as the application
means destroying the app can destroy the trust that deploys it.

```bash
# 1. The provider, once per account.
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com

# 2. Trust policy — note the `sub` condition. Without it, ANY repository on
#    GitHub can assume this role.
cat > trust.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub":
          "repo:Dharaneswara-Reddy/Multi_Agent_RAG_System_For_Enterprise_Log_And_Knowledge_Search:*"
      }
    }
  }]
}
JSON

aws iam create-role --role-name aiops-github-deploy \
  --assume-role-policy-document file://trust.json
```

Restrict `sub` further once it works — `:ref:refs/heads/main` or
`:environment:production` rather than `:*` — so a pull request from a fork
cannot assume the deploy role.

The role needs: ECR push, `ecs:DescribeServices`, `ecs:DescribeTaskDefinition`,
`ecs:RegisterTaskDefinition`, `ecs:UpdateService`, and `iam:PassRole` scoped to
the two task roles Terraform creates.

## Running a deploy by hand

Actions → Deploy → Run workflow. Leave `image_tag` empty to build from `HEAD`,
or supply an existing tag (`sha-abc123def456`) to redeploy or roll back —
because the build is skipped when the tag exists, a rollback takes about as
long as the ECS rollout itself.
