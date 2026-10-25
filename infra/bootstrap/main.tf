# Bootstrap: things that must exist BEFORE the main config and outlive `terraform destroy` of it.
#   - S3 bucket holding the main config's Terraform state
#   - ECR repositories (images must exist before ECS can start tasks)
#   - the CI deploy role and the IAM user that can only assume it
# Uses local state (terraform.tfstate, git-ignored): it cannot live in the bucket it creates.
# Run by hand once: scripts/first-deploy.sh. Removed at teardown: scripts/teardown.sh.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "urly", ManagedBy = "terraform-bootstrap" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "urly-tfstate-${local.account_id}"
  repos        = toset(["urly-api", "urly-ui"])
}

# ---------- Terraform state ----------

resource "aws_s3_bucket" "state" {
  bucket        = local.state_bucket
  force_destroy = true # teardown must be able to delete it
}

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
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------- Container registries ----------

resource "aws_ecr_repository" "repo" {
  for_each             = local.repos
  name                 = each.key
  image_tag_mutability = "IMMUTABLE" # a tag (the git SHA) always means the same image
  force_delete         = true        # teardown deletes the images too

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "keep_last_10" {
  for_each   = aws_ecr_repository.repo
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# ---------- CI credentials ----------
# GitHub OIDC cannot be used here (the Free plan blocks creating the OIDC provider), so CI
# uses a dedicated IAM user whose ONLY permission is to assume the deploy role below.
# The access key is NOT created here (it would end up in Terraform state): scripts/ creates it
# and stores it straight into GitHub secrets.

resource "aws_iam_user" "ci" {
  name          = "urly-github-deploy"
  force_destroy = true # teardown also deletes its access key
}

resource "aws_iam_role" "deploy" {
  name                 = "urly-deploy"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_user.ci.arn }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_user_policy" "ci_assume_only" {
  name = "assume-deploy-role-only"
  user = aws_iam_user.ci.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = aws_iam_role.deploy.arn
    }]
  })
}

# What the pipeline may do: build and deploy this app, nothing else. Service-level actions for
# the services Terraform manages; IAM and S3 are limited to this project's names.
resource "aws_iam_role_policy" "deploy" {
  name = "deploy-urly"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "EcrPushPull"
        Effect   = "Allow"
        Action   = ["ecr:*"]
        Resource = [for r in aws_ecr_repository.repo : r.arn]
      },
      {
        Sid      = "EcrRead"
        Effect   = "Allow"
        Action   = ["ecr:DescribeRepositories", "ecr:ListTagsForResource", "ecr:GetLifecyclePolicy", "ecr:GetRepositoryPolicy"]
        Resource = "*"
      },
      {
        Sid      = "InfrastructureServices"
        Effect   = "Allow"
        Action   = ["ec2:*", "elasticloadbalancing:*", "ecs:*", "rds:*", "logs:*", "cloudwatch:*", "sns:*", "application-autoscaling:*"]
        Resource = "*"
      },
      {
        Sid      = "DatabaseSecretOnly"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy"]
        Resource = "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:rds!*" # only the RDS-managed secret
      },
      {
        Sid      = "KeyUseForRdsManagedPassword"
        Effect   = "Allow"
        Action   = ["kms:DescribeKey", "kms:Decrypt", "kms:GenerateDataKey", "kms:CreateGrant"]
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = ["rds.${var.region}.amazonaws.com", "secretsmanager.${var.region}.amazonaws.com"] }
        }
      },
      {
        Sid    = "ProjectRolesOnly"
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags",
          "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole"
        ]
        # Only the two roles the app needs, not any role named urly-*.
        Resource = [
          "arn:aws:iam::${local.account_id}:role/urly-task-execution",
          "arn:aws:iam::${local.account_id}:role/urly-task",
        ]
      },
      {
        Sid    = "PassRolesToEcsOnly"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          "arn:aws:iam::${local.account_id}:role/urly-task-execution",
          "arn:aws:iam::${local.account_id}:role/urly-task",
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        Sid      = "ServiceLinkedRoles"
        Effect   = "Allow"
        Action   = ["iam:CreateServiceLinkedRole"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "iam:AWSServiceName" = [
              "ecs.amazonaws.com", "elasticloadbalancing.amazonaws.com", "rds.amazonaws.com",
              "ecs.application-autoscaling.amazonaws.com"
            ]
          }
        }
      },
      {
        Sid      = "TerraformStateObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.state.arn}/*"
      },
      {
        Sid      = "TerraformStateBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.state.arn
      }
    ]
  })
}
