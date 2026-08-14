output "app_url" {
  description = "The public URL. This is what goes on the CV."
  value       = "https://${aws_cloudfront_distribution.app.domain_name}"
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.app.domain_name
}

output "alb_dns_name" {
  description = "CloudFront's origin. Not publicly reachable — the ALB security group admits CloudFront only."
  value       = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  description = "Push target for the ARM64 image."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  value = aws_ecs_service.app.name
}

output "task_family" {
  value = aws_ecs_task_definition.app.family
}

output "data_bucket" {
  description = "Upload the corpus to docs/ and the index to index/ here."
  value       = aws_s3_bucket.data.bucket
}

output "docs_uri" {
  description = "Value the task receives as AIOPS_DOCS_URI."
  value       = local.s3_docs_uri
}

output "index_uri" {
  description = "Value the task receives as AIOPS_INDEX_URI."
  value       = local.s3_index_uri
}

output "database_endpoint" {
  description = "Private. Reachable only from the task security group."
  value       = aws_db_instance.main.address
}

output "db_password_parameter" {
  description = "SSM parameter holding the generated master password."
  value       = aws_ssm_parameter.db_password.name
}

output "anthropic_key_parameter" {
  description = "Populate out of band: aws ssm put-parameter --overwrite --type SecureString --name <this> --value sk-..."
  value       = aws_ssm_parameter.anthropic_api_key.name
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "estimated_monthly_cost" {
  description = "Fixed cost only. Usage-based items round to zero at demo traffic."
  value = format(
    "~$%.2f/month = Fargate ARM %.2f vCPU / %.0f GB $%.2f + ALB $16.43 + public IPv4 $3.65 + RDS %s $11.68 + storage %dGB $%.2f + ~$0.55 other",
    local.fargate_monthly + 16.43 + 3.65 + 11.68 + (var.db_allocated_storage * 0.115) + 0.55,
    var.task_cpu / 1024,
    var.task_memory / 1024,
    local.fargate_monthly,
    var.db_instance_class,
    var.db_allocated_storage,
    var.db_allocated_storage * 0.115,
  )
}
