# Derived values and naming.
#
# Several variables default to `null` rather than a concrete value. That is
# deliberate: `null` means "decide from the environment", so a dev stack is
# cheap and a prod stack is durable without anyone having to remember a list of
# overrides. Passing an explicit value always wins.

locals {
  name = "${var.project}-${var.environment}"

  is_production = var.environment == "prod"

  tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "Multi_Agent_RAG_System_For_Enterprise_Log_And_Knowledge_Search"
    },
    var.extra_tags,
  )

  # NAT is the single largest line item in a small deployment (~$32/month per
  # gateway plus data processing), and this workload's only outbound need is
  # api.anthropic.com. Everything else it talks to — ECR, S3, Secrets Manager,
  # CloudWatch — is reachable through VPC endpoints.
  #
  #   none    : no egress. Valid only when force_offline is set, because the
  #             synthesis call cannot leave the VPC.
  #   single  : one gateway shared by all AZs. Cheapest working option; an AZ
  #             failure takes egress with it.
  #   per_az  : one per AZ. Doubles the cost to remove that shared fate.
  nat_gateway_mode = coalesce(
    var.nat_gateway_mode,
    var.force_offline ? "none" : (local.is_production ? "per_az" : "single"),
  )

  nat_gateway_count = {
    none   = 0
    single = 1
    per_az = var.az_count
  }[local.nat_gateway_mode]

  # Interface endpoints cost ~$7.20/month each per AZ. With NAT already present
  # they mostly buy privacy rather than savings; with NAT absent they are the
  # only way the tasks reach ECR at all, so they stop being optional.
  enable_interface_endpoints = var.enable_interface_endpoints || local.nat_gateway_mode == "none"

  db_multi_az                 = coalesce(var.db_multi_az, local.is_production)
  s3_force_destroy            = coalesce(var.s3_force_destroy, !local.is_production)
  secret_recovery_window_days = coalesce(var.secret_recovery_window_days, local.is_production ? 30 : 0)

  # Streamlit serves its health endpoint under the base URL path when one is
  # configured, so the target group has to follow it.
  ui_health_check_path = coalesce(
    var.ui_health_check_path,
    var.ui_base_url_path == "" ? "/_stcore/health" : "/${trim(var.ui_base_url_path, "/")}/_stcore/health",
  )

  # Environment shared by both task definitions. Secrets are injected
  # separately via `secrets` so their values never appear in the task
  # definition JSON, which is readable by anyone with ecs:DescribeTaskDefinition.
  common_environment = merge(
    {
      AIOPS_INDEX_URI  = "s3://${aws_s3_bucket.artifacts.bucket}/${trim(var.index_prefix, "/")}/"
      AIOPS_DB_HOST    = aws_db_instance.main.address
      AIOPS_DB_PORT    = tostring(aws_db_instance.main.port)
      AIOPS_DB_NAME    = var.db_name
      AIOPS_DB_USER    = var.db_username
      AIOPS_DB_SCHEME  = var.db_url_scheme
      AIOPS_SERVICE_NAME = local.name
      AWS_REGION       = var.region
      # ONNX Runtime sizes its thread pool from the host's core count, which on
      # Fargate is the instance's rather than the task's. Left unbounded a small
      # task spawns dozens of threads and thrashes.
      OMP_NUM_THREADS = tostring(max(1, floor(var.api_cpu / 1024)))
    },
    var.force_offline ? { AIOPS_FORCE_OFFLINE = "1" } : {},
    var.otlp_endpoint != "" ? { AIOPS_OTLP_ENDPOINT = var.otlp_endpoint } : {},
    var.extra_environment,
  )
}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

