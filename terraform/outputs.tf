output "alb_dns_name" {
  description = "DNS name of the Application Load Balancer (point your CNAME here)"
  value       = module.ecs.alb_dns_name
}

output "ecr_repository_url" {
  description = "ECR repository URL — used by CI to push images"
  value       = module.ecs.ecr_repo_url
}

output "rds_endpoint" {
  description = "RDS instance endpoint (private — not publicly accessible)"
  value       = module.rds.db_endpoint
  sensitive   = true
}

output "db_secret_arn" {
  description = "Secrets Manager ARN for RDS master password"
  value       = module.rds.db_secret_arn
}

output "api_key_secret_arn" {
  description = "Secrets Manager ARN for API bearer token"
  value       = aws_secretsmanager_secret.api_key.arn
}

output "cloudwatch_dashboard" {
  description = "CloudWatch ops dashboard name"
  value       = module.monitoring.dashboard_name
}

output "sns_alert_topic_arn" {
  description = "SNS topic ARN for alert subscriptions"
  value       = module.monitoring.sns_topic_arn
}
