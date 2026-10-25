# Scale the API on average CPU: fast out (60s), slow in (300s). The UI stays at min_tasks.
# The ceiling is set by the database, not by money: each task may open up to 10 connections
# (pool 5 + 5 overflow), so max_tasks x 10 must stay below the DB's max_connections (~100).

resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_tasks
  max_capacity       = var.max_tasks
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "urly-api-cpu-50"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = 50
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# Fast scale-out. Target tracking (above) needs 3 consecutive one-minute readings over the target
# before it acts, which was too slow for a steep ramp (load test runs 1 and 2: ~5 minutes from
# "CPU over 50%" to a third task). This alarm fires on ONE reading, and the steps add more tasks
# the further CPU is above 50%. Target tracking stays: it still handles the slow scale-in.
# AWS accepts both policies on one target and takes the larger scale-out.

resource "aws_appautoscaling_policy" "api_cpu_step_out" {
  name               = "urly-api-cpu-step-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 60
    metric_aggregation_type = "Average"

    # bounds are relative to the alarm threshold (50%): 50-70% -> +1, 70-85% -> +2, above -> +3
    step_adjustment {
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = 20
      scaling_adjustment          = 1
    }
    step_adjustment {
      metric_interval_lower_bound = 20
      metric_interval_upper_bound = 35
      scaling_adjustment          = 2
    }
    step_adjustment {
      metric_interval_lower_bound = 35
      scaling_adjustment          = 3
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "api_cpu_high_fast" {
  alarm_name          = "urly-api-cpu-high-fast"
  alarm_description   = "API CPU at or above 50% for one minute: scale out (not an email alarm)"
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  dimensions          = { ClusterName = aws_ecs_cluster.main.name, ServiceName = aws_ecs_service.api.name }
  statistic           = "Average"
  period              = 60
  evaluation_periods  = 1
  threshold           = 50
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = [aws_appautoscaling_policy.api_cpu_step_out.arn]
}
