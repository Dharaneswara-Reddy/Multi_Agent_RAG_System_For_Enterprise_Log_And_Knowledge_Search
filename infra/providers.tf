provider "aws" {
  region = var.region

  # Tagging every taggable resource from one place rather than per-resource.
  # This is not cosmetic: cost allocation by `Environment` is the only way to
  # answer "what does dev actually cost" once dev and prod share an account,
  # and the itemised estimate in infra/README.md is unverifiable without it.
  #
  # Note the gap, because default_tags is often assumed to be total: it applies
  # only to resources the AWS API tags at create time. Application Auto Scaling
  # targets and policies, and a handful of sub-resources, are not tagged by it.
  default_tags {
    tags = local.tags
  }
}

# Used only to make the S3 bucket names globally unique deterministically.
# `random_id` would also work and is the more common idiom; it is avoided here
# because a bucket name that depends on random state is a bucket name nobody can
# reconstruct from the code, and re-creating the state means re-creating the
# bucket.
data "aws_caller_identity" "current" {}
