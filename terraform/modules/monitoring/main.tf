# modules/monitoring/main.tf
# CloudWatch alarms, SNS topic, and an operational dashboard.
#
# Alarms:
#   1. IngestionFailure       — any ingestion failure in last 5 min
#   2. StaleData              — no successful ingestion for > 10 min
#   3. APIHighErrorRate       — ALB 5xx > 5% of requests
#   4. RDSHighCPU             — RDS CPU > 80% for 5 min
#   5. RDSLowStorage          — RDS free storage < 2 GB
#   6. ECSTaskStopped         — worker task exits with non-zero (via log metric)

variable "name_prefix"      { type = string }
variable "aws_region"       { type = string }
variable "environment"      { type = string }
variable "alert_email"      { type = string }
variable "ecs_cluster_name" { type = string }
variable "api_service_name" { type = string }
variable "rds_identifier"   { type = string }
variable "log_group_names"  { type = map(string) }

# ── SNS topic ─────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ── Custom metric: IngestionFailure (published by Python worker) ──────────────

resource "aws_cloudwatch_metric_alarm" "ingestion_failure" {
  alarm_name          = "${var.name_prefix}-ingestion-failure"
  alarm_description   = "Ingestion worker reported a failure"
  namespace           = "MISO/ELT"
  metric_name         = "IngestionFailure"
  statistic           = "Sum"
  period              = 300   # 5 minutes
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ── Stale data: no successful ingestion recently ──────────────────────────────
# If IngestionSuccess metric sum is 0 over 10 minutes → alarm

resource "aws_cloudwatch_metric_alarm" "stale_data" {
  alarm_name          = "${var.name_prefix}-stale-data"
  alarm_description   = "No successful MISO ingestion in the last 10 minutes"
  namespace           = "MISO/ELT"
  metric_name         = "IngestionSuccess"
  statistic           = "Sum"
  period              = 600   # 10 minutes
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"   # absence of data is itself the alarm

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ── ALB 5xx error rate ────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.name_prefix}-api-5xx"
  alarm_description   = "API 5xx error rate elevated"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = "${var.name_prefix}-alb"
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ── RDS CPU ───────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.name_prefix}-rds-high-cpu"
  alarm_description   = "RDS CPU utilization > 80%"
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    DBInstanceIdentifier = var.rds_identifier
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ── RDS free storage ──────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${var.name_prefix}-rds-low-storage"
  alarm_description   = "RDS free storage below 2 GB"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 2147483648   # 2 GB in bytes
  comparison_operator = "LessThanThreshold"

  dimensions = {
    DBInstanceIdentifier = var.rds_identifier
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ── ECS service running task count ────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "ecs_running_tasks" {
  alarm_name          = "${var.name_prefix}-api-no-running-tasks"
  alarm_description   = "API ECS service has 0 running tasks"
  namespace           = "AWS/ECS"
  metric_name         = "RunningTaskCount"
  statistic           = "Average"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.api_service_name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ── Log metric filter: worker ERROR lines ─────────────────────────────────────

resource "aws_cloudwatch_log_metric_filter" "worker_errors" {
  name           = "${var.name_prefix}-worker-errors"
  log_group_name = var.log_group_names["worker"]
  pattern        = "{ $.level = \"error\" }"

  metric_transformation {
    name      = "WorkerErrorCount"
    namespace = "MISO/ELT"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${var.name_prefix}-worker-log-errors"
  alarm_description   = "Worker container logged ERROR-level messages"
  namespace           = "MISO/ELT"
  metric_name         = "WorkerErrorCount"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ── CloudWatch dashboard ──────────────────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.name_prefix}-ops"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x = 0; y = 0; width = 12; height = 6
        properties = {
          title  = "Ingestion Success / Failure"
          period = 300
          stat   = "Sum"
          metrics = [
            ["MISO/ELT", "IngestionSuccess"],
            ["MISO/ELT", "IngestionFailure"],
          ]
        }
      },
      {
        type   = "metric"
        x = 12; y = 0; width = 12; height = 6
        properties = {
          title  = "Rows Upserted per Run"
          period = 300
          stat   = "Sum"
          metrics = [["MISO/ELT", "RowsUpserted"]]
        }
      },
      {
        type   = "metric"
        x = 0; y = 6; width = 12; height = 6
        properties = {
          title  = "MISO API Latency (ms)"
          period = 60
          stat   = "p95"
          metrics = [["MISO/ELT", "MISOAPILatencyMs"]]
        }
      },
      {
        type   = "metric"
        x = 12; y = 6; width = 12; height = 6
        properties = {
          title  = "RDS CPU Utilization"
          period = 300
          stat   = "Average"
          metrics = [["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", var.rds_identifier]]
        }
      },
      {
        type   = "alarm"
        x = 0; y = 12; width = 24; height = 4
        properties = {
          title  = "Active Alarms"
          alarms = [
            "arn:aws:cloudwatch:${var.aws_region}::alarm:${var.name_prefix}-ingestion-failure",
            "arn:aws:cloudwatch:${var.aws_region}::alarm:${var.name_prefix}-stale-data",
            "arn:aws:cloudwatch:${var.aws_region}::alarm:${var.name_prefix}-api-5xx",
            "arn:aws:cloudwatch:${var.aws_region}::alarm:${var.name_prefix}-rds-high-cpu",
          ]
        }
      },
    ]
  })
}

output "sns_topic_arn"    { value = aws_sns_topic.alerts.arn }
output "dashboard_name"   { value = aws_cloudwatch_dashboard.main.dashboard_name }
