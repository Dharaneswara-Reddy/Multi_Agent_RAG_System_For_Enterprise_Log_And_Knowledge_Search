# Remote state backend, created before the main stack exists.
#
# This module uses **local state on purpose**: a bucket cannot hold the state
# file that describes the bucket. It is applied once per account, produces a
# `backend.hcl` for the root module, and is then left alone.
#
#   cd infra/bootstrap
#   terraform init && terraform apply
#   terraform output -raw backend_config > ../backend.hcl
#
# Commit `bootstrap/terraform.tfstate` or not, as you prefer — it contains no
# secrets, only bucket names. Losing it costs an import, not the bucket.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.59"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "project" {
  type    = string
  default = "aiops"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  bucket = "${var.project}-tfstate-${data.aws_caller_identity.current.account_id}"

  # No force_destroy. Deleting the state bucket by accident means the
  # infrastructure still exists and Terraform no longer knows about any of it.
  tags = {
    Project   = var.project
    Purpose   = "terraform-state"
    ManagedBy = "terraform"
  }
}

# Non-negotiable for state: a corrupted or truncated write is otherwise
# unrecoverable, and versioning is the only way back.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# State contains the RDS password in plaintext. TLS-only is the minimum.
data "aws_iam_policy_document" "state" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]

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

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state.json
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "backend_config" {
  description = "Write to ../backend.hcl and run: terraform init -backend-config=backend.hcl"

  # `use_lockfile` is S3-native locking, stable since Terraform 1.11. It
  # replaces the DynamoDB table that older guides still require — which is why
  # there is no DynamoDB anywhere in this repository.
  value = <<-EOT
    bucket       = "${aws_s3_bucket.state.bucket}"
    key          = "aiops/terraform.tfstate"
    region       = "${var.region}"
    encrypt      = true
    use_lockfile = true
  EOT
}
