#!/usr/bin/env bash
# First deployment, run by hand: bootstrap -> build and push images -> apply -> migrate -> smoke test.
# Needs: an AWS login (`aws login`), Docker running, terraform, git. Takes ~15 minutes (RDS).
# Usage: scripts/first-deploy.sh [db_multi_az: true|false]
set -euo pipefail
cd "$(dirname "$0")/.."

REGION=eu-north-1
trap 'echo "FAILED. Resources created so far may still be billing: fix and re-run this script (it is safe to re-run), or run scripts/teardown.sh." >&2' ERR
MULTI_AZ="${1:-false}"

aws sts get-caller-identity >/dev/null 2>&1 || { echo "No valid AWS session. Run: aws login" >&2; exit 1; }
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

[ -z "$(git status --porcelain)" ] || { echo "Commit or remove all changes (including new files) first: the image tag is the git SHA." >&2; exit 1; }
TAG=$(git rev-parse --short=12 HEAD)

echo "==> 1/5 bootstrap (state bucket, ECR, CI role and user)"
terraform -chdir=infra/bootstrap init -input=false
terraform -chdir=infra/bootstrap apply -input=false -auto-approve

echo "==> 2/5 build and push images tagged $TAG (linux/amd64: what Fargate runs)"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
for pair in "urly-api:api" "urly-ui:ui"; do
  repo="${pair%%:*}"; dir="${pair##*:}"
  if aws ecr describe-images --repository-name "$repo" --image-ids imageTag="$TAG" --region "$REGION" >/dev/null 2>&1; then
    echo "$repo:$TAG already in ECR, skipping"
  else
    docker buildx build --platform linux/amd64 -t "$REGISTRY/$repo:$TAG" --push "$dir"
  fi
done

echo "==> 3/5 terraform apply (network, ALB, RDS, ECS, alarms)"
terraform -chdir=infra init -input=false
terraform -chdir=infra apply -input=false -auto-approve -var "image_tag=$TAG" -var "db_multi_az=$MULTI_AZ"

echo "==> 4/5 database migration (one-off ECS task)"
scripts/migrate.sh

echo "==> 5/5 smoke test"
URL=$(terraform -chdir=infra output -raw url)
scripts/smoke-test.sh "$URL"
echo
echo "Deployed: $URL"
echo "Alarm emails: confirm the SNS subscription email once (check the inbox of the alarm address)."
