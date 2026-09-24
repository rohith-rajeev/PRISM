output "dynamodb_table_name" {
  description = "DynamoDB table storing commit-sha -> Google Chat thread mappings."
  value       = aws_dynamodb_table.pr_deploy_threads.name
}

output "pr_merge_notifier_function_name" {
  value = aws_lambda_function.pr_merge_notifier.function_name
}

output "deployment_notifier_function_name" {
  value = aws_lambda_function.deployment_notifier.function_name
}

output "pr_merged_event_rule_name" {
  value = aws_cloudwatch_event_rule.pr_merged.name
}

output "pipeline_succeeded_event_rule_name" {
  value = aws_cloudwatch_event_rule.pipeline_succeeded.name
}
