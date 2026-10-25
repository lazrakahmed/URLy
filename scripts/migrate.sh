#!/usr/bin/env bash
# Run the database migration as a one-off ECS task (same image the API runs; ADR 0002).
# Needs AWS credentials and an applied infra/ (the migrate task definition must point at the
# image tag you are about to deploy). Used by first-deploy.sh and the pipeline.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${AWS_REGION:-eu-north-1}"
CLUSTER=$(terraform -chdir=infra output -raw cluster_name)
TASKDEF=$(terraform -chdir=infra output -raw migrate_task_definition)
SUBNETS=$(terraform -chdir=infra output -json task_subnets | python3 -c 'import sys,json; print(",".join(json.load(sys.stdin)))')
SG=$(terraform -chdir=infra output -raw task_security_group)

TASK_ARN=$(aws ecs run-task --region "$REGION" --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition "$TASKDEF" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --query 'tasks[0].taskArn' --output text)
[ -n "$TASK_ARN" ] && [ "$TASK_ARN" != "None" ] || { echo "The migration task did not start (check subnets, security group, capacity)." >&2; exit 1; }
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN"
EXIT_CODE=$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
[ "$EXIT_CODE" = "0" ] || { echo "Migration failed (exit code $EXIT_CODE). See log group /ecs/urly" >&2; exit 1; }
echo "migration ok"
