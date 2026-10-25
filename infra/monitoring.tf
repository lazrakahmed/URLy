# Alarms, log-based metrics and one dashboard. Platform signals (ALB, ECS) are recorded by AWS
# automatically; the metric filters turn the few app events we log into counters.

resource "aws_sns_topic" "alarms" {
  name = "urly-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email # AWS sends a confirmation email: click it once
}

# ---------- Metrics from log events ----------

resource "aws_cloudwatch_log_metric_filter" "db_error" {
  name           = "db_error"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"db_error\" }"
  metric_transformation {
    name          = "DbErrors"
    namespace     = "URLy"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "code_collision" {
  name           = "code_collision"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"code_collision\" }"
  metric_transformation {
    name          = "CodeCollisions"
    namespace     = "URLy"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "link_created" {
  name           = "link_created"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"link_created\" }"
  metric_transformation {
    name          = "LinksCreated"
    namespace     = "URLy"
    value         = "1"
    default_value = "0"
  }
}
# Deliberately no filter on redirects: volume (cost); the ALB request count already shows it.

# ---------- Alarms (all notify the SNS topic) ----------

resource "aws_cloudwatch_metric_alarm" "http_5xx" {
  alarm_name          = "urly-5xx"
  alarm_description   = "The API returned 5xx responses"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "latency" {
  alarm_name          = "urly-latency-p95"
  alarm_description   = "p95 response time above 500 ms"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix }
  extended_statistic  = "p95"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = 0.5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_targets" {
  alarm_name          = "urly-unhealthy-api-targets"
  alarm_description   = "The load balancer sees unhealthy API tasks"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.api.arn_suffix }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "db_errors" {
  alarm_name          = "urly-db-errors"
  alarm_description   = "The API cannot reach or use the database (db_error log events)"
  namespace           = "URLy"
  metric_name         = "DbErrors"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching" # known gap: no traffic during an outage means no event
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# ---------- Dashboard ----------

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "urly"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Requests and 5xx"
          region = var.region
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.main.arn_suffix],
            [".", "HTTPCode_Target_5XX_Count", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Latency (p95)"
          region = var.region
          stat   = "p95"
          period = 60
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.main.arn_suffix],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "API tasks: CPU and memory"
          region = var.region
          stat   = "Average"
          period = 60
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.api.name],
            [".", "MemoryUtilization", ".", ".", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "App events (from logs)"
          region = var.region
          stat   = "Sum"
          period = 60
          metrics = [
            ["URLy", "LinksCreated"],
            [".", "DbErrors"],
            [".", "CodeCollisions"],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title  = "Healthy and unhealthy API targets"
          region = var.region
          stat   = "Maximum"
          period = 60
          metrics = [
            ["AWS/ApplicationELB", "HealthyHostCount", "LoadBalancer", aws_lb.main.arn_suffix, "TargetGroup", aws_lb_target_group.api.arn_suffix],
            [".", "UnHealthyHostCount", ".", ".", ".", "."],
          ]
        }
      },
    ]
  })
}
