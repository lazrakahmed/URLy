terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State lives in the bootstrap bucket (private, versioned, encrypted). `use_lockfile` = S3-native
  # locking, no DynamoDB table. The bucket name contains the account id (not secret).
  backend "s3" {
    bucket       = "urly-tfstate-419819288397"
    key          = "urly/terraform.tfstate"
    region       = "eu-north-1"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "urly", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# The ECR repositories are created in bootstrap so images survive `terraform destroy`.
data "aws_ecr_repository" "api" {
  name = "urly-api"
}

data "aws_ecr_repository" "ui" {
  name = "urly-ui"
}
