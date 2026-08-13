terraform {
  # 1.11 is a floor, not a preference. Two things in this stack need it:
  #
  #   - the S3 backend's `use_lockfile` argument (S3-native state locking), which
  #     stopped being labelled experimental in 1.11. It is why there is no
  #     DynamoDB table anywhere in this repository — see infra/README.md.
  #   - `validation` blocks that reference *other* variables (1.9+), used in
  #     network.tf to fail at plan time when the VPC is configured with no
  #     egress path at all, rather than at first task start.
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.x is the current major line; 6.59.0 was the latest release when this
      # was written (2026-08-13). Pinned to the major so `-upgrade` picks up
      # fixes and new resources but never crosses a major boundary: v6 removed
      # all 17 OpsWorks resources, removed `aws_eip.vpc`, and started enforcing
      # strict booleans, so a silent v7 jump would not be a cosmetic diff.
      version = "~> 6.59"
    }
    random = {
      source = "hashicorp/random"
      # Used for exactly one thing: the RDS master password. See data.tf.
      version = "~> 3.7"
    }
  }

  # Remote state is configured out-of-band so the same code serves dev and prod:
  #
  #   terraform init -backend-config=backend.hcl
  #
  # Create the bucket first with the `bootstrap/` root module, which uses local
  # state on purpose (a bucket cannot hold the state describing itself).
  #
  # Deliberately empty rather than commented out: an empty block means Terraform
  # *demands* a backend config and refuses to silently write terraform.tfstate
  # into the working directory, which is the failure mode that ends with two
  # engineers holding divergent state files.
  backend "s3" {}
}
