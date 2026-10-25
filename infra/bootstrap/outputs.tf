output "state_bucket" {
  value = aws_s3_bucket.state.id
}

output "ecr_repository_urls" {
  value = { for k, r in aws_ecr_repository.repo : k => r.repository_url }
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}

output "ci_user_name" {
  value = aws_iam_user.ci.name
}
