# Demo deployment — a separate root module from `infra/`, on purpose.
#
# `infra/` is the production architecture: Fargate, ALB, Multi-AZ RDS, NAT. It
# is validated and unchanged. This module is the thing that actually runs 24/7
# on a $120 credit budget, and it is a *different* architecture rather than the
# same one with smaller numbers — no ALB, no NAT, no second task, one instance.
#
# Two roots rather than one with a `demo` flag, because the two share almost no
# resources and a flag would mean every file grew a conditional that only ever
# takes one branch in any given deployment. Separate state keys in the same
# bucket keep them independent: applying or destroying one cannot touch the
# other.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.59"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # Same bucket as the production root, different key. Configured out of band:
  #
  #   terraform init -backend-config=../backend.hcl \
  #                  -backend-config="key=aiops/demo/terraform.tfstate"
  #
  # See README.md. Empty rather than commented out so Terraform refuses to
  # silently write state into the working directory.
  backend "s3" {}
}
