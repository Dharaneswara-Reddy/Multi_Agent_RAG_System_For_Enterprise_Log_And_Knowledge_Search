# Outputs the deploy pipeline and a human operator both need.

output "alb_dns_name" {
  description = "Public hostname. Point a CNAME at this, or use it directly when there is no certificate."
  value       = aws_lb.main.dns_name
}

output "api_url" {
  description = "Base URL for the API."
  value       = "${var.acm_certificate_arn != "" ? "https" : "http"}://${aws_lb.main.dns_name}"
}

output "ui_url" {
  description = "Streamlit console URL, when enabled."
  value       = var.enable_ui ? "${var.acm_certificate_arn != "" ? "https" : "http"}://${aws_lb.main.dns_name}/${trim(var.ui_base_url_path, "/")}" : null
}

output "ecr_repository_url" {
  description = "Push target for CI. Set as the ECR_REPOSITORY GitHub variable."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "Set as the ECS_CLUSTER GitHub variable."
  value       = aws_ecs_cluster.main.name
}

output "ecs_api_service_name" {
  description = "Set as the ECS_SERVICE GitHub variable."
  value       = aws_ecs_service.api.name
}

output "ecs_ui_service_name" {
  description = "Set as the ECS_UI_SERVICE GitHub variable. Null when the console is disabled — leave the variable unset and the deploy skips it."
  value       = var.enable_ui ? aws_ecs_service.ui[0].name : null
}

output "api_task_family" {
  description = "Task definition family the deploy registers new revisions against. Set as ECS_TASK_FAMILY."
  value       = aws_ecs_task_definition.api.family
}

output "ui_task_family" {
  description = "Set as the ECS_UI_TASK_FAMILY GitHub variable. Null when the console is disabled."
  value       = var.enable_ui ? aws_ecs_task_definition.ui[0].family : null
}

output "artifacts_bucket" {
  description = "Upload index artefacts here; the tasks read them from this prefix."
  value       = aws_s3_bucket.artifacts.bucket
}

output "index_uri" {
  description = "Value the tasks receive as AIOPS_INDEX_URI."
  value       = "s3://${aws_s3_bucket.artifacts.bucket}/${trim(var.index_prefix, "/")}/"
}

output "anthropic_secret_arn" {
  description = "Populate out of band: aws secretsmanager put-secret-value --secret-id <this> --secret-string sk-..."
  value       = aws_secretsmanager_secret.anthropic.arn
}

output "database_secret_arn" {
  description = "Generated RDS credential as JSON (username, password, host, port, dbname)."
  value       = aws_secretsmanager_secret.database.arn
}

output "database_endpoint" {
  description = "RDS address. Private — reachable only from the task security group."
  value       = aws_db_instance.main.address
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "task_security_group_id" {
  description = "Attach to a bastion or a one-off task that needs database access."
  value       = aws_security_group.tasks.id
}

output "private_subnet_ids" {
  description = "For `aws ecs run-task`, e.g. running the one-shot `setup` command."
  value       = aws_subnet.private[*].id
}

output "estimated_monthly_cost_note" {
  description = "Rough guide only. See infra/README.md for the itemised breakdown."
  value = format(
    "~$%d/month: %d Fargate API task(s), %s UI, %s, %s NAT gateway(s), %d interface endpoint(s)",
    (var.api_desired_count * 30) + (var.enable_ui ? 15 : 0) + 17 + 14 + (local.nat_gateway_count * 33) + (local.enable_interface_endpoints ? 29 : 0),
    var.api_desired_count,
    var.enable_ui ? (var.ui_use_fargate_spot ? "spot" : "on-demand") : "disabled",
    var.db_instance_class,
    local.nat_gateway_mode,
    local.enable_interface_endpoints ? 4 : 0,
  )
}
