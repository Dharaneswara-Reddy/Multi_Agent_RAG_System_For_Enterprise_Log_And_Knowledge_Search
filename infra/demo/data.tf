# Durable state: PostgreSQL for relational data, S3 for documents and artefacts.
#
# The container holds neither. That is the requirement this file exists to
# satisfy — an instance can be replaced, the image can be rebuilt, and nothing
# that matters is lost.

# --- S3 ---------------------------------------------------------------------

resource "aws_s3_bucket" "data" {
  bucket = "${local.name}-${data.aws_caller_identity.current.account_id}"

  # The corpus and the index can be rebuilt from the repository, so a demo
  # bucket that refuses to delete would be an obstacle rather than a safeguard.
  force_destroy = true

  tags = { Name = local.name }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket = aws_s3_bucket.data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    # Not strictly needed at AES256, but it is free and it stops a later switch
    # to KMS from silently multiplying request costs.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Versioning without expiry grows forever. The corpus is small, but "small and
# unbounded" is still unbounded, and this is the cheapest possible guard.
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "data_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "data" {
  bucket = aws_s3_bucket.data.id
  policy = data.aws_iam_policy_document.data_bucket.json

  # A public access block and a bucket policy applied concurrently can race,
  # and the failure mode is a bucket that briefly accepts a policy it should
  # have rejected.
  depends_on = [aws_s3_bucket_public_access_block.data]
}

# --- PostgreSQL -------------------------------------------------------------

resource "random_password" "database" {
  length = 32
  # RDS rejects '/', '@', '"' and ' ' in a master password, and a DSN-safe
  # password avoids a class of bug that only appears once the URL is assembled.
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = local.name }
}

resource "aws_db_parameter_group" "main" {
  name   = local.name
  family = "postgres17"

  # Log slow statements only. `all` on a demo database would ingest more
  # CloudWatch data than the application itself produces.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name  = var.db_name
  username = var.db_username
  password = random_password.database.result

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  parameter_group_name   = aws_db_parameter_group.main.name

  # The whole point of the private subnets. Never true.
  publicly_accessible = false

  # Single-AZ is the deliberate demo trade-off: it halves the cost and it means
  # this deployment is NOT highly available. See the comparison in README.md.
  multi_az = false

  backup_retention_period    = var.db_backup_retention_days
  backup_window              = "03:00-04:00"
  maintenance_window         = "sun:04:00-sun:05:00"
  auto_minor_version_upgrade = true

  # A demo stack has to be destroyable without ceremony.
  deletion_protection = false
  skip_final_snapshot = true

  # Both off on purpose: Performance Insights and log exports are useful and
  # neither is free at this budget. The production root turns both on.
  performance_insights_enabled = false

  tags = { Name = local.name }
}

# --- secrets ----------------------------------------------------------------
#
# SSM Parameter Store rather than Secrets Manager. Standard SecureString
# parameters are free where Secrets Manager is $0.40 per secret per month, and
# at this scale the difference in features is rotation — which this deployment
# does not use. Both are KMS-encrypted and both are read by the task execution
# role at container start, so the security properties are identical here.

resource "aws_ssm_parameter" "db_password" {
  name        = "/${local.name}/db/password"
  description = "RDS master password. Generated by Terraform, never typed."
  type        = "SecureString"
  value       = random_password.database.result

  tags = { Name = local.name }
}

resource "aws_ssm_parameter" "groq_api_key" {
  name        = "/${local.name}/groq/api-key"
  description = "Populate out of band. Read when llm_provider = groq and force_offline = 0."
  type        = "SecureString"
  # Placeholder, same pattern as the Anthropic key below: the parameter must
  # exist before the task definition can reference it, and the real value is
  # written with `aws ssm put-parameter` so it never passes through Terraform
  # and never lands in state.
  value = "unset"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = local.name }
}

resource "aws_ssm_parameter" "anthropic_api_key" {
  name        = "/${local.name}/anthropic/api-key"
  description = "Populate out of band. Only read when AIOPS_FORCE_OFFLINE=0."
  type        = "SecureString"
  # A placeholder, because the parameter must exist before the task definition
  # can reference it. The real value is written with `aws ssm put-parameter`, so
  # it never passes through Terraform and never lands in state.
  value = "unset"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = local.name }
}
