# Two roles, trusted by the same service but used at different moments and for
# different things. The execution role runs *before* the container starts and
# injects its secrets; the task role is what the application itself holds.
# Collapsing them would hand the application the ability to read every
# parameter the platform can.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- 1. task execution: used by the agent, before the container starts ------

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Named parameters, not a wildcard. This role injects secrets into the
# container, so its blast radius is exactly the set of parameters listed here.
data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid     = "ReadInjectedParameters"
    effect  = "Allow"
    actions = ["ssm:GetParameters"]
    resources = [
      aws_ssm_parameter.db_password.arn,
      aws_ssm_parameter.anthropic_api_key.arn,
      aws_ssm_parameter.groq_api_key.arn,
    ]
  }

  statement {
    sid       = "DecryptThoseParameters"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-parameters"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# --- 2. the task: used by the application itself ----------------------------

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# Read-only on the corpus and the index, and write only under a backups/ prefix.
# The application reads its knowledge and never edits it, so write access to the
# document prefix would be a permission it has no code path to use.
data "aws_iam_policy_document" "task" {
  statement {
    sid       = "ListOwnBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${trim(var.docs_prefix, "/")}/*", "${trim(var.index_prefix, "/")}/*", "backups/*"]
    }
  }

  statement {
    sid     = "ReadCorpusAndIndex"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.data.arn}/${trim(var.docs_prefix, "/")}/*",
      "${aws_s3_bucket.data.arn}/${trim(var.index_prefix, "/")}/*",
    ]
  }

  statement {
    sid       = "WriteBackupsOnly"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.data.arn}/backups/*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "s3-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# Deliberately absent from the task role: any rds:* permission. The application
# reaches PostgreSQL over the network with a password, so IAM grants it nothing
# — which means a compromised container cannot modify, snapshot or delete the
# database through the control plane.
