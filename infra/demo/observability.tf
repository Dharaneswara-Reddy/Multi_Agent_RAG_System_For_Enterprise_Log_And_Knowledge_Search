# Deliberately small. CloudWatch bills $0.50 per GB ingested and $0.10 per alarm
# per month, and on a $120 budget an observability stack costing more than the
# database would be the wrong trade. One log group, short retention, and alarms
# only when an email is supplied.

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days

  tags = { Name = local.name }
}

# --- optional alerting ------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  count = local.enable_alarms ? 1 : 0

  name = "${local.name}-alerts"
  tags = { Name = local.name }
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count = local.enable_alarms ? 1 : 0

  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# The one that matters. With a single task there is no redundancy, so the
# question is not "is capacity degraded" but "is anything serving at all".
resource "aws_cloudwatch_metric_alarm" "no_healthy_targets" {
  count = local.enable_alarms ? 1 : 0

  alarm_name          = "${local.name}-no-healthy-targets"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  # Missing data is the signal: no task means no metric reported.
  treat_missing_data = "breaching"

  alarm_description = "No healthy target behind the load balancer. The application is down."
  alarm_actions     = [aws_sns_topic.alerts[0].arn]
  ok_actions        = [aws_sns_topic.alerts[0].arn]

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }

  tags = { Name = local.name }
}

# Memory is the constraint this deployment was designed around: 1563 MB
# measured against a 2048 MiB limit. Exceeding it is an OOM kill, not a slow
# request, so the warning needs to arrive before the ceiling.
resource "aws_cloudwatch_metric_alarm" "task_memory" {
  count = local.enable_alarms ? 1 : 0

  alarm_name          = "${local.name}-task-memory-high"
  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_description = "Task memory above 85% of 2 GB; approaching an OOM kill."
  alarm_actions     = [aws_sns_topic.alerts[0].arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.app.name
  }

  tags = { Name = local.name }
}

# Storage never shrinks on its own, and a full disk on the database is a silent
# write failure rather than a visible outage.
resource "aws_cloudwatch_metric_alarm" "db_storage" {
  count = local.enable_alarms ? 1 : 0

  alarm_name          = "${local.name}-db-storage-low"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 2 * 1024 * 1024 * 1024 # 2 GB of 20
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_description = "RDS free storage below 2 GB."
  alarm_actions     = [aws_sns_topic.alerts[0].arn]

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }

  tags = { Name = local.name }
}
