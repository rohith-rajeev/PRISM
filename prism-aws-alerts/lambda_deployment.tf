data "archive_file" "deployment_notifier" {
  type        = "zip"
  source_dir  = "${path.module}/src/deployment_notifier"
  output_path = "${path.module}/build/deployment_notifier.zip"
}

resource "aws_cloudwatch_log_group" "deployment_notifier" {
  name              = "/aws/lambda/${var.name_prefix}-deployment-notifier"
  retention_in_days = var.lambda_log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "deployment_notifier" {
  function_name    = "${var.name_prefix}-deployment-notifier"
  role             = aws_iam_role.deployment_notifier.arn
  handler          = "index.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128
  filename         = data.archive_file.deployment_notifier.output_path
  source_code_hash = data.archive_file.deployment_notifier.output_base64sha256

  environment {
    variables = {
      CHAT_TARGETS_JSON         = jsonencode(var.chat_targets)
      PIPELINE_BRANCHES_JSON    = jsonencode(var.pipeline_branches)
      DYNAMODB_TABLE_NAME       = aws_dynamodb_table.pr_deploy_threads.name
      POST_UNLINKED_DEPLOYMENTS = tostring(var.post_unlinked_deployments)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.deployment_notifier,
    aws_iam_role_policy.deployment_notifier,
    aws_iam_role_policy_attachment.deployment_notifier_logs,
  ]

  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "pipeline_succeeded" {
  name        = "${var.name_prefix}-pipeline-succeeded"
  description = "Fires when a watched CodePipeline execution succeeds or fails, so the deployment can be posted as a reply to its originating PR-merge card."

  event_pattern = jsonencode({
    source      = ["aws.codepipeline"]
    detail-type = ["CodePipeline Pipeline Execution State Change"]
    detail = {
      state    = ["SUCCEEDED", "FAILED"]
      pipeline = keys(var.pipeline_branches)
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "pipeline_succeeded" {
  rule = aws_cloudwatch_event_rule.pipeline_succeeded.name
  arn  = aws_lambda_function.deployment_notifier.arn
}

resource "aws_lambda_permission" "pipeline_succeeded_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.deployment_notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.pipeline_succeeded.arn
}
