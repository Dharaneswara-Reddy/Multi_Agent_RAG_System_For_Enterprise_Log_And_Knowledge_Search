# Task roles.
#
# Two roles, because they are used by different principals at different times:
#
#   execution role — used by the ECS agent *before* the container starts, to
#                    pull the image and resolve secrets into the environment
#   task role      — assumed by the application itself, at runtime
#
# Conflating them gives the running process permission to pull images and read
# every secret the agent can, which is exactly the blast radius you do not want
# a process that executes model output to have.

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Confused-deputy guard: without this, any account that knows the role ARN
    # could ask ECS to assume it on their behalf.
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# --- execution role -------------------------------------------------------

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The managed policy above covers ECR and logs but not Secrets Manager, and it
# grants ecr:GetAuthorizationToken on `*` because that action has no resource.
# Secret access is scoped to exactly the two secrets this stack creates.
data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid    = "ReadInjectedSecrets"
    effect = "Allow"

    actions = ["secretsmanager:GetSecretValue"]

    resources = [
      aws_secretsmanager_secret.anthropic.arn,
      aws_secretsmanager_secret.database.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# --- task role ------------------------------------------------------------

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "task" {
  # Read the index artefacts, and nothing else in S3. Read-only: the running
  # service consumes an index, it never publishes one — that is CI's job.
  statement {
    sid       = "ReadIndexArtifacts"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.artifacts.arn}/${trim(var.index_prefix, "/")}/*"]
  }

  statement {
    sid       = "ListArtifactBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${trim(var.index_prefix, "/")}/*"]
    }
  }

  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "runtime"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# Traces go to X-Ray's OTLP endpoint, which authenticates with SigV4 and so
# needs a grant. Attached only when an endpoint is configured, so a stack with
# no tracing carries no unused permission.
resource "aws_iam_role_policy_attachment" "task_xray" {
  count = var.otlp_endpoint != "" ? 1 : 0

  role       = aws_iam_role.task.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

# ECS Exec opens an interactive shell into a running task. Useful for
# debugging, and a way into a container that holds a database credential —
# hence off by default and gated behind an explicit variable.
data "aws_iam_policy_document" "task_exec" {
  count = var.enable_execute_command ? 1 : 0

  statement {
    sid    = "SSMChannel"
    effect = "Allow"

    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]

    resources = ["*"] # these actions do not support resource scoping
  }
}

resource "aws_iam_role_policy" "task_exec" {
  count = var.enable_execute_command ? 1 : 0

  name   = "ecs-exec"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_exec[0].json
}
