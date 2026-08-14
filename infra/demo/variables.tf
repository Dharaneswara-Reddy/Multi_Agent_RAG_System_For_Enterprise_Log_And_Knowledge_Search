variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "aiops"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

# --- compute ---------------------------------------------------------------

variable "task_cpu" {
  description = <<-EOT
    Fargate CPU units. 512 = 0.5 vCPU, the smallest size that supports 2 GB of
    memory (0.25 vCPU caps at 2 GB but pairs badly with a CPU-bound reranker).

    Reranking is the bottleneck at roughly 0.1s per candidate pair, so a
    30-candidate rerank takes several seconds here. That is the trade this
    deployment makes knowingly: 10-20 visitors a month do not justify paying
    for a full vCPU that idles.
  EOT
  type        = number
  default     = 512
}

variable "task_memory" {
  description = <<-EOT
    MiB. 2048 is sufficient because of a measured result, not an estimate: with
    the ONNX CPU arena disabled the application plateaus at 1563 MB over 40
    queries, leaving ~485 MB of headroom.

    Fargate is what makes 2 GB work where 2 GiB of EC2 did not. This limit
    applies to the container alone — no host OS, container runtime or ECS agent
    is charged against it, which on EC2 cost roughly 550 MB and pushed the same
    workload over the edge.
  EOT
  type        = number
  default     = 2048

  validation {
    condition     = var.task_memory >= 2048
    error_message = "Measured steady-state RSS is 1563 MB; below 2048 MiB the task will be OOM-killed."
  }
}

variable "desired_count" {
  description = <<-EOT
    One. This is a demo: it is not highly available and does not autoscale, and
    raising this number is the single change that would begin to make it so.
    The target group already has stickiness configured for that day, because
    Streamlit sessions are bound to a task.
  EOT
  type        = number
  default     = 1
}

variable "image_tag" {
  description = "Image tag to run. The ECR repository starts empty — see README.md."
  type        = string
  default     = "bootstrap"
}

variable "container_port" {
  description = "Streamlit's port inside the container."
  type        = number
  default     = 8501
}

# --- database --------------------------------------------------------------

variable "db_instance_class" {
  description = "db.t4g.micro is the smallest PostgreSQL instance AWS offers."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "GB. 20 is the RDS minimum, so this is a floor rather than a choice."
  type        = number
  default     = 20
}

variable "db_engine_version" {
  description = <<-EOT
    Checked against the API rather than assumed, because the constraint is not
    "which versions exist" but "which are orderable for this instance class".
    PostgreSQL 17.5 through 17.10 all exist in us-east-1, but
    `describe-orderable-db-instance-options` lists only 17.9 and 17.10 for
    db.t4g.micro — anything earlier fails at apply, not at plan.
  EOT
  type        = string
  default     = "17.10"
}

variable "db_name" {
  type    = string
  default = "aiops"
}

variable "db_username" {
  type    = string
  default = "aiops"
}

variable "db_backup_retention_days" {
  description = <<-EOT
    Capped by the account plan, not by preference. This account is on the AWS
    Free Plan (created June 2026, after the July 2025 free-tier change), and
    RDS rejects a longer retention outright:

      FreeTierRestrictionError: The specified backup retention period exceeds
      the maximum available to free tier customers.

    1 day still gives automated daily snapshots and point-in-time recovery
    within that window, which is the difference between an accident being an
    inconvenience and being permanent. 0 disables backups entirely and should
    only be used if 1 is also refused.

    The production root is unconstrained and uses 7.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.db_backup_retention_days <= 1
    error_message = "The AWS Free Plan caps RDS backup retention; values above 1 are rejected at apply."
  }
}

# --- observability ---------------------------------------------------------

variable "log_retention_days" {
  description = "Short on purpose: logs are billed per GB ingested and per GB-month stored."
  type        = number
  default     = 7
}

variable "alarm_email" {
  description = "Optional. Empty disables the SNS topic and its subscription entirely."
  type        = string
  default     = ""
}

# --- application -----------------------------------------------------------

variable "llm_provider" {
  description = <<-EOT
    Which LLM provider the application calls: "anthropic" or "groq".

    Groq is OpenAI-wire-compatible and has a free tier, which is why it is an
    option here. Setting this alone is not enough — write the matching key into
    the provider's SSM parameter and set force_offline = "0", or the deployment
    stays on the deterministic extractive path.
  EOT
  type        = string
  default     = "anthropic"

  validation {
    condition     = contains(["anthropic", "groq"], var.llm_provider)
    error_message = "llm_provider must be anthropic or groq."
  }
}

variable "force_offline" {
  description = <<-EOT
    1 keeps answers deterministic and extractive, and needs no API key. Set to
    0 only after putting a key in the Secrets parameter — otherwise every
    synthesis call fails at request time rather than at deploy time.
  EOT
  type        = string
  default     = "1"
}

variable "docs_prefix" {
  description = "S3 prefix holding the markdown corpus. The cloud source of truth."
  type        = string
  default     = "docs"
}

variable "index_prefix" {
  description = "S3 prefix holding vectors.npy / chunks.pkl / stats.json."
  type        = string
  default     = "index"
}
