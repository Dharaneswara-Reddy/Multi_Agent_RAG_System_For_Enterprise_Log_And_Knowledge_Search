# Logs and alarms.
#
# The alarms below are deliberately few. An alarm that fires on noise gets
# muted, and a muted alarm is worse than no alarm because it looks like
# coverage — a lesson this project's own corpus documents at length.

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days

  tags = local.tags
}

resource "aws_sns_topic" "alerts" {
  count = var.alarm_email != "" ? 1 : 0

  name = "${local.name}-alerts"
  tags = local.tags
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count = var.alarm_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarm_actions = var.alarm_email != "" ? [aws_sns_topic.alerts[0].arn] : []

  # The ALB dimension wants the id suffix, not the full ARN.
  alb_suffix = aws_lb.main.arn_suffix
}

# Target 5xx, not ELB 5xx: the first means the application failed, the second
# usually means a task is starting or draining and is expected during a deploy.
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  alarm_description   = "API returning server errors — the application is failing, not merely deploying."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.alarm_5xx_rate_threshold
  comparison_operator = "GreaterThanThreshold"
  # Missing data means no traffic, which is not a fault.
  treat_missing_data = "notBreaching"

  dimensions = {
    LoadBalancer = local.alb_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = local.tags
}

# Zero healthy hosts is the condition that means the service is down, as
# distinct from slow. Evaluated over two periods so a rolling deploy does not
# page anyone.
resource "aws_cloudwatch_metric_alarm" "api_unhealthy" {
  alarm_name          = "${local.name}-api-unhealthy-hosts"
  alarm_description   = "No healthy API tasks behind the load balancer."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    LoadBalancer = local.alb_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = local.tags
}

# Latency is expected to be seconds — reranking is CPU-bound and synthesis is a
# network call to a model. The threshold is set well above normal so it catches
# a regression rather than describing the steady state.
resource "aws_cloudwatch_metric_alarm" "api_latency" {
  alarm_name          = "${local.name}-api-latency"
  alarm_description   = "API p95 latency above the expected ceiling."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 3
  threshold           = var.alarm_p95_latency_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = local.alb_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }

  alarm_actions = local.alarm_actions

  tags = local.tags
}

# Storage exhaustion on the database is silent until writes start failing, and
# the audit table grows with every answered question.
resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name          = "${local.name}-db-free-storage"
  alarm_description   = "RDS free storage below 2GB. The audit table grows with traffic."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 2 * 1024 * 1024 * 1024
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }

  alarm_actions = local.alarm_actions

  tags = local.tags
}
