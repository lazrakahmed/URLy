#!/usr/bin/env bash
# Delete EVERYTHING this project created in AWS (main environment, then bootstrap).
# The ALB, RDS and Fargate bill hourly: run this after the demo.
set -euo pipefail
cd "$(dirname "$0")/.."

aws sts get-caller-identity >/dev/null 2>&1 || { echo "No valid AWS session. Run: aws login" >&2; exit 1; }
read -r -p "Destroy the whole URLy environment in AWS (data, images, CI user)? Type 'destroy': " ANSWER
[ "$ANSWER" = "destroy" ] || { echo "Aborted."; exit 1; }

trap 'echo "FAILED. Anything not yet deleted is still billing (ALB, RDS, Fargate). Fix the error and re-run this script." >&2' ERR

# (The CI user's access key is deleted with the user: force_destroy in bootstrap.)
terraform -chdir=infra init -input=false
terraform -chdir=infra destroy -input=false -auto-approve -var image_tag=teardown
terraform -chdir=infra/bootstrap destroy -input=false -auto-approve

echo "Done. Also remove the GitHub secrets/variables:"
echo "  gh secret delete AWS_ACCESS_KEY_ID; gh secret delete AWS_SECRET_ACCESS_KEY"
echo "  gh variable delete AWS_DEPLOY_ROLE_ARN; gh variable delete AWS_REGION"
