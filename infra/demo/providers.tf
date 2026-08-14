provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Environment = "demo"
      ManagedBy   = "terraform"
      Root        = "infra/demo"
    }
  }
}

# CloudFront needs an ACM certificate in us-east-1 if a custom domain is ever
# added. Declared now so adding one later is a variable, not a refactor — an
# aliased provider cannot be introduced without reinitialising.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = var.project
      Environment = "demo"
      ManagedBy   = "terraform"
      Root        = "infra/demo"
    }
  }
}
