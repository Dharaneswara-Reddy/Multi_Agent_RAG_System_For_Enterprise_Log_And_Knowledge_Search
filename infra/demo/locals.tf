data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# CloudFront's origin-facing ranges, as a managed prefix list. Looked up rather
# than hardcoded: the list is maintained by AWS and its contents change, so a
# copied CIDR block becomes a broken origin the day it is rotated.
data "aws_ec2_managed_prefix_list" "cloudfront_origins" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

locals {
  name = "${var.project}-demo"

  # Two public subnets because an ALB requires two availability zones, and two
  # private ones because an RDS subnet group does too — even for a Single-AZ
  # instance. Only one of each is actually used. Subnets are free.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
  public_subnets = [
    cidrsubnet(var.vpc_cidr, 8, 0),
    cidrsubnet(var.vpc_cidr, 8, 1),
  ]
  private_subnets = [
    cidrsubnet(var.vpc_cidr, 8, 10),
    cidrsubnet(var.vpc_cidr, 8, 11),
  ]

  s3_docs_uri  = "s3://${aws_s3_bucket.data.bucket}/${trim(var.docs_prefix, "/")}/"
  s3_index_uri = "s3://${aws_s3_bucket.data.bucket}/${trim(var.index_prefix, "/")}/"

  enable_alarms = var.alarm_email != ""

  # Rates from the AWS Pricing API for us-east-1, Graviton Fargate:
  # $0.03238 per vCPU-hour, $0.00356 per GB-hour, 730 hours per month.
  fargate_monthly = ((0.03238 * (var.task_cpu / 1024)) + (0.00356 * (var.task_memory / 1024))) * 730

  # Non-secret configuration only. Anything here is readable by anyone holding
  # ecs:DescribeTaskDefinition; secrets arrive through `secrets` instead.
  container_environment = [
    # The guard that makes "no SQLite in the cloud" true rather than intended.
    # With this set, a missing or malformed AIOPS_DB_URL stops the process
    # instead of silently producing a container-local database.
    { name = "AIOPS_REQUIRE_POSTGRES", value = "1" },

    { name = "AIOPS_DB_HOST", value = aws_db_instance.main.address },
    { name = "AIOPS_DB_PORT", value = tostring(aws_db_instance.main.port) },
    { name = "AIOPS_DB_NAME", value = var.db_name },
    { name = "AIOPS_DB_USER", value = var.db_username },

    # S3 is the source of truth for both knowledge and index artefacts.
    { name = "AIOPS_DOCS_URI", value = local.s3_docs_uri },
    { name = "AIOPS_INDEX_URI", value = local.s3_index_uri },

    # Measured: peak RSS 2146 MB -> 1563 MB (-29.6%) for +24.5% latency, with
    # retrieval bit-identical. This is what makes a 2 GB task viable.
    { name = "AIOPS_ONNX_DISABLE_CPU_ARENA", value = "1" },

    # Which provider the LLM layer calls. "groq" has a free tier, which is the
    # reason it is here; "anthropic" remains the default everywhere else.
    { name = "AIOPS_LLM_PROVIDER", value = var.llm_provider },

    { name = "AIOPS_FORCE_OFFLINE", value = var.force_offline },
    { name = "AIOPS_UI_PORT", value = tostring(var.container_port) },
    { name = "AWS_REGION", value = var.region },

    # ONNX Runtime and OpenMP size their pools from the host's core count,
    # which on Fargate is the instance's rather than the task's. At 0.5 vCPU an
    # unbounded pool spends its time context-switching.
    { name = "OMP_NUM_THREADS", value = "1" },
  ]
}
